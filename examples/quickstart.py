#!/usr/bin/env python3
"""Quickstart: one invoked run, one refusal, one ambient run.

Shows the harness's three signature behaviours in a few lines:
an agent acting on-behalf-of its invoker, a policy refusal when a
DM-only tool is invoked in a shared channel, and an ambient run
acting under a service identity — each fully audited.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from open_company_harness import Surface, build_harness


def show(label, result):
    print(f"\n== {label}: {result.run_id} -> {result.status} "
          f"({result.total_tokens} tokens, ${result.total_cost_usd:.4f})")
    for s in result.steps:
        who = s.acting_identity or "-"
        extra = f" [{s.detail}]" if s.status != "ok" and s.detail else ""
        print(f"  {s.seq}. {s.action.kind:<14} {s.status:<8} as {who}{extra}")


def main():
    harness = build_harness()

    # Ana asks the engineering agent to review a PR in #eng.
    show("ana invokes reviewer in #eng",
         harness.invoke("reviewer", "review_pr", user="ana",
                        surface=Surface.CHANNEL, channel_id="eng",
                        params={"pr": "PR-101"}))

    # Cy tries to have mail sent from a shared channel: the mail tool
    # is DM-only, so both mail steps are refused — in the open.
    show("cy invokes pipeline in #gtm (mail must refuse)",
         harness.invoke("pipeline", "followup_email", user="cy",
                        surface=Surface.CHANNEL, channel_id="gtm"))

    # The same request in a DM works, acting as Cy.
    show("cy invokes pipeline in a DM",
         harness.invoke("pipeline", "followup_email", user="cy",
                        surface=Surface.DM, channel_id="dm-cy"))

    # An event fires an ambient review: no invoker, service identity.
    harness.scheduler.add_event_trigger(
        event_type="pull_request.opened", agent="reviewer", task="review_pr")
    for result in harness.emit_event("pull_request.opened", {"pr": "PR-102"}):
        show("ambient: PR opened", result)

    stamped, total = harness.audit.stamp_coverage()
    print(f"\naudit: {len(harness.audit.entries)} entries; "
          f"executed-action stamps {stamped}/{total}")


if __name__ == "__main__":
    main()
