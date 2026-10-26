"""The ambient scheduler: most runs are not invoked by anyone.

A harness that only answers when spoken to misses the point of the
form: the valuable runs are the ones nobody remembered to ask for —
the PR that gets reviewed because it was opened, the account brief
that lands every morning. This scheduler fires agent runs from two
kinds of triggers:

- **Schedules** — simple cron-style declarations on the harness's
  deterministic logical clock (`every N ticks`, or `daily at tick T`
  of each 100-tick day). A schedule is registered either by a
  developer (system schedules) or by an agent on a user's behalf
  during an invoked run; in the latter case the registering user is
  recorded as the schedule's owner and stays the *responsible human*
  for whitelist purposes on every ambient run it causes.
- **Events** — `emit("pull_request.opened", {...})` fires every
  trigger bound to that event type.

Ambient runs execute under service identities (there is no invoking
user — the identity broker enforces exactly that), are budgeted like
invoked runs (per-run caps from the agent declaration, plus an
optional per-schedule tick-window spend cap), and are audited with
their trigger and commissioning owner.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Callable

from .audit import AuditTrail
from .identity import Clock
from .models import RunContext, Surface

DAY_TICKS = 100   # one simulated day, for "daily at tick T" schedules


@dataclass
class Schedule:
    id: str
    agent: str
    task: str                       # task template id the provider plans from
    every_ticks: int | None = None  # interval schedules
    daily_at: int | None = None     # tick-of-day for daily schedules
    owner: str | None = None        # responsible human (None = system)
    registered_by: str = "system"   # "system" or an agent name
    next_fire: int = 0
    enabled: bool = True
    max_cost_per_day_usd: float | None = None   # ambient spend cap

    def due(self, now: int) -> bool:
        return self.enabled and now >= self.next_fire

    def advance(self, now: int):
        if self.every_ticks:
            self.next_fire = now + self.every_ticks
        elif self.daily_at is not None:
            today = (now // DAY_TICKS) * DAY_TICKS + self.daily_at
            self.next_fire = today if today > now else today + DAY_TICKS


@dataclass
class EventTrigger:
    id: str
    event_type: str
    agent: str
    task: str
    owner: str | None = None


@dataclass
class AmbientScheduler:
    clock: Clock
    audit: AuditTrail
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    schedules: list[Schedule] = field(default_factory=list)
    triggers: list[EventTrigger] = field(default_factory=list)

    def _new_id(self, prefix: str) -> str:
        return f"{prefix}-{next(self._ids):03d}"

    # -- registration ---------------------------------------------------------
    def add_schedule(self, *, agent: str, task: str,
                     every_ticks: int | None = None,
                     daily_at: int | None = None,
                     owner: str | None = None,
                     registered_by: str = "system") -> Schedule:
        if (every_ticks is None) == (daily_at is None):
            raise ValueError("a schedule needs exactly one of every_ticks / daily_at")
        sched = Schedule(id=self._new_id("sched"), agent=agent, task=task,
                         every_ticks=every_ticks, daily_at=daily_at,
                         owner=owner, registered_by=registered_by,
                         next_fire=self.clock.now())
        if daily_at is not None:
            # first fire is the next occurrence of the tick-of-day
            sched.advance(self.clock.now())
        self.schedules.append(sched)
        self.audit.record(
            tick=self.clock.now(), kind="schedule", agent=agent, run_id="",
            acting_identity=None, invoking_user=owner, surface="ambient",
            target=sched.id, decision="n/a",
            detail=f"schedule registered by {registered_by} for task {task!r}")
        return sched

    def add_event_trigger(self, *, event_type: str, agent: str, task: str,
                          owner: str | None = None) -> EventTrigger:
        trig = EventTrigger(id=self._new_id("trig"), event_type=event_type,
                            agent=agent, task=task, owner=owner)
        self.triggers.append(trig)
        self.audit.record(
            tick=self.clock.now(), kind="schedule", agent=agent, run_id="",
            acting_identity=None, invoking_user=owner, surface="ambient",
            target=trig.id, decision="n/a",
            detail=f"event trigger on {event_type!r} for task {task!r}")
        return trig

    # -- firing -----------------------------------------------------------------
    def due_contexts(self, now: int) -> list[tuple[Schedule, RunContext]]:
        out = []
        for sched in self.schedules:
            if sched.due(now):
                sched.advance(now)
                out.append((sched, RunContext(
                    agent=sched.agent, invoking_user=None,
                    surface=Surface.AMBIENT, on_behalf_of=sched.owner)))
        return out

    def event_contexts(self, event_type: str,
                       payload: dict) -> list[tuple[EventTrigger, RunContext]]:
        # payload is carried for the runtime's task parameters; the
        # contexts themselves need only the trigger's identity shape.
        _ = payload
        return [(trig, RunContext(agent=trig.agent, invoking_user=None,
                                  surface=Surface.AMBIENT,
                                  on_behalf_of=trig.owner))
                for trig in self.triggers if trig.event_type == event_type]
