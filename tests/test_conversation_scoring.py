import pytest
from scripts.evaluate_conversations import answer_matches_rows


@pytest.mark.parametrize(
    "answer,passed",
    [
        ("There are 6 orders.", True),
        ("There are 16 orders.", False),
        ("There are 10 orders, not 6.", False),
        ("There are six orders.", False),
        ('[{"order_count": 6}]', True),
        ("There are 6.5 orders.", False),
    ],
)
def test_numeric_answer_contract_rejects_incidental_substrings(answer, passed):
    assert answer_matches_rows(answer, [{"order_count": 6}]) is passed


def test_grouped_answer_must_preserve_value_assignment():
    expected = [{"customer_id": 1, "orders": 2}, {"customer_id": 2, "orders": 3}]
    assert answer_matches_rows("Customer 1: 2 orders. Customer 2: 3 orders.", expected)
    assert not answer_matches_rows("Customer 1: 3 orders. Customer 2: 2 orders.", expected)
