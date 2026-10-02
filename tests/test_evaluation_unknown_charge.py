import asyncio
import json

import httpx
import pytest
from scripts import evaluation_proxy as proxy


@pytest.mark.asyncio
async def test_unknown_failed_charge_is_fully_reserved_and_never_retried_by_proxy(
    tmp_path, monkeypatch
):
    allowance = {
        "approval_reference": "synthetic-unit",
        "model": "openai/gpt-4.1-mini",
        "max_usd": 1,
        "input_usd_per_million": 0.4,
        "output_usd_per_million": 1.6,
        "embedding_usd_per_million": 0.02,
    }
    path = tmp_path / "budget.json"
    monkeypatch.setenv("EVALUATION_BUDGET_FILE", str(path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "synthetic-not-a-key")
    for name, value in [
        ("allowance", allowance),
        ("reserved_usd", 0),
        ("chat_calls", 0),
        ("embedding_tokens", 0),
        ("receipts", []),
        ("budget_stop_reason", None),
        ("lock", asyncio.Lock()),
    ]:
        monkeypatch.setattr(proxy, name, value)
    calls = []

    async def timeout(request):
        calls.append(request)
        raise httpx.ReadTimeout("Synthetic unknown final charge")

    upstream = httpx.AsyncClient(transport=httpx.MockTransport(timeout))
    monkeypatch.setattr(proxy, "client", upstream)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=proxy.app, raise_app_exceptions=False),
        base_url="http://test",
    ) as client:
        for _ in range(2):
            response = await client.post(
                "/chat/completions",
                json={
                    "model": allowance["model"],
                    "messages": [{"role": "user", "content": "Synthetic request"}],
                },
            )
            assert response.status_code == 500
    assert len(calls) == 2
    ledger = json.loads(path.read_text())
    assert ledger["chat_calls"] == 2 and ledger["reserved_usd"] > 0
    assert all(row["charge_state"] == "unknown_reserved" for row in ledger["receipts"])
    assert sum(row["reserved_usd"] for row in ledger["receipts"]) <= ledger["reserved_usd"] <= 1
    await upstream.aclose()
