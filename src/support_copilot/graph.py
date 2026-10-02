import hashlib
import json
from operator import add
from typing import Annotated, Any, Literal, Protocol, TypedDict

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from support_copilot.db import StoreRepository
from support_copilot.schemas import Extraction, RefundProposal, RouteDecision, SqlPlan


class ModelClient(Protocol):
    async def structured(self, schema: type[Any], system: str, user: str) -> Any: ...

    async def generate(self, system: str, user: str) -> str: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


class CacheClient(Protocol):
    async def get(self, key: str) -> bytes | str | None: ...

    async def set(self, key: str, value: str, *, ex: int | None = None) -> Any: ...


class SupportState(TypedDict, total=False):
    run_id: str
    message: str
    customer_id: int | None
    selected_order_number: str | None
    extraction: dict[str, Any] | None
    routing: dict[str, Any] | None
    handler: Literal["rag", "sql", "refund"] | None
    tool_context: str | None
    sources: list[str] | None
    proposed_refund: dict[str, Any] | None
    decision: Literal["approve", "reject"] | None
    answer: str | None
    conversation_history: Annotated[list[dict[str, str]], add]


class GraphDependencies:
    def __init__(self, model: ModelClient, repository: StoreRepository, cache: CacheClient) -> None:
        self.model = model
        self.repository = repository
        self.cache = cache


def build_nodes(deps: GraphDependencies) -> dict[str, Any]:
    async def extract(state: SupportState) -> dict[str, Any]:
        extraction = await deps.model.structured(
            Extraction,
            "Extract store-support fields. Do not invent an order number or title. Use only the fixed enum values.",
            state["message"],
        )
        fields = extraction.model_dump()
        if state.get("customer_id") is not None:
            fields["order_number"] = state["selected_order_number"]
        return {"extraction": fields}

    async def route(state: SupportState) -> dict[str, Any]:
        if state["extraction"] is None:
            raise ValueError("Extraction is missing")
        extraction = json.dumps(state["extraction"])
        decision = await deps.model.structured(
            RouteDecision,
            """Classify a physical-media support message. Use refund only when the customer explicitly requests a refund or a damaged/missing item needs a refund review. Use sql only for aggregate or store-data questions such as sales counts. Use rag for every policy or order-support question. lane is billing, shipping, returns, or general.""",
            f"Message: {state['message']}\nExtraction: {extraction}",
        )
        return {"routing": decision.model_dump(), "handler": decision.handler}

    async def rag(state: SupportState) -> dict[str, Any]:
        vector = (await deps.model.embed([state["message"]]))[0]
        documents = await deps.repository.retrieve(vector)
        context = "\n\n".join(f"[{doc['document_name']}] {doc['content']}" for doc in documents)
        return {
            "tool_context": context or "No help document was retrieved.",
            "sources": [doc["document_name"] for doc in documents],
        }

    async def sql(state: SupportState) -> dict[str, Any]:
        plan = await deps.model.structured(
            SqlPlan,
            """Write one PostgreSQL SELECT query for the physical-media store. Available tables are customers(id, email, name), products(id, title, format, sku, price_cents), and orders(order_number, customer_id, product_id, quantity, ordered_at, status, refund_status). Never write data. Use current_date for relative dates.""",
            state["message"],
        )
        customer_id = state.get("customer_id")
        cache_scope = f"customer:{customer_id}" if customer_id is not None else "employee"
        cache_key = f"tool:sql:{cache_scope}:" + hashlib.sha256(plan.sql.encode()).hexdigest()
        cached = await deps.cache.get(cache_key)
        if cached is None:
            rows = (
                await deps.repository.query_customer_readonly(plan.sql, customer_id)
                if customer_id is not None
                else await deps.repository.query_readonly(plan.sql)
            )
            await deps.cache.set(cache_key, json.dumps(rows, default=str), ex=300)
        else:
            rows = json.loads(cached.decode() if isinstance(cached, bytes) else cached)
        return {"tool_context": json.dumps(rows, default=str), "sources": ["business database"]}

    async def prepare_refund(state: SupportState) -> dict[str, Any]:
        if state["extraction"] is None:
            raise ValueError("Extraction is missing")
        extraction = Extraction.model_validate(state["extraction"])
        proposal = await deps.repository.refund_proposal(
            extraction.order_number, extraction.issue_type
        )
        return {"proposed_refund": proposal.model_dump()}

    async def refund(state: SupportState) -> dict[str, Any]:
        # Checkpoint the proposed amount before asking for a decision. Resume never recalculates it.
        proposal = RefundProposal.model_validate(state["proposed_refund"])
        decision = interrupt({"proposed_refund": proposal.model_dump()})
        if decision not in {"approve", "reject"}:
            raise ValueError("Decision must be approve or reject")
        approved = decision == "approve"
        outcome = await deps.repository.record_simulated_refund(proposal, approved, state["run_id"])
        return {
            "proposed_refund": proposal.model_dump(),
            "decision": "approve" if approved else "reject",
            "tool_context": outcome,
            "sources": ["fake business data"],
        }

    async def respond(state: SupportState) -> dict[str, Any]:
        if state.get("handler") == "refund":
            # An action result is a committed fact, not a prose-generation task.
            # Never let a model turn a terminal refusal into a pending review or future promise.
            response = state.get("tool_context")
            if not response:
                raise ValueError("A trusted refund action outcome is required")
            return {
                "answer": response,
                "conversation_history": [{"role": "assistant", "content": response}],
            }
        history = state.get("conversation_history", [])[-6:]
        prior_context = (
            "\n".join(f"{entry['role'].title()}: {entry['content']}" for entry in history[:-1])
            or "No prior messages in this thread."
        )
        response = await deps.model.generate(
            "Answer as a concise store support copilot. Use only the supplied evidence and prior thread context. If a refund was rejected, say so plainly. Never claim a real payment was issued.",
            "\n".join(
                [
                    f"Prior thread context:\n{prior_context}",
                    f"Customer message: {state['message']}",
                    f"Route: {state['routing']}",
                    f"Evidence: {state.get('tool_context', '')}",
                ]
            ),
        )
        return {
            "answer": response,
            "conversation_history": [{"role": "assistant", "content": response}],
        }

    return {
        "extract": extract,
        "route": route,
        "rag": rag,
        "sql": sql,
        "prepare_refund": prepare_refund,
        "refund": refund,
        "respond": respond,
    }


def choose_handler(state: SupportState) -> Literal["rag", "sql", "refund"]:
    handler = state["handler"]
    if handler is None:
        raise ValueError("Handler is missing")
    return handler


def build_graph(
    deps: GraphDependencies, checkpointer: BaseCheckpointSaver[Any] | None = None
) -> Any:
    nodes = build_nodes(deps)
    builder = StateGraph(SupportState)
    for name, node in nodes.items():
        builder.add_node(name, node)
    builder.add_edge(START, "extract")
    builder.add_edge("extract", "route")
    builder.add_conditional_edges(
        "route", choose_handler, {"rag": "rag", "sql": "sql", "refund": "prepare_refund"}
    )
    builder.add_edge("prepare_refund", "refund")
    for handler in ("rag", "sql", "refund"):
        builder.add_edge(handler, "respond")
    builder.add_edge("respond", END)
    return builder.compile(checkpointer=checkpointer)
