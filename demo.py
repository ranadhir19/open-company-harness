#!/usr/bin/env python3
"""A day at Meridian Labs: the whole harness in one scripted run.

People post in channels; agents are invoked in place and break their
work out into side threads; a DM-only tool refuses in a channel and
works in a DM; publishing pauses for a human approval; an opened PR
fires an ambient review under a service identity; an agent registers
its own schedule and the scheduler fires it as the clock advances.
Every run is written to results/demo_run.json.
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from open_company_harness import Surface, build_harness

OUT = Path(__file__).resolve().parent / "results" / "demo_run.json"


def main():
    harness = build_harness()
    ws = harness.workspace
    runs = []

    # Morning: Ana posts in #eng and asks the reviewer to look at a PR.
    msg = ws.post("eng", "ana",
                  "@reviewer please review PR-101 before standup", tick=1)
    thread = ws.breakout(msg, "reviewer", title="PR-101 review")
    runs.append(harness.invoke(
        "reviewer", "review_pr", user="ana", surface=Surface.THREAD,
        channel_id="eng", thread_id=thread.id, params={"pr": "PR-101"}))

    # Cy asks for a mail follow-up in the open #gtm channel: refused —
    # mail is a DM-only, on-behalf-of tool. In a DM it works, as Cy.
    runs.append(harness.invoke(
        "pipeline", "followup_email", user="cy", surface=Surface.CHANNEL,
        channel_id="gtm"))
    runs.append(harness.invoke(
        "pipeline", "followup_email", user="cy", surface=Surface.DM,
        channel_id="dm-cy"))

    # Publishing a review needs a human: the run pauses, Ana approves.
    paused = harness.invoke(
        "reviewer", "publish_review", user="ana", surface=Surface.CHANNEL,
        channel_id="eng", params={"pr": "PR-101"})
    if paused.status == "awaiting_approval":
        # approve() resumes and returns the same run, now completed.
        runs.append(harness.approve(paused.run_id, approver="ana"))
    else:
        runs.append(paused)

    # Dee (whitelisted) publishes the launch post; the brand voice acts.
    runs.append(harness.invoke(
        "studio", "publish_announcement", user="dee",
        surface=Surface.CHANNEL, channel_id="studio"))

    # Cy asks the pipeline agent to brief them every 25 ticks: the
    # agent registers a schedule owned by Cy, and the clock fires it.
    runs.append(harness.invoke(
        "pipeline", "self_schedule", user="cy", surface=Surface.DM,
        channel_id="dm-cy", params={"task": "account_brief",
                                    "every_ticks": 25}))
    harness.clock.advance(30)
    runs.extend(harness.run_due())

    # A PR is opened: the event trigger fires an ambient review that
    # acts as the codehost service identity, no human involved.
    harness.scheduler.add_event_trigger(
        event_type="pull_request.opened", agent="reviewer",
        task="review_pr")
    runs.extend(harness.emit_event("pull_request.opened", {"pr": "PR-102"}))

    stamped, total = harness.audit.stamp_coverage()
    payload = {
        "runs": [r.to_dict() for r in runs],
        "audit_entries": len(harness.audit.entries),
        "audit_stamp_coverage": f"{stamped}/{total}",
        "blocked_decisions": len(harness.audit.blocked()),
    }
    OUT.write_text(json.dumps(payload, indent=1))
    for r in runs:
        marks = "".join({"ok": ".", "blocked": "x", "error": "!",
                         "skipped": "-"}[s.status] for s in r.steps)
        print(f"{r.run_id} {r.agent:<9} {r.status:<18} steps[{marks}] "
              f"{r.total_tokens} tok ${r.total_cost_usd:.4f}")
    print(f"audit: {payload['audit_entries']} entries, "
          f"stamps {payload['audit_stamp_coverage']}, "
          f"{payload['blocked_decisions']} blocked decisions")
    print(f"wrote {OUT}")


if __name__ == "__main__":
    main()
