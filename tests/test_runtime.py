"""Runtime end-to-end: traces, approvals, budgets, provider recovery."""

import pytest

from open_company_harness import (Action, AgentSpec, ScriptedProvider,
                                  Surface, TASK_PLANS, build_harness)


@pytest.fixture()
def harness():
    return build_harness()


def test_review_pr_completes_with_expected_steps_and_identities(harness):
    result = harness.invoke("reviewer", "review_pr", user="ana",
                            surface=Surface.CHANNEL, channel_id="eng",
                            params={"pr": "PR-101"})
    assert result.status == "completed"
    assert [s.action.kind for s in result.steps] == [
        "memory_recall", "tool_call", "tool_call", "respond"]
    assert [s.status for s in result.steps] == ["ok", "ok", "ok", "ok"]
    assert [s.acting_identity for s in result.steps] == [
        "user:ana", "user:ana", "user:ana", None]
    tool_ops = [s.action.operation for s in result.steps
                if s.action.kind == "tool_call"]
    assert tool_ops == ["read_pr", "draft_comment"]


def test_followup_email_in_channel_blocked_but_run_completes(harness):
    result = harness.invoke("pipeline", "followup_email", user="cy",
                            surface=Surface.CHANNEL, channel_id="gtm")
    assert result.status == "completed"
    assert [s.status for s in result.steps] == ["blocked", "blocked", "ok"]
    assert all("not available on surface" in s.detail
               for s in result.steps[:2])
    assert harness.services["mail"].outbox == []
    assert harness.services["mail"].drafts == []


def test_publish_review_pauses_then_approval_completes(harness):
    result = harness.invoke("reviewer", "publish_review", user="ana",
                            surface=Surface.CHANNEL, channel_id="eng",
                            params={"pr": "PR-101"})
    assert result.status == "awaiting_approval"
    assert result.pending_approval["target"] == "codehost.publish_comment"
    backend = harness.services["codehost"]
    assert backend.prs["PR-101"]["comments"] == []

    finished = harness.approve(result.run_id, "ana")
    assert finished.status == "completed"
    assert [s.status for s in finished.steps] == ["ok", "ok", "ok"]
    assert backend.prs["PR-101"]["comments"] == [
        {"by": "user:ana", "text": "Review published: no blocking issues."}]
    publish_step = finished.steps[1]
    assert publish_step.action.operation == "publish_comment"
    assert publish_step.acting_identity == "user:ana"


def test_budget_governor_blocks_tiny_budget_agent(harness):
    harness.registry.register_agent(AgentSpec(
        name="tiny", role="budget probe", instructions="Probe the governor.",
        tools=("codehost",), max_tokens_per_run=10))
    result = harness.invoke("tiny", "review_pr", user="ana",
                            surface=Surface.CHANNEL, channel_id="eng")
    assert result.status == "budget_exceeded"
    assert len(result.steps) == 1
    assert result.steps[0].status == "skipped"
    assert "budget governor" in result.steps[0].detail


def test_flaky_provider_recovers_with_one_retry(harness):
    harness.provider = ScriptedProvider(
        "stub-pro", task_plans=TASK_PLANS, flaky={"review_pr": 1})
    result = harness.invoke("reviewer", "review_pr", user="ana",
                            surface=Surface.CHANNEL, channel_id="eng")
    assert result.status == "completed"
    assert all(s.status == "ok" for s in result.steps)


def test_unknown_task_fails(harness):
    result = harness.invoke("reviewer", "no_such_task", user="ana",
                            surface=Surface.CHANNEL, channel_id="eng")
    assert result.status == "failed"
    assert result.steps == []
    assert "planning failed" in result.output


def test_tool_not_declared_for_agent_is_blocked_step(harness):
    harness.provider.task_plans = {
        **harness.provider.task_plans,
        "use_crm": lambda p: [
            Action(kind="tool_call", tool="crm", operation="lookup_account",
                   payload={"account": "Acme Corp"}),
            Action(kind="respond", text="done"),
        ],
    }
    result = harness.invoke("reviewer", "use_crm", user="ana",
                            surface=Surface.CHANNEL, channel_id="eng")
    assert result.status == "completed"
    assert result.steps[0].status == "blocked"
    assert "not declared for agent reviewer" in result.steps[0].detail
    assert result.steps[1].status == "ok"


def test_audit_stamp_coverage_after_mixed_workload(harness):
    harness.invoke("reviewer", "review_pr", user="ana",
                   surface=Surface.CHANNEL, channel_id="eng")
    harness.invoke("pipeline", "followup_email", user="cy",
                   surface=Surface.DM, channel_id="dm-cy")
    harness.invoke("pipeline", "followup_email", user="cy",
                   surface=Surface.CHANNEL, channel_id="gtm")
    paused = harness.invoke("reviewer", "publish_review", user="ben",
                            surface=Surface.CHANNEL, channel_id="eng",
                            params={"pr": "PR-102"})
    harness.approve(paused.run_id, "ben")
    harness.scheduler.add_event_trigger(
        event_type="pull_request.opened", agent="reviewer", task="review_pr")
    harness.emit_event("pull_request.opened", {"pr": "PR-102"})
    harness.invoke("studio", "exec_digest", user="ana",
                   surface=Surface.CHANNEL, channel_id="studio")
    stamped, total = harness.audit.stamp_coverage()
    assert total > 0
    assert stamped == total


def test_stub_basic_plan_omits_memory_recall(harness):
    basic = build_harness(tier="stub-basic")
    result = basic.invoke("reviewer", "review_pr", user="ana",
                          surface=Surface.CHANNEL, channel_id="eng")
    assert result.status == "completed"
    kinds = [s.action.kind for s in result.steps]
    assert "memory_recall" not in kinds
    assert kinds == ["tool_call", "tool_call", "respond"]
    # Contrast: the pro tier on the same task does recall memory.
    pro_kinds = [s.action.kind for s in harness.invoke(
        "reviewer", "review_pr", user="ana", surface=Surface.CHANNEL,
        channel_id="eng").steps]
    assert "memory_recall" in pro_kinds
