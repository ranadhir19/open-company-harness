"""open-company-harness: an open, model-agnostic company harness.

Declared agents that live where people work (channel layer), act
under a brokered identity policy (identity broker), remember in two
tiers with a hard wall between users (memory), execute code behind a
secret proxy (sandbox), and run ambiently on schedules and events
(scheduler) — with run traces, budget governance and approval gates
throughout (runtime).
"""

from .audit import AuditTrail
from .channels import Channel, Workspace
from .identity import CapabilityToken, Clock, Decision, IdentityBroker
from .memory import Fact, MemoryStore
from .models import (Action, IdentityMode, RunContext, RunResult, Surface,
                     Tier, User)
from .providers import ProviderError, ScriptedProvider, provider_from_env
from .registry import AgentSpec, OpPolicy, Registry, ToolSpec
from .runtime import Harness
from .sandbox import (DeterministicSandbox, SecretProxy, SecretVault,
                      SubprocessSandbox)
from .scheduler import AmbientScheduler, EventTrigger, Schedule
from .scenario import TASK_PLANS, build_harness, build_registry

__version__ = "0.1.0"

__all__ = [
    "Action", "AgentSpec", "AmbientScheduler", "AuditTrail",
    "CapabilityToken", "Channel", "Clock", "Decision",
    "DeterministicSandbox", "EventTrigger", "Fact", "Harness",
    "IdentityBroker", "IdentityMode", "MemoryStore", "OpPolicy",
    "ProviderError", "Registry", "RunContext", "RunResult", "Schedule",
    "ScriptedProvider", "SecretProxy", "SecretVault", "SubprocessSandbox",
    "Surface", "TASK_PLANS", "Tier", "ToolSpec", "User", "Workspace",
    "build_harness", "build_registry", "provider_from_env",
]
