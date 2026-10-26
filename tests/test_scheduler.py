"""Ambient scheduler: intervals, daily schedules, events, spend caps."""

import pytest

from open_company_harness import Surface, build_harness


@pytest.fixture()
def harness():
    return build_harness()


def test_interval_schedule_fires_at_due_ticks_and_not_before(harness):
    harness.scheduler.add_schedule(agent="pipeline", task="account_brief",
                                   every_ticks=10)
    first = harness.run_due()
    assert len(first) == 1 and first[0].status == "completed"
    assert harness.run_due() == []          # next fire is tick 10
    harness.clock.advance(9)
    assert harness.run_due() == []          # tick 9: still not due
    harness.clock.advance(1)
    second = harness.run_due()
    assert len(second) == 1 and second[0].status == "completed"
    assert harness.run_due() == []


def test_daily_at_fires_once_per_day(harness):
    harness.scheduler.add_schedule(agent="pipeline", task="daily_digest",
                                   daily_at=20)
    assert harness.run_due() == []          # tick 0, before 20
    harness.clock.advance(19)
    assert harness.run_due() == []          # tick 19
    harness.clock.advance(1)                # tick 20
    fired = harness.run_due()
    assert len(fired) == 1 and fired[0].status == "completed"
    assert harness.run_due() == []          # not twice in one day
    harness.clock.advance(99)               # tick 119, next day not yet at 20
    assert harness.run_due() == []
    harness.clock.advance(1)                # tick 120: day 2 at tick 20
    assert len(harness.run_due()) == 1


def test_event_trigger_fires_only_on_matching_event(harness):
    harness.scheduler.add_event_trigger(
        event_type="pull_request.opened", agent="reviewer", task="review_pr")
    assert harness.emit_event("pull_request.closed", {"pr": "PR-102"}) == []
    assert harness.emit_event("account.renewed", {}) == []
    fired = harness.emit_event("pull_request.opened", {"pr": "PR-102"})
    assert len(fired) == 1
    assert fired[0].status == "completed"
    tool_steps = [s for s in fired[0].steps if s.action.kind == "tool_call"]
    assert tool_steps
    assert all(s.acting_identity == "service:codehost-bot"
               for s in tool_steps)


def test_agent_registered_schedule_records_owner(harness):
    result = harness.invoke("pipeline", "self_schedule", user="ana",
                            surface=Surface.CHANNEL, channel_id="gtm",
                            params={"task": "account_brief",
                                    "every_ticks": 25})
    assert result.status == "completed"
    sched = harness.scheduler.schedules[-1]
    assert sched.owner == "ana"
    assert sched.registered_by == "pipeline"
    assert sched.agent == "pipeline"
    assert sched.task == "account_brief"
    assert sched.every_ticks == 25


def test_daily_spend_cap_blocks_later_same_day_fires(harness):
    sched = harness.scheduler.add_schedule(
        agent="pipeline", task="account_brief", every_ticks=5)
    sched.max_cost_per_day_usd = 0.001
    first = harness.run_due()
    assert len(first) == 1
    assert first[0].total_cost_usd > sched.max_cost_per_day_usd
    harness.clock.advance(5)                # due again, same simulated day
    assert harness.run_due() == []
    blocks = [e for e in harness.audit.entries
              if e.decision == "block" and e.target == sched.id]
    assert blocks and "spend cap" in blocks[-1].detail
