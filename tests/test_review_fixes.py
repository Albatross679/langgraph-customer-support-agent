import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import NoneType, SimpleNamespace, UnionType
from typing import Any, Literal, get_args, get_origin

import pytest
from fastapi import HTTPException
from pydantic import BaseModel

from support_copilot.api import (
    ANONYMOUS_THREAD_OWNER,
    claim_daily_run,
    create_customer,
    create_order,
    create_product,
    create_run,
    decide_run,
    delete_customer,
    delete_order,
    delete_product,
    get_customer_run,
    get_daily_run_limit,
    get_run,
    identify_customer,
    list_customer_orders,
    list_customers,
    list_orders,
    list_products,
    list_runs,
    update_customer,
    update_daily_run_limit,
    update_order,
    update_product,
)
from support_copilot.ingest import find_help_directory
from support_copilot.model import strict_json_schema
from support_copilot.run_store import awaiting_orders_key, write_run_status
from support_copilot.schemas import (
    Customer,
    CustomerIdentificationRequest,
    CustomerIdentificationResponse,
    CustomerIdentity,
    CustomerInput,
    CustomerList,
    CustomerOrder,
    CustomerOrderList,
    DailyRunLimit,
    DecisionRequest,
    Extraction,
    Order,
    OrderInput,
    OrderList,
    Product,
    ProductInput,
    ProductList,
    RefundProgress,
    RefundProposal,
    RouteDecision,
    RunCreated,
    RunList,
    RunRequest,
    RunState,
    RunStatus,
)
from support_copilot.worker import THREAD_LOCK_TIMEOUT_SECONDS, WorkerSettings, run_agent


class FakeCustomerRepository:
    def __init__(self) -> None:
        self.customer = CustomerIdentity(id=1, name="Maya Chen", email="maya@example.test")
        self.other_customer = CustomerIdentity(id=2, name="Avery Stone", email="avery@example.test")
        self.daily_limit = 50
        self.thread_owners: dict[str, str] = {}
        self.orders = [
            CustomerOrder(
                order_number="ORD-1001",
                title="The Last Horizon",
                media_format="4K UHD",
                quantity=1,
                ordered_at="2025-01-01 00:00:00+00",
                status="delivered",
                refund_progress="none",
            )
        ]

    async def daily_run_limit(self, default: int) -> int:
        return self.daily_limit

    async def set_daily_run_limit(self, limit: int) -> int:
        self.daily_limit = limit
        return limit

    async def thread_owner(self, thread_id: str) -> str | None:
        return self.thread_owners.get(thread_id)

    async def create_thread_owner(self, thread_id: str, owner: str) -> None:
        if thread_id in self.thread_owners:
            raise ValueError("Thread owner already exists")
        self.thread_owners[thread_id] = owner

    async def identify_customer(self, name: str, email: str) -> CustomerIdentity | None:
        for customer in (self.customer, self.other_customer):
            if (name, email) == (customer.name, customer.email):
                return customer
        return None

    async def customer_orders(self, customer_id: int) -> list[CustomerOrder]:
        return self.orders if customer_id == self.customer.id else []

    async def customer_owns_order(self, customer_id: int, order_number: str) -> bool:
        return customer_id == self.customer.id and any(
            order.order_number == order_number for order in self.orders
        )


class FakeRedis:
    def __init__(self, run: dict[str, Any]) -> None:
        self.values = {f"run:{run['run_id']}": json.dumps(run)}
        self.enqueued: list[tuple[Any, ...]] = []
        self.hashes: dict[str, dict[str, str]] = {}
        self.enqueue_error: Exception | None = None
        self.enqueue_result: object | None = object()
        self.expires: dict[str, int] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str) -> None:
        self.values[key] = value

    async def eval(self, script: str, numkeys: int, *args: Any) -> int:
        assert "INCR" in script
        assert numkeys == 1
        key, limit, expires = args
        current = int(self.values.get(key, "0"))
        if current >= limit:
            return 0
        current += 1
        self.values[key] = str(current)
        if current == 1:
            self.expires[key] = expires
        return current

    async def hvals(self, key: str) -> list[str]:
        return list(self.hashes.get(key, {}).values())

    def pipeline(self, *, transaction: bool) -> "FakePipeline":
        assert transaction is True
        return FakePipeline(self)

    async def enqueue_job(self, *args: Any, **kwargs: Any) -> object:
        if self.enqueue_error:
            raise self.enqueue_error
        self.enqueued.append((*args, kwargs))
        return self.enqueue_result

    async def scan_iter(self, *, match: str) -> Any:
        assert match == "run:*"
        for key in self.values:
            if key.startswith("run:"):
                yield key

    @asynccontextmanager
    async def lock(self, *args: Any, **kwargs: Any):
        yield


class FakePipeline:
    def __init__(self, redis: FakeRedis) -> None:
        self.redis = redis
        self.commands: list[tuple[str, tuple[Any, ...]]] = []

    async def __aenter__(self) -> "FakePipeline":
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    def set(self, *args: Any) -> None:
        self.commands.append(("set", args))

    def hdel(self, *args: Any) -> None:
        self.commands.append(("hdel", args))

    def hset(self, *args: Any) -> None:
        self.commands.append(("hset", args))

    async def execute(self) -> None:
        for command, args in self.commands:
            if command == "set":
                self.redis.values[args[0]] = args[1]
            elif command == "hdel":
                self.redis.hashes.get(args[0], {}).pop(args[1], None)
            else:
                self.redis.hashes.setdefault(args[0], {})[args[1]] = args[2]


CONTRACT_MODEL_NAMES = {
    CustomerIdentity: "CustomerIdentity",
    CustomerIdentificationRequest: "CustomerIdentificationRequest",
    CustomerIdentificationResponse: "CustomerIdentificationResponse",
    CustomerOrder: "CustomerOrder",
    CustomerOrderList: "CustomerOrderList",
    Extraction: "StructuredExtraction",
    RouteDecision: "SupportRoute",
    RefundProposal: "ProposedRefund",
    RunStatus: "SupportRun",
    RunRequest: "CreateRunRequest",
    RunCreated: "CreateRunResponse",
    DecisionRequest: "DecisionRequest",
    DailyRunLimit: "DailyRunLimit",
    RunList: "RunListResponse",
    CustomerInput: "CustomerInput",
    Customer: "Customer",
    CustomerList: "CustomerListResponse",
    ProductInput: "ProductInput",
    Product: "Product",
    ProductList: "ProductListResponse",
    OrderInput: "OrderInput",
    Order: "Order",
    OrderList: "OrderListResponse",
}


def contract_type(annotation: Any) -> Any:
    if annotation is RunState:
        return {"ref": "RunStatus"}
    if annotation is RefundProgress:
        return {"ref": "RefundProgress"}
    origin = get_origin(annotation)
    arguments = get_args(annotation)
    if origin is Literal:
        return {"enum": list(arguments)}
    if origin is UnionType:
        non_null = [argument for argument in arguments if argument is not NoneType]
        assert len(non_null) == 1 and len(arguments) == 2
        return {"nullable": contract_type(non_null[0])}
    if origin is list:
        return {"array": contract_type(arguments[0])}
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return {"ref": CONTRACT_MODEL_NAMES[annotation]}
    return {str: "string", int: "integer", datetime: "string"}[annotation]


def contract_model(model: type[BaseModel]) -> dict[str, Any]:
    return {
        "properties": {
            name: contract_type(field.annotation) for name, field in model.model_fields.items()
        },
        "required": [name for name, field in model.model_fields.items() if field.is_required()],
    }


def test_shared_api_contract_matches_backend_models() -> None:
    contract_path = Path(__file__).parents[1] / "web" / "api-contract.json"
    contract = json.loads(contract_path.read_text())

    expected_types = {
        "RunStatus": {"enum": list(get_args(RunState.__value__))},
        "RefundProgress": {"enum": list(get_args(RefundProgress.__value__))},
        **{
            frontend_name: contract_model(model)
            for model, frontend_name in CONTRACT_MODEL_NAMES.items()
        },
    }

    assert contract["types"] == expected_types


@pytest.mark.asyncio
async def test_run_browser_navigation_returns_console(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    console_dist = tmp_path / "dist"
    console_dist.mkdir()
    (console_dist / "index.html").write_text("<main>Support Copilot</main>")
    monkeypatch.setattr("support_copilot.api.CONSOLE_DIST", console_dist)
    request = SimpleNamespace(headers={"accept": "text/html,application/xhtml+xml"})

    response = await get_run("run-1", request, FakeRedis({"run_id": "run-1"}))

    assert response.media_type == "text/html"
    assert Path(response.path).name == "index.html"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["vary"] == "Accept"


def test_openrouter_strict_schema_requires_every_property() -> None:
    schema = strict_json_schema(Extraction)

    assert schema["required"] == list(schema["properties"])
    assert schema["additionalProperties"] is False
    assert all(
        "default" not in property_schema for property_schema in schema["properties"].values()
    )


def test_help_documents_are_found_from_the_process_working_directory(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    help_directory = tmp_path / "docs" / "help"
    help_directory.mkdir(parents=True)
    (help_directory / "returns.md").write_text("Return policy")
    monkeypatch.chdir(tmp_path)

    assert find_help_directory() == help_directory


def test_thread_lock_outlives_worker_job_timeout() -> None:
    assert THREAD_LOCK_TIMEOUT_SECONDS > WorkerSettings.job_timeout


@pytest.mark.asyncio
async def test_list_runs_filters_awaiting_approval() -> None:
    paused = {
        "run_id": "run-paused",
        "thread_id": "thread-1",
        "status": "awaiting_approval",
        "proposed_refund": {
            "order_number": "ORD-1001",
            "amount_cents": 2999,
            "currency": "USD",
            "reason": "damaged_disc",
        },
    }
    redis = FakeRedis(paused)
    redis.values["run:run-completed"] = json.dumps(
        {"run_id": "run-completed", "thread_id": "thread-2", "status": "completed"}
    )

    result = await list_runs("awaiting_approval", redis=redis)

    assert [run.run_id for run in result.runs] == ["run-paused"]
    assert result.runs[0].proposed_refund is not None
    assert result.runs[0].proposed_refund.amount_cents == 2999
    assert result.total == 1
    assert result.limit == 25
    assert result.offset == 0


@pytest.mark.asyncio
async def test_customer_lookup_orders_and_scoped_run_access() -> None:
    repository = FakeCustomerRepository()
    repository.orders[0] = repository.orders[0].model_copy(update={"refund_progress": "approved"})
    redis = FakeRedis({"run_id": "run-paused", "thread_id": "thread-1", "status": "queued"})
    await write_run_status(
        redis,
        "run-paused",
        status="awaiting_approval",
        customer_id=1,
        order_number="ORD-1001",
    )
    await write_run_status(
        redis,
        "run-paused-2",
        status="awaiting_approval",
        customer_id=1,
        order_number="ORD-1001",
    )

    matched = await identify_customer(
        CustomerIdentificationRequest(name="Maya Chen", email="maya@example.test"), repository
    )
    missing = await identify_customer(
        CustomerIdentificationRequest(name="Maya Chen", email="wrong@example.test"), repository
    )
    orders = await list_customer_orders(1, "Maya Chen", "maya@example.test", redis, repository)
    run = await get_customer_run(
        1, "run-paused", "Maya Chen", "maya@example.test", redis, repository
    )

    assert matched.customer == repository.customer
    assert missing.customer is None
    # A second conversation must not mask the order's terminal approved action.
    assert orders.orders[0].refund_progress == "approved"
    assert run.run_id == "run-paused"

    await write_run_status(redis, "run-paused", status="completed")
    assert await redis.hvals(awaiting_orders_key(1)) == ["ORD-1001"]

    await write_run_status(redis, "run-paused-2", status="completed")
    assert await redis.hvals(awaiting_orders_key(1)) == []


@pytest.mark.asyncio
async def test_daily_run_cap_claims_until_limit_and_expires_at_utc_midnight() -> None:
    redis = FakeRedis({"run_id": "existing", "thread_id": "thread-existing", "status": "queued"})
    now = datetime(2025, 1, 2, 23, 59, 45, tzinfo=UTC)

    assert await claim_daily_run(redis, 2, now)
    assert await claim_daily_run(redis, 2, now)
    assert not await claim_daily_run(redis, 2, now)
    assert redis.values["daily:runs:2025-01-02"] == "2"
    assert redis.expires["daily:runs:2025-01-02"] == 15


@pytest.mark.asyncio
async def test_create_run_returns_429_when_daily_limit_is_reached() -> None:
    redis = FakeRedis({"run_id": "existing", "thread_id": "thread-existing", "status": "queued"})
    repository = FakeCustomerRepository()
    repository.daily_limit = 1

    created = await create_run(RunRequest(message="First request"), redis, repository)
    with pytest.raises(HTTPException) as error:
        await create_run(RunRequest(message="Second request"), redis, repository)

    assert created.run_id
    assert error.value.status_code == 429
    assert error.value.detail == "Daily demo budget is used up, come back tomorrow."


@pytest.mark.asyncio
async def test_follow_up_rejects_a_different_customer_identity() -> None:
    redis = FakeRedis({"run_id": "existing", "thread_id": "thread-existing", "status": "queued"})
    repository = FakeCustomerRepository()
    first = await create_run(
        RunRequest(message="First request", customer=repository.customer), redis, repository
    )
    redis = FakeRedis({"run_id": "after-restart", "thread_id": "unused", "status": "queued"})

    with pytest.raises(HTTPException) as error:
        await create_run(
            RunRequest(
                message="Other customer follow-up",
                thread_id=first.thread_id,
                customer=repository.other_customer,
            ),
            redis,
            repository,
        )

    assert repository.thread_owners[first.thread_id] == "customer:1"
    assert all(not key.startswith("daily:runs:") for key in redis.values)
    assert error.value.status_code == 403
    assert (
        error.value.detail == "This support thread cannot be continued with this customer identity."
    )


@pytest.mark.asyncio
async def test_anonymous_thread_cannot_be_claimed_by_a_customer() -> None:
    redis = FakeRedis({"run_id": "existing", "thread_id": "thread-existing", "status": "queued"})
    repository = FakeCustomerRepository()
    first = await create_run(RunRequest(message="Anonymous request"), redis, repository)

    with pytest.raises(HTTPException) as error:
        await create_run(
            RunRequest(
                message="Customer follow-up",
                thread_id=first.thread_id,
                customer=repository.customer,
            ),
            redis,
            repository,
        )

    assert repository.thread_owners[first.thread_id] == ANONYMOUS_THREAD_OWNER
    assert error.value.status_code == 403


@pytest.mark.asyncio
async def test_follow_up_rejects_a_thread_without_a_durable_owner() -> None:
    redis = FakeRedis({"run_id": "existing", "thread_id": "legacy-thread", "status": "queued"})
    repository = FakeCustomerRepository()

    with pytest.raises(HTTPException) as error:
        await create_run(
            RunRequest(
                message="Legacy follow-up",
                thread_id="legacy-thread",
                customer=repository.customer,
            ),
            redis,
            repository,
        )

    assert error.value.status_code == 403
    assert (
        error.value.detail == "This support thread has no recorded owner. Start a new conversation."
    )
    assert all(not key.startswith("daily:runs:") for key in redis.values)


@pytest.mark.asyncio
async def test_runtime_daily_limit_change_takes_effect_without_restart() -> None:
    redis = FakeRedis({"run_id": "existing", "thread_id": "thread-existing", "status": "queued"})
    repository = FakeCustomerRepository()

    assert (await get_daily_run_limit(repository)).daily_run_limit == 50
    assert (
        await update_daily_run_limit(DailyRunLimit(daily_run_limit=1), repository)
    ).daily_run_limit == 1
    await create_run(RunRequest(message="First request"), redis, repository)
    with pytest.raises(HTTPException) as error:
        await create_run(RunRequest(message="Second request"), redis, repository)

    assert error.value.status_code == 429


@pytest.mark.asyncio
async def test_daily_run_cap_is_disabled_when_limit_is_zero() -> None:
    redis = FakeRedis({"run_id": "existing", "thread_id": "thread-existing", "status": "queued"})

    assert await claim_daily_run(redis, 0)
    assert redis.expires == {}
    assert all(not key.startswith("daily:runs:") for key in redis.values)


@pytest.mark.asyncio
async def test_customer_run_rejects_an_order_outside_their_account() -> None:
    repository = FakeCustomerRepository()
    redis = FakeRedis({"run_id": "run-1", "thread_id": "thread-1", "status": "queued"})

    with pytest.raises(HTTPException) as error:
        await create_run(
            RunRequest(
                message="Please help",
                customer=repository.customer,
                order_number="ORD-9999",
            ),
            redis,
            repository,
        )

    assert error.value.status_code == 422


@pytest.mark.asyncio
async def test_failed_decision_enqueue_leaves_run_retryable() -> None:
    run = {
        "run_id": "run-1",
        "thread_id": "thread-1",
        "status": "awaiting_approval",
        "proposed_refund": {
            "order_number": "ORD-1001",
            "amount_cents": 2999,
            "currency": "USD",
            "reason": "damaged_disc",
        },
    }
    redis = FakeRedis(run)
    redis.enqueue_error = RuntimeError("queue unavailable")

    with pytest.raises(RuntimeError, match="queue unavailable"):
        await decide_run("run-1", DecisionRequest(decision="approve"), redis)

    stored = json.loads(redis.values["run:run-1"])
    assert stored["status"] == "awaiting_approval"


@pytest.mark.asyncio
async def test_duplicate_decision_is_rejected() -> None:
    run = {"run_id": "run-1", "thread_id": "thread-1", "status": "awaiting_approval"}
    redis = FakeRedis(run)
    redis.enqueue_result = None

    with pytest.raises(HTTPException) as error:
        await decide_run("run-1", DecisionRequest(decision="reject"), redis)

    assert error.value.status_code == 409
    assert error.value.detail == "A decision is already queued"


class FakeBusinessCursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.row: dict[str, Any] | None = None
        self.rowcount = 0

    async def execute(self, query: str, params: tuple[Any, ...] = ()) -> None:
        if query.startswith("SELECT"):
            return
        if query.startswith("INSERT"):
            fields = query.split("(", 1)[1].split(")", 1)[0].split(", ")
            self.row = {"id": max((row["id"] for row in self.rows), default=0) + 1}
            self.row.update(dict(zip(fields, params, strict=True)))
            self.rows.append(self.row)
            return
        if query.startswith("UPDATE"):
            assignments = query.split(" SET ", 1)[1].split(" WHERE ", 1)[0]
            fields = [assignment.split(" = ", 1)[0] for assignment in assignments.split(", ")]
            row_id = params[-1]
            self.row = next((row for row in self.rows if row["id"] == row_id), None)
            if self.row is not None:
                self.row.update(dict(zip(fields, params[:-1], strict=True)))
            return
        if query.startswith("DELETE"):
            row_id = params[0]
            previous = len(self.rows)
            self.rows[:] = [row for row in self.rows if row["id"] != row_id]
            self.rowcount = previous - len(self.rows)

    async def fetchall(self) -> list[dict[str, Any]]:
        return self.rows

    async def fetchone(self) -> dict[str, Any] | None:
        return self.row

    async def __aenter__(self) -> "FakeBusinessCursor":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class FakeBusinessConnection:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def cursor(self, **kwargs: Any) -> FakeBusinessCursor:
        return FakeBusinessCursor(self.rows)

    @asynccontextmanager
    async def transaction(self) -> Any:
        yield

    async def __aenter__(self) -> "FakeBusinessConnection":
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None


class FakeBusinessPool:
    def __init__(self, rows: list[dict[str, Any]] | None = None) -> None:
        self.rows = rows or [{"id": 1, "email": "maya@example.test", "name": "Maya Chen"}]

    def connection(self) -> FakeBusinessConnection:
        return FakeBusinessConnection(self.rows)


@pytest.mark.asyncio
async def test_customer_crud_endpoints_use_the_business_pool() -> None:
    pool = FakeBusinessPool()

    listed = await list_customers(pool)
    created = await create_customer(
        CustomerInput(email="sam@example.test", name="Sam Rivera"), pool
    )
    updated = await update_customer(
        created.id, CustomerInput(email="sam@example.test", name="Sam R."), pool
    )
    deleted = await delete_customer(created.id, pool)

    assert [customer.id for customer in listed.customers] == [1]
    assert updated.name == "Sam R."
    assert deleted.status_code == 204
    assert pool.rows == [{"id": 1, "email": "maya@example.test", "name": "Maya Chen"}]


@pytest.mark.asyncio
async def test_product_crud_endpoints_use_the_business_pool() -> None:
    pool = FakeBusinessPool(
        [
            {
                "id": 1,
                "title": "Demo disc",
                "format": "DVD",
                "sku": "DVD-1",
                "price_cents": 999,
            }
        ]
    )

    listed = await list_products(pool)
    created = await create_product(
        ProductInput(title="Demo 4K", format="4K UHD", sku="UHD-2", price_cents=2999), pool
    )
    updated = await update_product(
        created.id,
        ProductInput(title="Demo 4K edited", format="4K UHD", sku="UHD-2", price_cents=2499),
        pool,
    )
    deleted = await delete_product(created.id, pool)

    assert [product.id for product in listed.products] == [1]
    assert updated.title == "Demo 4K edited"
    assert deleted.status_code == 204
    assert [product["id"] for product in pool.rows] == [1]


@pytest.mark.asyncio
async def test_order_crud_endpoints_use_the_business_pool() -> None:
    ordered_at = datetime.fromisoformat("2025-01-01T12:00:00+00:00")
    pool = FakeBusinessPool(
        [
            {
                "id": 1,
                "order_number": "ORD-1",
                "customer_id": 1,
                "product_id": 1,
                "quantity": 1,
                "ordered_at": ordered_at,
                "status": "delivered",
                "refund_status": "none",
            }
        ]
    )

    listed = await list_orders(pool)
    created = await create_order(
        OrderInput(
            order_number="ORD-2",
            customer_id=1,
            product_id=1,
            quantity=2,
            ordered_at=ordered_at,
            status="processing",
        ),
        pool,
    )
    updated = await update_order(
        created.id,
        OrderInput(
            order_number="ORD-2",
            customer_id=1,
            product_id=1,
            quantity=2,
            ordered_at=ordered_at,
            status="shipped",
        ),
        pool,
    )
    deleted = await delete_order(created.id, pool)

    assert [order.id for order in listed.orders] == [1]
    assert updated.status == "shipped"
    assert deleted.status_code == 204
    assert [order["id"] for order in pool.rows] == [1]


@pytest.mark.asyncio
async def test_run_listing_is_paginated_and_newest_first() -> None:
    redis = FakeRedis(
        {
            "run_id": "old",
            "thread_id": "thread-old",
            "status": "completed",
            "created_at": "2025-01-01T00:00:00Z",
        }
    )
    redis.values["run:new"] = json.dumps(
        {
            "run_id": "new",
            "thread_id": "thread-new",
            "status": "completed",
            "created_at": "2025-01-02T00:00:00Z",
            "message_preview": "Recent customer message",
        }
    )

    result = await list_runs(limit=1, offset=0, redis=redis)

    assert [run.run_id for run in result.runs] == ["new"]
    assert result.total == 2
    assert result.runs[0].message_preview == "Recent customer message"


@pytest.mark.asyncio
async def test_new_thread_run_clears_values_from_previous_run() -> None:
    snapshots = [
        SimpleNamespace(values={"answer": "old", "proposed_refund": {"amount_cents": 1}}, tasks=[]),
        SimpleNamespace(
            values={"run_id": "run-2", "answer": "new", "proposed_refund": None},
            tasks=[],
        ),
    ]

    class FakeGraph:
        def __init__(self) -> None:
            self.input: dict[str, Any] | None = None

        async def aget_state(self, config: dict[str, Any]) -> Any:
            return snapshots.pop(0)

        async def ainvoke(self, state: dict[str, Any], config: dict[str, Any]) -> None:
            self.input = state

    graph = FakeGraph()
    redis = FakeRedis({"run_id": "run-2", "thread_id": "thread-1", "status": "queued"})
    result = await run_agent(
        {"redis": redis, "graph": graph},
        "run-2",
        "new question",
        "thread-1",
    )

    assert graph.input is not None
    assert graph.input["answer"] is None
    assert graph.input["proposed_refund"] is None
    assert result == {"answer": "new"}


@pytest.mark.asyncio
async def test_cancelled_run_remains_recoverable() -> None:
    class CancelledGraph:
        async def aget_state(self, config: dict[str, Any]) -> Any:
            return SimpleNamespace(values={}, tasks=[])

        async def ainvoke(self, state: dict[str, Any], config: dict[str, Any]) -> None:
            raise asyncio.CancelledError

    redis = FakeRedis({"run_id": "run-3", "thread_id": "thread-3", "status": "queued"})

    with pytest.raises(asyncio.CancelledError):
        await run_agent(
            {"redis": redis, "graph": CancelledGraph()},
            "run-3",
            "question",
            "thread-3",
        )

    stored = json.loads(redis.values["run:run-3"])
    assert stored["status"] == "running"
    assert 'error' not in stored
