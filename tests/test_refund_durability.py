"""Secret-free, real-stack regressions. Run only with tests/compose.integration.yml."""

import asyncio
import os
import uuid

import httpx
import psycopg
import pytest
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from support_copilot.config import Settings
from support_copilot.db import StoreRepository
from support_copilot.graph import GraphDependencies, build_graph
from support_copilot.model import OpenRouterClient
from support_copilot.schemas import RefundProposal

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_ISOLATED_INTEGRATION") != "1", reason="Isolated Compose only"
    ),
]
DSN = "postgresql://postgres:postgres@127.0.0.1:15482/support_copilot"
API = "http://127.0.0.1:18082"


def order():
    number = "ORD-" + str(uuid.uuid4().int)[:14]
    with psycopg.connect(DSN) as conn:
        conn.execute(
            """INSERT INTO orders (order_number, customer_id, product_id, quantity, ordered_at)
                        VALUES (%s, 1, 1, 1, now())""",
            (number,),
        )
    return number


async def poll(client, run_id, status):
    for _ in range(240):
        run = (await client.get(f"/runs/{run_id}")).json()
        if run["status"] == status:
            return run
        assert run["status"] != "failed", run
        await asyncio.sleep(0.1)
    raise TimeoutError(run_id)


@pytest.mark.asyncio
async def test_cross_conversation_decisions_replay_and_redis_loss():
    number = order()
    async with httpx.AsyncClient(base_url=API) as client:
        runs = []
        for _ in range(2):
            response = await client.post("/runs", json={"message": f"Refund damaged {number}"})
            response.raise_for_status()
            runs.append(response.json())
        for run in runs:
            await poll(client, run["run_id"], "awaiting_approval")
        with psycopg.connect(DSN) as conn:
            assert (
                conn.execute(
                    "SELECT refund_status FROM orders WHERE order_number=%s", (number,)
                ).fetchone()[0]
                == "none"
            )
        for run, decision in zip(runs, ["approve", "reject"], strict=True):
            assert (
                await client.post(f"/runs/{run['run_id']}/decision", json={"decision": decision})
            ).status_code == 202
            await poll(client, run["run_id"], "completed")
        replay = await client.post(
            f"/runs/{runs[0]['run_id']}/decision", json={"decision": "approve"}
        )
        conflict = await client.post(
            f"/runs/{runs[0]['run_id']}/decision", json={"decision": "reject"}
        )
        assert replay.status_code == 202
        assert conflict.status_code == 409
        with psycopg.connect(DSN) as conn:
            assert (
                conn.execute(
                    "SELECT refund_status FROM orders WHERE order_number=%s", (number,)
                ).fetchone()[0]
                == "approved"
            )
            assert (
                conn.execute(
                    "SELECT count(*) FROM refund_actions WHERE order_number=%s", (number,)
                ).fetchone()[0]
                == 1
            )
            assert (
                conn.execute(
                    "SELECT count(*) FROM refund_decisions WHERE order_number=%s", (number,)
                ).fetchone()[0]
                == 2
            )
        redis = Redis.from_url("redis://127.0.0.1:16382/0")
        await redis.delete(f"run:{runs[0]['run_id']}")
        assert (await client.get(f"/runs/{runs[0]['run_id']}")).json()["status"] == "completed"
        assert runs[0]["run_id"] in [
            r["run_id"] for r in (await client.get("/runs?limit=100")).json()["runs"]
        ]
        await redis.aclose()


@pytest.mark.asyncio
async def test_concurrent_order_actions_and_immutable_decision():
    number = order()
    async with AsyncConnectionPool(DSN, open=False, kwargs={"autocommit": True}) as pool:
        repository = StoreRepository(pool)
        proposal = await repository.refund_proposal(number, "test")
        ids = [str(uuid.uuid4()) for _ in range(8)]
        outcomes = await asyncio.gather(
            *[repository.record_simulated_refund(proposal, True, i) for i in ids]
        )
        assert outcomes.count("The simulated refund was approved.") == 1
        assert await repository.record_simulated_refund(proposal, True, ids[0]) == outcomes[0]
        with pytest.raises(ValueError, match="different decision"):
            await repository.record_simulated_refund(proposal, False, ids[0])
        # The durable action, not a mutable employee demo-data field, controls terminal state.
        with psycopg.connect(DSN) as conn:
            conn.execute("UPDATE orders SET refund_status='none' WHERE order_number=%s", (number,))
        assert "already" in await repository.record_simulated_refund(
            proposal, False, str(uuid.uuid4())
        )
        with psycopg.connect(DSN) as conn:
            assert (
                conn.execute(
                    "SELECT refund_status FROM orders WHERE order_number=%s", (number,)
                ).fetchone()[0]
                == "approved"
            )
            assert (
                conn.execute(
                    "SELECT count(*) FROM refund_actions WHERE order_number=%s", (number,)
                ).fetchone()[0]
                == 1
            )
        missing = RefundProposal(
            order_number="ORD-99999999999999999", amount_cents=0, reason="test"
        )
        assert "not found" in await repository.record_simulated_refund(
            missing, True, str(uuid.uuid4())
        )


@pytest.mark.asyncio
async def test_crash_after_commit_before_checkpoint_replays_action_once():
    number = order()
    run_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())

    class CrashRepository(StoreRepository):
        async def record_simulated_refund(self, proposal, approved, request_id):
            await super().record_simulated_refund(proposal, approved, request_id)
            raise asyncio.CancelledError

    async with AsyncConnectionPool(DSN, open=False, kwargs={"autocommit": True}) as pool:
        model = OpenRouterClient(
            Settings(
                openrouter_api_key="synthetic-not-a-secret",
                openrouter_base_url="http://127.0.0.1:18083",
            )
        )
        cache = Redis.from_url("redis://127.0.0.1:16382/0")
        config = {"configurable": {"thread_id": thread_id}}
        graph = build_graph(
            GraphDependencies(model, CrashRepository(pool), cache), AsyncPostgresSaver(pool)
        )
        await graph.ainvoke({"run_id": run_id, "message": f"Refund {number}"}, config)
        with pytest.raises(asyncio.CancelledError):
            await graph.ainvoke(Command(resume="approve"), config)
        # New graph/checkpointer instance, same PostgreSQL state, no in-memory checkpoint reuse.
        recovered = build_graph(
            GraphDependencies(model, StoreRepository(pool), cache), AsyncPostgresSaver(pool)
        )
        await recovered.ainvoke(Command(resume="approve"), config)
        snapshot = await recovered.aget_state(config)
        assert not snapshot.next
        assert snapshot.values["answer"] == "The simulated refund was approved."
        with psycopg.connect(DSN) as conn:
            assert (
                conn.execute(
                    "SELECT count(*) FROM refund_actions WHERE order_number=%s", (number,)
                ).fetchone()[0]
                == 1
            )
            assert (
                conn.execute(
                    "SELECT count(*) FROM refund_decisions WHERE request_id=%s", (run_id,)
                ).fetchone()[0]
                == 1
            )
        await model.close()
        await cache.aclose()
