import json
from types import SimpleNamespace

import pytest
from redis.exceptions import ConnectionError

from support_copilot.worker import run_agent
from tests.test_review_fixes import FakeRedis


@pytest.mark.asyncio
async def test_redis_loss_does_not_turn_recoverable_graph_state_into_terminal_failure():
    class LostRedisGraph:
        async def aget_state(self, config):
            return SimpleNamespace(values={}, tasks=[])

        async def ainvoke(self, state, config):
            raise ConnectionError("Synthetic isolated Redis outage")

    redis = FakeRedis({"run_id": "recoverable", "thread_id": "thread", "status": "queued"})
    with pytest.raises(ConnectionError):
        await run_agent(
            {"redis": redis, "graph": LostRedisGraph()},
            "recoverable",
            "Synthetic question",
            "thread",
        )
    assert json.loads(redis.values["run:recoverable"])["status"] == "running"
