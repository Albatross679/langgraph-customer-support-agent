"""Run 50 distinct synthetic conversations through the production API/worker graph.

Offline transport mode is integration evidence, NOT a paid-model evaluation result.
Run only against a fresh isolated Compose project, never the EC2/demo database.
"""

import argparse
import asyncio
import hashlib
import json
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import httpx
import psycopg
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from redis.asyncio import Redis

from support_copilot.config import Settings
from support_copilot.db import StoreRepository
from support_copilot.evaluation_rubric import score_checks
from support_copilot.graph import GraphDependencies, build_graph
from support_copilot.model import OpenRouterClient

DSN = "postgresql://postgres:postgres@127.0.0.1:15482/support_copilot"
API = "http://127.0.0.1:18082"
LIVE = False


def answer_matches_rows(answer, rows):
    # Numeric answer contract: report the gold row values in row/column order, not incidental substrings.
    expected = [str(value) for row in rows for value in row.values()]
    return re.findall(r"(?<![\w.])\d+(?![\w.])", answer or "") == expected


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def snapshot(conn):
    return {
        table: conn.execute(f"SELECT {columns} FROM {table} ORDER BY {sort}").fetchall()
        for table, columns, sort in [
            (
                "orders",
                "order_number, customer_id, product_id, quantity, status, refund_status",
                "order_number",
            ),
            ("refund_actions", "order_number, action, request_id, amount_cents", "order_number"),
            ("refund_decisions", "request_id, order_number, approved, outcome", "request_id"),
        ]
    }


async def poll(client, run_id, target=None):
    for _ in range(900):
        response = await client.get(f"/runs/{run_id}")
        response.raise_for_status()
        run = response.json()
        if run["status"] == "failed":
            raise AssertionError(run)
        if (
            run["status"] == target
            or target is None
            and run["status"] in {"completed", "awaiting_approval"}
        ):
            return run
        await asyncio.sleep(0.1)
    raise TimeoutError(run_id)


def compose(project, *args):
    subprocess.run(
        [
            "docker",
            "compose",
            "-p",
            project,
            "-f",
            "docker-compose.yml",
            "-f",
            "tests/compose.integration.yml",
            *(["-f", "tests/compose.evaluation.yml"] if LIVE else []),
            *args,
        ],
        check=True,
        capture_output=True,
    )


def verify_isolation(project):
    if not project.startswith("support-gap-"):
        raise ValueError("Task-owned support-gap- Compose project required")
    # Confirm loopback PostgreSQL port belongs to this exact task project before any fixture writes.
    inspection = json.loads(
        subprocess.check_output(["docker", "inspect", f"{project}-postgres-1"])
    )[0]
    assert inspection["Config"]["Labels"]["com.docker.compose.project"] == project
    assert {"HostIp": "127.0.0.1", "HostPort": "15482"} in inspection["NetworkSettings"]["Ports"][
        "5432/tcp"
    ]


async def run(args):
    global LIVE
    LIVE = args.mode == "live"
    if LIVE:
        allowance = json.loads(Path(args.allowance_receipt).read_text())
        os.environ["EVALUATION_ALLOWANCE_FILE"] = str(Path(args.allowance_receipt).resolve())
        receipt = (await asyncio.to_thread(httpx.get, "http://127.0.0.1:18084/receipts")).json()
        if (
            receipt.get("transport") != "openrouter-live-budgeted"
            or receipt.get("approval_reference") != allowance["approval_reference"]
        ):
            raise ValueError("Live stack must use the approved bounded evaluation proxy")
    verify_isolation(args.project)
    manifest = json.loads(Path(args.manifest).read_text())
    assert manifest["synthetic"] and len(manifest["cases"]) == 50
    assert len({c["case_id"] for c in manifest["cases"]}) == 50
    settings = Settings(
        database_url=DSN,
        openrouter_api_key="synthetic-not-a-secret",
        openrouter_base_url="http://127.0.0.1:18083",
    )
    report = {
        "version": manifest["version"],
        "mode": args.mode,
        "model_quality_measured": args.mode == "live",
        "historical_evidence": False,
        "started_at": datetime.now(UTC).isoformat(),
        "manifest_sha256": digest(args.manifest),
        "source_revision": (
            await asyncio.to_thread(
                subprocess.check_output, ["git", "rev-parse", "HEAD"], text=True
            )
        ).strip(),
        "source_files": {
            str(p): digest(p)
            for p in sorted(Path("src").rglob("*"))
            if p.is_file() and "__pycache__" not in str(p)
        },
        "cases": [],
    }
    model = OpenRouterClient(settings)
    redis = Redis.from_url("redis://127.0.0.1:16382/0")
    with psycopg.connect(DSN, row_factory=dict_row, autocommit=True) as conn:
        # Fresh evaluator project required, so SQL gold rows refer to exactly the six seed orders.
        assert conn.execute("SELECT count(*) AS n FROM orders").fetchone()["n"] == 6
        assert conn.execute("SELECT count(*) AS n FROM support_runs").fetchone()["n"] == 0
        async with (
            AsyncConnectionPool(DSN, open=False, kwargs={"autocommit": True}) as pool,
            httpx.AsyncClient(base_url=API, timeout=30) as client,
        ):
            graph = build_graph(
                GraphDependencies(model, StoreRepository(pool), redis), AsyncPostgresSaver(pool)
            )
            for case in manifest["cases"]:
                outcome = {
                    "case_id": case["case_id"],
                    "expected": case,
                    "checks": {},
                    "passed": False,
                }
                try:
                    if case.get("create_order"):
                        conn.execute(
                            """INSERT INTO orders (order_number, customer_id, product_id, quantity, ordered_at)
                                        VALUES (%s, 1, 1, 1, now()) ON CONFLICT DO NOTHING""",
                            (case["order_number"],),
                        )
                    before = snapshot(conn)
                    outcome["before"] = before
                    created = await client.post("/runs", json={"message": case["message"]})
                    created.raise_for_status()
                    run_info = created.json()
                    run_id = run_info["run_id"]
                    outcome["run"] = run_info
                    current = await poll(client, run_id)
                    if case["kind"] == "refund":
                        outcome["checks"]["approval_required"] = (
                            current["status"] == "awaiting_approval"
                        )
                        outcome["checks"]["no_write_before_decision"] = snapshot(conn) == before
                        event = case.get("event")
                        if event == "redis-loss-paused":
                            await redis.flushdb()
                            recovered = await client.get(f"/runs/{run_id}")
                            outcome["checks"]["redis_status_recovery"] = (
                                recovered.json()["status"] == "awaiting_approval"
                            )
                        if event == "worker-restart-paused":
                            await asyncio.to_thread(compose, args.project, "restart", "worker")
                        if event == "amount-changed":
                            conn.execute(
                                "UPDATE orders SET quantity=2 WHERE order_number=%s",
                                (case["order_number"],),
                            )
                            outcome["fixture_change"] = (
                                "Quantity changed from 1 to 2 after proposal checkpoint"
                            )
                        if event == "crash-after-action":
                            await redis.set(f"eval:arm-crash:{run_id}", "1")
                        response = await client.post(
                            f"/runs/{run_id}/decision", json={"decision": case["decision"]}
                        )
                        outcome["decision_http_status"] = response.status_code
                        outcome["checks"]["decision_accepted"] = response.status_code == 202
                        if event == "crash-after-action":
                            # Provider holds response generation. Kill after the action transaction committed.
                            for _ in range(300):
                                if conn.execute(
                                    "SELECT 1 FROM refund_decisions WHERE request_id=%s", (run_id,)
                                ).fetchone():
                                    break
                                await asyncio.sleep(0.1)
                            else:
                                raise TimeoutError("action commit")
                            for _ in range(300):
                                if await redis.get(f"eval:committed:{run_id}"):
                                    break
                                await asyncio.sleep(0.1)
                            else:
                                raise TimeoutError("post-commit/pre-checkpoint fault barrier")
                            compose(args.project, "kill", "-s", "SIGKILL", "worker")
                            # Redis loss deliberately removes the dead process lock, queue, and API cache.
                            await redis.flushdb()
                            compose(args.project, "up", "-d", "--no-deps", "worker")
                        current = await poll(client, run_id, "completed")
                        if event in {"replay", "conflict", "restart-completed"}:
                            if event == "restart-completed":
                                compose(args.project, "restart", "worker", "redis", "api")
                                await asyncio.sleep(3)
                            decision = (
                                case["decision"]
                                if event != "conflict"
                                else ("reject" if case["decision"] == "approve" else "approve")
                            )
                            replay = await client.post(
                                f"/runs/{run_id}/decision", json={"decision": decision}
                            )
                            outcome["checks"]["replay_http"] = replay.status_code == (
                                409 if event == "conflict" else 202
                            )
                        after = snapshot(conn)
                        number = case.get("order_number")
                        target = next(
                            (row for row in after["orders"] if row["order_number"] == number), None
                        )
                        outcome["checks"]["order_status"] = (
                            target["refund_status"] if target else None
                        ) == case["expected_status"]
                        outcome["checks"]["order_action_count"] = (
                            sum(row["order_number"] == number for row in after["refund_actions"])
                            == case["expected_actions"]
                        )
                        outcome["checks"]["unrelated_orders_unchanged"] = [
                            r for r in before["orders"] if r["order_number"] != number
                        ] == [r for r in after["orders"] if r["order_number"] != number]
                        outcome["checks"]["one_decision"] = (
                            sum(r["request_id"] == run_id for r in after["refund_decisions"]) == 1
                        )
                        outcome["checks"]["prior_actions_immutable"] = all(
                            row in after["refund_actions"] for row in before["refund_actions"]
                        )
                        outcome["checks"]["prior_decisions_immutable"] = all(
                            row in after["refund_decisions"] for row in before["refund_decisions"]
                        )
                        outcome["checks"]["only_one_new_decision"] = (
                            len(after["refund_decisions"]) == len(before["refund_decisions"]) + 1
                        )
                        expected_new_actions = (
                            1
                            if case["expected_status"] == "approved"
                            and not any(
                                row["order_number"] == number for row in before["refund_actions"]
                            )
                            else 0
                        )
                        outcome["checks"]["exact_new_action_count"] = (
                            len(after["refund_actions"])
                            == len(before["refund_actions"]) + expected_new_actions
                        )
                        if event == "multi-turn":
                            followup = await client.post(
                                "/runs",
                                json={
                                    "message": "What is the policy for simulated refunds?",
                                    "thread_id": run_info["thread_id"],
                                },
                            )
                            followup.raise_for_status()
                            outcome["followup"] = await poll(
                                client, followup.json()["run_id"], "completed"
                            )
                            outcome["checks"]["followup_no_write"] = snapshot(conn) == after
                    else:
                        after = snapshot(conn)
                        outcome["checks"]["database_unchanged"] = after == before
                    state = await graph.aget_state(
                        {"configurable": {"thread_id": run_info["thread_id"]}}
                    )
                    # Multi-turn may have replaced latest values, so collect evidence from original run below.
                    outcome["actual"] = current
                    outcome["after"] = after
                    outcome["evidence"] = {
                        k: state.values.get(k) for k in ["sources", "tool_context"]
                    }
                    if case["kind"] in {"policy", "sql"}:
                        outcome["checks"]["expected_source"] = case["source"] in state.values.get(
                            "sources", []
                        )
                    if case["kind"] == "sql":
                        outcome["checks"]["sql_rows"] = (
                            json.loads(state.values["tool_context"]) == case["expected_rows"]
                        )
                        outcome["checks"]["answer_rows"] = answer_matches_rows(
                            current.get("answer"), case["expected_rows"]
                        )
                    else:
                        answer = (current.get("answer") or "").lower()
                        outcome["checks"]["answer"] = all(
                            word.lower() in answer for word in case["answer_contains"]
                        )
                    outcome["checks"]["completed"] = current["status"] == "completed"
                    if manifest.get("rubric_version", 1) >= 2:
                        outcome["checks"] = score_checks(
                            case, current, outcome["evidence"], outcome["checks"]
                        )
                    outcome["passed"] = all(outcome["checks"].values())
                except Exception as error:
                    outcome["error"] = repr(error)
                    outcome["after"] = snapshot(conn)
                    if outcome.get("run"):
                        try:
                            outcome["actual"] = (
                                await client.get(f"/runs/{outcome['run']['run_id']}")
                            ).json()
                        except httpx.HTTPError:
                            pass
                report["cases"].append(outcome)
                print(case["case_id"], "PASS" if outcome["passed"] else "FAIL", flush=True)
                Path(args.output).write_text(json.dumps(report, indent=2, default=str) + "\n")
                if LIVE:
                    budget = (await client.get("http://127.0.0.1:18084/receipts")).json()
                    if budget.get("budget_stop_reason"):
                        report["budget_stop_reason"] = budget["budget_stop_reason"]
                        break
    await model.close()
    await redis.aclose()
    report["completed_at"] = datetime.now(UTC).isoformat()
    report["measured_pass_count"] = sum(c["passed"] for c in report["cases"])
    report["measured_case_count"] = len(report["cases"])
    report["planned_case_count"] = len(manifest["cases"])
    report["completed_conversation_count"] = sum(
        case.get("actual", {}).get("status") == "completed" for case in report["cases"]
    )
    report["not_started_case_ids"] = [
        case["case_id"] for case in manifest["cases"][len(report["cases"]) :]
    ]
    report["unfinished_case_ids"] = [
        case["case_id"]
        for case in report["cases"]
        if case.get("actual", {}).get("status") != "completed"
    ] + report["not_started_case_ids"]
    async with httpx.AsyncClient() as receipt_client:
        report["transport_receipts"] = (
            await receipt_client.get(
                "http://127.0.0.1:18084/receipts" if LIVE else "http://127.0.0.1:18083/receipts"
            )
        ).json()
    Path(args.output).write_text(json.dumps(report, indent=2, default=str) + "\n")
    print(
        f"{report['measured_pass_count']}/{report['measured_case_count']} {args.mode} cases passed"
    )
    return report["measured_pass_count"] == 50


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="tests/evals/conversations-v1.json")
    parser.add_argument("--project", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--mode", choices=["offline", "live"], default="offline")
    parser.add_argument(
        "--allowance-receipt", help="Nonsecret firstmate spending approval receipt for live mode"
    )
    args = parser.parse_args()
    if args.mode == "live" and not args.allowance_receipt:
        parser.error("Firstmate spending allowance receipt is required for live mode")
    raise SystemExit(0 if asyncio.run(run(args)) else 1)


if __name__ == "__main__":
    main()
