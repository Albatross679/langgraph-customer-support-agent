import json
import os
import uuid
from datetime import UTC, datetime

import httpx
import psycopg
import pytest

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.getenv("RUN_ISOLATED_INTEGRATION") != "1", reason="Isolated Compose only"
    ),
]


def test_real_api_projects_old_action_answer_without_rewriting_evidence():
    run_id = str(uuid.uuid4())
    original = "Your refund is under review. We will update you."
    outcome = "No simulated refund was applied: order amount changed; start a new review."
    payload = {
        "run_id": run_id,
        "thread_id": str(uuid.uuid4()),
        "status": "completed",
        "created_at": datetime.now(UTC).isoformat(),
        "route": {"lane": "billing", "handler": "refund", "rationale": "Synthetic legacy result"},
        "answer": original,
    }
    with psycopg.connect(
        "postgresql://postgres:postgres@127.0.0.1:15482/support_copilot", autocommit=True
    ) as conn:
        conn.execute(
            "INSERT INTO support_runs(run_id,payload,decision) VALUES (%s,%s::jsonb,%s)",
            (run_id, json.dumps(payload), "approve"),
        )
        conn.execute(
            "INSERT INTO refund_decisions(request_id,order_number,approved,outcome) VALUES (%s,%s,true,%s)",
            (run_id, "synthetic-legacy", outcome),
        )
        with httpx.Client(base_url="http://127.0.0.1:18082") as client:
            assert client.get(f"/runs/{run_id}").json()["answer"] == outcome
            listed = client.get("/runs?limit=100").json()["runs"]
            assert next(run for run in listed if run["run_id"] == run_id)["answer"] == outcome
            replay = client.post(f"/runs/{run_id}/decision", json={"decision": "approve"})
            assert replay.status_code == 202 and replay.json()["answer"] == outcome
        assert (
            conn.execute(
                "SELECT payload->>'answer' FROM support_runs WHERE run_id=%s", (run_id,)
            ).fetchone()[0]
            == original
        )
