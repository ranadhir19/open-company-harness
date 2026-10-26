"""Shared vocabulary of the company harness.

Everything in the harness is one of a few shapes: *users* who work in
*surfaces* (a DM, a shared channel, a breakout thread, or no surface at
all for ambient runs), *agents* declared in the registry, and *runs*
that execute actions under a brokered identity. This module holds the
plain data types those shapes share, with no behaviour of its own.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class Surface(enum.Enum):
    """Where a run was invoked from. AMBIENT means no human invoked it:
    the scheduler or an event trigger fired it."""

    DM = "dm"
    CHANNEL = "channel"
    THREAD = "thread"
    AMBIENT = "ambient"


class IdentityMode(enum.Enum):
    """How a tool's actions are attributed, per the identity broker.

    ON_BEHALF_OF - act as the invoking user (their handle, their rights).
    SERVICE      - act as a fixed service identity, whoever invoked it.
    DUAL         - on-behalf-of when a user invokes it, the service
                   identity when the run is ambient.
    """

    ON_BEHALF_OF = "on_behalf_of"
    SERVICE = "service"
    DUAL = "dual"


class Tier(enum.Enum):
    GLOBAL = "global"   # applies to every user
    USER = "user"       # visible only to its owner


@dataclass(frozen=True)
class User:
    id: str
    name: str
    handle: str
    roles: tuple[str, ...] = ()

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.name} (@{self.handle})"


@dataclass
class RunContext:
    """Who is asking, where, and on whose behalf a run executes.

    ``invoking_user`` is None exactly for ambient runs. ``on_behalf_of``
    is set when a user asks an agent to schedule later work for them:
    the ambient run still acts under service identities, but the audit
    trail keeps the human who commissioned it.
    """

    agent: str
    invoking_user: str | None
    surface: Surface
    channel_id: str | None = None
    thread_id: str | None = None
    on_behalf_of: str | None = None
    run_id: str = ""

    @property
    def is_ambient(self) -> bool:
        return self.surface is Surface.AMBIENT


@dataclass
class Action:
    """One step an agent attempts inside a run.

    ``kind`` is one of: tool_call, sandbox_exec, memory_write,
    memory_recall, message, schedule_create, respond. The runtime —
    never the agent — routes each kind through the subsystem that
    enforces its policy (broker, memory store, sandbox, scheduler).
    """

    kind: str
    tool: str | None = None          # for tool_call
    operation: str | None = None     # for tool_call
    payload: dict = field(default_factory=dict)
    text: str = ""                   # instruction / message / fact text


@dataclass
class StepRecord:
    """One executed (or refused) action in a run trace."""

    seq: int
    action: Action
    status: str                      # ok | blocked | error | skipped
    acting_identity: str | None      # identity stamp, None only pre-decision
    detail: str = ""
    output: str = ""
    tokens: int = 0
    cost_usd: float = 0.0


@dataclass
class RunResult:
    run_id: str
    agent: str
    status: str                      # completed | awaiting_approval | budget_exceeded | failed
    steps: list[StepRecord] = field(default_factory=list)
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    pending_approval: dict | None = None   # the action awaiting a human
    output: str = ""

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "agent": self.agent,
            "status": self.status,
            "total_tokens": self.total_tokens,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "steps": [
                {"seq": s.seq, "kind": s.action.kind, "tool": s.action.tool,
                 "operation": s.action.operation, "status": s.status,
                 "acting_identity": s.acting_identity, "detail": s.detail,
                 "output": s.output[:400], "tokens": s.tokens,
                 "cost_usd": round(s.cost_usd, 6)}
                for s in self.steps
            ],
            "output": self.output,
        }
