import json
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from psycopg import AsyncConnection
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlglot import exp, parse
from sqlglot.errors import ParseError
from sqlglot.optimizer.scope import traverse_scope

from support_copilot.schemas import CustomerIdentity, CustomerOrder, RefundProposal

ALLOWED_TABLES = {"customers", "products", "orders"}
ALLOWED_FUNCTIONS = {
    "avg",
    "coalesce",
    "count",
    "current_date",
    "date_trunc",
    "extract",
    "lower",
    "max",
    "min",
    "round",
    "sum",
    "timestamp_trunc",
    "upper",
}


class StoreRepository:
    def __init__(self, pool: AsyncConnectionPool) -> None:
        self.pool = pool

    async def daily_run_limit(self, default: int) -> int:
        async with self.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    INSERT INTO runtime_settings (key, value) VALUES ('daily_run_limit', %s)
                    ON CONFLICT (key) DO NOTHING
                    RETURNING value
                    """,
                    (default,),
                )
                row = await cur.fetchone()
                if row is None:
                    await cur.execute(
                        "SELECT value FROM runtime_settings WHERE key = 'daily_run_limit'"
                    )
                    row = await cur.fetchone()
            await conn.commit()
        assert row is not None
        return int(row["value"])

    async def set_daily_run_limit(self, limit: int) -> int:
        async with self.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    INSERT INTO runtime_settings (key, value) VALUES ('daily_run_limit', %s)
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value
                    RETURNING value
                    """,
                    (limit,),
                )
                row = await cur.fetchone()
            await conn.commit()
        assert row is not None
        return int(row["value"])

    async def thread_owner(self, thread_id: str) -> str | None:
        async with self.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT owner FROM thread_owners WHERE thread_id = %s", (thread_id,)
                )
                row = await cur.fetchone()
        return str(row["owner"]) if row else None

    async def create_thread_owner(self, thread_id: str, owner: str) -> None:
        async with self.pool.connection() as conn:
            await conn.execute(
                "INSERT INTO thread_owners (thread_id, owner) VALUES (%s, %s)",
                (thread_id, owner),
            )
            await conn.commit()

    async def identify_customer(self, name: str, email: str) -> CustomerIdentity | None:
        async with self.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "SELECT id, name, email FROM customers WHERE name = %s AND email = %s",
                    (name, email),
                )
                row = await cur.fetchone()
        return CustomerIdentity.model_validate(row) if row else None

    async def customer_orders(self, customer_id: int) -> list[CustomerOrder]:
        async with self.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT o.order_number, p.title, p.format AS media_format, o.quantity,
                           o.ordered_at::text AS ordered_at, o.status,
                           o.refund_status AS refund_progress
                    FROM orders o
                    JOIN products p ON p.id = o.product_id
                    WHERE o.customer_id = %s
                    ORDER BY o.ordered_at DESC
                    """,
                    (customer_id,),
                )
                rows = await cur.fetchall()
        return [CustomerOrder.model_validate(row) for row in rows]

    async def customer_owns_order(self, customer_id: int, order_number: str) -> bool:
        async with self.pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "SELECT 1 FROM orders WHERE customer_id = %s AND order_number = %s",
                    (customer_id, order_number),
                )
                return await cur.fetchone() is not None

    async def retrieve(self, embedding: list[float], limit: int = 4) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT document_name, content, metadata,
                           1 - (embedding <=> %s::vector) AS similarity
                    FROM help_document_embeddings
                    ORDER BY embedding <=> %s::vector
                    LIMIT %s
                    """,
                    (vector_literal(embedding), vector_literal(embedding), limit),
                )
                return list(await cur.fetchall())

    async def query_readonly(self, sql: str) -> list[dict[str, Any]]:
        validate_readonly_sql(sql)
        return await self._execute_readonly(sql)

    async def query_customer_readonly(self, sql: str, customer_id: int) -> list[dict[str, Any]]:
        validate_readonly_sql(sql)
        return await self._execute_readonly(scope_customer_sql(sql, customer_id))

    async def _execute_readonly(self, sql: str) -> list[dict[str, Any]]:
        async with self.pool.connection() as conn:
            async with conn.transaction():
                await conn.execute("SET TRANSACTION READ ONLY")
                await conn.execute("SET LOCAL ROLE support_copilot_reader")
                await conn.execute("SET LOCAL statement_timeout = '5s'")
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(sql)
                    return list(await cur.fetchall())

    async def refund_proposal(self, order_number: str | None, reason: str) -> RefundProposal:
        if not order_number:
            return RefundProposal(
                order_number="unknown", amount_cents=0, reason=f"{reason}; order number required"
            )
        async with self.pool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    SELECT o.order_number, o.quantity * p.price_cents AS amount_cents
                    FROM orders o JOIN products p ON p.id = o.product_id
                    WHERE o.order_number = %s
                    """,
                    (order_number,),
                )
                row = await cur.fetchone()
        if row is None:
            return RefundProposal(
                order_number=order_number, amount_cents=0, reason=f"{reason}; order not found"
            )
        return RefundProposal(
            order_number=row["order_number"], amount_cents=row["amount_cents"], reason=reason
        )

    async def record_simulated_refund(
        self, proposal: RefundProposal, approved: bool, request_id: str
    ) -> str:
        """Commit decision and permitted order transition together, replaying by request ID."""
        async with self.pool.connection() as conn:
            async with conn.transaction():
                # Serializes even missing orders and concurrent decisions for the same request.
                await conn.execute(
                    "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))", (request_id,)
                )
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "SELECT * FROM refund_decisions WHERE request_id = %s", (request_id,)
                    )
                    previous = await cur.fetchone()
                    if previous:
                        if (
                            previous["approved"] != approved
                            or previous["order_number"] != proposal.order_number
                        ):
                            raise ValueError(
                                "A different decision is already recorded for this request"
                            )
                        return previous["outcome"]
                    await cur.execute(
                        """SELECT o.refund_status, o.quantity * p.price_cents AS amount_cents
                           FROM orders o JOIN products p ON p.id = o.product_id
                           WHERE o.order_number = %s FOR UPDATE OF o""",
                        (proposal.order_number,),
                    )
                    order = await cur.fetchone()
                    await cur.execute(
                        "SELECT 1 FROM refund_actions WHERE order_number = %s",
                        (proposal.order_number,),
                    )
                    action_exists = await cur.fetchone() is not None
                    if not order:
                        outcome = "No simulated refund was applied: order not found; an order number is required."
                    elif order["refund_status"] == "approved" or action_exists:
                        if action_exists and order["refund_status"] != "approved":
                            await cur.execute(
                                "UPDATE orders SET refund_status = 'approved' WHERE order_number = %s",
                                (proposal.order_number,),
                            )
                        outcome = "The order already has an approved simulated refund. No additional action was applied."
                    elif order["amount_cents"] != proposal.amount_cents:
                        outcome = "No simulated refund was applied: order amount changed; start a new review."
                    else:
                        if approved:
                            await cur.execute(
                                """INSERT INTO refund_actions (order_number, request_id, amount_cents)
                                   VALUES (%s, %s, %s) ON CONFLICT (order_number) DO NOTHING
                                   RETURNING order_number""",
                                (proposal.order_number, request_id, proposal.amount_cents),
                            )
                            if await cur.fetchone() is None:
                                raise ValueError("Refund ledger conflicts with order status")
                        await cur.execute(
                            """UPDATE orders SET refund_status = %s
                               WHERE order_number = %s AND refund_status IN ('none', 'rejected')""",
                            ("approved" if approved else "rejected", proposal.order_number),
                        )
                        outcome = (
                            "The simulated refund was approved."
                            if approved
                            else "The simulated refund was rejected."
                        )
                    await cur.execute(
                        "INSERT INTO refund_decisions (request_id, order_number, approved, outcome) VALUES (%s, %s, %s, %s)",
                        (request_id, proposal.order_number, approved, outcome),
                    )
                    return outcome


def vector_literal(values: Sequence[float]) -> str:
    """
    Convert a list of floats to a PostgreSQL vector literal.
    """
    return "[" + ",".join(str(value) for value in values) + "]"


def validate_readonly_sql(sql: str) -> None:
    normalized = sql.strip()
    try:
        statements = parse(normalized, read="postgres")
    except ParseError as error:
        raise ValueError("SQL could not be parsed") from error
    if len(statements) != 1 or not isinstance(statements[0], exp.Query):
        raise ValueError("Only SELECT queries are permitted")
    statement = statements[0]
    tables = [
        source
        for scope in traverse_scope(statement)
        for source in scope.sources.values()
        if isinstance(source, exp.Table)
    ]
    if not tables or any(
        table.catalog or table.db or table.name.lower() not in ALLOWED_TABLES for table in tables
    ):
        raise ValueError("Query must reference only business tables")
    if any(isinstance(dot.expression, exp.Func) for dot in statement.find_all(exp.Dot)):
        raise ValueError("Schema-qualified functions are not permitted")
    functions = called_function_names(statement)
    if not functions.issubset(ALLOWED_FUNCTIONS):
        raise ValueError("Query contains a function that is not permitted")


def scope_customer_sql(sql: str, customer_id: int) -> str:
    statement = parse(sql.strip(), read="postgres")[0]
    for table in list(statement.find_all(exp.Table)):
        table_name = table.name.lower()
        restricted_column = {"customers": "id", "orders": "customer_id"}.get(table_name)
        if restricted_column is None:
            continue
        alias = table.alias_or_name
        source = table.copy()
        source.set("alias", None)
        restricted = (
            exp.select("*")
            .from_(source)
            .where(exp.column(restricted_column).eq(exp.Literal.number(customer_id)))
            .subquery(alias)
        )
        table.replace(restricted)
    return statement.sql(dialect="postgres")


def called_function_names(statement: exp.Query) -> set[str]:
    names: set[str] = set()
    for function in statement.find_all(exp.Func):
        name = function_name(function)
        if isinstance(function, exp.CurrentDate) or function.sql().lower().lstrip().startswith(
            f"{name}("
        ):
            names.add(name)
    return names


def function_name(function: exp.Func) -> str:
    if isinstance(function, exp.Anonymous):
        return function.name.lower()
    return function.sql_name().lower()


async def apply_schema(conn: AsyncConnection[Any], embedding_dim: int) -> None:
    schema_path = Path(__file__).with_name("schema.sql")
    schema = schema_path.read_text().replace("{{EMBEDDING_DIM}}", str(embedding_dim))
    await conn.execute(schema)
    await conn.execute("DROP INDEX IF EXISTS help_document_embeddings_embedding_idx")
    await conn.commit()


def encode_run(value: dict[str, Any]) -> str:
    return json.dumps(value, default=str)
