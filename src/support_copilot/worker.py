import asyncio
import logging
from typing import Any

from arq import cron
from arq.connections import RedisSettings, create_pool
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool
from redis.exceptions import RedisError

from support_copilot.config import get_settings
from support_copilot.db import StoreRepository
from support_copilot.graph import GraphDependencies, build_graph
from support_copilot.model import OpenRouterClient
from support_copilot.run_store import load_durable_run, write_run_status

logger = logging.getLogger(__name__)
settings = get_settings()
JOB_TIMEOUT_SECONDS = 500
THREAD_LOCK_TIMEOUT_SECONDS = JOB_TIMEOUT_SECONDS + 30
THREAD_LOCK_BLOCKING_TIMEOUT_SECONDS = 180


async def startup(ctx: dict[str, Any]) -> None:
    pool = AsyncConnectionPool(
        settings.database_url, min_size=2, max_size=10, open=False, kwargs={"autocommit": True}
    )
    await pool.open()
    redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    model = OpenRouterClient(settings)
    ctx.update(
        pool=pool,
        redis=redis,
        model=model,
        graph=build_graph(
            GraphDependencies(model, StoreRepository(pool), redis), AsyncPostgresSaver(pool)
        ),
    )
    await recover_runs(ctx)


async def shutdown(ctx: dict[str, Any]) -> None:
    await ctx["model"].close()
    await ctx["redis"].aclose()
    await ctx["pool"].close()


async def update_run(ctx: dict[str, Any], run_id: str, **update: Any) -> None:
    await write_run_status(ctx["redis"], run_id, pool=ctx.get("pool"), **update)


def state_payload(snapshot: Any) -> dict[str, Any]:
    values = snapshot.values
    payload = {
        key: values[key]
        for key in ("extraction", "proposed_refund", "answer")
        if values.get(key) is not None
    }
    if values.get("routing") is not None:
        payload["route"] = values["routing"]
    return payload


def interrupt_payload(snapshot: Any) -> dict[str, Any]:
    for task in snapshot.tasks:
        if task.interrupts:
            value = task.interrupts[0].value
            return value if isinstance(value, dict) else {}
    return {}


async def recover_runs(ctx: dict[str, Any]) -> None:
    """Rebuild Redis status/index and requeue incomplete work from Postgres."""
    async with ctx["pool"].connection() as conn:
        result = await conn.execute("SELECT run_id, payload, decision FROM support_runs")
        records = await result.fetchall()
    for run_id, data, decision in records:
        # Only cache missing records here; never overwrite live worker status with stale data.
        if await ctx["redis"].get(f"run:{run_id}") is None:
            await write_run_status(
                ctx["redis"], run_id, **{k: v for k, v in data.items() if k != "run_id"}
            )
        if data["status"] in {"queued", "running"} or (
            data["status"] == "awaiting_approval" and decision
        ):
            if not await ctx["redis"].exists(f"thread-lock:{data['thread_id']}"):
                await ctx["redis"].enqueue_job(
                    "recover_agent", run_id, _job_id=f"{run_id}:recovery"
                )


async def recover_agent(ctx: dict[str, Any], run_id: str) -> dict[str, Any]:
    data = await load_durable_run(ctx["pool"], run_id)
    if not data or data["status"] in {"completed", "failed"}:
        return {}
    async with ctx["pool"].connection() as conn:
        result = await conn.execute(
            "SELECT decision FROM support_runs WHERE run_id = %s", (run_id,)
        )
        decision = (await result.fetchone())[0]
    if decision:
        return await resume_agent(ctx, run_id, data["thread_id"], decision)
    return await run_agent(
        ctx,
        run_id,
        data["message"],
        data["thread_id"],
        data.get("customer_id"),
        data.get("order_number"),
    )


async def run_agent(
    ctx: dict[str, Any],
    run_id: str,
    message: str,
    thread_id: str,
    customer_id: int | None = None,
    order_number: str | None = None,
) -> dict[str, Any]:
    return await execute(
        ctx, run_id, thread_id, message=message, customer_id=customer_id, order_number=order_number
    )


async def resume_agent(
    ctx: dict[str, Any], run_id: str, thread_id: str, decision: str
) -> dict[str, Any]:
    return await execute(ctx, run_id, thread_id, decision=decision)


async def execute(
    ctx: dict[str, Any],
    run_id: str,
    thread_id: str,
    *,
    message: str | None = None,
    customer_id: int | None = None,
    order_number: str | None = None,
    decision: str | None = None,
) -> dict[str, Any]:
    config = {"configurable": {"thread_id": thread_id}}
    try:
        async with ctx["redis"].lock(
            f"thread-lock:{thread_id}",
            timeout=THREAD_LOCK_TIMEOUT_SECONDS,
            blocking_timeout=THREAD_LOCK_BLOCKING_TIMEOUT_SECONDS,
        ):
            previous = await ctx["graph"].aget_state(config)
            same_run = previous.values.get("run_id") == run_id
            if decision and not same_run:
                raise ValueError("Run does not own the paused thread checkpoint")
            if not same_run and interrupt_payload(previous):
                raise ValueError("Thread already has a run awaiting approval")
            await update_run(ctx, run_id, status="running")
            if same_run:
                if interrupt_payload(previous) and decision:
                    await ctx["graph"].ainvoke(Command(resume=decision), config=config)
                elif previous.next and not interrupt_payload(previous):
                    await ctx["graph"].ainvoke(None, config=config)
            else:
                await ctx["graph"].ainvoke(
                    {
                        "run_id": run_id,
                        "message": message,
                        "customer_id": customer_id,
                        "selected_order_number": order_number,
                        "extraction": None,
                        "routing": None,
                        "handler": None,
                        "tool_context": None,
                        "sources": None,
                        "proposed_refund": None,
                        "decision": None,
                        "answer": None,
                        "conversation_history": [{"role": "user", "content": message}],
                    },
                    config=config,
                )
            snapshot = await ctx["graph"].aget_state(config)
            payload = state_payload(snapshot)
            interrupted = interrupt_payload(snapshot)
            if interrupted:
                payload["proposed_refund"] = interrupted.get("proposed_refund")
                await update_run(ctx, run_id, status="awaiting_approval", **payload)
                return {"status": "awaiting_approval"}
            await update_run(ctx, run_id, status="completed", error=None, **payload)
            return payload
    except asyncio.CancelledError:
        # Preserve recoverable durable state, rather than losing a committed refund after cancellation.
        logger.warning("run %s cancelled; checkpoint recovery will retry", run_id)
        raise
    except RedisError:
        # A lost queue/cache or lock is not a terminal graph failure. Postgres remains recoverable.
        logger.exception("run %s lost Redis access; durable checkpoint recovery will retry", run_id)
        raise
    except Exception as error:
        logger.exception("run %s failed", run_id)
        await update_run(ctx, run_id, status="failed", error=str(error))
        raise


class WorkerSettings:
    functions = [run_agent, resume_agent, recover_agent]
    cron_jobs = [cron(recover_runs, second={0, 15, 30, 45})]
    on_startup = startup
    on_shutdown = shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    job_timeout = JOB_TIMEOUT_SECONDS
    max_tries = 1
    keep_result = 1
    health_check_interval = 10
