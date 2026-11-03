# Open Company Harness

**The open, model-agnostic company harness: agents that live where people work, act under brokered identities, remember in two tiers, and touch secrets only through a proxy.**

Commercial "company harness" offerings put agents inside a company's chat
workspace, give them tools, memory, sandboxed code execution, and scheduled
background work. This repository builds the same five components as an open,
deterministic, offline-first Python package — so the policy questions (whose
identity does an agent act under? what may one user's agent tell another
user? how does code call a service without ever holding its secret?) can be
studied, tested, and measured instead of taken on trust.

> **Attribution.** The five-component framing follows LangChain's
> "The five components of building a company harness" (2026-10-24):
> https://www.langchain.com/blog/components-building-company-harness
> That post describes their commercial Managed Deep Agents platform.
> **What this repo adds:** an open implementation of the same five
> components over a declared-agent core, with an identity broker that
> stamps every action, a two-tier memory with enforced isolation, a
> secret proxy that keeps raw credentials out of the sandbox, and a
> **measured experiment suite** (E1–E5 below) with independently derived
> expectations and near-miss negative batteries.

Everything below runs **offline, with no API keys**. Hosted model providers
are opt-in adapters behind the same seam.

## Measured results (real runs — see results/results.md)

| Experiment | Result |
|---|---|
| E1 Cross-user memory isolation | **0 leaks** in 36 cross-user queries over 12 ordered user pairs; 0 ambient leaks; promotion battery agreement **9/9** (incl. paraphrase corroboration refused, non-owner consent refused) |
| E2 Identity-policy enforcement | **192 decision cells, 0 mismatches** vs an independent evaluator; 142 allowed / 50 blocked; **142/142 legitimate cells passed** (no over-blocking) |
| E3 Secret-proxy exfiltration | **5/5 adversarial proxy probes refused** (forged, wrong-service, out-of-scope, cross-run replay, expired); **0 raw-secret materializations** across all sandbox transcripts and proxy outputs |
| E4 Ambient scheduling | Fire counts **identical to an independent simulator** on all four triggers (11 interval + 3 daily + 13 self-scheduled + 2 event = 29 ambient runs, 97% of all runs); **0 ambient identity violations**; per-run and per-day budget caps both enforced |
| E5 Audit completeness | Stamp coverage **20/20**; **0 identity mismatches** in 19 checkable entries; blocked steps matched **2/2**; runs with a run entry **8/8** |

Reproduce: `PYTHONPATH=src python3 experiments/run_experiments.py`
(writes results/results.json, results/results.md, results/charts.png;
two consecutive runs are byte-identical).

## The five components

| # | Blog component | This repo |
|---|---|---|
| 1 | Lives where people work | `channels.py` — workspace model (channels, DMs, threads); conversations **break out** into agent-owned threads so agent work stays out of the human thread; chat-adapter seam with an in-memory Slack-shaped stub |
| 2 | Identity | `identity.py` — every tool declares `ON_BEHALF_OF`, `SERVICE`, or `DUAL`; per-surface gating (mail is DM-only); per-operation whitelists (publish only for whitelisted humans); every action identity-stamped in `audit.py`; violations blocked and logged, never silently rerouted |
| 3 | Two-tier memory | `memory.py` — global tier + per-user tier with enforced isolation and provenance on every fact; promotion to global only by owner consent, multi-user corroboration (strict token-set equivalence), or a declared-global route that refuses user-sourced content |
| 4 | Sandbox + secret proxy | `sandbox.py` — deterministic sandbox seam plus a real subprocess implementation with a scrubbed environment; services reachable only through `SecretProxy`, which validates scoped, short-lived, single-run capability tokens and injects credentials host-side; raw secrets never enter the sandbox |
| 5 | Scheduled / ambient runs | `scheduler.py` — interval and daily cron-style schedules plus event triggers; agents register schedules on a user's behalf; ambient runs are budgeted (per-run and per-day caps) and audited exactly like invoked runs |

Managed-layer carry-overs (from the sibling
[open-managed-agents](https://github.com/ranadhir19/open-managed-agents)):
declared agents compiled to runs at runtime, run traces, per-step token/cost
accounting, a budget governor, and human-approval gates for sensitive
operations (e.g. publishing a review comment pauses the run until a human
approves; approval resumes under the originally brokered identity).

## Quickstart

```bash
pip install open-company-harness        # or: pip install -e .
PYTHONPATH=src python3 examples/quickstart.py
PYTHONPATH=src python3 demo.py          # a scripted Meridian Labs day -> results/demo_run.json
```

The bundled scenario is a fictional company, **Meridian Labs**: four users
(ana, ben, cy, dee), five channels, five tools (code host, CRM, mail, docs,
social), and three agents (reviewer, pipeline, studio). Ana's channel review
runs as `user:ana` (DUAL → on-behalf-of); the same mail task invoked in a
shared channel is blocked (DM-only) while the run completes; an ambient
PR-opened run acts as `service:codehost-bot`.

## Demo

`demo/` contains a simulated company workspace: a React UI (channels,
people, agents, an ambient run feed, and identity/audit/memory viewers) over
a FastAPI + GraphQL backend (`demo/backend/app.py`). `render.yaml` at the
repo root is a ready Render blueprint — **deploy-ready, not deployed**.
The web service needs `VITE_API_URL` set to the API service URL once the
API exists (commented hint in render.yaml).

## Tests

```bash
PYTHONPATH=src python3 -m pytest tests/ -q    # 53 tests
python3 validate.py                            # full house harness: compile, tests, content scan, consistency checks
```

## Limitations (measured, not hedged)

- The subprocess sandbox is a **process boundary, not a hardened
  container**: its environment is scrubbed of secrets, but it does not
  itself block networking or filesystem access. A production deployment
  needs a real container/VM sandbox behind the same seam.
- Capability tokens **are visible inside the sandbox by design** (env key
  `OCH_CAP_TOKENS`). They are opaque, single-service, single-run,
  short-lived; E3 is the evidence they are useless outside their scope.
- Memory corroboration uses **strict lexical token-set equivalence**:
  paraphrases do not promote (measured in E1). A semantic-equivalence
  route would promote more, at the cost of a fuzzier wall between users.
- All experiment runs use the **scripted provider** over declared task
  templates; the hosted-provider adapters run in direct-completion mode.
  Nothing here measures model behaviour.
- The backing services (code host, CRM, mail, docs, social) are
  deterministic in-memory stubs; the subject is the policy around service
  access, not the services. The clock is logical (ticks; a 100-tick
  simulated day) — no wall-clock timing is measured.

## Sibling projects

- [open-managed-agents](https://github.com/ranadhir19/open-managed-agents) — the declared-agent managed core this harness extends into the workspace
- [unified-agent-memory](https://github.com/ranadhir19/unified-agent-memory) — the harness-agnostic memory layer; this repo's two-tier store adds per-user isolation and promotion policy

## License

MIT — see LICENSE.
