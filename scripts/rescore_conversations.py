"""Rescore immutable v1 outputs with rubric v2, without calling a model or changing originals."""

import argparse
import gzip
import hashlib
import json
from pathlib import Path

from support_copilot.evaluation_rubric import VERSION, score_checks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    source, target = Path(args.input), Path(args.output)
    if source.resolve() == target.resolve():
        parser.error("Original evidence is immutable; use a separate output")
    raw = source.read_bytes()
    content = gzip.decompress(raw) if source.suffix == ".gz" else raw
    original = json.loads(content)
    results = []
    for case in original["cases"]:
        result = {"case_id": case["case_id"], "original_passed": case["passed"]}
        if case["case_id"] == "conversation-27" and original["version"] == 1:
            result.update(
                passed=None,
                classification="ambiguous frozen question",
                reason="Count product price range does not require min/max rather than bucket counts; clarification belongs to manifest v2 only.",
            )
        else:
            result["checks"] = score_checks(
                case["expected"], case.get("actual", {}), case.get("evidence", {}), case["checks"]
            )
            result["passed"] = not case.get("error") and all(result["checks"].values())
            result["classification"] = (
                "confirmed evaluator false negative"
                if result["passed"] and not case["passed"]
                else "genuine application answer defect"
                if case["case_id"] == "conversation-30"
                else "unchanged"
            )
        results.append(result)
    output = {
        "rubric_version": VERSION,
        "source_file_sha256": hashlib.sha256(raw).hexdigest(),
        "source_content_sha256": hashlib.sha256(content).hexdigest(),
        "fresh_model_run": False,
        "original_pass_count": original["measured_pass_count"],
        "original_case_count": original["measured_case_count"],
        "rescored_pass_count": sum(case["passed"] is True for case in results),
        "scorable_case_count": sum(case["passed"] is not None for case in results),
        "ambiguous_case_count": sum(case["passed"] is None for case in results),
        "cases": results,
    }
    target.write_text(json.dumps(output, indent=2) + "\n")
    print(
        output["rescored_pass_count"],
        "/",
        output["scorable_case_count"],
        "rescored; ambiguous:",
        output["ambiguous_case_count"],
    )


if __name__ == "__main__":
    main()
