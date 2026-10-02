"""Version 2 deterministic scoring of facts, quantities, units and bound SQL rows.

Aliases and equivalent wording are presentation, not different database answers.
This is not a model judge; incorrect quantities, units, identities or action facts fail.
"""

import json
import re
from decimal import Decimal, InvalidOperation

VERSION = 2
CUSTOMERS = {"maya chen": 1, "sam rivera": 2, "lee patel": 3}
UNITS = {
    21: ("order",),
    22: ("customer",),
    23: ("product",),
    24: ("unit", "cop", "item"),
    25: ("cent",),
    26: ("order",),
    27: ("cent",),
    28: ("order",),
}


def prose(text):
    return " ".join(
        (text or "").lower().replace("’", "'").replace("−", "-").replace("_", " ").split()
    )


def numeric(value):
    if isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value).replace(",", ""))
        return number if number.is_finite() else None
    except InvalidOperation:
        return None


def numbers(text):
    return [
        numeric(value)
        for value in re.findall(
            r"(?<![\w.])[+-]?\d+(?:,\d{3})*(?:\.\d+)?(?!\w)", (text or "").replace("−", "-")
        )
    ]


def sql_rows_match(case, actual):
    expected = case["expected_rows"]
    if (
        not isinstance(actual, list)
        or len(actual) != len(expected)
        or not all(isinstance(row, dict) for row in actual)
    ):
        return False
    index = int(case["case_id"].split("-")[-1])
    keys = " ".join(key.lower() for row in actual for key in row)
    if index in {25, 27} and re.search(r"dollar|\busd\b|euro", keys.replace("_", " ")):
        return False
    if index == 24 and re.search(r"\borders\b|dollar|\bcustomers\b", keys.replace("_", " ")):
        return False
    if index == 26:
        bound = []
        for row in actual:
            if len(row) != 2:
                return False
            identity_keys = [
                key for key in row if "customer" in key.lower() or key.lower() in {"id", "name"}
            ]
            if len(identity_keys) != 1:
                return False
            identity = row[identity_keys[0]]
            identity = CUSTOMERS.get(str(identity).lower(), numeric(identity))
            count = numeric(next(value for key, value in row.items() if key != identity_keys[0]))
            if identity is None or count is None:
                return False
            bound.append((identity, count))
        return sorted(bound) == sorted(
            (numeric(row["customer_id"]), numeric(row["orders"])) for row in expected
        )
    if index == 27:
        row = actual[0]
        if len(row) != 2:
            return False
        low = [value for key, value in row.items() if re.search(r"min|lowest", key.lower())]
        high = [value for key, value in row.items() if re.search(r"max|highest", key.lower())]
        return (
            len(low) == len(high) == 1
            and numeric(low[0]) == numeric(expected[0]["minimum"])
            and numeric(high[0]) == numeric(expected[0]["maximum"])
        )
    return len(actual[0]) == 1 and numeric(next(iter(actual[0].values()))) == numeric(
        next(iter(expected[0].values()))
    )


def sql_answer_match(case, answer):
    try:
        rows = json.loads(answer)
    except (ValueError, TypeError):
        rows = None
    if isinstance(rows, list):
        return sql_rows_match(case, rows)
    index = int(case["case_id"].split("-")[-1])
    text = prose(answer)
    if not any(unit in text for unit in UNITS[index]):
        return False
    if re.search(r"\babout\b|\broughly\b|\bapproximately\b", text):
        return False
    if index == 26:
        pairs = re.findall(r"customer\s+#?(\d+)\s*(?::|has|placed|ordered)\s*(\d+)\s+orders?", text)
        numeric_identities = bool(pairs)
        if not pairs:
            for name, identity in CUSTOMERS.items():
                match = re.search(
                    re.escape(name) + r"\s*(?::|has|placed|ordered)\s*(\d+)\s+orders?", text
                )
                if match:
                    pairs.append((str(identity), match.group(1)))
        expected = [(str(row["customer_id"]), str(row["orders"])) for row in case["expected_rows"]]
        return sorted(pairs) == sorted(expected) and len(numbers(answer)) == (
            6 if numeric_identities else 3
        )
    if index == 27:
        normalized = re.sub(r"(?<=\d),(?=\d{3}\b)", "", text)
        low = re.search(r"(?:minimum|lowest|min)\b.{0,40}?([+-]?\d+(?:\.\d+)?)\b", normalized)
        high = re.search(r"(?:maximum|highest|max)\b.{0,40}?([+-]?\d+(?:\.\d+)?)\b", normalized)
        return bool(
            low
            and high
            and numeric(low.group(1)) == case["expected_rows"][0]["minimum"]
            and numeric(high.group(1)) == case["expected_rows"][0]["maximum"]
            and len(numbers(answer)) == 2
        )
    if index == 28 and "shipped" not in text:
        return False
    return numbers(answer) == [
        numeric(value) for row in case["expected_rows"] for value in row.values()
    ]


def fact_answer_match(case, answer):
    text = prose(answer)
    index = int(case["case_id"].split("-")[-1])
    if index == 9:
        before = re.search(r"before.{0,50}(?:packing|packed)", text)
        forbidden = re.search(
            r"\bcan\b.{0,35}\bafter\b.{0,25}(?:packing|packed)|(?:cannot|can\'t|not possible|not allowed).{0,35}\bbefore\b",
            text,
        )
        return bool(
            before and ("only" in text or "cannot" in text or "can't" in text) and not forbidden
        )
    if index == 11:
        correct = re.search(
            r"(?:charg\w*|bill\w*|payment).{0,40}(?:when|at)\b.{0,35}(?:plac\w*|checkout)", text
        )
        forbidden = re.search(
            r"(?:not|never|n\'t)\b.{0,12}(?:charg|bill)|(?:charg\w*|bill\w*).{0,10}\bnot\b", text
        )
        return bool(correct and not forbidden)
    if index == 14:
        return "simulated" in text and bool(
            re.search(
                r"(?:never|do not|does not|not)\b.{0,20}connect.{0,50}(?:payment|provider)", text
            )
        )
    if index in {45, 46}:
        return (
            "order" in text
            and bool(
                re.search(
                    r"not found|could(?:n\'t| not) find|could not locate|unable to find", text
                )
            )
            and not re.search(r"\brefund\b.{0,15}(?:issued|approved|processed)", text)
        )
    if index in {47, 48}:
        return (
            "order number" in text
            and bool(re.search(r"provide|need|required|unable|cannot|can\'t", text))
            and not re.search(
                r"(?:do not|don\'t|not)\s+(?:need|require)|order number.{0,15}not required|\b(?:can|(?<!not )able to)\s+process.{0,25}without",
                text,
            )
        )
    if index == 30:
        return bool(
            re.search(r"amount.{0,15}changed", text)
            and re.search(r"(?:new|another|restart).{0,15}(?:review|request)", text)
            and not re.search(r"under review|will update|review is pending", text)
        )
    return all(prose(word) in text for word in case["answer_contains"])


def score_checks(case, actual, evidence, original_checks):
    """Retain every original database/safety check, replacing only presentation scoring."""
    checks = dict(original_checks)
    if case["kind"] == "sql":
        try:
            rows = json.loads(evidence.get("tool_context") or "")
        except ValueError:
            rows = None
        checks["sql_rows"] = sql_rows_match(case, rows)
        checks["answer_rows"] = sql_answer_match(case, actual.get("answer"))
    else:
        checks["answer"] = fact_answer_match(case, actual.get("answer"))
    return checks
