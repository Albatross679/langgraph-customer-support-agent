import asyncio
import os
import uuid

import httpx
import psycopg
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_ISOLATED_INTEGRATION") != "1", reason="Isolated Compose only"
    ),
]


@pytest.mark.asyncio
async def test_approval_binds_checkpointed_amount_not_a_recalculated_order():
    number = "ORD-" + str(uuid.uuid4().int)[:14]
    dsn = "postgresql://postgres:postgres@127.0.0.1:15482/support_copilot"
    with psycopg.connect(dsn) as conn:
        conn.execute(
            """INSERT INTO orders (order_number, customer_id, product_id, quantity, ordered_at)
                        VALUES (%s, 1, 1, 1, now())""",
            (number,),
        )
    async with httpx.AsyncClient(base_url="http://127.0.0.1:18082") as client:
        created = (await client.post("/runs", json={"message": f"Refund {number}"})).json()
        for _ in range(100):
            run = (await client.get(f"/runs/{created['run_id']}")).json()
            if run["status"] == "awaiting_approval":
                break
            await asyncio.sleep(0.1)
        assert run["proposed_refund"]["amount_cents"] == 2999
        with psycopg.connect(dsn) as conn:
            conn.execute("UPDATE orders SET quantity=2 WHERE order_number=%s", (number,))
        decision = await client.post(
            f"/runs/{created['run_id']}/decision", json={"decision": "approve"}
        )
        assert decision.status_code == 202
        for _ in range(100):
            run = (await client.get(f"/runs/{created['run_id']}")).json()
            if run["status"] == "completed":
                break
            await asyncio.sleep(0.1)
        assert run["status"] == "completed"
        assert "amount changed" in run["answer"]
        assert run["proposed_refund"]["amount_cents"] == 2999
        with psycopg.connect(dsn) as conn:
            assert (
                conn.execute(
                    "SELECT refund_status FROM orders WHERE order_number=%s", (number,)
                ).fetchone()[0]
                == "none"
            )
            assert (
                conn.execute(
                    "SELECT count(*) FROM refund_actions WHERE order_number=%s", (number,)
                ).fetchone()[0]
                == 0
            )
