"""The runtime: executes plans through the policy subsystems.

A run is a plan (from a provider) executed action by action. The
runtime is deliberately *untrusted-agent* shaped: the agent proposes
actions, and the runtime routes each one through the subsystem that
owns its policy —

- ``tool_call``     → the identity broker decides; if allowed, the
                      call executes through the secret proxy under a
                      freshly minted capability token.
- ``sandbox_exec``  → code runs in a sandbox whose only service
                      access is capability tokens scoped by broker
                      decisions for the agent's declared tools.
- ``memory_write``  → the memory store's own invariants (tier,
                      owner, promotion routes) are enforced by the
                      store; refusals surface as blocked steps.
- ``memory_recall`` → the store returns only facts the context may see.
- ``schedule_create`` → the ambient scheduler; the invoking user
  becomes the schedule's responsible owner.
- ``message`` / ``respond`` → the channel layer / the run's output.

Managed-layer behaviour carried over from open-managed-agents: every
step is traced with token and cost accounting; a **budget governor**
stops a run before a step that would exceed the agent's declared
per-run caps; **approval gates** pause a run at an operation declared
approval-required until a human approves it; a transient provider
failure is retried once. Policy blocks do not abort the run — a
refusal is information, recorded in the trace and the audit, and the
run continues with the remaining actions.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass

from .audit import AuditTrail
from .channels import Workspace
from .identity import Clock, IdentityBroker
from .memory import MemoryError, MemoryStore
from .models import (Action, RunContext, RunResult, StepRecord, Surface,
                     Tier)
from .providers import ProviderError, ScriptedProvider
from .registry import Registry
from .sandbox import (DeterministicSandbox, ProxyClient, SecretProxy,
                      SecretVault, SubprocessSandbox, default_services)
from .scheduler import AmbientScheduler, DAY_TICKS


@dataclass
class _PausedRun:
    ctx: RunContext
    plan: list
    next_index: int
    result: RunResult
    pending_decision: object      # broker Decision for the gated action


class Harness:
    """One company harness: registry + subsystems + runtime, wired."""

    def __init__(self, registry: Registry, provider: ScriptedProvider,
                 *, salt: str = "och"):
        self.registry = registry
        self.provider = provider
        self.clock = Clock()
        self.audit = AuditTrail()
        self.broker = IdentityBroker(self.audit, self.clock, salt=salt)
        self.memory = MemoryStore(self.audit, self.clock)
        self.workspace = Workspace()
        self.services = default_services()
        self.vault = SecretVault()
        self.proxy = SecretProxy(self.broker, self.vault, self.services,
                                 self.audit, self.clock)
        self.scheduler = AmbientScheduler(self.clock, self.audit)
        self._run_ids = itertools.count(1)
        self._paused: dict[str, _PausedRun] = {}
        self._schedule_spend: dict[tuple[str, int], float] = {}

    # -- public entry points ------------------------------------------------
    def invoke(self, agent_name: str, task: str, *,
               user: str | None = None, surface: Surface = Surface.CHANNEL,
               channel_id: str | None = None, thread_id: str | None = None,
               params: dict | None = None) -> RunResult:
        ctx = RunContext(agent=agent_name, invoking_user=user,
                         surface=surface, channel_id=channel_id,
                         thread_id=thread_id)
        return self._start(ctx, task, params or {})

    def run_due(self) -> list[RunResult]:
        """Fire every schedule due at the current tick. Ambient runs
        honour a per-schedule daily spend cap when one is set."""
        results = []
        for sched, ctx in self.scheduler.due_contexts(self.clock.now()):
            day = self.clock.now() // DAY_TICKS
            spent = self._schedule_spend.get((sched.id, day), 0.0)
            cap = getattr(sched, "max_cost_per_day_usd", None)
            if cap is not None and spent >= cap:
                self.audit.record(
                    tick=self.clock.now(), kind="schedule", agent=sched.agent,
                    run_id="", acting_identity=None,
                    invoking_user=sched.owner, surface="ambient",
                    target=sched.id, decision="block",
                    detail=f"daily spend cap reached ({spent:.4f} >= {cap})")
                continue
            result = self._start(ctx, sched.task, {})
            self._schedule_spend[(sched.id, day)] = spent + result.total_cost_usd
            results.append(result)
        return results

    def emit_event(self, event_type: str, payload: dict | None = None) -> list[RunResult]:
        payload = payload or {}
        return [self._start(ctx, trig.task, payload)
                for trig, ctx in self.scheduler.event_contexts(event_type, payload)]

    def approve(self, run_id: str, approver: str) -> RunResult:
        """Resume a run paused at an approval gate. The approval is
        audited with the approver's identity; the gated action then
        executes under its originally brokered identity."""
        paused = self._paused.pop(run_id, None)
        if paused is None:
            raise KeyError(f"no paused run: {run_id}")
        self.audit.record(
            tick=self.clock.now(), kind="approval", agent=paused.ctx.agent,
            run_id=run_id, acting_identity=f"user:{approver}",
            invoking_user=approver, surface=paused.ctx.surface.value,
            target=paused.result.pending_approval["target"]
            if paused.result.pending_approval else "",
            decision="allow", detail=f"approved by {approver}")
        action = paused.plan[paused.next_index]
        self._execute(paused.ctx, paused.result, action,
                      pre_decision=paused.pending_decision)
        return self._continue(paused.ctx, paused.result, paused.plan,
                              paused.next_index + 1)

    # -- run lifecycle ---------------------------------------------------------
    def _start(self, ctx: RunContext, task: str, params: dict) -> RunResult:
        agent = self.registry.agent(ctx.agent)
        ctx.run_id = f"run-{next(self._run_ids):04d}"
        result = RunResult(run_id=ctx.run_id, agent=ctx.agent, status="running")
        self.audit.record(
            tick=self.clock.now(), kind="run", agent=ctx.agent,
            run_id=ctx.run_id, acting_identity=None,
            invoking_user=ctx.invoking_user or ctx.on_behalf_of,
            surface=ctx.surface.value, target=task, decision="n/a",
            detail=f"run started (surface={ctx.surface.value})")
        try:
            plan = self.provider.plan(agent, task, ctx, params)
        except ProviderError:
            try:
                plan = self.provider.plan(agent, task, ctx, params)
            except ProviderError as exc:
                result.status = "failed"
                result.output = f"planning failed: {exc}"
                return result
        return self._continue(ctx, result, plan, 0)

    def _continue(self, ctx: RunContext, result: RunResult,
                  plan: list, start: int) -> RunResult:
        agent = self.registry.agent(ctx.agent)
        for index in range(start, len(plan)):
            action = plan[index]
            est = self.provider.estimate_tokens(action)
            est_cost = self.provider.cost(est)
            if (result.total_tokens + est > agent.max_tokens_per_run
                    or result.total_cost_usd + est_cost > agent.max_cost_per_run_usd):
                result.steps.append(StepRecord(
                    seq=len(result.steps) + 1, action=action, status="skipped",
                    acting_identity=None,
                    detail="budget governor: step would exceed the agent's "
                           "declared per-run caps"))
                result.status = "budget_exceeded"
                return result
            pause = self._execute(ctx, result, action)
            if pause is not None:
                result.status = "awaiting_approval"
                result.pending_approval = pause
                self._paused[result.run_id] = _PausedRun(
                    ctx=ctx, plan=plan, next_index=index, result=result,
                    pending_decision=pause["decision"])
                return result
        result.status = "completed"
        return result

    # -- action dispatch -----------------------------------------------------
    def _execute(self, ctx: RunContext, result: RunResult, action: Action,
                 pre_decision=None) -> dict | None:
        """Execute one action. Returns a pause dict when the action is
        approval-gated and not yet approved, else None."""
        handler = {"tool_call": self._do_tool_call,
                   "sandbox_exec": self._do_sandbox,
                   "memory_write": self._do_memory_write,
                   "memory_recall": self._do_memory_recall,
                   "message": self._do_message,
                   "schedule_create": self._do_schedule_create,
                   "respond": self._do_respond}.get(action.kind)
        seq = len(result.steps) + 1
        if handler is None:
            result.steps.append(StepRecord(
                seq=seq, action=action, status="error", acting_identity=None,
                detail=f"unknown action kind {action.kind!r}"))
            self._charge(result, action)
            return None
        return handler(ctx, result, action, pre_decision)

    def _charge(self, result: RunResult, action: Action):
        tokens = self.provider.estimate_tokens(action)
        result.total_tokens += tokens
        result.total_cost_usd += self.provider.cost(tokens)
        return tokens, self.provider.cost(tokens)

    def _step(self, result: RunResult, action: Action, status: str,
              identity: str | None, detail: str = "", output: str = ""):
        tokens, cost = self._charge(result, action)
        result.steps.append(StepRecord(
            seq=len(result.steps) + 1, action=action, status=status,
            acting_identity=identity, detail=detail, output=output,
            tokens=tokens, cost_usd=cost))

    # -- tool calls --------------------------------------------------------------
    def _do_tool_call(self, ctx, result, action, pre_decision):
        agent = self.registry.agent(ctx.agent)
        if action.tool not in agent.tools:
            self._step(result, action, "blocked", None,
                       f"tool {action.tool} is not declared for agent {ctx.agent}")
            return None
        tool = self.registry.tool(action.tool)
        decision = pre_decision or self.broker.decide(ctx, tool, action.operation)
        if not decision.allowed:
            self._step(result, action, "blocked", decision.acting_identity,
                       decision.reason)
            return None
        if decision.needs_approval and pre_decision is None:
            return {"target": f"{tool.name}.{action.operation}",
                    "decision": decision}
        token = self.broker.mint_capability(ctx, decision, tool,
                                            (action.operation,))
        res = self.proxy.call(agent=ctx.agent, run_id=ctx.run_id,
                              token_str=token.token, service=tool.service,
                              operation=action.operation,
                              payload=action.payload)
        self.audit.record(
            tick=self.clock.now(), kind="action", agent=ctx.agent,
            run_id=ctx.run_id, acting_identity=decision.acting_identity,
            invoking_user=ctx.invoking_user, surface=ctx.surface.value,
            target=f"{tool.name}.{action.operation}", decision="allow",
            detail="executed via secret proxy")
        self._step(result, action, "ok" if res.ok else "error",
                   decision.acting_identity,
                   "" if res.ok else res.reason, output=res.output)
        return None

    # -- sandbox ---------------------------------------------------------------------
    def _do_sandbox(self, ctx, result, action, pre_decision):
        agent = self.registry.agent(ctx.agent)
        if not agent.can_execute_code:
            self._step(result, action, "blocked", None,
                       f"agent {ctx.agent} is not declared for code execution")
            return None
        services = action.payload.get("services", [])
        tokens: dict[str, str] = {}
        for service in services:
            ops: list[str] = []
            identity_for_service = None
            for tool_name in agent.tools:
                tool = self.registry.tool(tool_name)
                if tool.service != service:
                    continue
                for op in tool.operations:
                    d = self.broker.decide(ctx, tool, op)
                    if d.allowed and not d.needs_approval:
                        ops.append(op)
                        identity_for_service = d.acting_identity
            if ops:
                tool0 = self.registry.tool(
                    next(t for t in agent.tools
                         if self.registry.tool(t).service == service))
                decision = self.broker.decide(ctx, tool0, ops[0])
                token = self.broker.mint_capability(
                    ctx, decision, tool0, tuple(sorted(set(ops))))
                tokens[service] = token.token
                _ = identity_for_service
        client = ProxyClient(self.proxy, agent=ctx.agent,
                             run_id=ctx.run_id, tokens=tokens)
        engine = action.payload.get("engine", "deterministic")
        if engine == "subprocess":
            sandbox = SubprocessSandbox()
            res = sandbox.run(action.payload.get("code", ""), client, tokens)
        else:
            env_view = {f"TOKEN[{svc}]": tok for svc, tok in tokens.items()}
            sandbox = DeterministicSandbox(env_view)
            res = sandbox.run(action.payload.get("steps", []), client)
        self.audit.record(
            tick=self.clock.now(), kind="action", agent=ctx.agent,
            run_id=ctx.run_id,
            acting_identity=(f"user:{ctx.invoking_user}"
                             if ctx.invoking_user else None),
            invoking_user=ctx.invoking_user, surface=ctx.surface.value,
            target=f"sandbox:{engine}", decision="allow",
            detail=f"{len(res.calls)} service call(s) attempted")
        self._step(result, action, "ok" if res.ok else "error",
                   None, res.error, output=res.transcript[:2000])
        return None

    # -- memory ------------------------------------------------------------------
    def _do_memory_write(self, ctx, result, action, pre_decision):
        agent = self.registry.agent(ctx.agent)
        p = action.payload
        try:
            if "promote" in p:
                promo = p["promote"]
                if promo["by"] == "consent":
                    fact = self.memory.promote_by_consent(
                        ctx, promo["fact_id"], promo["consenting_user"])
                else:
                    fact = self.memory.promote_by_corroboration(
                        ctx, tuple(promo["fact_ids"]))
                self._step(result, action, "ok", None,
                           f"promoted to global as {fact.id}", output=fact.id)
                return None
            tier = p.get("tier", "user")
            if tier == "global":
                if not agent.can_write_global:
                    self._step(result, action, "blocked", None,
                               f"agent {ctx.agent} may not write the global tier")
                    return None
                fact = self.memory.remember_declared_global(
                    ctx, action.text, source_user=p.get("source_user"),
                    tags=tuple(p.get("tags", ())))
            else:
                owner = p.get("owner") or ctx.invoking_user
                if owner is None:
                    self._step(result, action, "blocked", None,
                               "a user-tier fact needs an owner; ambient runs "
                               "must name one explicitly")
                    return None
                fact = self.memory.remember_user_fact(
                    ctx, owner, action.text, tags=tuple(p.get("tags", ())))
            self._step(result, action, "ok",
                       f"user:{fact.owner}" if fact.tier is Tier.USER else None,
                       f"stored {fact.id} in {fact.tier.value} tier",
                       output=fact.id)
        except MemoryError as exc:
            self._step(result, action, "blocked", None, str(exc))
        return None

    def _do_memory_recall(self, ctx, result, action, pre_decision):
        facts = self.memory.recall(ctx, action.text,
                                   limit=action.payload.get("limit", 5))
        out = "; ".join(f"[{f.id} {f.tier.value}] {f.text}" for f in facts)
        self._step(result, action, "ok",
                   f"user:{ctx.invoking_user}" if ctx.invoking_user else None,
                   f"{len(facts)} fact(s) visible", output=out)
        return None

    # -- channels / scheduling / output ---------------------------------------------
    def _do_message(self, ctx, result, action, pre_decision):
        channel_id = action.payload.get("channel") or ctx.channel_id
        if channel_id is None:
            self._step(result, action, "error", None,
                       "message action has no channel")
            return None
        author = f"agent:{ctx.agent}"
        self.workspace.post(channel_id, author, action.text,
                            tick=self.clock.now(), thread_id=ctx.thread_id)
        self.audit.record(
            tick=self.clock.now(), kind="action", agent=ctx.agent,
            run_id=ctx.run_id, acting_identity=author,
            invoking_user=ctx.invoking_user, surface=ctx.surface.value,
            target=f"channel:{channel_id}", decision="n/a",
            detail="message posted")
        self._step(result, action, "ok", author,
                   f"posted to {channel_id}", output=action.text)
        return None

    def _do_schedule_create(self, ctx, result, action, pre_decision):
        if ctx.invoking_user is None:
            self._step(result, action, "blocked", None,
                       "an ambient run cannot register schedules for nobody")
            return None
        p = action.payload
        sched = self.scheduler.add_schedule(
            agent=ctx.agent, task=p["task"],
            every_ticks=p.get("every_ticks"), daily_at=p.get("daily_at"),
            owner=ctx.invoking_user, registered_by=ctx.agent)
        if p.get("max_cost_per_day_usd") is not None:
            sched.max_cost_per_day_usd = p["max_cost_per_day_usd"]
        self._step(result, action, "ok", f"user:{ctx.invoking_user}",
                   f"schedule {sched.id} registered for {ctx.invoking_user}",
                   output=sched.id)
        return None

    def _do_respond(self, ctx, result, action, pre_decision):
        prior = [s.output for s in result.steps if s.output]
        text = action.text or ""
        result.output = (text + "\n" + "\n".join(prior)).strip() \
            if text else "\n".join(prior)
        self._step(result, action, "ok", None, "", output=text)
        return None
