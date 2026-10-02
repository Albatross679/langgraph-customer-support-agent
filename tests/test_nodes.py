import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from support_copilot.graph import GraphDependencies, build_graph, build_nodes
from support_copilot.schemas import Extraction, RouteDecision
from tests.conftest import FakeCache, FakeModel, FakeRepository, defaults


@pytest.mark.asyncio
async def test_extract_and_route_use_structured_model_output() -> None:
    model = FakeModel(defaults())
    nodes = build_nodes(GraphDependencies(model, FakeRepository(), FakeCache()))
    extracted = await nodes["extract"]({"message": "Can I return ORD-1001?"})
    routed = await nodes["route"]({"message": "Can I return ORD-1001?", **extracted})
    assert extracted["extraction"]["order_number"] == "ORD-1001"
    assert routed["handler"] == "rag"
    assert model.calls == [Extraction, RouteDecision]


@pytest.mark.asyncio
async def test_selected_order_overrides_model_extraction() -> None:
    model = FakeModel(defaults())
    nodes = build_nodes(GraphDependencies(model, FakeRepository(), FakeCache()))

    extracted = await nodes["extract"](
        {
            "message": "My disc arrived scratched",
            "customer_id": 1,
            "selected_order_number": "ORD-1004",
        }
    )

    assert extracted["extraction"]["order_number"] == "ORD-1004"


@pytest.mark.asyncio
async def test_customer_general_question_clears_model_order_extraction() -> None:
    model = FakeModel(defaults())
    nodes = build_nodes(GraphDependencies(model, FakeRepository(), FakeCache()))

    extracted = await nodes["extract"](
        {
            "message": "How do refunds work for ORD-1001?",
            "customer_id": 1,
            "selected_order_number": None,
        }
    )

    assert extracted["extraction"]["order_number"] is None


@pytest.mark.asyncio
async def test_rag_retrieves_documents_and_responds_from_context() -> None:
    model = FakeModel(defaults())
    nodes = build_nodes(GraphDependencies(model, FakeRepository(), FakeCache()))
    retrieved = await nodes["rag"]({"message": "What is the return window?"})
    answer = await nodes["respond"](
        {"message": "What is the return window?", "routing": {"handler": "rag"}, **retrieved}
    )
    assert retrieved["sources"] == ["returns.md"]
    assert "30 days" in retrieved["tool_context"]
    assert answer["answer"] == "Grounded support reply"


@pytest.mark.asyncio
async def test_same_thread_answer_receives_prior_messages() -> None:
    class ContextModel(FakeModel):
        def __init__(self) -> None:
            super().__init__(defaults())
            self.prompts: list[str] = []

        async def generate(self, system: str, user: str) -> str:
            self.prompts.append(user)
            return f"Reply {len(self.prompts)}"

    model = ContextModel()
    graph = build_graph(GraphDependencies(model, FakeRepository(), FakeCache()), InMemorySaver())
    config = {"configurable": {"thread_id": "context-thread"}}
    await graph.ainvoke(
        {
            "message": "What are the return rules for unopened items?",
            "conversation_history": [
                {"role": "user", "content": "What are the return rules for unopened items?"}
            ],
        },
        config=config,
    )
    await graph.ainvoke(
        {
            "message": "What did I just ask about?",
            "conversation_history": [{"role": "user", "content": "What did I just ask about?"}],
        },
        config=config,
    )

    assert "What are the return rules for unopened items?" in model.prompts[-1]
    assert "Reply 1" in model.prompts[-1]


@pytest.mark.asyncio
async def test_sql_query_is_cached() -> None:
    repository = FakeRepository()
    cache = FakeCache()
    model = FakeModel(defaults("sql"))
    sql_node = build_nodes(GraphDependencies(model, repository, cache))["sql"]
    first = await sql_node({"message": "How many copies sold?"})
    second = await sql_node({"message": "How many copies sold?"})
    assert first["tool_context"] == second["tool_context"]
    assert repository.queries == ["SELECT COUNT(*) AS copies_sold FROM orders"]
    assert len(cache.values) == 1


@pytest.mark.asyncio
async def test_customer_sql_query_uses_customer_scope_and_separate_cache() -> None:
    repository = FakeRepository()
    cache = FakeCache()
    model = FakeModel(defaults("sql"))
    sql_node = build_nodes(GraphDependencies(model, repository, cache))["sql"]

    employee = await sql_node({"message": "How many copies sold?"})
    customer = await sql_node({"message": "How many copies did I buy?", "customer_id": 7})

    assert employee["tool_context"] == '[{"copies_sold": 3}]'
    assert customer["tool_context"] == '[{"copies_sold": 1}]'
    assert repository.queries == [
        "SELECT COUNT(*) AS copies_sold FROM orders",
        "customer:7:SELECT COUNT(*) AS copies_sold FROM orders",
    ]
    assert len(cache.values) == 2


@pytest.mark.asyncio
async def test_refund_pauses_then_records_human_approval() -> None:
    repository = FakeRepository()
    model = FakeModel(defaults("refund"))
    graph = build_graph(GraphDependencies(model, repository, FakeCache()), InMemorySaver())
    config = {"configurable": {"thread_id": "refund-thread"}}
    await graph.ainvoke({"run_id": "refund-test", "message": "Refund damaged ORD-1001"}, config=config)
    paused = await graph.aget_state(config)
    assert paused.tasks[0].interrupts[0].value["proposed_refund"]["amount_cents"] == 2999
    completed = await graph.ainvoke(Command(resume="approve"), config=config)
    assert completed["answer"] == "The simulated refund was approved."
    assert repository.refunds[0][1] is True


def test_sql_schema_rejects_writes() -> None:
    from support_copilot.db import scope_customer_sql, validate_readonly_sql

    with pytest.raises(ValueError):
        validate_readonly_sql("DELETE FROM orders")
    with pytest.raises(ValueError):
        validate_readonly_sql("SELECT * FROM help_document_embeddings")
    with pytest.raises(ValueError):
        validate_readonly_sql("SELECT pg_read_file('/etc/passwd') FROM orders LIMIT 1")
    with pytest.raises(ValueError):
        validate_readonly_sql("SELECT public.count(*) FROM orders")
    with pytest.raises(ValueError):
        validate_readonly_sql("SELECT p.* FROM orders o, pg_catalog.pg_tables p")
    with pytest.raises(ValueError):
        validate_readonly_sql(
            "SELECT * FROM orders WHERE product_id IN (SELECT oid FROM pg_catalog.pg_class)"
        )
    validate_readonly_sql("SELECT title, format FROM products")
    validate_readonly_sql("SELECT COUNT(*) FROM orders")
    validate_readonly_sql("WITH recent AS (SELECT * FROM orders) SELECT COUNT(*) FROM recent")
    validate_readonly_sql(
        "SELECT COUNT(*) FROM orders "
        "WHERE ordered_at >= date_trunc('month', current_date) - INTERVAL '1 month' "
        "AND ordered_at < date_trunc('month', current_date)"
    )
    scoped = scope_customer_sql(
        "SELECT c.email, COUNT(*) FROM customers c JOIN orders o ON o.customer_id = c.id "
        "JOIN products p ON p.id = o.product_id GROUP BY c.email",
        7,
    )
    assert "FROM (SELECT * FROM customers WHERE id = 7) AS c" in scoped
    assert "JOIN (SELECT * FROM orders WHERE customer_id = 7) AS o" in scoped
