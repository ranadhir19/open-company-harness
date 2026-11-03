"""The identity broker: who acts, on whose credentials, is policy.

Every tool call in the harness passes through the broker before it
executes. The broker resolves the *acting identity* from the tool's
declared mode and the run context:

- ON_BEHALF_OF tools act as the invoking user — and therefore cannot
  act at all in an ambient run, where there is no invoking user.
- SERVICE tools act as their fixed service account, whoever asked.
- DUAL tools act as the invoking user when a user invoked the run and
  as the service account when the run is ambient.

On top of attribution, the broker enforces surface gating (a tool
declared DM-only is refused in a shared channel — the refusal is the
point: private-capability tools must not leak into public surfaces)
and per-operation whitelists, which gate on the *responsible human*:
the invoking user, or for ambient runs the user who commissioned the
schedule. A blocked call is never silently rerouted to a different
identity; it is refused, and the refusal is audited.

The broker also mints the capability tokens the secret proxy honours:
opaque, scoped to one service and a set of operations, bound to one
run, and short-lived. A token is not a credential — it names no
secret, and the sandbox that holds it can do nothing with it outside
its scope.
"""

from __future__ import annotations

import hashlib
import itertools
from dataclasses import dataclass, field

from .audit import AuditTrail
from .models import IdentityMode, RunContext, Surface
from .registry import ToolSpec


@dataclass
class Decision:
    allowed: bool
    acting_identity: str | None     # "user:<id>" or "service:<account>"
    reason: str
    needs_approval: bool = False


@dataclass
class CapabilityToken:
    token: str
    acting_identity: str
    service: str
    operations: frozenset
    run_id: str
    issued_tick: int
    expires_tick: int

    def expired(self, now_tick: int) -> bool:
        return now_tick > self.expires_tick


@dataclass
class Clock:
    """A deterministic logical clock. The scheduler advances it; token
    expiry and audit ticks read from it. No wall time anywhere, so
    every experiment reproduces exactly."""

    tick: int = 0

    def now(self) -> int:
        return self.tick

    def advance(self, n: int = 1) -> int:
        self.tick += n
        return self.tick


class IdentityBroker:
    def __init__(self, audit: AuditTrail, clock: Clock, salt: str = "och"):
        self._audit = audit
        self._clock = clock
        self._salt = salt
        self._issued: dict[str, CapabilityToken] = {}
        self._counter = itertools.count(1)

    # -- policy decisions ---------------------------------------------------
    def decide(self, ctx: RunContext, tool: ToolSpec,
               operation: str) -> Decision:
        decision = self._evaluate(ctx, tool, operation)
        self._audit.record(
            tick=self._clock.now(), kind="decision", agent=ctx.agent,
            run_id=ctx.run_id, acting_identity=decision.acting_identity,
            invoking_user=ctx.invoking_user, surface=ctx.surface.value,
            target=f"{tool.name}.{operation}",
            decision="allow" if decision.allowed else "block",
            detail=decision.reason)
        return decision

    def _evaluate(self, ctx: RunContext, tool: ToolSpec,
                  operation: str) -> Decision:
        if operation not in tool.operations:
            return Decision(False, None,
                            f"operation {operation!r} not declared on tool {tool.name}")
        if ctx.surface not in tool.surfaces:
            return Decision(False, self._attempting_identity(ctx, tool),
                            f"tool {tool.name} is not available on surface "
                            f"{ctx.surface.value} (declared: "
                            f"{', '.join(s.value for s in tool.surfaces)})")
        acting = self._resolve_identity(ctx, tool)
        if acting is None:
            return Decision(False, None,
                            f"tool {tool.name} acts on-behalf-of a user but "
                            f"this run is ambient — nobody to act for")
        policy = tool.policy_for(operation)
        if policy.whitelist_only:
            responsible = ctx.invoking_user or ctx.on_behalf_of
            if responsible is None:
                return Decision(False, acting,
                                f"{tool.name}.{operation} is whitelist-only and "
                                f"this run has no responsible human")
            if responsible not in tool.whitelist:
                return Decision(False, acting,
                                f"{tool.name}.{operation} is whitelist-only; "
                                f"{responsible} is not on the whitelist")
        return Decision(True, acting, "policy permits",
                        needs_approval=policy.approval_required)

    def _resolve_identity(self, ctx: RunContext, tool: ToolSpec) -> str | None:
        mode = tool.identity_mode
        if mode is IdentityMode.SERVICE:
            return f"service:{tool.service_account}"
        if mode is IdentityMode.ON_BEHALF_OF:
            return f"user:{ctx.invoking_user}" if ctx.invoking_user else None
        # DUAL
        if ctx.invoking_user:
            return f"user:{ctx.invoking_user}"
        return f"service:{tool.service_account}"

    def _attempting_identity(self, ctx: RunContext,
                             tool: ToolSpec) -> str | None:
        """Who tried, for stamping a refusal (never used to execute)."""
        if ctx.invoking_user:
            return f"user:{ctx.invoking_user}"
        if tool.identity_mode in (IdentityMode.SERVICE, IdentityMode.DUAL):
            return f"service:{tool.service_account}"
        return None

    # -- capability tokens ----------------------------------------------------
    def mint_capability(self, ctx: RunContext, decision: Decision,
                        tool: ToolSpec, operations: tuple[str, ...],
                        ttl_ticks: int = 50) -> CapabilityToken:
        if not decision.allowed or decision.acting_identity is None:
            raise ValueError("cannot mint a capability for a refused decision")
        n = next(self._counter)
        raw = (f"{self._salt}|{n}|{decision.acting_identity}|{tool.service}|"
               f"{','.join(sorted(operations))}|{ctx.run_id}")
        token_str = "cap_" + hashlib.sha256(raw.encode()).hexdigest()[:24]
        token = CapabilityToken(
            token=token_str, acting_identity=decision.acting_identity,
            service=tool.service, operations=frozenset(operations),
            run_id=ctx.run_id, issued_tick=self._clock.now(),
            expires_tick=self._clock.now() + ttl_ticks)
        self._issued[token_str] = token
        return token

    def token_identity(self, token_str: str) -> str | None:
        """The acting identity a token was minted for (None if the
        token was never issued here). The proxy stamps its audit
        entries from this — never from caller claims."""
        token = self._issued.get(token_str)
        return token.acting_identity if token else None

    def validate_capability(self, token_str: str, *, service: str,
                            operation: str, run_id: str) -> tuple[bool, str]:
        """The secret proxy's check. Scope, service, run binding and
        expiry are all verified; any mismatch is a refusal with a
        reason (the proxy logs it)."""
        token = self._issued.get(token_str)
        if token is None:
            return False, "unknown or forged capability token"
        if token.expired(self._clock.now()):
            return False, "capability token expired"
        if token.service != service:
            return False, f"token is scoped to service {token.service}, not {service}"
        if operation not in token.operations:
            return False, f"operation {operation} outside token scope {sorted(token.operations)}"
        if token.run_id != run_id:
            return False, "capability token is bound to a different run (replay)"
        return True, "ok"
