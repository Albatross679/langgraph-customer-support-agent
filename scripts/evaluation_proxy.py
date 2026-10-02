"""Budget-limited live evaluation transport. Requires a firstmate allowance receipt.

No keys are stored or logged. Mount the receipt read-only into the isolated proxy.
"""

import asyncio
import json
import math
import os
from pathlib import Path

import httpx
from fastapi import FastAPI, HTTPException, Request

app = FastAPI()
lock = asyncio.Lock()
receipts = []
reserved_usd = 0.0
chat_calls = 0
embedding_tokens = 0
allowance = None
client = None
seen_barriers = set()
budget_stop_reason = None


async def initialize():
    global \
        allowance, \
        client, \
        reserved_usd, \
        chat_calls, \
        embedding_tokens, \
        receipts, \
        budget_stop_reason, \
        seen_barriers
    path = os.getenv("EVALUATION_ALLOWANCE_FILE")
    if not path:
        raise HTTPException(403, "Firstmate spending allowance receipt required")
    candidate = json.loads(Path(path).read_text())
    required = [
        "approval_reference",
        "model",
        "max_usd",
        "input_usd_per_million",
        "output_usd_per_million",
        "embedding_usd_per_million",
    ]
    if any(k not in candidate for k in required) or not candidate["approval_reference"]:
        raise HTTPException(403, "Incomplete spending allowance receipt")
    rates = [
        "max_usd",
        "input_usd_per_million",
        "output_usd_per_million",
        "embedding_usd_per_million",
    ]
    if any(
        not isinstance(candidate[key], int | float)
        or not math.isfinite(candidate[key])
        or candidate[key] <= 0
        for key in rates
    ):
        raise HTTPException(403, "Finite positive allowance values required")
    if candidate["model"] != "openai/gpt-4.1-mini" or not 0 < candidate["max_usd"] <= 1:
        raise HTTPException(403, "Only the bounded mini evaluation profile is supported")
    if not os.getenv("OPENROUTER_API_KEY"):
        raise HTTPException(403, "Evaluation key is unavailable")
    if client is not None:
        await client.aclose()
    client = httpx.AsyncClient(timeout=60, transport=httpx.AsyncHTTPTransport(retries=0))
    # Metadata requests are read-only and do not invoke a model.
    response = await client.get("https://openrouter.ai/api/v1/models")
    response.raise_for_status()
    models = {m["id"]: m for m in response.json()["data"]}
    response = await client.get("https://openrouter.ai/api/v1/embeddings/models")
    response.raise_for_status()
    models.update({m["id"]: m for m in response.json()["data"]})
    for name, kind in [
        (candidate["model"], "chat"),
        ("openai/text-embedding-3-small", "embedding"),
    ]:
        pricing = models[name]["pricing"]
        rates = (
            [("prompt", "input_usd_per_million"), ("completion", "output_usd_per_million")]
            if kind == "chat"
            else [("prompt", "embedding_usd_per_million")]
        )
        for price, ceiling in rates:
            if float(pricing.get(price, 0)) * 1_000_000 > candidate[ceiling]:
                raise HTTPException(403, "Current provider price exceeds the approved estimate")
        if any(
            float(value) > 0
            for key, value in pricing.items()
            if key
            not in {"prompt", "completion", "input_cache_read", "input_cache_write", "web_search"}
        ):
            raise HTTPException(403, "Unbudgeted provider pricing component")
    if any(
        candidate[key] <= 0
        for key in ["input_usd_per_million", "output_usd_per_million", "embedding_usd_per_million"]
    ):
        raise HTTPException(403, "Positive approved price ceilings required")
    budget_path = Path(os.environ["EVALUATION_BUDGET_FILE"])
    if budget_path.exists():
        persisted = json.loads(budget_path.read_text())
        if (
            persisted["approval_reference"] != candidate["approval_reference"]
            or persisted.get("allowance") != candidate
        ):
            raise HTTPException(403, "Persistent budget belongs to a different approval")
        reserved_usd = persisted["reserved_usd"]
        chat_calls = persisted["chat_calls"]
        embedding_tokens = persisted["embedding_tokens"]
        receipts = persisted["receipts"]
        budget_stop_reason = persisted.get("budget_stop_reason")
        seen_barriers = set(persisted.get("seen_barriers", []))
    allowance = candidate


def persist_budget():
    path = Path(os.environ["EVALUATION_BUDGET_FILE"])
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "approval_reference": allowance["approval_reference"],
        "allowance": allowance,
        "reserved_usd": reserved_usd,
        "chat_calls": chat_calls,
        "embedding_tokens": embedding_tokens,
        "receipts": receipts,
        "budget_stop_reason": budget_stop_reason,
        "seen_barriers": sorted(seen_barriers),
    }
    temporary = path.with_suffix(".tmp")
    with temporary.open("w") as file:
        file.write(json.dumps(data))
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


def stop_budget(reason):
    global budget_stop_reason
    budget_stop_reason = reason
    persist_budget()
    raise HTTPException(429, reason)


async def forward(request, kind):
    global reserved_usd, chat_calls, embedding_tokens
    data = await request.json()
    allowed_fields = (
        {"model", "messages", "temperature", "response_format", "max_tokens"}
        if kind == "chat"
        else {"model", "input"}
    )
    if set(data) - allowed_fields:
        raise HTTPException(403, "Unbudgeted request options are forbidden")
    if kind == "chat" and any(
        not isinstance(message.get("content"), str) for message in data.get("messages", [])
    ):
        raise HTTPException(403, "Only text evaluation messages are allowed")
    async with lock:
        if allowance is None:
            await initialize()
        if budget_stop_reason:
            raise HTTPException(429, budget_stop_reason)
        if kind == "chat":
            # UTF-8 bytes upper-bound token count without relying on chars-per-token averages.
            size = len(json.dumps(data, ensure_ascii=False).encode()) + 1024
            if size > 64000 or chat_calls >= 400 or data["model"] != allowance["model"]:
                stop_budget("Evaluation chat call/input limit exceeded")
            data["max_tokens"] = min(data.get("max_tokens", 1024), 1024)
            reservation = (
                size * allowance["input_usd_per_million"]
                + 1024 * allowance["output_usd_per_million"]
            ) / 1_000_000
        else:
            size = len(json.dumps(data, ensure_ascii=False).encode()) + 1024
            if (
                embedding_tokens + size > 1_000_000
                or data["model"] != "openai/text-embedding-3-small"
            ):
                stop_budget("Evaluation embedding limit exceeded")
            reservation = size * allowance["embedding_usd_per_million"] / 1_000_000
        if reserved_usd + reservation > allowance["max_usd"]:
            stop_budget("Evaluation allowance exhausted")
        # Round reservations upward to whole microdollars; never release failed/unknown charges.
        reservation = math.ceil(reservation * 1_000_000) / 1_000_000
        if math.ceil((reserved_usd + reservation) * 1_000_000) > math.floor(
            allowance["max_usd"] * 1_000_000
        ):
            stop_budget("Evaluation allowance exhausted")
        reserved_usd = math.ceil((reserved_usd + reservation) * 1_000_000) / 1_000_000
        data["provider"] = {
            "allow_fallbacks": False,
            "max_price": {
                "prompt": allowance["input_usd_per_million"]
                if kind == "chat"
                else allowance["embedding_usd_per_million"],
                "completion": allowance["output_usd_per_million"] if kind == "chat" else 0,
                "request": 0,
            },
        }
        if kind == "chat":
            chat_calls += 1
        else:
            embedding_tokens += size
        # Reserve before calling; failed/crashed calls remain charged against the ceiling.
        receipt = {
            "kind": kind,
            "model": data["model"],
            "input_byte_ceiling": size,
            "reserved_usd": reservation,
            "charge_state": "unknown_reserved",
            "provider_policy": data["provider"],
        }
        marker = (
            next(
                (
                    word
                    for word in data["messages"][-1]["content"].split()
                    if word.startswith("[crash-after-action-")
                ),
                None,
            )
            if kind == "chat" and "response_format" not in data
            else None
        )
        hold_response = bool(marker and marker not in seen_barriers)
        if hold_response:
            seen_barriers.add(marker)
            receipt["barrier_marker"] = marker
        receipts.append(receipt)
        persist_budget()
    try:
        response = await client.post(
            "https://openrouter.ai/api/v1/"
            + ("chat/completions" if kind == "chat" else "embeddings"),
            headers={"Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"]},
            json=data,
        )
        response.raise_for_status()
        result = response.json()
    except Exception as error:
        receipt["error_type"] = type(error).__name__
        receipt["http_status"] = response.status_code if "response" in locals() else None
        async with lock:
            persist_budget()
        raise
    receipt["charge_state"] = "response_received_reserved"
    receipt["usage"] = result.get("usage")
    receipt["served_model"] = result.get("model")
    receipt["provider"] = result.get("provider")
    receipt["generation_id"] = result.get("id")
    async with lock:
        persist_budget()
    if hold_response:
        await asyncio.sleep(60)
    return result


@app.post("/chat/completions")
async def chat(request: Request):
    return await forward(request, "chat")


@app.post("/embeddings")
async def embeddings(request: Request):
    return await forward(request, "embedding")


@app.get("/receipts")
async def get_receipts():
    async with lock:
        if allowance is None and os.getenv("EVALUATION_ALLOWANCE_FILE"):
            await initialize()  # Read-only price checks, then restore the same persisted ledger.
    return {
        "transport": "openrouter-live-budgeted",
        "calls": receipts,
        "reserved_usd": reserved_usd,
        "max_usd": allowance["max_usd"] if allowance else 1,
        "remaining_reserved_usd": max(0, allowance["max_usd"] - reserved_usd) if allowance else 1,
        "chat_calls": chat_calls,
        "chat_call_limit": 400,
        "embedding_input_byte_ceiling": embedding_tokens,
        "approval_reference": allowance["approval_reference"] if allowance else None,
        "budget_stop_reason": budget_stop_reason,
    }
