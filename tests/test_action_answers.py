import pytest

from support_copilot.graph import GraphDependencies, build_nodes
from support_copilot.run_store import project_action_answer
from tests.conftest import FakeCache, FakeRepository


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        "The simulated refund was approved.",
        "The simulated refund was rejected.",
        "The order already has an approved simulated refund. No additional action was applied.",
        "No simulated refund was applied: order amount changed; start a new review.",
        "No simulated refund was applied: order not found; an order number is required.",
    ],
)
async def test_committed_action_outcomes_cannot_be_rewritten_by_a_model(outcome):
    class MisleadingModel:
        async def generate(self, system, user):
            raise AssertionError("A model must not rewrite a terminal action into under review")

    respond = build_nodes(GraphDependencies(MisleadingModel(), FakeRepository(), FakeCache()))[
        "respond"
    ]
    result = await respond({"handler": "refund", "tool_context": outcome})
    assert result["answer"] == outcome
    assert result["conversation_history"] == [{"role": "assistant", "content": outcome}]


def test_projection_corrects_legacy_prose_without_mutating_saved_evidence():
    saved = {
        "status": "completed",
        "route": {"handler": "refund"},
        "answer": "Under review; we will update you.",
    }
    outcome = "No simulated refund was applied: order amount changed; start a new review."
    projected = project_action_answer(saved, outcome)
    assert projected["answer"] == outcome
    assert saved["answer"] == "Under review; we will update you."
    assert (
        project_action_answer({**saved, "route": {"handler": "rag"}}, outcome)["answer"]
        == saved["answer"]
    )
