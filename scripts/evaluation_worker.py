"""Isolated post-commit/pre-checkpoint barrier using the production worker and graph.

The production Compose command never enables this adapter. The evaluator arms an
exact run ID in task-owned Redis before approval, then kills after the SQL commit.
"""

import asyncio

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

from support_copilot import worker
from support_copilot.db import StoreRepository
from support_copilot.graph import GraphDependencies, build_graph


class FaultRepository(StoreRepository):
    def __init__(self, pool, redis):
        super().__init__(pool)
        self.redis = redis

    async def record_simulated_refund(self, proposal, approved, request_id):
        outcome = await super().record_simulated_refund(proposal, approved, request_id)
        if await self.redis.delete(f"eval:arm-crash:{request_id}"):
            await self.redis.set(f"eval:committed:{request_id}", "1")
            await asyncio.sleep(120)
        return outcome


async def startup(ctx):
    await worker.startup(ctx)
    ctx["graph"] = build_graph(
        GraphDependencies(ctx["model"], FaultRepository(ctx["pool"], ctx["redis"]), ctx["redis"]),
        AsyncPostgresSaver(ctx["pool"]),
    )


class WorkerSettings:
    # arq reads settings.__dict__, not inherited class attributes.
    functions = worker.WorkerSettings.functions
    cron_jobs = worker.WorkerSettings.cron_jobs
    on_startup = startup
    on_shutdown = worker.shutdown
    redis_settings = worker.WorkerSettings.redis_settings
    job_timeout = worker.WorkerSettings.job_timeout
    max_tries = worker.WorkerSettings.max_tries
    keep_result = worker.WorkerSettings.keep_result
    health_check_interval = worker.WorkerSettings.health_check_interval
