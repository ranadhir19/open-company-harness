"""Model providers: the model-agnostic seam of the harness.

A *provider* turns an agent + task into a plan (a list of actions).
The runtime executes plans; it never names a model vendor. Two
families ship here, mirroring the sibling platform:

- ``ScriptedProvider`` — deterministic, offline, seeded. Plans come
  from the scenario's declared task templates, so every run and every
  experiment reproduces with no API keys. Two capability tiers:
  ``stub-basic`` plans skip memory recall and intermediate messages
  (a measurably thinner run), ``stub-pro`` plans use the full
  template. Token counts are a deterministic function of the plan, so
  cost accounting reproduces exactly.

- ``OpenAIProvider`` / ``AnthropicProvider`` — thin REST adapters used
  only when the matching API key is in the environment. They run in
  *direct-completion* mode: the plan is a single ``respond`` action
  carrying the model's completion. They do not drive the harness's
  tool policy — brokered tool use is a scripted-provider behaviour in
  this build (a stated limitation, as in the sibling platform). They
  are never constructed by the demo, tests or experiments unless a key
  is present.
"""

from __future__ import annotations

import json
import os
import urllib.request
from dataclasses import dataclass, field
from typing import Callable

from .models import Action, RunContext
from .registry import AgentSpec


class ProviderError(Exception):
    """A provider failed to plan (retryable once by the runtime)."""


# Deterministic per-action token estimates, used both for the budget
# governor's pre-step reservation and for cost accounting.
ACTION_TOKENS = {"tool_call": 240, "sandbox_exec": 600,
                 "memory_write": 120, "memory_recall": 180,
                 "message": 60, "schedule_create": 90, "respond": 300}


def action_tokens(action: Action) -> int:
    base = ACTION_TOKENS.get(action.kind, 100)
    text_len = len(action.text) + sum(len(str(v)) for v in action.payload.values())
    return base + text_len // 4


@dataclass
class ScriptedProvider:
    """Deterministic offline provider over declared task templates.

    ``task_plans`` maps a task id to a function of the run parameters
    returning the plan. ``fail_tasks`` / ``flaky`` inject planning
    failures for the recovery tests, as in the sibling platform.
    """

    tier: str = "stub-pro"
    task_plans: dict = field(default_factory=dict)
    fail_tasks: frozenset = frozenset()
    flaky: dict = field(default_factory=dict)

    # price per 1K tokens (USD): stub-basic mimics a small hosted
    # model, stub-pro a frontier model (as in open-managed-agents).
    PRICES = {"stub-basic": (0.0002, 0.0004), "stub-pro": (0.003, 0.015)}

    def __post_init__(self):
        if self.tier not in self.PRICES:
            raise ValueError(f"unknown tier: {self.tier}")
        self.name = self.tier
        self.price_per_token = self.PRICES[self.tier][1] / 1000.0
        self._flaky = dict(self.flaky)

    def plan(self, agent: AgentSpec, task: str, ctx: RunContext,
             params: dict) -> list[Action]:
        if task in self.fail_tasks:
            raise ProviderError(f"planner down for task {task} (injected)")
        left = self._flaky.get(task, 0)
        if left > 0:
            self._flaky[task] = left - 1
            raise ProviderError(f"planner transient failure for {task} (injected)")
        builder: Callable[[dict], list[Action]] | None = self.task_plans.get(task)
        if builder is None:
            raise ProviderError(f"no task template named {task!r}")
        actions = builder(params)
        if self.tier == "stub-basic":
            # The basic tier plans thin: it never recalls memory and
            # posts no intermediate messages. Same task, less context.
            actions = [a for a in actions
                       if a.kind not in ("memory_recall", "message")]
        return actions

    def estimate_tokens(self, action: Action) -> int:
        return action_tokens(action)

    def cost(self, tokens: int) -> float:
        return tokens * self.price_per_token


class _RESTProvider:  # pragma: no cover - key-gated
    """Base for hosted-model adapters. Never constructed without a key."""

    name = "rest"

    def __init__(self, model: str, api_key: str):
        self.model = model
        self._key = api_key
        self.price_per_token = 0.0

    def estimate_tokens(self, action: Action) -> int:
        return action_tokens(action)

    def cost(self, tokens: int) -> float:
        return tokens * self.price_per_token

    def _post(self, url: str, payload: dict, headers: dict) -> dict:
        req = urllib.request.Request(
            url, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", **headers})
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode())


class OpenAIProvider(_RESTProvider):  # pragma: no cover - key-gated
    name = "openai"

    def __init__(self, model: str = "gpt-4o-mini", api_key: str | None = None):
        key = api_key or os.environ.get("OPENAI_API_KEY")
        if not key:
            raise ProviderError("OPENAI_API_KEY is not set")
        super().__init__(model, key)
        self.price_per_token = 0.0006 / 1000.0

    def plan(self, agent: AgentSpec, task: str, ctx: RunContext,
             params: dict) -> list[Action]:
        out = self._post(
            "https://api.openai.com/v1/chat/completions",
            {"model": self.model,
             "messages": [{"role": "user",
                           "content": f"You are {agent.name}, {agent.role}. "
                                      f"{agent.instructions}\nTask: {task} "
                                      f"with parameters {params}. Respond."}]},
            {"Authorization": f"Bearer {self._key}"})
        return [Action(kind="respond",
                       text=out["choices"][0]["message"]["content"])]


class AnthropicProvider(_RESTProvider):  # pragma: no cover - key-gated
    name = "anthropic"

    def __init__(self, model: str = "claude-sonnet-4-5",
                 api_key: str | None = None):
        key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not key:
            raise ProviderError("ANTHROPIC_API_KEY is not set")
        super().__init__(model, key)
        self.price_per_token = 0.015 / 1000.0

    def plan(self, agent: AgentSpec, task: str, ctx: RunContext,
             params: dict) -> list[Action]:
        out = self._post(
            "https://api.anthropic.com/v1/messages",
            {"model": self.model, "max_tokens": 1024,
             "messages": [{"role": "user",
                           "content": f"You are {agent.name}, {agent.role}. "
                                      f"{agent.instructions}\nTask: {task} "
                                      f"with parameters {params}. Respond."}]},
            {"x-api-key": self._key, "anthropic-version": "2023-06-01"})
        text = "".join(b.get("text", "") for b in out.get("content", []))
        return [Action(kind="respond", text=text)]


def provider_from_env(name: str, task_plans: dict | None = None):
    """Build a provider by name; scripted tiers need no key."""
    if name in ScriptedProvider.PRICES:
        return ScriptedProvider(name, task_plans=task_plans or {})
    if name == "openai":  # pragma: no cover - key-gated
        return OpenAIProvider()
    if name == "anthropic":  # pragma: no cover - key-gated
        return AnthropicProvider()
    raise ValueError(f"unknown provider: {name}")
