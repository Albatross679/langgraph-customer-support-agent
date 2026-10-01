"""Generate v1's explicitly synthetic ground-truth suite, not measured results."""

import json
from pathlib import Path


def cases():
    result = []
    policies = [
        (
            "box-sets.md",
            [
                ("What is the policy for missing box set components?", ["30 days"]),
                ("What is the policy on collector editions restocking?", ["distributor"]),
            ],
        ),
        (
            "damaged-discs.md",
            [
                ("What is the policy for photos of damaged discs?", ["photos", "30 days"]),
                ("What is the policy for disc damage replacements?", ["replacement", "approval"]),
            ],
        ),
        (
            "disc-playback.md",
            [
                ("What is the policy for playback troubleshooting?", ["second compatible player"]),
                ("What is the policy for cleaning fingerprints?", ["microfiber", "center outward"]),
            ],
        ),
        (
            "format-guide.md",
            [
                ("What is the policy guide for DVD quality?", ["standard definition"]),
                ("What is the policy guide for 4K players?", ["compatible 4K"]),
            ],
        ),
        (
            "order-changes.md",
            [
                ("What is the policy on order address changes?", ["before warehouse packing"]),
                ("What is the policy for combining orders?", ["cannot combine"]),
            ],
        ),
        (
            "preorders.md",
            [
                ("What is the policy for preorder charges?", ["when the order is placed"]),
                ("What is the policy for preorder street dates?", ["distributor"]),
            ],
        ),
        (
            "refunds.md",
            [
                ("What is the policy for simulated refunds?", ["human", "approve"]),
                ("What is the policy for refund payments in this demo?", ["never connect"]),
            ],
        ),
        (
            "region-codes.md",
            [
                ("What is the policy guide for Region A?", ["North America"]),
                ("What is the policy guide for 4K region locks?", ["bonus discs"]),
            ],
        ),
        (
            "returns.md",
            [
                ("What is the policy for unopened returns?", ["30 days"]),
                ("What is the policy for opened returns?", ["defective", "damaged"]),
            ],
        ),
        (
            "shipping.md",
            [
                ("What is the policy for economy shipping times?", ["five to eight"]),
                ("What is the policy for shipping tracking?", ["carrier accepts"]),
            ],
        ),
    ]
    for source, questions in policies:
        for message, words in questions:
            result.append(
                {
                    "kind": "policy",
                    "message": message,
                    "source": source,
                    "answer_contains": words,
                    "database": "unchanged",
                }
            )
    sql = [
        ("Count all orders", [{"order_count": 6}]),
        ("Count all customers", [{"customer_count": 3}]),
        ("Count all products", [{"product_count": 5}]),
        ("Count all units ordered", [{"units": 9}]),
        ("Count total order revenue cents", [{"revenue_cents": 27391}]),
        (
            "Count orders by customer",
            [
                {"customer_id": 1, "orders": 2},
                {"customer_id": 2, "orders": 2},
                {"customer_id": 3, "orders": 2},
            ],
        ),
        ("Count product price range", [{"minimum": 1299, "maximum": 7999}]),
        ("Count orders with shipped status", [{"shipped": 1}]),
    ]
    for message, rows in sql:
        result.append(
            {
                "kind": "sql",
                "message": message,
                "expected_rows": rows,
                "source": "business database",
                "database": "unchanged",
            }
        )
    scenarios = [
        ("ORD-3001", "approve", "approved", "approved", None),
        ("ORD-3015", "approve", "none", "amount changed", "amount-changed"),
        ("ORD-3001", "reject", "approved", "already", None),
        ("ORD-3002", "reject", "rejected", "rejected", None),
        ("ORD-3002", "approve", "approved", "approved", None),
        ("ORD-3003", "approve", "approved", "approved", "replay"),
        ("ORD-3004", "reject", "rejected", "rejected", "replay"),
        ("ORD-3005", "approve", "approved", "approved", "conflict"),
        ("ORD-3006", "reject", "rejected", "rejected", "conflict"),
        ("ORD-3007", "approve", "approved", "approved", "redis-loss-paused"),
        ("ORD-3008", "reject", "rejected", "rejected", "redis-loss-paused"),
        ("ORD-3009", "approve", "approved", "approved", "worker-restart-paused"),
        ("ORD-3010", "reject", "rejected", "rejected", "worker-restart-paused"),
        ("ORD-3011", "approve", "approved", "approved", "restart-completed"),
        ("ORD-3012", "approve", "approved", "approved", "crash-after-action"),
        ("ORD-3013", "reject", "rejected", "rejected", "crash-after-action"),
        ("ORD-99991", "approve", None, "not found", None),
        ("ORD-99992", "reject", None, "not found", None),
        (None, "approve", None, "required", None),
        (None, "reject", None, "required", None),
        ("ORD-3014", "approve", "approved", "approved", "multi-turn"),
        ("ORD-3014", "reject", "approved", "already", "multi-turn"),
    ]
    for index, (number, decision, status, word, event) in enumerate(scenarios):
        message = (
            f"Refund my damaged order {number}."
            if number
            else "Refund my damaged disc, I do not know the order number."
        )
        if event == "crash-after-action":
            message += f" [crash-after-action-{index}]"
        result.append(
            {
                "kind": "refund",
                "message": message,
                "order_number": number,
                "create_order": bool(number and number.startswith("ORD-30")),
                "decision": decision,
                "expected_status": status,
                "answer_contains": [word],
                "event": event,
                "expected_actions": 1 if status == "approved" else 0,
            }
        )
    return [dict(case_id=f"conversation-{i:02}", **case) for i, case in enumerate(result, 1)]


if __name__ == "__main__":
    output = {"version": 1, "synthetic": True, "historical_evidence": False, "cases": cases()}
    assert len(output["cases"]) == 50
    Path("tests/evals/conversations-v1.json").write_text(json.dumps(output, indent=2) + "\n")
