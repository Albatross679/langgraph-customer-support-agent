import json
import math
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

from arq.connections import RedisSettings, create_pool
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from psycopg import IntegrityError
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from support_copilot.config import get_settings
from support_copilot.db import StoreRepository
from support_copilot.run_store import (
    awaiting_orders_key,
    claim_decision,
    load_durable_run,
    save_durable_run,
    write_run_status,
)
from support_copilot.schemas import (
    Customer,
    CustomerIdentificationRequest,
    CustomerIdentificationResponse,
    CustomerInput,
    CustomerList,
    CustomerOrderList,
    DailyRunLimit,
    DecisionRequest,
    Order,
    OrderInput,
    OrderList,
    Product,
    ProductInput,
    ProductList,
    RunCreated,
    RunList,
    RunRequest,
    RunState,
    RunStatus,
)

settings = get_settings()
CONSOLE_DIST = Path.cwd() / "web" / "dist"
DAILY_RUN_KEY_PREFIX = "daily:runs:"
ANONYMOUS_THREAD_OWNER = "anonymous"
DAILY_RUN_CAP_SCRIPT = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current >= tonumber(ARGV[1]) then return 0 end
current = redis.call('INCR', KEYS[1])
if current == 1 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
return current
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    app.state.business_pool = AsyncConnectionPool(settings.database_url, open=False)
    await app.state.business_pool.open()
    app.state.repository = StoreRepository(app.state.business_pool)
    app.state.redis.durable_pool = app.state.business_pool
    async with app.state.business_pool.connection() as conn:
        result = await conn.execute("SELECT payload FROM support_runs")
        records = await result.fetchall()
    for (data,) in records:
        await write_run_status(
            app.state.redis, data["run_id"], **{k: v for k, v in data.items() if k != "run_id"}
        )
    yield
    await app.state.redis.aclose()
    await app.state.business_pool.close()


app = FastAPI(title="Portfolio Support Copilot", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE"],
    allow_headers=["Content-Type"],
)


def redis_client(request: Request) -> Any:
    return request.app.state.redis


def business_pool(request: Request) -> AsyncConnectionPool:
    return request.app.state.business_pool


def store_repository(request: Request) -> StoreRepository:
    return request.app.state.repository


def daily_run_key(now: datetime) -> str:
    return f"{DAILY_RUN_KEY_PREFIX}{now.astimezone(UTC).date().isoformat()}"


def seconds_until_next_utc_day(now: datetime) -> int:
    utc_now = now.astimezone(UTC)
    tomorrow = utc_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return max(1, math.ceil((tomorrow.timestamp() + 86_400) - utc_now.timestamp()))


async def claim_daily_run(redis: Any, limit: int, now: datetime | None = None) -> bool:
    if limit == 0:
        return True
    now = now or datetime.now(UTC)
    claimed = await redis.eval(
        DAILY_RUN_CAP_SCRIPT,
        1,
        daily_run_key(now),
        limit,
        seconds_until_next_utc_day(now),
    )
    return bool(claimed)


async def read_run(redis: Any, run_id: str) -> dict[str, Any]:
    pool = getattr(redis, "durable_pool", None)
    if pool is not None:
        durable = await load_durable_run(pool, run_id)
        if durable is not None:
            return durable
    value = await redis.get(f"run:{run_id}")
    if value is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return json.loads(value.decode() if isinstance(value, bytes) else value)


async def list_business_rows(pool: AsyncConnectionPool, table: str) -> list[dict[str, Any]]:
    async with pool.connection() as conn:
        async with conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"SELECT * FROM {table} ORDER BY id")
            return list(await cur.fetchall())


async def create_business_row(
    pool: AsyncConnectionPool, table: str, values: dict[str, Any]
) -> dict[str, Any]:
    fields = tuple(values)
    placeholders = ", ".join("%s" for _ in fields)
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"INSERT INTO {table} ({', '.join(fields)}) VALUES ({placeholders}) RETURNING *",
                    tuple(values[field] for field in fields),
                )
                row = await cur.fetchone()
    assert row is not None
    return row


async def update_business_row(
    pool: AsyncConnectionPool, table: str, row_id: int, values: dict[str, Any]
) -> dict[str, Any] | None:
    fields = tuple(values)
    assignments = ", ".join(f"{field} = %s" for field in fields)
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    f"UPDATE {table} SET {assignments} WHERE id = %s RETURNING *",
                    (*[values[field] for field in fields], row_id),
                )
                return await cur.fetchone()


async def delete_business_row(pool: AsyncConnectionPool, table: str, row_id: int) -> bool:
    async with pool.connection() as conn:
        async with conn.transaction():
            async with conn.cursor() as cur:
                await cur.execute(f"DELETE FROM {table} WHERE id = %s", (row_id,))
                return cur.rowcount == 1


def conflict_message(table: str, deleting: bool = False) -> HTTPException:
    if deleting:
        return HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Cannot delete this row because related records still reference it.",
        )
    return HTTPException(
        status_code=status.HTTP_409_CONFLICT,
        detail=f"A {table[:-1]} with one of these unique values already exists.",
    )


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/ready")
async def ready(
    redis: Any = Depends(redis_client), pool: AsyncConnectionPool = Depends(business_pool)
) -> dict[str, str]:
    await redis.ping()
    async with pool.connection() as conn:
        await conn.execute("SELECT run_id FROM support_runs LIMIT 1")
    return {"status": "ready"}


@app.post("/customers/identify", response_model=CustomerIdentificationResponse)
async def identify_customer(
    payload: CustomerIdentificationRequest, repository: StoreRepository = Depends(store_repository)
) -> CustomerIdentificationResponse:
    return CustomerIdentificationResponse(
        customer=await repository.identify_customer(payload.name, payload.email)
    )


async def require_customer(
    customer_id: int, name: str, email: str, repository: StoreRepository
) -> None:
    customer = await repository.identify_customer(name, email)
    if customer is None or customer.id != customer_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")


@app.get("/customers/{customer_id}/orders", response_model=CustomerOrderList)
async def list_customer_orders(
    customer_id: int,
    name: str,
    email: str,
    redis: Any = Depends(redis_client),
    repository: StoreRepository = Depends(store_repository),
) -> CustomerOrderList:
    await require_customer(customer_id, name, email, repository)
    orders = await repository.customer_orders(customer_id)
    indexed_orders = await redis.hvals(awaiting_orders_key(customer_id))
    awaiting_approval = {
        value.decode() if isinstance(value, bytes) else value for value in indexed_orders
    }
    return CustomerOrderList(
        orders=[
            order.model_copy(
                update={"refund_progress": "awaiting_approval"}
                if order.order_number in awaiting_approval and order.refund_progress != "approved"
                else {}
            )
            for order in orders
        ]
    )


@app.get("/settings/daily-run-limit", response_model=DailyRunLimit)
async def get_daily_run_limit(
    repository: StoreRepository = Depends(store_repository),
) -> DailyRunLimit:
    return DailyRunLimit(daily_run_limit=await repository.daily_run_limit(settings.daily_run_limit))


@app.put("/settings/daily-run-limit", response_model=DailyRunLimit)
async def update_daily_run_limit(
    payload: DailyRunLimit, repository: StoreRepository = Depends(store_repository)
) -> DailyRunLimit:
    return DailyRunLimit(
        daily_run_limit=await repository.set_daily_run_limit(payload.daily_run_limit)
    )


@app.post("/runs", response_model=RunCreated, status_code=status.HTTP_202_ACCEPTED)
async def create_run(
    payload: RunRequest,
    redis: Any = Depends(redis_client),
    repository: StoreRepository = Depends(store_repository),
) -> RunCreated:
    customer_id: int | None = None
    if payload.customer:
        await require_customer(
            payload.customer.id, payload.customer.name, payload.customer.email, repository
        )
        customer_id = payload.customer.id
        if payload.order_number and not await repository.customer_owns_order(
            customer_id, payload.order_number
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Order not found"
            )
    elif payload.order_number:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="An order selection requires a customer identity",
        )
    owner = ANONYMOUS_THREAD_OWNER if customer_id is None else f"customer:{customer_id}"
    if payload.thread_id:
        thread_id = payload.thread_id
        recorded_owner = await repository.thread_owner(thread_id)
        if recorded_owner is None:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This support thread has no recorded owner. Start a new conversation.",
            )
        if recorded_owner != owner:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="This support thread cannot be continued with this customer identity.",
            )
    else:
        thread_id = str(uuid.uuid4())
    daily_run_limit = await repository.daily_run_limit(settings.daily_run_limit)
    if not await claim_daily_run(redis, daily_run_limit):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Daily demo budget is used up, come back tomorrow.",
        )
    if payload.thread_id is None:
        await repository.create_thread_owner(thread_id, owner)
    run_id = str(uuid.uuid4())
    preview = " ".join(payload.message.split())[:200]
    data = {
        "run_id": run_id,
        "thread_id": thread_id,
        "status": "queued",
        "created_at": datetime.now(UTC).isoformat(),
        "message_preview": preview,
        "message": payload.message,
        "customer_id": customer_id,
        "order_number": payload.order_number,
    }
    pool = getattr(redis, "durable_pool", None)
    if pool is not None:
        await save_durable_run(pool, data)
    await redis.set(f"run:{run_id}", json.dumps(data))
    await redis.enqueue_job(
        "run_agent",
        run_id,
        payload.message,
        thread_id,
        customer_id,
        payload.order_number,
        _job_id=run_id,
    )
    return RunCreated(run_id=run_id, thread_id=thread_id)


@app.get("/customers/{customer_id}/runs/{run_id}", response_model=RunStatus)
async def get_customer_run(
    customer_id: int,
    run_id: str,
    name: str,
    email: str,
    redis: Any = Depends(redis_client),
    repository: StoreRepository = Depends(store_repository),
) -> RunStatus:
    await require_customer(customer_id, name, email, repository)
    run = await read_run(redis, run_id)
    if run.get("customer_id") != customer_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found")
    return RunStatus.model_validate(run)


@app.get("/runs", response_model=RunList)
async def list_runs(
    run_status: Annotated[RunState | None, Query(alias="status")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 25,
    offset: Annotated[int, Query(ge=0)] = 0,
    redis: Any = Depends(redis_client),
) -> RunList:
    runs: list[RunStatus] = []
    pool = getattr(redis, "durable_pool", None)
    if pool is not None:
        async with pool.connection() as conn:
            result = await conn.execute("SELECT payload FROM support_runs")
            records = [row[0] for row in await result.fetchall()]
    else:
        records = []
        async for key in redis.scan_iter(match="run:*"):
            key_text = key.decode() if isinstance(key, bytes) else key
            records.append(await read_run(redis, key_text.removeprefix("run:")))
    for data in records:
        run = RunStatus.model_validate(data)
        if run_status is None or run.status == run_status:
            runs.append(run)
    runs.sort(key=lambda run: run.created_at or datetime.min.replace(tzinfo=UTC), reverse=True)
    return RunList(runs=runs[offset : offset + limit], total=len(runs), limit=limit, offset=offset)


@app.get("/runs/{run_id}", response_model=RunStatus)
async def get_run(
    run_id: str, request: Request, redis: Any = Depends(redis_client)
) -> RunStatus | FileResponse:
    if "text/html" in request.headers.get("accept", "") and CONSOLE_DIST.is_dir():
        return FileResponse(
            CONSOLE_DIST / "index.html",
            headers={"Cache-Control": "no-store", "Vary": "Accept"},
        )
    return RunStatus.model_validate(await read_run(redis, run_id))


@app.post("/runs/{run_id}/decision", response_model=RunStatus, status_code=status.HTTP_202_ACCEPTED)
async def decide_run(
    run_id: str, payload: DecisionRequest, redis: Any = Depends(redis_client)
) -> RunStatus:
    run = await read_run(redis, run_id)
    pool = getattr(redis, "durable_pool", None)
    if pool is not None:
        if not await claim_decision(pool, run_id, payload.decision):
            raise HTTPException(
                status_code=409,
                detail="Run is not awaiting approval or a conflicting decision exists",
            )
        if run["status"] == "completed":
            return RunStatus.model_validate(run)
    elif run["status"] != "awaiting_approval":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Run is not awaiting approval"
        )
    job = await redis.enqueue_job(
        "resume_agent",
        run_id,
        run["thread_id"],
        payload.decision,
        _job_id=f"{run_id}:decision",
    )
    if job is None and pool is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="A decision is already queued"
        )
    return RunStatus.model_validate(await read_run(redis, run_id))


@app.get("/customers", response_model=CustomerList)
async def list_customers(pool: AsyncConnectionPool = Depends(business_pool)) -> CustomerList:
    return CustomerList(customers=await list_business_rows(pool, "customers"))


@app.post("/customers", response_model=Customer, status_code=status.HTTP_201_CREATED)
async def create_customer(
    payload: CustomerInput, pool: AsyncConnectionPool = Depends(business_pool)
) -> Customer:
    try:
        return Customer.model_validate(
            await create_business_row(pool, "customers", payload.model_dump())
        )
    except IntegrityError as error:
        raise conflict_message("customers") from error


@app.put("/customers/{customer_id}", response_model=Customer)
async def update_customer(
    customer_id: int, payload: CustomerInput, pool: AsyncConnectionPool = Depends(business_pool)
) -> Customer:
    try:
        row = await update_business_row(pool, "customers", customer_id, payload.model_dump())
    except IntegrityError as error:
        raise conflict_message("customers") from error
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    return Customer.model_validate(row)


@app.delete("/customers/{customer_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_customer(
    customer_id: int, pool: AsyncConnectionPool = Depends(business_pool)
) -> Response:
    try:
        deleted = await delete_business_row(pool, "customers", customer_id)
    except IntegrityError as error:
        raise conflict_message("customers", deleting=True) from error
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Customer not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/products", response_model=ProductList)
async def list_products(pool: AsyncConnectionPool = Depends(business_pool)) -> ProductList:
    return ProductList(products=await list_business_rows(pool, "products"))


@app.post("/products", response_model=Product, status_code=status.HTTP_201_CREATED)
async def create_product(
    payload: ProductInput, pool: AsyncConnectionPool = Depends(business_pool)
) -> Product:
    try:
        return Product.model_validate(
            await create_business_row(pool, "products", payload.model_dump())
        )
    except IntegrityError as error:
        raise conflict_message("products") from error


@app.put("/products/{product_id}", response_model=Product)
async def update_product(
    product_id: int, payload: ProductInput, pool: AsyncConnectionPool = Depends(business_pool)
) -> Product:
    try:
        row = await update_business_row(pool, "products", product_id, payload.model_dump())
    except IntegrityError as error:
        raise conflict_message("products") from error
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return Product.model_validate(row)


@app.delete("/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(
    product_id: int, pool: AsyncConnectionPool = Depends(business_pool)
) -> Response:
    try:
        deleted = await delete_business_row(pool, "products", product_id)
    except IntegrityError as error:
        raise conflict_message("products", deleting=True) from error
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@app.get("/orders", response_model=OrderList)
async def list_orders(pool: AsyncConnectionPool = Depends(business_pool)) -> OrderList:
    return OrderList(orders=await list_business_rows(pool, "orders"))


@app.post("/orders", response_model=Order, status_code=status.HTTP_201_CREATED)
async def create_order(
    payload: OrderInput, pool: AsyncConnectionPool = Depends(business_pool)
) -> Order:
    try:
        return Order.model_validate(await create_business_row(pool, "orders", payload.model_dump()))
    except IntegrityError as error:
        raise conflict_message("orders") from error


@app.put("/orders/{order_id}", response_model=Order)
async def update_order(
    order_id: int, payload: OrderInput, pool: AsyncConnectionPool = Depends(business_pool)
) -> Order:
    try:
        row = await update_business_row(pool, "orders", order_id, payload.model_dump())
    except IntegrityError as error:
        raise conflict_message("orders") from error
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return Order.model_validate(row)


@app.delete("/orders/{order_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_order(
    order_id: int, pool: AsyncConnectionPool = Depends(business_pool)
) -> Response:
    try:
        deleted = await delete_business_row(pool, "orders", order_id)
    except IntegrityError as error:
        raise conflict_message("orders", deleting=True) from error
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Order not found")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


if CONSOLE_DIST.is_dir():
    app.mount("/assets", StaticFiles(directory=CONSOLE_DIST / "assets"), name="console-assets")

    @app.get("/{path:path}", include_in_schema=False)
    async def console(path: str) -> FileResponse:
        return FileResponse(CONSOLE_DIST / "index.html")
