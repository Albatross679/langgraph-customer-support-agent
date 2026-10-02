import asyncio
import json

import httpx
import pytest
from scripts import evaluation_proxy as proxy


@pytest.fixture
async def budget(monkeypatch, tmp_path):
    capsule = {
        "approval_reference": "synthetic-test-approval-not-spending-authorization",
        "model": "openai/gpt-4.1-mini",
        "max_usd": 0.01,
        "input_usd_per_million": 0.4,
        "output_usd_per_million": 1.6,
        "embedding_usd_per_million": 0.02,
    }
    calls = []

    async def upstream(request):
        calls.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": "synthetic"}}], "usage": {"total_tokens": 5}},
        )

    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("EVALUATION_BUDGET_FILE", str(tmp_path / "budget.json"))
    monkeypatch.setattr(proxy, "allowance", capsule)
    monkeypatch.setattr(proxy, "client", httpx.AsyncClient(transport=httpx.MockTransport(upstream)))
    monkeypatch.setattr(proxy, "chat_calls", 0)
    monkeypatch.setattr(proxy, "embedding_tokens", 0)
    monkeypatch.setattr(proxy, "reserved_usd", 0)
    monkeypatch.setattr(proxy, "receipts", [])
    monkeypatch.setattr(proxy, "lock", asyncio.Lock())
    monkeypatch.setattr(proxy, "budget_stop_reason", None)
    yield calls
    await proxy.client.aclose()


def payload(text="synthetic"):
    return {
        "model": "openai/gpt-4.1-mini",
        "messages": [{"role": "user", "content": text}],
        "max_tokens": 9000,
    }


@pytest.mark.asyncio
async def test_budget_and_token_ceiling_are_enforced_before_upstream(budget):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy.app), base_url="http://test"
    ) as client:
        statuses = [
            (await client.post("/chat/completions", json=payload())).status_code for _ in range(8)
        ]
        assert 200 in statuses and statuses[-1] == 429
        assert all(call["max_tokens"] == 1024 for call in budget)
        assert all(call["provider"]["allow_fallbacks"] is False for call in budget)
        assert all(call["provider"]["max_price"]["request"] == 0 for call in budget)
        assert proxy.reserved_usd <= 0.01
        n = len(budget)
        assert (
            await client.post("/chat/completions", json=payload("x" * 65000))
        ).status_code == 429
        assert len(budget) == n
        request = {**payload(), "plugins": [{"id": "web"}]}
        assert (await client.post("/chat/completions", json=request)).status_code == 403
        assert len(budget) == n
        proxy.chat_calls = 400
        assert (await client.post("/chat/completions", json=payload())).status_code == 429
        assert len(budget) == n


@pytest.mark.asyncio
async def test_price_refusal_stays_closed_on_retry(tmp_path, monkeypatch):
    receipt = {
        "approval_reference": "synthetic-test",
        "model": "openai/gpt-4.1-mini",
        "max_usd": 1,
        "input_usd_per_million": 0.4,
        "output_usd_per_million": 1.6,
        "embedding_usd_per_million": 0.02,
    }
    path = tmp_path / "receipt.json"
    path.write_text(json.dumps(receipt))
    monkeypatch.setenv("EVALUATION_ALLOWANCE_FILE", str(path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")
    monkeypatch.setattr(proxy, "allowance", None)
    monkeypatch.setattr(proxy, "lock", asyncio.Lock())
    requests = []

    async def upstream(request):
        requests.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "openai/gpt-4.1-mini",
                        "pricing": {"prompt": ".001", "completion": ".001"},
                    },
                    {"id": "openai/text-embedding-3-small", "pricing": {"prompt": ".00000002"}},
                ]
            },
        )

    real_client = httpx.AsyncClient
    mocks = []

    def factory(**kwargs):
        client = real_client(transport=httpx.MockTransport(upstream))
        mocks.append(client)
        return client

    monkeypatch.setattr(proxy.httpx, "AsyncClient", factory)
    async with real_client(
        transport=httpx.ASGITransport(app=proxy.app), base_url="http://test"
    ) as client:
        for _ in range(2):
            assert (await client.post("/chat/completions", json=payload())).status_code == 403
            assert proxy.allowance is None
    assert requests == ["/api/v1/models", "/api/v1/embeddings/models"] * 2
    for mock in mocks:
        await mock.aclose()
