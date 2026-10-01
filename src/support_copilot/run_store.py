import json
from typing import Any


def awaiting_orders_key(customer_id: int) -> str:
    return f"customer:{customer_id}:awaiting-orders"


async def load_durable_run(pool: Any, run_id: str) -> dict[str, Any] | None:
    async with pool.connection() as conn:
        result = await conn.execute("SELECT payload FROM support_runs WHERE run_id = %s", (run_id,))
        row = await result.fetchone()
    return row[0] if row else None


async def save_durable_run(pool: Any, data: dict[str, Any]) -> None:
    async with pool.connection() as conn:
        await conn.execute(
            """INSERT INTO support_runs (run_id, payload) VALUES (%s, %s::jsonb)
               ON CONFLICT (run_id) DO UPDATE SET
               payload = support_runs.payload || EXCLUDED.payload, updated_at = now()""",
            (data["run_id"], json.dumps(data, default=str)),
        )
        await conn.commit()


async def claim_decision(pool: Any, run_id: str, decision: str) -> bool:
    async with pool.connection() as conn:
        async with conn.transaction():
            result = await conn.execute(
                """UPDATE support_runs SET decision = %s, updated_at = now()
                   WHERE run_id = %s AND decision IS NULL
                     AND payload->>'status' = 'awaiting_approval' RETURNING run_id""",
                (decision, run_id),
            )
            if await result.fetchone():
                return True
            result = await conn.execute(
                "SELECT decision FROM support_runs WHERE run_id = %s", (run_id,)
            )
            row = await result.fetchone()
            return bool(row and row[0] == decision)


async def write_run_status(redis: Any, run_id: str, *, pool: Any = None, **update: Any) -> None:
    key = f"run:{run_id}"
    existing = await redis.get(key)
    durable = await load_durable_run(pool, run_id) if pool is not None else None
    previous = durable or (
        json.loads(existing.decode() if isinstance(existing, bytes) else existing)
        if existing
        else {"run_id": run_id}
    )
    data = {**previous, **update}
    if pool is not None:
        await save_durable_run(pool, data)
    async with redis.pipeline(transaction=True) as transaction:
        transaction.set(key, json.dumps(data, default=str))
        if (
            previous.get("status") == "awaiting_approval"
            and previous.get("customer_id") is not None
            and previous.get("order_number")
        ):
            transaction.hdel(awaiting_orders_key(previous["customer_id"]), run_id)
        if (
            data.get("status") == "awaiting_approval"
            and data.get("customer_id") is not None
            and data.get("order_number")
        ):
            transaction.hset(awaiting_orders_key(data["customer_id"]), run_id, data["order_number"])
        await transaction.execute()
