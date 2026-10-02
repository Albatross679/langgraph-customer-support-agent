"""Deterministic HTTP transport. Proves integration, not model answer quality."""

import asyncio
import json
import re

from fastapi import FastAPI, Request

app = FastAPI()
seen_barriers = set()
receipts = []
TOPICS = [
    ["box set", "collector"],
    ["damaged discs", "disc damage"],
    ["playback", "fingerprints"],
    ["format guide", "DVD quality", "4K players"],
    ["order changes", "order address", "combining orders"],
    ["preorder"],
    ["refund"],
    ["region"],
    ["returns", "return"],
    ["shipping", "tracking"],
]
SQL = {
    "count all orders": "SELECT count(*) AS order_count FROM orders",
    "count all customers": "SELECT count(*) AS customer_count FROM customers",
    "count all products": "SELECT count(*) AS product_count FROM products",
    "count all units ordered": "SELECT sum(quantity) AS units FROM orders",
    "count total order revenue cents": "SELECT sum(o.quantity*p.price_cents) AS revenue_cents FROM orders o JOIN products p ON p.id=o.product_id",
    "count orders by customer": "SELECT customer_id, count(*) AS orders FROM orders GROUP BY customer_id ORDER BY customer_id",
    "count product price range": "SELECT min(price_cents) AS minimum, max(price_cents) AS maximum FROM products",
    "what are the minimum and maximum product prices in cents?": "SELECT min(price_cents) AS minimum, max(price_cents) AS maximum FROM products",
    "count orders with shipped status": "SELECT count(*) AS shipped FROM orders WHERE status='shipped'",
}


@app.get("/receipts")
async def get_receipts():
    return receipts


@app.post("/embeddings")
async def embeddings(request: Request):
    data = await request.json()
    vectors = []
    for text in data["input"]:
        vector = [0.0] * 1536
        headers = [
            "Box sets and collector editions",
            "Damaged discs",
            "Playback troubleshooting",
            "Format guide",
            "Order changes",
            "Preorders",
            "Refund review",
            "Region codes",
            "Returns policy",
            "Shipping",
        ]
        header_index = next(
            (i for i, header in enumerate(headers) if text.startswith("# " + header)), None
        )
        match_text = text.splitlines()[0] if text.startswith("#") else text
        index = next(
            (
                i
                for i, words in enumerate(TOPICS)
                if any(w.lower() in match_text.lower() for w in words)
            ),
            6,
        )
        vector[header_index if header_index is not None else index] = 1.0
        vectors.append({"embedding": vector})
    return {"data": vectors}


@app.post("/chat/completions")
async def completions(request: Request):
    data = await request.json()
    text = data["messages"][-1]["content"]
    schema = data.get("response_format", {}).get("json_schema", {}).get("name")
    receipts.append(
        {"schema": schema, "messages": data["messages"], "max_tokens": data.get("max_tokens")}
    )
    if schema == "extraction":
        match = re.search(r"ORD-\d+", text)
        result = {
            "order_number": match.group() if match else None,
            "product_title": None,
            "media_format": "unknown",
            "issue_type": "refund",
            "sentiment": "neutral",
        }
    elif schema == "routedecision":
        handler = (
            "sql"
            if "count" in text.lower() or "minimum and maximum" in text.lower()
            else "rag"
            if "policy" in text.lower()
            else "refund"
        )
        result = {
            "lane": "billing" if handler == "refund" else "general",
            "handler": handler,
            "rationale": "Synthetic transport fixture",
        }
    elif schema == "sqlplan":
        result = {
            "sql": SQL.get(text.lower(), SQL["count all orders"]),
            "explanation": "Synthetic ground truth query",
        }
    else:
        marker = re.search(r"\[crash-after-action-\d+\]", text)
        if marker and marker.group() not in seen_barriers:
            seen_barriers.add(marker.group())
            await asyncio.sleep(60)
        result = text.split("Evidence: ", 1)[-1]
    return {
        "choices": [
            {"message": {"content": result if isinstance(result, str) else json.dumps(result)}}
        ]
    }
