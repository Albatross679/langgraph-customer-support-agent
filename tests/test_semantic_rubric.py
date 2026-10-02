import json
from pathlib import Path

import pytest

from support_copilot.evaluation_rubric import fact_answer_match, sql_answer_match, sql_rows_match

CASES = {
    case["case_id"]: case
    for case in json.loads(Path("tests/evals/conversations-v2.json").read_text())["cases"]
}


@pytest.mark.parametrize(
    "actual,passed",
    [
        ([{"total_orders": 6}], True),
        ([{"count": 16}], False),
        ([{"count": 6}, {"count": 6}], False),
        ([{"count": 6, "extra": 7}], False),
    ],
)
def test_row_aliases_do_not_change_values_or_cardinality(actual, passed):
    assert sql_rows_match(CASES["conversation-21"], actual) is passed


@pytest.mark.parametrize(
    "answer,passed",
    [
        ("The total units ordered are 9.", True),
        ("9.0 units.", True),
        ("19 units.", False),
        ("9.5 units.", False),
        ("−9 units.", False),
        ("9 orders.", False),
        ("About 9 units.", False),
    ],
)
def test_exact_quantity_unit_and_terminal_punctuation(answer, passed):
    assert sql_answer_match(CASES["conversation-24"], answer) is passed


@pytest.mark.parametrize(
    "answer,passed",
    [
        ("The total revenue is 27,391 cents.", True),
        ("Revenue is 27391 cents.", True),
        ("Revenue is $273.91.", False),
        ("Revenue is 27,390 cents.", False),
    ],
)
def test_cents_and_thousands_are_not_currency_conversion(answer, passed):
    assert sql_answer_match(CASES["conversation-25"], answer) is passed


def test_grouped_rows_bind_identity_not_row_order():
    case = CASES["conversation-26"]
    assert sql_rows_match(
        case,
        [
            {"customer_id": 3, "order_count": 2},
            {"customer_id": 1, "n": 2},
            {"customer_id": 2, "orders": 2},
        ],
    )
    assert not sql_rows_match(
        case,
        [
            {"customer_id": 3, "order_count": 2},
            {"customer_id": 1, "n": 3},
            {"customer_id": 2, "orders": 2},
        ],
    )
    assert sql_answer_match(
        case, "Customer 3: 2 orders. Customer 1: 2 orders. Customer 2: 2 orders."
    )
    assert not sql_answer_match(
        case, "Customer 3: 1 orders. Customer 1: 3 orders. Customer 2: 2 orders."
    )


@pytest.mark.parametrize(
    "answer,passed",
    [
        ("The minimum is 1,299 cents and maximum is 7,999 cents.", True),
        ("The minimum is 1299.5 cents and maximum is 7999 cents.", False),
        ("The minimum is -1299 cents and maximum is 7999 cents.", False),
        ("The minimum is 7999 cents and maximum is 1299 cents.", False),
    ],
)
def test_price_bounds_remain_bound_to_their_labels(answer, passed):
    assert sql_answer_match(CASES["conversation-27"], answer) is passed


@pytest.mark.parametrize(
    "index,answer,passed",
    [
        (9, "Address changes are only possible before the order is packed.", True),
        (9, "You can change an address after packing begins.", False),
        (9, "Address changes cannot be made before packing.", False),
        (11, "Preorder charges are applied when you place the order.", True),
        (11, "Preorders are not charged when you place the order.", False),
        (14, "Refunds are simulated and do not connect to a real payment provider.", True),
        (14, "Refunds are simulated but connect to a real payment provider.", False),
        (45, "I couldn't find the order. Please double-check it.", True),
        (45, "The order was found and the refund was issued.", False),
        (47, "I'm unable to process a refund without the order number. Please provide it.", True),
        (47, "You do not need an order number.", False),
        (30, "No simulated refund was applied: order amount changed; start a new review.", True),
        (30, "Your refund is under review. We will update you.", False),
    ],
)
def test_equivalent_facts_do_not_reward_opposite_meaning(index, answer, passed):
    assert fact_answer_match(CASES[f"conversation-{index:02}"], answer) is passed
