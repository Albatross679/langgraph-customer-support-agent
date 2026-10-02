from collections import defaultdict
from typing import Any

from support_copilot.schemas import Extraction, RefundProposal, RouteDecision, SqlPlan


class FakeModel:
    def __init__(
        self, responses: dict[type[Any], Any], answer: str = "Grounded support reply"
    ) -> None:
        self.responses = responses
        self.answer = answer
        self.calls: list[type[Any]] = []

    async def structured(self, schema: type[Any], system: str, user: str) -> Any:
        self.calls.append(schema)
        return self.responses[schema]

    async def generate(self, system: str, user: str) -> str:
        return self.answer

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.1] * 1536 for _ in texts]


class FakeRepository:
    def __init__(self) -> None:
        self.queries: list[str] = []
        self.refunds: list[tuple[RefundProposal, bool]] = []

    async def retrieve(self, embedding: list[float], limit: int = 4) -> list[dict[str, Any]]:
        return [{"document_name": "returns.md", "content": "Returns are accepted within 30 days."}]

    async def query_readonly(self, sql: str) -> list[dict[str, Any]]:
        self.queries.append(sql)
        return [{"copies_sold": 3}]

    async def query_customer_readonly(self, sql: str, customer_id: int) -> list[dict[str, Any]]:
        self.queries.append(f"customer:{customer_id}:{sql}")
        return [{"copies_sold": 1}]

    async def refund_proposal(self, order_number: str | None, reason: str) -> RefundProposal:
        return RefundProposal(
            order_number=order_number or "unknown", amount_cents=2999, reason=reason
        )

    async def record_simulated_refund(
        self, proposal: RefundProposal, approved: bool, request_id: str
    ) -> str:
        self.refunds.append((proposal, approved))
        return (
            "The simulated refund was approved."
            if approved
            else "The simulated refund was rejected."
        )


class FakeCache:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.set_calls = defaultdict(int)

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, *, ex: int | None = None) -> None:
        self.values[key] = value
        self.set_calls[key] += 1


def defaults(handler: str = "rag") -> dict[type[Any], Any]:
    return {
        Extraction: Extraction(
            order_number="ORD-1001",
            product_title="The Last Horizon",
            media_format="4K UHD",
            issue_type="returns",
            sentiment="neutral",
        ),
        RouteDecision: RouteDecision(lane="returns", handler=handler, rationale="test route"),
        SqlPlan: SqlPlan(
            sql="SELECT COUNT(*) AS copies_sold FROM orders", explanation="count orders"
        ),
    }
