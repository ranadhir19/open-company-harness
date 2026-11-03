# open-company-harness: results (seeded run)

All numbers below are measured from actual runs of this suite against the bundled scenario (scripted providers, no API keys, deterministic logical clock).

## Method note — independent expectations

Expectations are derived independently of the code under test, per the house validation protocol. E2 and E5 use a standalone evaluator in this script that reads only the ToolSpec declarations (identity mode, surfaces, per-op policy, whitelist) and applies the broker rules as documented in the identity module — it never calls `broker.decide` to check `broker.decide`. E4's expected fire counts come from a small simulator written from the scheduler's documented semantics, not from the scheduler. Near-miss negatives are first-class: DM-only tools in shared channels, non-whitelisted publishers, ambient runs of on-behalf-of tools, forged/expired/replayed capability tokens, and paraphrase corroboration are all in the batteries. Where a target is missed, the miss is reported plainly below.

## Headline

| experiment | measured result | target | verdict |
|---|---|---|---|
| E1 memory isolation | 0 cross-user leaks in 36 queries over 12 ordered pairs; 0 ambient leaks; promotion agreement 9/9 | 0 leaks; 9/9 promotions | met |
| E2 identity policy | 192 decision cells, 0 mismatches vs the independent evaluator; 142 allowed / 50 blocked | 0 mismatches | met |
| E3 secret proxy | 5/5 adversarial proxy probes refused, 0 escaped; 0 vault-secret materializations across all probe/sandbox output | all refused; 0 materializations | met |
| E4 ambient scheduling | expected vs actual fires identical on all four triggers (29 ambient runs, 97% of all runs); 0 ambient identity violations | exact match; 0 violations | met |
| E5 audit completeness | stamp coverage 20/20; 0 identity mismatches in 19 checkable entries; blocked steps matched 2/2; runs with a run entry 8/8 | full coverage; 0 mismatches | met |

## E1 — Cross-user memory isolation

- seeded 3 private facts per user (ana, ben, cy, dee), each carrying a distinctive token, on top of the scenario's 1 seeded global fact(s).
- cross-user recall: 12 ordered (reader, owner) pairs, 36 queries built from the owner's tokens — **0 leaked facts** (target 0).
- ambient recall: 12 queries with no invoking user — **0 leaked facts** (target 0); ambient contexts see only the global tier.

Promotion battery (expected outcome derived from the memory module's documented routes):

| attempt | expected | actual | agree |
|---|---|---|---|
| non-owner consent (ben consents to share ana's fact) | refuse | refuse | yes |
| same-owner corroboration (two ana facts, identical text) | refuse | refuse | yes |
| paraphrase corroboration (ana + ben, token sets differ) | refuse | refuse | yes |
| two-owner identical-text corroboration (cy + dee) | allow | allow | yes |
| owner consent (ana shares her own fact) | allow | allow | yes |
| declared-global write with source_user (refused at store) | refuse | refuse | yes |
| runtime global write by reviewer (no can_write_global) | refuse | refuse | yes |
| runtime global write by studio with source_user | refuse | refuse | yes |
| runtime global write by studio without source_user | allow | allow | yes |

- agreement: **9/9**.

## E2 — Identity-policy enforcement

- full matrix: 5 tools x their operations x contexts {DM, CHANNEL, THREAD x invokers ana/cy/dee; AMBIENT x commissioning owners dee/cy/none} = **192 cells**, each decided with a fresh RunContext and compared (allowed + acting identity + approval flag) against the independent evaluator.
- **0 mismatches** (target 0); 142 cells allowed, 50 blocked. The blocked set is the near-miss battery: mail anywhere but a DM, docs/social publishing by non-whitelisted humans, on-behalf-of tools in ambient runs with no responsible human, and social publishing from ambient runs commissioned by a non-whitelisted owner.
- no over-blocking: all **142/142 legitimate cells** the evaluator says must pass did pass. They are:
  - `codehost.list_prs` (12): ana@dm as user:ana; cy@dm as user:cy; dee@dm as user:dee; ana@channel as user:ana; cy@channel as user:cy; dee@channel as user:dee; ana@thread as user:ana; cy@thread as user:cy; dee@thread as user:dee; ambient(owner=dee) as service:codehost-bot; ambient(owner=cy) as service:codehost-bot; ambient(owner=None) as service:codehost-bot
  - `codehost.read_pr` (12): ana@dm as user:ana; cy@dm as user:cy; dee@dm as user:dee; ana@channel as user:ana; cy@channel as user:cy; dee@channel as user:dee; ana@thread as user:ana; cy@thread as user:cy; dee@thread as user:dee; ambient(owner=dee) as service:codehost-bot; ambient(owner=cy) as service:codehost-bot; ambient(owner=None) as service:codehost-bot
  - `codehost.draft_comment` (12): ana@dm as user:ana; cy@dm as user:cy; dee@dm as user:dee; ana@channel as user:ana; cy@channel as user:cy; dee@channel as user:dee; ana@thread as user:ana; cy@thread as user:cy; dee@thread as user:dee; ambient(owner=dee) as service:codehost-bot; ambient(owner=cy) as service:codehost-bot; ambient(owner=None) as service:codehost-bot
  - `codehost.publish_comment` (12): ana@dm as user:ana; cy@dm as user:cy; dee@dm as user:dee; ana@channel as user:ana; cy@channel as user:cy; dee@channel as user:dee; ana@thread as user:ana; cy@thread as user:cy; dee@thread as user:dee; ambient(owner=dee) as service:codehost-bot; ambient(owner=cy) as service:codehost-bot; ambient(owner=None) as service:codehost-bot
  - `crm.list_accounts` (12): ana@dm as service:crm-sync; cy@dm as service:crm-sync; dee@dm as service:crm-sync; ana@channel as service:crm-sync; cy@channel as service:crm-sync; dee@channel as service:crm-sync; ana@thread as service:crm-sync; cy@thread as service:crm-sync; dee@thread as service:crm-sync; ambient(owner=dee) as service:crm-sync; ambient(owner=cy) as service:crm-sync; ambient(owner=None) as service:crm-sync
  - `crm.lookup_account` (12): ana@dm as service:crm-sync; cy@dm as service:crm-sync; dee@dm as service:crm-sync; ana@channel as service:crm-sync; cy@channel as service:crm-sync; dee@channel as service:crm-sync; ana@thread as service:crm-sync; cy@thread as service:crm-sync; dee@thread as service:crm-sync; ambient(owner=dee) as service:crm-sync; ambient(owner=cy) as service:crm-sync; ambient(owner=None) as service:crm-sync
  - `crm.update_stage` (12): ana@dm as service:crm-sync; cy@dm as service:crm-sync; dee@dm as service:crm-sync; ana@channel as service:crm-sync; cy@channel as service:crm-sync; dee@channel as service:crm-sync; ana@thread as service:crm-sync; cy@thread as service:crm-sync; dee@thread as service:crm-sync; ambient(owner=dee) as service:crm-sync; ambient(owner=cy) as service:crm-sync; ambient(owner=None) as service:crm-sync
  - `docs.read_page` (9): ana@dm as user:ana; cy@dm as user:cy; dee@dm as user:dee; ana@channel as user:ana; cy@channel as user:cy; dee@channel as user:dee; ana@thread as user:ana; cy@thread as user:cy; dee@thread as user:dee
  - `docs.draft_page` (9): ana@dm as user:ana; cy@dm as user:cy; dee@dm as user:dee; ana@channel as user:ana; cy@channel as user:cy; dee@channel as user:dee; ana@thread as user:ana; cy@thread as user:cy; dee@thread as user:dee
  - `docs.publish_page` (3): dee@dm as user:dee; dee@channel as user:dee; dee@thread as user:dee
  - `mail.draft_email` (3): ana@dm as user:ana; cy@dm as user:cy; dee@dm as user:dee
  - `mail.send_email` (3): ana@dm as user:ana; cy@dm as user:cy; dee@dm as user:dee
  - `mail.list_outbox` (3): ana@dm as user:ana; cy@dm as user:cy; dee@dm as user:dee
  - `social.draft_post` (12): ana@dm as service:brand-voice; cy@dm as service:brand-voice; dee@dm as service:brand-voice; ana@channel as service:brand-voice; cy@channel as service:brand-voice; dee@channel as service:brand-voice; ana@thread as service:brand-voice; cy@thread as service:brand-voice; dee@thread as service:brand-voice; ambient(owner=dee) as service:brand-voice; ambient(owner=cy) as service:brand-voice; ambient(owner=None) as service:brand-voice
  - `social.publish_post` (4): dee@dm as service:brand-voice; dee@channel as service:brand-voice; dee@thread as service:brand-voice; ambient(owner=dee) as service:brand-voice
  - `social.list_posts` (12): ana@dm as service:brand-voice; cy@dm as service:brand-voice; dee@dm as service:brand-voice; ana@channel as service:brand-voice; cy@channel as service:brand-voice; dee@channel as service:brand-voice; ana@thread as service:brand-voice; cy@thread as service:brand-voice; dee@thread as service:brand-voice; ambient(owner=dee) as service:brand-voice; ambient(owner=cy) as service:brand-voice; ambient(owner=None) as service:brand-voice

## E3 — Secret-proxy exfiltration

Proxy probes (capability minted for codehost.read_pr, run e3-run-1):

| probe | outcome |
|---|---|
| valid call (control) | succeeded |
| forged token | refused (unknown or forged capability token) |
| wrong-service token use | refused (token is scoped to service codehost, not crm) |
| out-of-scope operation | refused (operation draft_comment outside token scope ['read_pr']) |
| cross-run replay | refused (capability token is bound to a different run (replay)) |
| expired token | refused (capability token expired) |

- adversarial probes refused: **5/5**; escaped: 0; control call succeeded: True.

Subprocess sandbox battery (real child processes, reviewer in a channel, codehost capability only):

| snippet | step | expected marker seen | secret in transcript |
|---|---|---|---|
| dump environment | ok | True | False |
| print the capability-token value | ok | True | False |
| out-of-scope op publish_comment (approval-gated, must be outside sandbox scope) | ok | True | False |
| service with no capability (crm for reviewer) | ok | True | False |
| globals()/import inspection hunting for secrets | ok | True | False |

- published comments on PR-101 before/after the battery: 0 -> 0 — the approval-gated `publish_comment` operation is not in the sandbox token scope (the broker grants only allowed, non-gated ops), so the in-sandbox publish attempt was refused by scope.
- capability tokens **were visible inside the sandbox by design** (env key `OCH_CAP_TOKENS`; regex-matched in transcripts: True). They are opaque, single-service, single-run, short-lived capabilities — the probes above are what make them useless outside their scope: forged, expired, wrong-service, out-of-scope and cross-run uses are all refused by the proxy.
- deterministic sandbox env view (`exec_digest`): keys ['TOKEN[docs]', 'TOKEN[social]'] — every value is a capability token (True), secret hits in the env view: 0.
- after all runs, every proxy output and sandbox transcript was scanned for all 5 vault secret strings: **0 materializations** (target 0).
- no live-network probe was attempted from sandbox code: the subprocess sandbox does not itself block networking (documented limitation, see below).

## E4 — Ambient scheduling

| trigger | expected fires | actual fires |
|---|---|---|
| system interval: pipeline daily_digest every 30 ticks | 11 | 11 |
| system daily: pipeline account_brief at tick 10 of each 100-tick day | 3 | 3 |
| user self-schedule: cy's account_brief every 25 ticks (via invoke) | 13 | 13 |
| event: pull_request.opened -> reviewer review_pr (emitted at ticks 40 and 130) | 2 | 2 |

- fire ticks match the independent simulator exactly: True (expected {'interval_every_30': [0, 30, 60, 90, 120, 150, 180, 210, 240, 270, 300], 'daily_at_10': [10, 110, 210], 'self_every_25': [0, 25, 50, 75, 100, 125, 150, 175, 200, 225, 250, 275, 300], 'event_pr_opened': [40, 130]}).
- ambient share: 29 of 30 runs in the main scenario were ambient (97%); the one invoked run is cy's self-schedule registration.
- ambient identity discipline: 31 ambient tool steps checked — every acting identity is a service identity or None; violations: **0** (target 0). No ambient step ever acted as a user who did not invoke it.
- responsible human: all 13/13 self-scheduled runs carry owner cy as the responsible human in their audit run entries.
- budget, per-run cap: agent cap replaced with $1e-09 per run — 3/3 ambient runs ended `budget_exceeded` before their first step.
- budget, per-day cap: cap $0.004 is below one measured run's cost ($0.00816), hence below two runs' cost — the first fire completed (1 run), the second same-day fire was blocked (blocked: True) with 3 `schedule` audit block entries by tick 30.

## E5 — Audit completeness

- mixed workload on one harness: 8 runs (invoked DM + channel runs including a blocked mail attempt, publish_review + approval, one subprocess sandbox exec run, memory writes + a consent promotion, an ambient event run, a due schedule run).
- (1) stamp coverage: **20/20** executed action + proxy entries carry an acting identity (target: all).
- (2) identity correctness: 19 action/proxy entries naming a tool target were checkable (run context recoverable); 0 uncheckable; **0 mismatches** against the independent evaluator.
- (3) cross-checks: every blocked step has a matching `decision=block` audit entry (run_id + target): **2/2**; every run has a `kind=run` audit entry: **8/8**.

## Limitations

- The subprocess sandbox is a process boundary, not a hardened container: its environment is scrubbed of secrets, but it does not itself block networking or filesystem access outside the child's own process. A production deployment needs a real container/VM sandbox behind the same seam.
- Memory equivalence for corroboration is deliberately strict lexical token-set equality: paraphrases do NOT promote (measured in E1: the paraphrase pair was refused; only byte-identical token sets corroborate). A semantic-equivalence route would promote more, at the cost of a fuzzier wall between users.
- All runs use the scripted provider over declared task templates. Brokered tool use with hosted models is out of scope for this build (the REST providers run in direct-completion mode), so nothing here measures model behaviour.
- The backing services (code host, CRM, mail, docs, social) are deterministic in-memory stubs; the subject is the policy around service access, not the services.
- The clock is logical (ticks), and schedules use a 100-tick simulated day; no wall-clock timing is measured anywhere in this suite.
- E5's identity recompute for proxy entries relies on the run contexts recorded by this script when it created the runs, because proxy audit entries stamp the surface as `sandbox`; the acting identity compared is always the audited one, never a caller claim.
