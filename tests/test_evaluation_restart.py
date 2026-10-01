import asyncio
import json

import httpx
import pytest
from scripts import evaluation_proxy as proxy


@pytest.mark.asyncio
async def test_persistent_spending_reservations_survive_proxy_restart(tmp_path, monkeypatch):
    allowance = {
        "approval_reference": "synthetic-test-not-authorization",
        "model": "openai/gpt-4.1-mini",
        "max_usd": 8,
        "input_usd_per_million": 0.4,
        "output_usd_per_million": 1.6,
        "embedding_usd_per_million": 0.02,
    }
    path = tmp_path / "allowance.json"
    path.write_text(json.dumps(allowance))
    state = tmp_path / "budget.json"
    monkeypatch.setenv("EVALUATION_ALLOWANCE_FILE", str(path))
    monkeypatch.setenv("EVALUATION_BUDGET_FILE", str(state))
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-test-key")
    monkeypatch.setattr(proxy, "allowance", allowance)
    monkeypatch.setattr(proxy, "chat_calls", 200)
    monkeypatch.setattr(proxy, "reserved_usd", 5.4)
    monkeypatch.setattr(proxy, "embedding_tokens", 1000)
    monkeypatch.setattr(proxy, "receipts", [{"kind": "chat", "reserved_usd": 5.4}])
    monkeypatch.setattr(proxy, "client", None)
    monkeypatch.setattr(proxy, "lock", asyncio.Lock())
    proxy.persist_budget()
    proxy.allowance = None
    proxy.chat_calls = 0
    proxy.reserved_usd = 0
    requests = []

    async def metadata(request):
        requests.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "openai/gpt-4.1-mini",
                        "pricing": {
                            "prompt": ".0000004",
                            "completion": ".0000016",
                            "web_search": ".01",
                        },
                    },
                    {"id": "openai/text-embedding-3-small", "pricing": {"prompt": ".00000002"}},
                ]
            },
        )

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        proxy.httpx,
        "AsyncClient",
        lambda **kwargs: real_client(transport=httpx.MockTransport(metadata)),
    )
    await proxy.initialize()
    assert proxy.chat_calls == 200 and proxy.reserved_usd == 5.4
    async with real_client(
        transport=httpx.ASGITransport(app=proxy.app), base_url="http://test"
    ) as client:
        response = await client.post(
            "/chat/completions",
            json={
                "model": allowance["model"],
                "messages": [{"role": "user", "content": "synthetic"}],
            },
        )
        assert response.status_code == 429
    assert requests == ["/api/v1/models", "/api/v1/embeddings/models"]
    await proxy.client.aclose()
