"""Declarations: tools carry identity policy; agents are data.

Following the house pattern from open-managed-agents, nothing in the
runtime hard-codes an agent. A ``ToolSpec`` declares how the tool's
actions are attributed (its ``IdentityMode``), which surfaces it may
be used from, and per-operation policy: whether an operation needs the
invoking user to be whitelisted, and whether it needs a human approval
gate. An ``AgentSpec`` declares which tools an agent may touch, which
provider runs it, its memory rights and its budget. Adding an agent or
a tool is adding a record; policy lives in the declarations, and the
identity broker enforces them uniformly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .models import IdentityMode, Surface


@dataclass(frozen=True)
class OpPolicy:
    whitelist_only: bool = False     # invoking user must be whitelisted for this op
    approval_required: bool = False  # a human must approve before execution


@dataclass(frozen=True)
class ToolSpec:
    name: str
    service: str                              # backing service behind the secret proxy
    identity_mode: IdentityMode
    service_account: str                      # identity used in SERVICE mode
    description: str = ""
    surfaces: tuple[Surface, ...] = (Surface.DM, Surface.CHANNEL,
                                     Surface.THREAD, Surface.AMBIENT)
    operations: tuple[str, ...] = ()
    op_policy: dict = field(default_factory=dict)   # op -> OpPolicy
    whitelist: frozenset = frozenset()              # user ids, for whitelist_only ops

    def policy_for(self, operation: str) -> OpPolicy:
        return self.op_policy.get(operation, OpPolicy())

    def __post_init__(self):
        bad = [op for op in self.op_policy if op not in self.operations]
        if bad:
            raise ValueError(f"tool {self.name}: policy for unknown ops {bad}")


@dataclass(frozen=True)
class AgentSpec:
    name: str
    role: str
    instructions: str
    tools: tuple[str, ...]
    provider: str = "stub-pro"
    can_write_global: bool = False        # may declare facts straight into the global tier
    can_execute_code: bool = False        # may use the sandbox
    max_tokens_per_run: int = 20000       # budget governor cap (tokens)
    max_cost_per_run_usd: float = 1.00    # budget governor cap (USD)

    def __post_init__(self):
        if self.max_tokens_per_run <= 0:
            raise ValueError(f"agent {self.name}: token budget must be positive")


@dataclass
class Registry:
    _tools: dict = field(default_factory=dict)
    _agents: dict = field(default_factory=dict)

    def register_tool(self, spec: ToolSpec) -> ToolSpec:
        if spec.name in self._tools:
            raise ValueError(f"duplicate tool: {spec.name}")
        self._tools[spec.name] = spec
        return spec

    def register_agent(self, spec: AgentSpec) -> AgentSpec:
        if spec.name in self._agents:
            raise ValueError(f"duplicate agent: {spec.name}")
        unknown = [t for t in spec.tools if t not in self._tools]
        if unknown:
            raise ValueError(f"agent {spec.name}: unknown tools {unknown}")
        self._agents[spec.name] = spec
        return spec

    def tool(self, name: str) -> ToolSpec:
        if name not in self._tools:
            raise KeyError(f"unknown tool: {name}")
        return self._tools[name]

    def agent(self, name: str) -> AgentSpec:
        if name not in self._agents:
            raise KeyError(f"unknown agent: {name}")
        return self._agents[name]

    def tool_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._tools))

    def agent_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._agents))
