"""Real API/worker/PG/Redis reproduction. Only run against the isolated test project."""

import argparse
import json
import time
from pathlib import Path

import httpx
import psycopg
from psycopg.rows import dict_row


def wait(client, run_id):
    for _ in range(120):
        response = client.get(f"/runs/{run_id}")
        response.raise_for_status()
        run = response.json()
        if run["status"] in {"awaiting_approval", "completed", "failed"}:
            return run
        time.sleep(0.25)
    raise TimeoutError(run_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True)
    parser.add_argument("--api", default="http://127.0.0.1:18082")
    parser.add_argument(
        "--database", default="postgresql://postgres:postgres@127.0.0.1:15482/support_copilot"
    )
    args = parser.parse_args()
    if not args.api.startswith("http://127.0.0.1:") or "@127.0.0.1:" not in args.database:
        parser.error("Only loopback isolated endpoints permitted")
    with (
        psycopg.connect(args.database, row_factory=dict_row) as conn,
        httpx.Client(base_url=args.api) as client,
    ):

        def snapshot():
            return conn.execute(
                "SELECT order_number, refund_status FROM orders ORDER BY order_number"
            ).fetchall()

        result = {"before": snapshot(), "runs": [], "decisions": [], "snapshots": []}
        for _ in range(2):
            response = client.post(
                "/runs", json={"message": "My damaged 4K order ORD-1001 needs a refund."}
            )
            response.raise_for_status()
            result["runs"].append(wait(client, response.json()["run_id"]))
        result["before_decisions"] = snapshot()
        for run, decision in zip(result["runs"], ["approve", "reject"], strict=True):
            response = client.post(f"/runs/{run['run_id']}/decision", json={"decision": decision})
            result["decisions"].append({"http_status": response.status_code, "decision": decision})
            time.sleep(1)
            result["decisions"][-1]["run"] = wait(client, run["run_id"])
            result["snapshots"].append(snapshot())
        replay = client.post(
            f"/runs/{result['runs'][0]['run_id']}/decision", json={"decision": "approve"}
        )
        result["replay_status"] = replay.status_code
        result["after"] = snapshot()
    Path(args.output).write_text(json.dumps(result, indent=2, default=str) + "\n")


if __name__ == "__main__":
    main()
