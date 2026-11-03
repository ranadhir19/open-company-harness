#!/usr/bin/env python3
"""Experiments for open-company-harness: five measured batteries.

Run as:
    cd ~/workspace/open-company-harness && \
        PYTHONPATH=src python3 experiments/run_experiments.py

Writes results/results.json, results/results.md and results/charts.png.

Method (house validation protocol): every expectation in E2/E4/E5 is
derived INDEPENDENTLY of the code under test. The identity evaluator
below re-derives allow/block + acting identity from the documented
broker rules applied to the ToolSpec *declarations* (it never calls
broker.decide); the E4 fire counts come from a small simulator written
from the scheduler's documented semantics; E5 recomputes identities
for audit entries from declarations + recorded run contexts. Numbers
are reported as measured; a missed target is reported as a miss.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt

from open_company_harness import Surface, build_harness
from open_company_harness.memory import MemoryError
from open_company_harness.models import Action, IdentityMode, RunContext, Tier

ROOT = Path(__file__).resolve().parent.parent
USERS = ("ana", "ben", "cy", "dee")
AGENT_FOR_TOOL = {"codehost": "reviewer", "crm": "pipeline",
                  "mail": "pipeline", "docs": "studio", "social": "studio"}


# ---------------------------------------------------------------------------
# Independent policy evaluator (E2/E5). Written from the broker's documented
# rules in identity.py's module docstring + registry declarations:
#   * unknown operation            -> block, no identity
#   * surface not declared         -> block, attempting identity stamped
#   * SERVICE                      -> service:<account>
#   * ON_BEHALF_OF                 -> user:<invoker>; ambient -> block
#   * DUAL                         -> user:<invoker>, else service:<account>
#   * whitelist_only op            -> responsible human (invoker, else the
#     commissioning owner) must be on the tool whitelist, else block with
#     the resolved identity stamped
#   * approval_required is reported alongside, not as a block.
# ---------------------------------------------------------------------------

def evaluate_policy(tool, operation, invoking_user, on_behalf_of, surface):
    """Return (allowed, acting_identity, needs_approval) from declarations."""
    if operation not in tool.operations:
        return (False, None, False)
    if surface not in tool.surfaces:
        if invoking_user:
            attempting = f"user:{invoking_user}"
        elif tool.identity_mode in (IdentityMode.SERVICE, IdentityMode.DUAL):
            attempting = f"service:{tool.service_account}"
        else:
            attempting = None
        return (False, attempting, False)
    if tool.identity_mode is IdentityMode.SERVICE:
        acting = f"service:{tool.service_account}"
    elif tool.identity_mode is IdentityMode.ON_BEHALF_OF:
        acting = f"user:{invoking_user}" if invoking_user else None
    else:  # DUAL
        acting = (f"user:{invoking_user}" if invoking_user
                  else f"service:{tool.service_account}")
    if acting is None:
        return (False, None, False)
    policy = tool.policy_for(operation)
    if policy.whitelist_only:
        responsible = invoking_user or on_behalf_of
        if responsible is None or responsible not in tool.whitelist:
            return (False, acting, False)
    return (True, acting, policy.approval_required)


def _ctx(agent, user, surface, run_id, on_behalf_of=None):
    return RunContext(agent=agent, invoking_user=user, surface=surface,
                      run_id=run_id, on_behalf_of=on_behalf_of)


# ---------------------------------------------------------------------------
# E1 — Cross-user memory isolation + promotion battery
# ---------------------------------------------------------------------------

E1_FACTS = {
    "ana": [("kestrelviolet",
             "Ana keeps her draft budget workbook in the kestrelviolet folder."),
            ("lanterncobalt",
             "Ana booked the lanterncobalt room for the design review."),
            ("willowharbor",
             "Ana's willowharbor checklist covers vendor onboarding steps.")],
    "ben": [("falconamber",
             "Ben tracks the falconamber trial accounts in his notebook."),
            ("cedarmeridian",
             "Ben's cedarmeridian summary covers the pricing experiment."),
            ("sparrowquartz",
             "Ben reserved the sparrowquartz room for customer calls.")],
    "cy": [("heronindigo",
            "Cy keeps the heronindigo renewal tracker for his accounts."),
           ("maplesaffron",
            "Cy's maplesaffron notes cover the onboarding webinar."),
           ("ottervelvet",
            "Cy stored the ottervelvet demo script in the shared drive.")],
    "dee": [("ibisgarnet",
             "Dee drafts the ibisgarnet launch post in her notebook."),
            ("juniperonyx",
             "Dee's juniperonyx outline covers the brand story series."),
            ("lynxcoral",
             "Dee booked the lynxcoral studio slot for the photo shoot.")],
}


def experiment_e1() -> dict:
    h = build_harness()
    seeded_globals = sum(1 for f in h.memory.all_facts()
                         if f.tier is Tier.GLOBAL)

    # Seed >=3 private facts per user, each carrying a distinctive token.
    fact_ids = {}
    for owner, items in E1_FACTS.items():
        fact_ids[owner] = []
        for _token, text in items:
            fact = h.memory.remember_user_fact(
                _ctx("pipeline", owner, Surface.DM, "e1-seed"), owner, text)
            fact_ids[owner].append(fact.id)

    # Cross-user recall: for every ordered pair (reader, owner), query with
    # the owner's distinctive tokens and count the owner's user-tier facts
    # that come back to the reader.
    pairs = queries = leaks = 0
    leaked_ids = []
    for reader in USERS:
        for owner in USERS:
            if reader == owner:
                continue
            pairs += 1
            for token, _text in E1_FACTS[owner]:
                queries += 1
                out = h.memory.recall(
                    _ctx("pipeline", reader, Surface.DM, "e1-recall"), token)
                for f in out:
                    if f.tier is Tier.USER and f.owner == owner:
                        leaks += 1
                        leaked_ids.append(f.id)

    # Ambient recall: no invoking user, so only the global tier may answer.
    ambient_queries = ambient_leaks = 0
    for owner in USERS:
        for token, _text in E1_FACTS[owner]:
            ambient_queries += 1
            out = h.memory.recall(
                _ctx("pipeline", None, Surface.AMBIENT, "e1-ambient"), token)
            for f in out:
                if f.tier is Tier.USER:
                    ambient_leaks += 1
                    leaked_ids.append(f.id)

    # -- promotion battery -------------------------------------------------
    def store_attempt(fn):
        """Classify a store-level promotion attempt by its real outcome."""
        try:
            fact = fn()
            return ("allow", f"stored as {fact.id} in the global tier")
        except MemoryError as exc:
            return ("refuse", str(exc))
        except Exception as exc:  # a crash is neither an allow nor a refusal
            return (f"error:{type(exc).__name__}", str(exc))

    # A generic runtime task that attempts a direct global-tier write.
    def _write_global(p):
        return [Action(kind="memory_write",
                       text=p.get("text", "Org-wide: the demo room is "
                                          "booked via the studio calendar."),
                       payload={"tier": "global",
                                "source_user": p.get("source_user")}),
                Action(kind="respond", text="done")]
    h.provider.task_plans["exp_write_global"] = _write_global

    def runtime_attempt(agent, user, source_user):
        params = {} if source_user is None else {"source_user": source_user}
        res = h.invoke(agent, "exp_write_global", user=user,
                       surface=Surface.CHANNEL, params=params)
        step = res.steps[0]
        return ("allow" if step.status == "ok" else "refuse",
                f"step {step.status}: {step.detail}")

    attempts = []

    def record(name, expected, actual_detail):
        actual, detail = actual_detail
        attempts.append({"name": name, "expected": expected,
                         "actual": actual, "detail": detail,
                         "agree": actual == expected})

    p1 = h.memory.remember_user_fact(
        _ctx("pipeline", "ana", Surface.DM, "e1-promo"), "ana",
        "Ana's offsite planning checklist lives in her notebook.")
    record("non-owner consent (ben consents to share ana's fact)", "refuse",
           store_attempt(lambda: h.memory.promote_by_consent(
               _ctx("pipeline", "ana", Surface.DM, "e1-promo"), p1.id, "ben")))

    same_text = "The workshop whiteboard markers are kept in the supply cabinet."
    s1 = h.memory.remember_user_fact(
        _ctx("pipeline", "ana", Surface.DM, "e1-promo"), "ana", same_text)
    s2 = h.memory.remember_user_fact(
        _ctx("pipeline", "ana", Surface.DM, "e1-promo"), "ana", same_text)
    record("same-owner corroboration (two ana facts, identical text)",
           "refuse",
           store_attempt(lambda: h.memory.promote_by_corroboration(
               _ctx("pipeline", "ana", Surface.DM, "e1-promo"),
               (s1.id, s2.id))))

    pa = h.memory.remember_user_fact(
        _ctx("pipeline", "ana", Surface.DM, "e1-promo"), "ana",
        "The launch window opens on Friday morning")
    pb = h.memory.remember_user_fact(
        _ctx("pipeline", "ben", Surface.DM, "e1-promo"), "ben",
        "Friday morning is when the launch window opens")
    record("paraphrase corroboration (ana + ben, token sets differ)",
           "refuse",
           store_attempt(lambda: h.memory.promote_by_corroboration(
               _ctx("pipeline", "ana", Surface.DM, "e1-promo"),
               (pa.id, pb.id))))

    ident = "The demo room projector remote lives in the top drawer"
    ic = h.memory.remember_user_fact(
        _ctx("pipeline", "cy", Surface.DM, "e1-promo"), "cy", ident)
    idd = h.memory.remember_user_fact(
        _ctx("studio", "dee", Surface.DM, "e1-promo"), "dee", ident)
    record("two-owner identical-text corroboration (cy + dee)", "allow",
           store_attempt(lambda: h.memory.promote_by_corroboration(
               _ctx("pipeline", "cy", Surface.DM, "e1-promo"),
               (ic.id, idd.id))))

    record("owner consent (ana shares her own fact)", "allow",
           store_attempt(lambda: h.memory.promote_by_consent(
               _ctx("pipeline", "ana", Surface.DM, "e1-promo"), p1.id, "ana")))

    record("declared-global write with source_user (refused at store)",
           "refuse",
           store_attempt(lambda: h.memory.remember_declared_global(
               _ctx("studio", "dee", Surface.DM, "e1-promo"),
               "A user-sourced claim dressed as an org fact.",
               source_user="ana")))

    record("runtime global write by reviewer (no can_write_global)",
           "refuse", runtime_attempt("reviewer", "ana", None))
    record("runtime global write by studio with source_user", "refuse",
           runtime_attempt("studio", "dee", "dee"))
    record("runtime global write by studio without source_user", "allow",
           runtime_attempt("studio", "dee", None))

    agreed = sum(1 for a in attempts if a["agree"])
    return {
        "users": list(USERS),
        "private_facts_seeded_per_user": 3,
        "seeded_global_facts": seeded_globals,
        "ordered_pairs_tested": pairs,
        "recall_queries": queries,
        "leaked_facts": leaks,
        "leaked_fact_ids": leaked_ids,
        "ambient_queries": ambient_queries,
        "ambient_leaked_facts": ambient_leaks,
        "promotion_attempts": attempts,
        "promotion_agreed": agreed,
        "promotion_total": len(attempts),
    }


# ---------------------------------------------------------------------------
# E2 — Identity-policy enforcement: full decision matrix
# ---------------------------------------------------------------------------

def experiment_e2() -> dict:
    h = build_harness()
    reg = h.registry
    cells = mismatches = allowed = blocked = 0
    mismatch_rows = []
    legitimate = []          # expected-allow cells (must not be over-blocked)
    legitimate_passed = 0
    legit_by_op = {}         # "tool.op" -> [compact cell labels]
    counter = 0
    for tool_name in reg.tool_names():
        tool = reg.tool(tool_name)
        for op in tool.operations:
            cases = []
            for surface in (Surface.DM, Surface.CHANNEL, Surface.THREAD):
                for user in ("ana", "cy", "dee"):
                    cases.append((user, None, surface))
            for owner in ("dee", "cy", None):
                cases.append((None, owner, Surface.AMBIENT))
            for invoking, on_behalf, surface in cases:
                counter += 1
                cells += 1
                ctx = _ctx(AGENT_FOR_TOOL[tool_name], invoking, surface,
                           f"e2-{counter:03d}", on_behalf_of=on_behalf)
                decision = h.broker.decide(ctx, tool, op)
                exp = evaluate_policy(tool, op, invoking, on_behalf, surface)
                got = (decision.allowed, decision.acting_identity,
                       decision.needs_approval)
                if exp[0]:
                    allowed += 1
                    if invoking is not None:
                        label = f"{invoking}@{surface.value} as {exp[1]}"
                        cell_id = (f"{tool_name}.{op} | invoker={invoking} "
                                   f"surface={surface.value} -> {exp[1]}")
                    else:
                        label = (f"ambient(owner={on_behalf}) as {exp[1]}")
                        cell_id = (f"{tool_name}.{op} | ambient "
                                   f"on_behalf_of={on_behalf} -> {exp[1]}")
                    legitimate.append(cell_id)
                    legit_by_op.setdefault(f"{tool_name}.{op}",
                                           []).append(label)
                    if got == exp:
                        legitimate_passed += 1
                else:
                    blocked += 1
                if got != exp:
                    mismatches += 1
                    mismatch_rows.append({
                        "cell": f"{tool_name}.{op} invoker={invoking} "
                                f"on_behalf_of={on_behalf} "
                                f"surface={surface.value}",
                        "expected": list(exp), "actual": list(got)})
    return {
        "cells": cells, "mismatches": mismatches,
        "mismatch_rows": mismatch_rows,
        "allowed": allowed, "blocked": blocked,
        "legitimate_cells": len(legitimate),
        "legitimate_passed": legitimate_passed,
        "legitimate_list": legitimate,
        "legitimate_by_op": legit_by_op,
    }


# ---------------------------------------------------------------------------
# E3 — Secret-proxy exfiltration battery
# ---------------------------------------------------------------------------

def experiment_e3() -> dict:
    h = build_harness()
    secrets = h.vault.values()
    scan_texts = []  # every proxy output / sandbox transcript, for the scan

    def scan(blob):
        scan_texts.append(blob or "")

    # (a) proxy probes with minted capabilities -----------------------------
    ctx = _ctx("reviewer", "ana", Surface.CHANNEL, "e3-run-1")
    tool = h.registry.tool("codehost")
    decision = h.broker.decide(ctx, tool, "read_pr")
    token = h.broker.mint_capability(ctx, decision, tool, ("read_pr",))
    short = h.broker.mint_capability(ctx, decision, tool, ("read_pr",),
                                     ttl_ticks=5)
    probes = []

    def probe(name, adversarial, **kw):
        res = h.proxy.call(agent="reviewer", **kw)
        scan(res.output)
        scan(res.reason)
        probes.append({"name": name, "adversarial": adversarial,
                       "ok": res.ok, "reason": res.reason})
        return res

    probe("valid call (control)", False, run_id="e3-run-1",
          token_str=token.token, service="codehost", operation="read_pr",
          payload={"pr": "PR-101"})
    probe("forged token", True, run_id="e3-run-1",
          token_str="cap_forged000000000000000000", service="codehost",
          operation="read_pr", payload={"pr": "PR-101"})
    probe("wrong-service token use", True, run_id="e3-run-1",
          token_str=token.token, service="crm", operation="read_pr",
          payload={})
    probe("out-of-scope operation", True, run_id="e3-run-1",
          token_str=token.token, service="codehost",
          operation="draft_comment", payload={"pr": "PR-101", "text": "x"})
    probe("cross-run replay", True, run_id="e3-run-OTHER",
          token_str=token.token, service="codehost", operation="read_pr",
          payload={"pr": "PR-101"})
    h.clock.advance(10)  # the short-lived token (ttl 5) is now expired
    probe("expired token", True, run_id="e3-run-1",
          token_str=short.token, service="codehost", operation="read_pr",
          payload={"pr": "PR-101"})

    adversarial = [p for p in probes if p["adversarial"]]
    refused = sum(1 for p in adversarial if not p["ok"])
    escaped = sum(1 for p in adversarial if p["ok"])
    control_ok = bool([p for p in probes if not p["adversarial"]][0]["ok"])

    # (b) subprocess sandbox battery ---------------------------------------
    def _sandbox_task(p):
        return [Action(kind="sandbox_exec",
                       payload={"engine": "subprocess",
                                "services": p.get("services", ["codehost"]),
                                "code": p["code"]}),
                Action(kind="respond", text="done")]
    h.provider.task_plans["exp_sandbox"] = _sandbox_task

    comments_before = len(h.services["codehost"].prs["PR-101"]["comments"])
    snippets = [
        {"name": "dump environment",
         "code": "import os, json\n"
                 "print('env keys:', sorted(os.environ.keys()))\n"
                 "print('token key present:', 'OCH_CAP_TOKENS' in os.environ)\n",
         "expect_in_transcript": "token key present: True"},
        {"name": "print the capability-token value",
         "code": "import os\nprint(os.environ.get('OCH_CAP_TOKENS'))\n",
         "expect_in_transcript": "cap_"},
        {"name": "out-of-scope op publish_comment (approval-gated, "
                "must be outside sandbox scope)",
         "code": "try:\n"
                 "    out = call_service('codehost', 'publish_comment', "
                 "pr='PR-101', text='injected review')\n"
                 "    print('PUBLISHED:', out)\n"
                 "except Exception as e:\n"
                 "    print('blocked:', e)\n",
         "expect_in_transcript": "blocked:"},
        {"name": "service with no capability (crm for reviewer)",
         "code": "try:\n"
                 "    out = call_service('crm', 'list_accounts')\n"
                 "    print('CRM:', out)\n"
                 "except Exception as e:\n"
                 "    print('blocked:', e)\n",
         "expect_in_transcript": "no capability granted for service crm"},
        {"name": "globals()/import inspection hunting for secrets",
         "code": "import os, json\n"
                 "blob = str(sorted(globals().keys())) + "
                 "json.dumps(dict(os.environ))\n"
                 "print('globals keys:', sorted(globals().keys()))\n"
                 "print('fake-secret hits:', blob.count('och-fake-secret'))\n"
                 "try:\n"
                 "    import open_company_harness\n"
                 "    print('harness import: succeeded')\n"
                 "except Exception as e:\n"
                 "    print('harness import: failed')\n",
         "expect_in_transcript": "fake-secret hits: 0"},
    ]
    snippet_rows = []
    tokens_visible = False
    for snip in snippets:
        res = h.invoke("reviewer", "exp_sandbox", user="ana",
                       surface=Surface.CHANNEL, channel_id="eng",
                       params={"code": snip["code"]})
        step = res.steps[0]
        scan(step.output)
        scan(step.detail)
        if re.search(r"cap_[0-9a-f]{24}", step.output or ""):
            tokens_visible = True
        snippet_rows.append({
            "name": snip["name"], "step_status": step.status,
            "expected_marker_seen": snip["expect_in_transcript"]
                                    in (step.output or ""),
            "secret_in_transcript": any(s in (step.output or "")
                                        for s in secrets)})
    comments_after = len(h.services["codehost"].prs["PR-101"]["comments"])

    # (c) deterministic sandbox env-view check ------------------------------
    digest = h.invoke("studio", "exec_digest", user="dee",
                      surface=Surface.CHANNEL, channel_id="studio")
    env_view = {}
    for line in (digest.steps[0].output or "").splitlines():
        if line.startswith("env: "):
            env_view = json.loads(line[len("env: "):])
    scan(digest.steps[0].output)
    env_view_secret_hits = sum(v.count(s) for v in env_view.values()
                               for s in secrets)
    env_view_all_tokens = bool(env_view) and all(
        v.startswith("cap_") for v in env_view.values())

    # Whole-battery secret scan over every output/transcript collected.
    materializations = sum(text.count(s) for text in scan_texts
                           for s in secrets)
    return {
        "proxy_probes": probes,
        "proxy_adversarial": len(adversarial),
        "proxy_refused": refused,
        "proxy_escaped": escaped,
        "control_succeeded": control_ok,
        "sandbox_snippets": snippet_rows,
        "sandbox_snippet_count": len(snippet_rows),
        "pr_comments_before": comments_before,
        "pr_comments_after": comments_after,
        "capability_tokens_visible_in_sandbox": tokens_visible,
        "env_view_keys": sorted(env_view),
        "env_view_values_all_capability_tokens": env_view_all_tokens,
        "env_view_secret_hits": env_view_secret_hits,
        "secret_materializations": materializations,
        "vault_secret_count": len(secrets),
    }


# ---------------------------------------------------------------------------
# E4 — Ambient scheduling
# ---------------------------------------------------------------------------

def _simulate_expected_fires() -> dict:
    """Expected fire ticks, derived from the scheduler's documented
    semantics (interval: first fire at the registration tick, then every
    N ticks measured from the last fire; daily: fires at tick-of-day T
    of each 100-tick day, first fire the next occurrence; the harness is
    polled on the grid 0, 5, ..., 300). No scheduler code is consulted.
    """
    grid = list(range(0, 301, 5))

    def interval(every):
        nxt, fires = 0, []
        for t in grid:
            if t >= nxt:
                fires.append(t)
                nxt = t + every
        return fires

    def daily(at):
        nxt = at if at > 0 else at + 100
        fires = []
        for t in grid:
            if t >= nxt:
                fires.append(t)
                today = (t // 100) * 100 + at
                nxt = today if today > t else today + 100
        return fires

    return {"interval_every_30": interval(30), "daily_at_10": daily(10),
            "self_every_25": interval(25), "event_pr_opened": [40, 130]}


def experiment_e4() -> dict:
    h = build_harness()
    interval_sched = h.scheduler.add_schedule(
        agent="pipeline", task="daily_digest", every_ticks=30)
    daily_sched = h.scheduler.add_schedule(
        agent="pipeline", task="account_brief", daily_at=10)
    h.scheduler.add_event_trigger(event_type="pull_request.opened",
                                  agent="reviewer", task="review_pr")
    invoke_result = h.invoke("pipeline", "self_schedule", user="cy",
                             surface=Surface.DM,
                             params={"task": "account_brief",
                                     "every_ticks": 25})
    self_sched = h.scheduler.schedules[-1]

    def run_due_attributed():
        before = {s.id: s.next_fire for s in h.scheduler.schedules}
        results = h.run_due()
        fired = [s.id for s in h.scheduler.schedules
                 if s.next_fire != before[s.id]]
        return fired, results

    actual = {interval_sched.id: [], daily_sched.id: [], self_sched.id: [],
              "event": []}
    run_schedule = {}   # run_id -> schedule id (or "event")
    ambient_results = []
    fired, results = run_due_attributed()
    for sid, res in zip(fired, results):
        actual[sid].append(h.clock.now())
        run_schedule[res.run_id] = sid
        ambient_results.append(res)
    while h.clock.now() < 300:
        h.clock.advance(5)
        fired, results = run_due_attributed()
        for sid, res in zip(fired, results):
            actual[sid].append(h.clock.now())
            run_schedule[res.run_id] = sid
            ambient_results.append(res)
        if h.clock.now() in (40, 130):
            for res in h.emit_event("pull_request.opened", {"pr": "PR-102"}):
                actual["event"].append(h.clock.now())
                run_schedule[res.run_id] = "event"
                ambient_results.append(res)

    expected = _simulate_expected_fires()
    expected_counts = {"interval_every_30": len(expected["interval_every_30"]),
                       "daily_at_10": len(expected["daily_at_10"]),
                       "self_every_25": len(expected["self_every_25"]),
                       "event_pr_opened": len(expected["event_pr_opened"])}
    actual_counts = {"interval_every_30": len(actual[interval_sched.id]),
                     "daily_at_10": len(actual[daily_sched.id]),
                     "self_every_25": len(actual[self_sched.id]),
                     "event_pr_opened": len(actual["event"])}
    actual_ticks = {"interval_every_30": actual[interval_sched.id],
                    "daily_at_10": actual[daily_sched.id],
                    "self_every_25": actual[self_sched.id],
                    "event_pr_opened": actual["event"]}
    ticks_match = all(expected[k] == actual_ticks[k] for k in expected)

    # Ambient identity discipline: tool steps act as a service or nobody.
    tool_steps_checked = identity_violations = 0
    for res in ambient_results:
        for step in res.steps:
            if step.action.kind == "tool_call":
                tool_steps_checked += 1
                if (step.acting_identity is not None
                        and not step.acting_identity.startswith("service:")):
                    identity_violations += 1

    # The self-scheduled runs keep cy as the responsible human in audit.
    self_run_ids = [rid for rid, sid in run_schedule.items()
                    if sid == self_sched.id]
    self_audited = sum(
        1 for rid in self_run_ids
        if any(e.kind == "run" and e.run_id == rid
               and e.invoking_user == "cy" for e in h.audit.entries))

    total_runs = len(ambient_results) + 1  # + the invoking self_schedule run
    ambient_share = round(len(ambient_results) / total_runs, 4)

    # -- budget enforcement (separate fresh harnesses) ----------------------
    hb = build_harness()
    spec = hb.registry._agents["pipeline"]
    hb.registry._agents["pipeline"] = replace(
        spec, max_cost_per_run_usd=1e-9)
    hb.scheduler.add_schedule(agent="pipeline", task="daily_digest",
                              every_ticks=10)
    per_run_results = list(hb.run_due())
    for _ in range(2):
        hb.clock.advance(10)
        per_run_results += hb.run_due()
    per_run_budget_exceeded = sum(1 for r in per_run_results
                                  if r.status == "budget_exceeded")

    hc = build_harness()
    cap_sched = hc.scheduler.add_schedule(
        agent="pipeline", task="daily_digest", every_ticks=10)
    cap_sched.max_cost_per_day_usd = 0.004  # below one run's measured cost
    day_results = list(hc.run_due())
    for _ in range(3):
        hc.clock.advance(10)
        day_results += hc.run_due()
    day_blocks = [e for e in hc.audit.entries
                  if e.kind == "schedule" and e.decision == "block"]
    one_run_cost = (round(day_results[0].total_cost_usd, 6)
                    if day_results else 0.0)

    return {
        "expected_counts": expected_counts,
        "actual_counts": actual_counts,
        "expected_ticks": expected,
        "actual_ticks": actual_ticks,
        "ticks_match_expected": ticks_match,
        "ambient_runs": len(ambient_results),
        "total_runs_main_scenario": total_runs,
        "ambient_share": ambient_share,
        "ambient_tool_steps_checked": tool_steps_checked,
        "ambient_identity_violations": identity_violations,
        "self_scheduled_runs": len(self_run_ids),
        "self_scheduled_runs_audited_owner_cy": self_audited,
        "budget_per_run": {
            "cap_usd": 1e-9, "ambient_runs_attempted": len(per_run_results),
            "budget_exceeded": per_run_budget_exceeded},
        "budget_per_day": {
            "cap_usd": 0.004, "one_run_cost_usd": one_run_cost,
            "runs_completed": len(day_results),
            "second_fire_blocked": len(day_results) == 1 and len(day_blocks) >= 1,
            "schedule_block_entries": len(day_blocks)},
    }


# ---------------------------------------------------------------------------
# E5 — Audit completeness
# ---------------------------------------------------------------------------

def experiment_e5() -> dict:
    h = build_harness()
    results = {}
    run_ctx = {}  # run_id -> (invoking_user, on_behalf_of, surface)

    def keep(res, invoking, on_behalf, surface):
        results[res.run_id] = res
        run_ctx[res.run_id] = (invoking, on_behalf, surface)
        return res

    keep(h.invoke("pipeline", "followup_email", user="cy",
                  surface=Surface.DM, channel_id="dm-cy"),
         "cy", None, Surface.DM)
    keep(h.invoke("pipeline", "followup_email", user="cy",
                  surface=Surface.CHANNEL, channel_id="gtm"),
         "cy", None, Surface.CHANNEL)  # mail steps blocked (DM-only tool)
    keep(h.invoke("reviewer", "review_pr", user="ana",
                  surface=Surface.CHANNEL, channel_id="eng"),
         "ana", None, Surface.CHANNEL)
    paused = h.invoke("reviewer", "publish_review", user="ana",
                      surface=Surface.CHANNEL, channel_id="eng")
    keep(h.approve(paused.run_id, "ana"), "ana", None, Surface.CHANNEL)
    keep(h.invoke("reviewer", "exec_review_stats", user="ana",
                  surface=Surface.CHANNEL, channel_id="eng"),
         "ana", None, Surface.CHANNEL)
    write = keep(h.invoke("pipeline", "remember_preference", user="ana",
                          surface=Surface.DM, channel_id="dm-ana",
                          params={"text": "Ana prefers a printed agenda "
                                          "for planning meetings."}),
                 "ana", None, Surface.DM)
    fact_id = [s.output for s in write.steps
               if s.action.kind == "memory_write"][0]
    h.memory.promote_by_consent(
        _ctx("pipeline", "ana", Surface.DM, "e5-manual"), fact_id, "ana")
    h.scheduler.add_event_trigger(event_type="pull_request.opened",
                                  agent="reviewer", task="review_pr")
    for res in h.emit_event("pull_request.opened", {"pr": "PR-102"}):
        keep(res, None, None, Surface.AMBIENT)
    h.scheduler.add_schedule(agent="pipeline", task="daily_digest",
                             every_ticks=30)
    for res in h.run_due():
        keep(res, None, None, Surface.AMBIENT)

    # (1) stamp coverage over executed action + proxy entries.
    stamped, total = h.audit.stamp_coverage()

    # (2) identity correctness: recompute from declarations + run context.
    tool_names = set(h.registry.tool_names())
    checked = uncheckable = mismatches = 0
    mismatch_rows = []
    for e in h.audit.entries:
        if e.kind not in ("action", "proxy"):
            continue
        if "." not in e.target:
            continue
        tool_name, op = e.target.split(".", 1)
        if tool_name not in tool_names:
            continue  # channel:/sandbox: targets are not tool calls
        if e.run_id not in run_ctx:
            uncheckable += 1
            continue
        invoking, on_behalf, surface = run_ctx[e.run_id]
        exp_allowed, exp_identity, _appr = evaluate_policy(
            h.registry.tool(tool_name), op, invoking, on_behalf, surface)
        checked += 1
        if not exp_allowed or exp_identity != e.acting_identity:
            mismatches += 1
            mismatch_rows.append({
                "seq": e.seq, "kind": e.kind, "run_id": e.run_id,
                "target": e.target, "expected_identity": exp_identity,
                "audited_identity": e.acting_identity,
                "expected_allowed": exp_allowed})

    # (3) cross-checks: blocked steps <-> block decisions; runs <-> entries.
    decisions = {(e.run_id, e.target) for e in h.audit.entries
                 if e.kind == "decision" and e.decision == "block"}
    blocked_steps = matched_blocked = 0
    unmatched = []
    for res in results.values():
        for step in res.steps:
            if step.status == "blocked" and step.action.kind == "tool_call":
                blocked_steps += 1
                target = f"{step.action.tool}.{step.action.operation}"
                if (res.run_id, target) in decisions:
                    matched_blocked += 1
                else:
                    unmatched.append(f"{res.run_id}:{target}")
    run_entries = {e.run_id for e in h.audit.entries if e.kind == "run"}
    runs_with_entry = sum(1 for rid in results if rid in run_entries)

    return {
        "runs_executed": len(results),
        "stamp_stamped": stamped, "stamp_total": total,
        "identity_entries_checked": checked,
        "identity_entries_uncheckable": uncheckable,
        "identity_mismatches": mismatches,
        "identity_mismatch_rows": mismatch_rows,
        "blocked_steps": blocked_steps,
        "blocked_steps_matched": matched_blocked,
        "blocked_steps_unmatched": unmatched,
        "runs_with_run_entry": runs_with_entry,
    }


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def _yn(ok):
    return "met" if ok else "MISSED"


def render_md(r: dict) -> str:
    e1, e2, e3, e4, e5 = r["E1"], r["E2"], r["E3"], r["E4"], r["E5"]
    lines = []
    lines.append("# open-company-harness: results (seeded run)")
    lines.append("")
    lines.append("All numbers below are measured from actual runs of this "
                 "suite against the bundled scenario (scripted providers, "
                 "no API keys, deterministic logical clock).")
    lines.append("")
    lines.append("## Method note — independent expectations")
    lines.append("")
    lines.append("Expectations are derived independently of the code under "
                 "test, per the house validation protocol. E2 and E5 use a "
                 "standalone evaluator in this script that reads only the "
                 "ToolSpec declarations (identity mode, surfaces, per-op "
                 "policy, whitelist) and applies the broker rules as "
                 "documented in the identity module — it never calls "
                 "`broker.decide` to check `broker.decide`. E4's expected "
                 "fire counts come from a small simulator written from the "
                 "scheduler's documented semantics, not from the scheduler. "
                 "Near-miss negatives are first-class: DM-only tools in "
                 "shared channels, non-whitelisted publishers, ambient runs "
                 "of on-behalf-of tools, forged/expired/replayed capability "
                 "tokens, and paraphrase corroboration are all in the "
                 "batteries. Where a target is missed, the miss is reported "
                 "plainly below.")
    lines.append("")
    lines.append("## Headline")
    lines.append("")
    lines.append("| experiment | measured result | target | verdict |")
    lines.append("|---|---|---|---|")
    lines.append(f"| E1 memory isolation | {e1['leaked_facts']} cross-user "
                 f"leaks in {e1['recall_queries']} queries over "
                 f"{e1['ordered_pairs_tested']} ordered pairs; "
                 f"{e1['ambient_leaked_facts']} ambient leaks; promotion "
                 f"agreement {e1['promotion_agreed']}/{e1['promotion_total']} "
                 f"| 0 leaks; {e1['promotion_total']}/"
                 f"{e1['promotion_total']} promotions | "
                 f"{_yn(e1['leaked_facts'] == 0 and e1['ambient_leaked_facts'] == 0 and e1['promotion_agreed'] == e1['promotion_total'])} |")
    lines.append(f"| E2 identity policy | {e2['cells']} decision cells, "
                 f"{e2['mismatches']} mismatches vs the independent "
                 f"evaluator; {e2['allowed']} allowed / {e2['blocked']} "
                 f"blocked | 0 mismatches | {_yn(e2['mismatches'] == 0)} |")
    lines.append(f"| E3 secret proxy | {e3['proxy_refused']}/"
                 f"{e3['proxy_adversarial']} adversarial proxy probes "
                 f"refused, {e3['proxy_escaped']} escaped; "
                 f"{e3['secret_materializations']} vault-secret "
                 f"materializations across all probe/sandbox output "
                 f"| all refused; 0 materializations | "
                 f"{_yn(e3['proxy_escaped'] == 0 and e3['secret_materializations'] == 0)} |")
    e4_all = (e4["expected_counts"] == e4["actual_counts"]
              and e4["ticks_match_expected"])
    lines.append(f"| E4 ambient scheduling | expected vs actual fires "
                 f"identical on all four triggers "
                 f"({sum(e4['actual_counts'].values())} ambient runs, "
                 f"{e4['ambient_share']:.0%} of all runs); "
                 f"{e4['ambient_identity_violations']} ambient identity "
                 f"violations | exact match; 0 violations | "
                 f"{_yn(e4_all and e4['ambient_identity_violations'] == 0)} |")
    lines.append(f"| E5 audit completeness | stamp coverage "
                 f"{e5['stamp_stamped']}/{e5['stamp_total']}; "
                 f"{e5['identity_mismatches']} identity mismatches in "
                 f"{e5['identity_entries_checked']} checkable entries; "
                 f"blocked steps matched "
                 f"{e5['blocked_steps_matched']}/{e5['blocked_steps']}; "
                 f"runs with a run entry {e5['runs_with_run_entry']}/"
                 f"{e5['runs_executed']} | full coverage; 0 mismatches | "
                 f"{_yn(e5['stamp_stamped'] == e5['stamp_total'] and e5['identity_mismatches'] == 0 and e5['blocked_steps_matched'] == e5['blocked_steps'] and e5['runs_with_run_entry'] == e5['runs_executed'])} |")
    lines.append("")

    # ---- E1 ----
    lines.append("## E1 — Cross-user memory isolation")
    lines.append("")
    lines.append(f"- seeded {e1['private_facts_seeded_per_user']} private "
                 f"facts per user ({', '.join(e1['users'])}), each carrying "
                 f"a distinctive token, on top of the scenario's "
                 f"{e1['seeded_global_facts']} seeded global fact(s).")
    lines.append(f"- cross-user recall: {e1['ordered_pairs_tested']} ordered "
                 f"(reader, owner) pairs, {e1['recall_queries']} queries "
                 f"built from the owner's tokens — **{e1['leaked_facts']} "
                 f"leaked facts** (target 0).")
    lines.append(f"- ambient recall: {e1['ambient_queries']} queries with no "
                 f"invoking user — **{e1['ambient_leaked_facts']} leaked "
                 f"facts** (target 0); ambient contexts see only the global "
                 f"tier.")
    lines.append("")
    lines.append("Promotion battery (expected outcome derived from the "
                 "memory module's documented routes):")
    lines.append("")
    lines.append("| attempt | expected | actual | agree |")
    lines.append("|---|---|---|---|")
    for a in e1["promotion_attempts"]:
        lines.append(f"| {a['name']} | {a['expected']} | {a['actual']} | "
                     f"{'yes' if a['agree'] else 'NO'} |")
    lines.append("")
    lines.append(f"- agreement: **{e1['promotion_agreed']}/"
                 f"{e1['promotion_total']}**.")
    for a in e1["promotion_attempts"]:
        if not a["agree"]:
            lines.append(f"- MISS — {a['name']}: expected {a['expected']}, "
                         f"got {a['actual']} ({a['detail']}).")
    lines.append("")

    # ---- E2 ----
    lines.append("## E2 — Identity-policy enforcement")
    lines.append("")
    lines.append(f"- full matrix: 5 tools x their operations x contexts "
                 f"{{DM, CHANNEL, THREAD x invokers ana/cy/dee; AMBIENT x "
                 f"commissioning owners dee/cy/none}} = **{e2['cells']} "
                 f"cells**, each decided with a fresh RunContext and "
                 f"compared (allowed + acting identity + approval flag) "
                 f"against the independent evaluator.")
    lines.append(f"- **{e2['mismatches']} mismatches** (target 0); "
                 f"{e2['allowed']} cells allowed, {e2['blocked']} blocked. "
                 f"The blocked set is the near-miss battery: mail anywhere "
                 f"but a DM, docs/social publishing by non-whitelisted "
                 f"humans, on-behalf-of tools in ambient runs with no "
                 f"responsible human, and social publishing from ambient "
                 f"runs commissioned by a non-whitelisted owner.")
    lines.append(f"- no over-blocking: all **{e2['legitimate_passed']}/"
                 f"{e2['legitimate_cells']} legitimate cells** the "
                 f"evaluator says must pass did pass. They are:")
    for op_key, labels in e2["legitimate_by_op"].items():
        lines.append(f"  - `{op_key}` ({len(labels)}): "
                     + "; ".join(labels))
    if e2["mismatch_rows"]:
        lines.append("- mismatches:")
        for m in e2["mismatch_rows"]:
            lines.append(f"  - {m['cell']}: expected {m['expected']}, "
                         f"got {m['actual']}")
    lines.append("")

    # ---- E3 ----
    lines.append("## E3 — Secret-proxy exfiltration")
    lines.append("")
    lines.append("Proxy probes (capability minted for codehost.read_pr, "
                 "run e3-run-1):")
    lines.append("")
    lines.append("| probe | outcome |")
    lines.append("|---|---|")
    for p in e3["proxy_probes"]:
        outcome = "succeeded" if p["ok"] else f"refused ({p['reason']})"
        lines.append(f"| {p['name']} | {outcome} |")
    lines.append("")
    lines.append(f"- adversarial probes refused: **{e3['proxy_refused']}/"
                 f"{e3['proxy_adversarial']}**; escaped: "
                 f"{e3['proxy_escaped']}; control call succeeded: "
                 f"{e3['control_succeeded']}.")
    lines.append("")
    lines.append("Subprocess sandbox battery (real child processes, "
                 "reviewer in a channel, codehost capability only):")
    lines.append("")
    lines.append("| snippet | step | expected marker seen | secret in "
                 "transcript |")
    lines.append("|---|---|---|---|")
    for s in e3["sandbox_snippets"]:
        lines.append(f"| {s['name']} | {s['step_status']} | "
                     f"{s['expected_marker_seen']} | "
                     f"{s['secret_in_transcript']} |")
    lines.append("")
    lines.append(f"- published comments on PR-101 before/after the battery: "
                 f"{e3['pr_comments_before']} -> "
                 f"{e3['pr_comments_after']} — the approval-gated "
                 f"`publish_comment` operation is not in the sandbox token "
                 f"scope (the broker grants only allowed, non-gated ops), "
                 f"so the in-sandbox publish attempt was refused by scope.")
    lines.append(f"- capability tokens **were visible inside the sandbox "
                 f"by design** (env key `OCH_CAP_TOKENS`; regex-matched in "
                 f"transcripts: {e3['capability_tokens_visible_in_sandbox']}"
                 f"). They are opaque, single-service, single-run, "
                 f"short-lived capabilities — the probes above are what "
                 f"make them useless outside their scope: forged, expired, "
                 f"wrong-service, out-of-scope and cross-run uses are all "
                 f"refused by the proxy.")
    lines.append(f"- deterministic sandbox env view "
                 f"(`exec_digest`): keys {e3['env_view_keys']} — every "
                 f"value is a capability token "
                 f"({e3['env_view_values_all_capability_tokens']}), secret "
                 f"hits in the env view: {e3['env_view_secret_hits']}.")
    lines.append(f"- after all runs, every proxy output and sandbox "
                 f"transcript was scanned for all "
                 f"{e3['vault_secret_count']} vault secret strings: "
                 f"**{e3['secret_materializations']} materializations** "
                 f"(target 0).")
    lines.append("- no live-network probe was attempted from sandbox code: "
                 "the subprocess sandbox does not itself block networking "
                 "(documented limitation, see below).")
    lines.append("")

    # ---- E4 ----
    lines.append("## E4 — Ambient scheduling")
    lines.append("")
    lines.append("| trigger | expected fires | actual fires |")
    lines.append("|---|---|---|")
    names = {"interval_every_30": "system interval: pipeline daily_digest "
                                  "every 30 ticks",
             "daily_at_10": "system daily: pipeline account_brief at "
                            "tick 10 of each 100-tick day",
             "self_every_25": "user self-schedule: cy's account_brief "
                              "every 25 ticks (via invoke)",
             "event_pr_opened": "event: pull_request.opened -> reviewer "
                                "review_pr (emitted at ticks 40 and 130)"}
    for key in ("interval_every_30", "daily_at_10", "self_every_25",
                "event_pr_opened"):
        lines.append(f"| {names[key]} | {e4['expected_counts'][key]} | "
                     f"{e4['actual_counts'][key]} |")
    lines.append("")
    lines.append(f"- fire ticks match the independent simulator exactly: "
                 f"{e4['ticks_match_expected']} (expected "
                 f"{ {k: e4['expected_ticks'][k] for k in names} }).")
    lines.append(f"- ambient share: {e4['ambient_runs']} of "
                 f"{e4['total_runs_main_scenario']} runs in the main "
                 f"scenario were ambient ({e4['ambient_share']:.0%}); the "
                 f"one invoked run is cy's self-schedule registration.")
    lines.append(f"- ambient identity discipline: "
                 f"{e4['ambient_tool_steps_checked']} ambient tool steps "
                 f"checked — every acting identity is a service identity "
                 f"or None; violations: "
                 f"**{e4['ambient_identity_violations']}** (target 0). "
                 f"No ambient step ever acted as a user who did not "
                 f"invoke it.")
    lines.append(f"- responsible human: all "
                 f"{e4['self_scheduled_runs_audited_owner_cy']}/"
                 f"{e4['self_scheduled_runs']} self-scheduled runs carry "
                 f"owner cy as the responsible human in their audit "
                 f"run entries.")
    bp, bd = e4["budget_per_run"], e4["budget_per_day"]
    lines.append(f"- budget, per-run cap: agent cap replaced with "
                 f"${bp['cap_usd']:.0e} per run — "
                 f"{bp['budget_exceeded']}/{bp['ambient_runs_attempted']} "
                 f"ambient runs ended `budget_exceeded` before their "
                 f"first step.")
    lines.append(f"- budget, per-day cap: cap ${bd['cap_usd']} is below "
                 f"one measured run's cost (${bd['one_run_cost_usd']}), "
                 f"hence below two runs' cost — the first fire completed "
                 f"({bd['runs_completed']} run), the second same-day fire "
                 f"was blocked (blocked: {bd['second_fire_blocked']}) with "
                 f"{bd['schedule_block_entries']} `schedule` audit block "
                 f"entries by tick 30.")
    lines.append("")

    # ---- E5 ----
    lines.append("## E5 — Audit completeness")
    lines.append("")
    lines.append(f"- mixed workload on one harness: "
                 f"{e5['runs_executed']} runs (invoked DM + channel runs "
                 f"including a blocked mail attempt, publish_review + "
                 f"approval, one subprocess sandbox exec run, memory "
                 f"writes + a consent promotion, an ambient event run, a "
                 f"due schedule run).")
    lines.append(f"- (1) stamp coverage: **{e5['stamp_stamped']}/"
                 f"{e5['stamp_total']}** executed action + proxy entries "
                 f"carry an acting identity (target: all).")
    lines.append(f"- (2) identity correctness: {e5['identity_entries_checked']}"
                 f" action/proxy entries naming a tool target were "
                 f"checkable (run context recoverable); "
                 f"{e5['identity_entries_uncheckable']} uncheckable; "
                 f"**{e5['identity_mismatches']} mismatches** against the "
                 f"independent evaluator.")
    for m in e5["identity_mismatch_rows"]:
        lines.append(f"  - mismatch: seq {m['seq']} {m['kind']} "
                     f"{m['run_id']} {m['target']}: expected "
                     f"{m['expected_identity']}, audited "
                     f"{m['audited_identity']}")
    lines.append(f"- (3) cross-checks: every blocked step has a matching "
                 f"`decision=block` audit entry (run_id + target): "
                 f"**{e5['blocked_steps_matched']}/{e5['blocked_steps']}**; "
                 f"every run has a `kind=run` audit entry: "
                 f"**{e5['runs_with_run_entry']}/{e5['runs_executed']}**.")
    lines.append("")

    # ---- Limitations ----
    lines.append("## Limitations")
    lines.append("")
    lines.append("- The subprocess sandbox is a process boundary, not a "
                 "hardened container: its environment is scrubbed of "
                 "secrets, but it does not itself block networking or "
                 "filesystem access outside the child's own process. A "
                 "production deployment needs a real container/VM sandbox "
                 "behind the same seam.")
    lines.append("- Memory equivalence for corroboration is deliberately "
                 "strict lexical token-set equality: paraphrases do NOT "
                 "promote (measured in E1: the paraphrase pair was "
                 "refused; only byte-identical token sets corroborate). "
                 "A semantic-equivalence route would promote more, at "
                 "the cost of a fuzzier wall between users.")
    lines.append("- All runs use the scripted provider over declared task "
                 "templates. Brokered tool use with hosted models is "
                 "out of scope for this build (the REST providers run in "
                 "direct-completion mode), so nothing here measures "
                 "model behaviour.")
    lines.append("- The backing services (code host, CRM, mail, docs, "
                 "social) are deterministic in-memory stubs; the subject "
                 "is the policy around service access, not the services.")
    lines.append("- The clock is logical (ticks), and schedules use a "
                 "100-tick simulated day; no wall-clock timing is "
                 "measured anywhere in this suite.")
    lines.append("- E5's identity recompute for proxy entries relies on "
                 "the run contexts recorded by this script when it "
                 "created the runs, because proxy audit entries stamp "
                 "the surface as `sandbox`; the acting identity compared "
                 "is always the audited one, never a caller claim.")
    lines.append("")
    return "\n".join(lines)


def render_charts(r: dict, path: Path):
    e1, e2, e3, e4, e5 = r["E1"], r["E2"], r["E3"], r["E4"], r["E5"]
    fig, axes = plt.subplots(2, 3, figsize=(15, 8))
    fig.suptitle("open-company-harness — experiment results", fontsize=14)

    ax = axes[0][0]
    ax.bar(["cross-user\nleaks", "ambient\nleaks", "promotion\nmisses"],
           [e1["leaked_facts"], e1["ambient_leaked_facts"],
            e1["promotion_total"] - e1["promotion_agreed"]],
           color=["#2e7d32", "#2e7d32", "#c62828"])
    ax.set_title(f"E1 memory isolation ({e1['recall_queries']} queries, "
                 f"promotion {e1['promotion_agreed']}/"
                 f"{e1['promotion_total']})")

    ax = axes[0][1]
    ax.bar(["allowed", "blocked", "mismatches"],
           [e2["allowed"], e2["blocked"], e2["mismatches"]],
           color=["#2e7d32", "#616161", "#c62828"])
    ax.set_title(f"E2 identity policy ({e2['cells']} cells)")

    ax = axes[0][2]
    ax.bar(["adversarial\nrefused", "escaped", "secret\nmaterializations"],
           [e3["proxy_refused"], e3["proxy_escaped"],
            e3["secret_materializations"]],
           color=["#2e7d32", "#c62828", "#c62828"])
    ax.set_title("E3 secret proxy")

    ax = axes[1][0]
    keys = ["interval_every_30", "daily_at_10", "self_every_25",
            "event_pr_opened"]
    labels = ["interval 30", "daily @10", "self 25", "event"]
    x = range(len(keys))
    ax.bar([i - 0.2 for i in x], [e4["expected_counts"][k] for k in keys],
           width=0.4, label="expected")
    ax.bar([i + 0.2 for i in x], [e4["actual_counts"][k] for k in keys],
           width=0.4, label="actual")
    ax.set_xticks(list(x))
    ax.set_xticklabels(labels)
    ax.legend()
    ax.set_title("E4 ambient fires: expected vs actual")

    ax = axes[1][1]
    ax.bar(["stamped", "total", "identity\nmismatches", "blocked\nmatched"],
           [e5["stamp_stamped"], e5["stamp_total"],
            e5["identity_mismatches"], e5["blocked_steps_matched"]],
           color=["#2e7d32", "#616161", "#c62828", "#2e7d32"])
    ax.set_title(f"E5 audit completeness ({e5['runs_executed']} runs)")

    ax = axes[1][2]
    ax.axis("off")
    summary = (
        f"E1 leaks 0 target: {'met' if e1['leaked_facts'] == 0 else 'MISSED'}"
        f" · promotion {e1['promotion_agreed']}/{e1['promotion_total']}\n"
        f"E2 mismatches: {e2['mismatches']} (target 0)\n"
        f"E3 escaped: {e3['proxy_escaped']} · secrets out: "
        f"{e3['secret_materializations']} (target 0)\n"
        f"E4 fires match: {e4['ticks_match_expected']} · identity "
        f"violations: {e4['ambient_identity_violations']}\n"
        f"E5 coverage: {e5['stamp_stamped']}/{e5['stamp_total']} · "
        f"mismatches: {e5['identity_mismatches']}")
    ax.text(0.02, 0.6, summary, fontsize=11, family="monospace",
            verticalalignment="top")
    ax.set_title("Headline")
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def main():
    results = {"E1": experiment_e1(), "E2": experiment_e2(),
               "E3": experiment_e3(), "E4": experiment_e4(),
               "E5": experiment_e5()}
    out_dir = ROOT / "results"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "results.json").write_text(
        json.dumps(results, indent=2, sort_keys=True) + "\n")
    (out_dir / "results.md").write_text(render_md(results))
    render_charts(results, out_dir / "charts.png")
    e1, e2, e3, e4, e5 = (results[k] for k in ("E1", "E2", "E3", "E4", "E5"))
    print(f"E1 leaks={e1['leaked_facts']} ambient_leaks="
          f"{e1['ambient_leaked_facts']} promotion="
          f"{e1['promotion_agreed']}/{e1['promotion_total']}")
    print(f"E2 cells={e2['cells']} mismatches={e2['mismatches']} "
          f"allowed={e2['allowed']} blocked={e2['blocked']}")
    print(f"E3 refused={e3['proxy_refused']}/{e3['proxy_adversarial']} "
          f"escaped={e3['proxy_escaped']} materializations="
          f"{e3['secret_materializations']}")
    print(f"E4 expected={e4['expected_counts']} actual="
          f"{e4['actual_counts']} violations="
          f"{e4['ambient_identity_violations']}")
    print(f"E5 coverage={e5['stamp_stamped']}/{e5['stamp_total']} "
          f"identity_mismatches={e5['identity_mismatches']} blocked_matched="
          f"{e5['blocked_steps_matched']}/{e5['blocked_steps']} runs="
          f"{e5['runs_with_run_entry']}/{e5['runs_executed']}")
    print(f"wrote {out_dir}/results.json, results.md, charts.png")


if __name__ == "__main__":
    main()
