"""Demo backend for open-company-harness: a simulated company workspace.

FastAPI app over one module-level seeded harness for the fictional
firm **Meridian Labs**. REST serves the React demo UI; a compact
Strawberry GraphQL schema is mounted at ``/graphql`` and mirrors the
same data through the same helper functions.

The harness is seeded exactly as in ``examples/quickstart.py``, plus
two ambient registrations so the demo shows runs nobody invoked:

- an event trigger: ``pull_request.opened`` -> reviewer / review_pr
- a system schedule: pipeline / daily_digest every 30 ticks

Imports use plain ``from open_company_harness import ...`` so the app
works with the package installed (``pip install ".[demo]"``) or with
``PYTHONPATH=src``.
"""

from __future__ import annotations

from typing import Any, Optional

import strawberry
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from strawberry.fastapi import GraphQLRouter

from open_company_harness import TASK_PLANS, Surface, build_harness

# ---------------------------------------------------------------------------
# Seeded harness (module level: one workspace per process)
# ---------------------------------------------------------------------------

harness = build_harness()

harness.scheduler.add_event_trigger(
    event_type="pull_request.opened", agent="reviewer", task="review_pr")
harness.scheduler.add_schedule(
    agent="pipeline", task="daily_digest", every_ticks=30)

# A little fictional life in the channels so the transcript is not empty.
_ws = harness.workspace
_ws.post("eng", "ana", "Morning — PR-102 is ready for a look when anyone has a moment.",
         tick=harness.clock.now())
_ws.post("gtm", "cy", "Acme Corp asked for a follow-up on the evaluation.",
         tick=harness.clock.now())
_ws.post("studio", "dee", "Drafting the launch announcement in #studio today.",
         tick=harness.clock.now())

USERS = ("ana", "ben", "cy", "dee")

# Every RunResult produced this session (invoked, approved, ambient),
# as plain dicts ready to serve. Replaced in place on approval resume.
RUNS: list[dict[str, Any]] = []


def _record(result) -> dict[str, Any]:
    data = result.to_dict()
    if result.pending_approval:
        # RunResult.to_dict() omits the gate; expose its target so the
        # UI can offer an Approve button. The broker Decision object
        # itself is not serialisable and is never sent.
        data["pending_approval"] = {
            "target": result.pending_approval.get("target", "")}
    else:
        data["pending_approval"] = None
    for i, existing in enumerate(RUNS):
        if existing["run_id"] == data["run_id"]:
            RUNS[i] = data
            return data
    RUNS.append(data)
    return data


# ---------------------------------------------------------------------------
# Shared helpers (REST and GraphQL both serve from these)
# ---------------------------------------------------------------------------

def _state_dict() -> dict[str, Any]:
    reg = harness.registry
    tools = []
    for name in reg.tool_names():
        t = reg.tool(name)
        tools.append({
            "name": t.name,
            "service": t.service,
            "identity_mode": t.identity_mode.value,
            "service_account": t.service_account,
            "description": t.description,
            "surfaces": [s.value for s in t.surfaces],
            "operations": list(t.operations),
            "whitelist": sorted(t.whitelist),
        })
    agents = []
    for name in reg.agent_names():
        a = reg.agent(name)
        agents.append({
            "name": a.name,
            "role": a.role,
            "instructions": a.instructions,
            "tools": list(a.tools),
            "provider": a.provider,
            "can_execute_code": a.can_execute_code,
            "can_write_global": a.can_write_global,
            "max_tokens_per_run": a.max_tokens_per_run,
            "max_cost_per_run_usd": a.max_cost_per_run_usd,
        })
    return {
        "company": "Meridian Labs",
        "tick": harness.clock.now(),
        "users": [{"id": u} for u in USERS],
        "channels": [
            {"id": c.id, "name": c.name, "kind": c.kind,
             "members": list(c.members)}
            for c in harness.workspace.channels()
        ],
        "agents": agents,
        "tools": tools,
        "tasks": sorted(TASK_PLANS),
    }


def _message_dict(m) -> dict[str, Any]:
    return {"id": m.id, "channel_id": m.channel_id, "author": m.author,
            "text": m.text, "tick": m.tick, "thread_id": m.thread_id,
            "mentions": list(m.mentions)}


def _audit_dicts() -> list[dict[str, Any]]:
    return [e.to_dict() for e in harness.audit.entries]


def _memory_dicts(user: Optional[str]) -> list[dict[str, Any]]:
    if user is None:
        facts = harness.memory.all_facts()
    else:
        facts = harness.memory.visible_to(user)
    return [f.to_dict() for f in facts]


def _runs_dicts() -> list[dict[str, Any]]:
    return list(RUNS)


def _parse_surface(value: Optional[str]) -> Surface:
    if not value:
        return Surface.CHANNEL
    try:
        return Surface(value.strip().lower())
    except ValueError:
        raise HTTPException(
            400, f"unknown surface {value!r}; expected one of "
                 f"{[s.value for s in Surface]}")


# ---------------------------------------------------------------------------
# REST
# ---------------------------------------------------------------------------

class PostIn(BaseModel):
    channel_id: str
    user: str
    text: str


class InvokeIn(BaseModel):
    agent: str
    task: str
    user: Optional[str] = None
    surface: Optional[str] = None
    channel_id: Optional[str] = None
    params: dict[str, Any] = Field(default_factory=dict)


class ApproveIn(BaseModel):
    run_id: str
    approver: str


class TickIn(BaseModel):
    ticks: int = 1


class EventIn(BaseModel):
    event_type: str
    pr: Optional[str] = None


app = FastAPI(title="open-company-harness demo", version="0.1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/state")
def state() -> dict[str, Any]:
    return _state_dict()


@app.get("/api/messages")
def messages(channel: str = Query(...),
             thread: Optional[str] = None,
             thread_id: Optional[str] = None) -> list[dict[str, Any]]:
    """Workspace transcript for a channel. With a ``thread`` /
    ``thread_id`` filter, only that thread's messages; without one,
    every message posted in the channel."""
    try:
        harness.workspace.channel(channel)
    except KeyError:
        raise HTTPException(404, f"unknown channel: {channel}")
    thread_filter = thread_id if thread_id is not None else thread
    if thread_filter is not None:
        msgs = harness.workspace.transcript(channel, thread_id=thread_filter)
    else:
        msgs = [m for m in harness.workspace.adapter.messages
                if m.channel_id == channel]
    return [_message_dict(m) for m in msgs]


@app.post("/api/post")
def post(body: PostIn) -> dict[str, Any]:
    """Post a human message. Workspace only — this never invokes an
    agent; agent runs go through /api/invoke."""
    try:
        harness.workspace.channel(body.channel_id)
    except KeyError:
        raise HTTPException(404, f"unknown channel: {body.channel_id}")
    msg = harness.workspace.post(body.channel_id, body.user, body.text,
                                  tick=harness.clock.now())
    return _message_dict(msg)


@app.post("/api/invoke")
def invoke(body: InvokeIn) -> dict[str, Any]:
    surface = _parse_surface(body.surface)
    user = body.user or None
    if surface is Surface.AMBIENT:
        user = None
    try:
        result = harness.invoke(
            body.agent, body.task, user=user, surface=surface,
            channel_id=body.channel_id or None, params=body.params or {})
    except KeyError as exc:
        raise HTTPException(404, str(exc))
    except Exception as exc:  # provider / plan errors surface as 400
        raise HTTPException(400, str(exc))
    return _record(result)


@app.post("/api/approve")
def approve(body: ApproveIn) -> dict[str, Any]:
    try:
        result = harness.approve(body.run_id, body.approver)
    except KeyError:
        raise HTTPException(404, f"no paused run: {body.run_id}")
    return _record(result)


@app.post("/api/tick")
def tick(body: TickIn) -> list[dict[str, Any]]:
    """Advance the logical clock, then fire every schedule that is due.
    Returns the ambient RunResults those schedules produced."""
    if body.ticks < 0:
        raise HTTPException(400, "ticks must be >= 0")
    harness.clock.advance(body.ticks)
    return [_record(r) for r in harness.run_due()]


@app.post("/api/event")
def event(body: EventIn) -> list[dict[str, Any]]:
    payload = {"pr": body.pr} if body.pr else {}
    return [_record(r) for r in harness.emit_event(body.event_type, payload)]


@app.get("/api/runs")
def runs() -> list[dict[str, Any]]:
    return _runs_dicts()


@app.get("/api/audit")
def audit() -> list[dict[str, Any]]:
    return _audit_dicts()


@app.get("/api/memory")
def memory(user: Optional[str] = None) -> list[dict[str, Any]]:
    """Facts visible to ``user`` (their own tier + the global tier).
    Without a user, every fact in the store."""
    return _memory_dicts(user)


# ---------------------------------------------------------------------------
# GraphQL (Strawberry, mounted at /graphql) — mirrors the REST data
# through the same helpers above.
# ---------------------------------------------------------------------------

@strawberry.type
class UserType:
    id: str


@strawberry.type
class ChannelType:
    id: str
    name: str
    kind: str
    members: list[str]


@strawberry.type
class AgentType:
    name: str
    role: str
    provider: str
    tools: list[str]


@strawberry.type
class ToolType:
    name: str
    service: str
    identity_mode: str
    service_account: str
    surfaces: list[str]
    operations: list[str]


@strawberry.type
class StateType:
    company: str
    tick: int
    users: list[UserType]
    channels: list[ChannelType]
    agents: list[AgentType]
    tools: list[ToolType]
    tasks: list[str]


@strawberry.type
class AuditEntryType:
    seq: int
    tick: int
    kind: str
    agent: str
    run_id: str
    acting_identity: Optional[str]
    invoking_user: Optional[str]
    surface: str
    target: str
    decision: str
    detail: str


@strawberry.type
class ProvenanceType:
    source_user: Optional[str]
    source_agent: str
    run_id: str
    surface: str
    tick: int
    route: str


@strawberry.type
class FactType:
    id: str
    text: str
    tier: str
    owner: Optional[str]
    tags: list[str]
    provenance: ProvenanceType


@strawberry.type
class StepType:
    seq: int
    kind: str
    tool: Optional[str]
    operation: Optional[str]
    status: str
    acting_identity: Optional[str]
    detail: str
    output: str
    tokens: int
    cost_usd: float


@strawberry.type
class RunType:
    run_id: str
    agent: str
    status: str
    total_tokens: int
    total_cost_usd: float
    output: str
    pending_target: Optional[str]
    steps: list[StepType]


def _state_type() -> StateType:
    d = _state_dict()
    return StateType(
        company=d["company"], tick=d["tick"],
        users=[UserType(id=u["id"]) for u in d["users"]],
        channels=[ChannelType(id=c["id"], name=c["name"], kind=c["kind"],
                              members=c["members"]) for c in d["channels"]],
        agents=[AgentType(name=a["name"], role=a["role"],
                          provider=a["provider"], tools=a["tools"])
                for a in d["agents"]],
        tools=[ToolType(name=t["name"], service=t["service"],
                        identity_mode=t["identity_mode"],
                        service_account=t["service_account"],
                        surfaces=t["surfaces"], operations=t["operations"])
               for t in d["tools"]],
        tasks=d["tasks"])


def _run_type(d: dict[str, Any]) -> RunType:
    pending = d.get("pending_approval") or {}
    return RunType(
        run_id=d["run_id"], agent=d["agent"], status=d["status"],
        total_tokens=d["total_tokens"], total_cost_usd=d["total_cost_usd"],
        output=d["output"], pending_target=pending.get("target"),
        steps=[StepType(seq=s["seq"], kind=s["kind"], tool=s["tool"],
                        operation=s["operation"], status=s["status"],
                        acting_identity=s["acting_identity"],
                        detail=s["detail"], output=s["output"],
                        tokens=s["tokens"], cost_usd=s["cost_usd"])
               for s in d["steps"]])


@strawberry.type
class Query:
    @strawberry.field
    def state(self) -> StateType:
        return _state_type()

    @strawberry.field
    def audit_entries(self) -> list[AuditEntryType]:
        return [AuditEntryType(**e) for e in _audit_dicts()]

    @strawberry.field
    def memory_facts(self, user: Optional[str] = None) -> list[FactType]:
        out = []
        for f in _memory_dicts(user):
            p = f["provenance"]
            out.append(FactType(
                id=f["id"], text=f["text"], tier=f["tier"],
                owner=f["owner"], tags=f["tags"],
                provenance=ProvenanceType(
                    source_user=p["source_user"],
                    source_agent=p["source_agent"], run_id=p["run_id"],
                    surface=p["surface"], tick=p["tick"], route=p["route"])))
        return out

    @strawberry.field
    def runs(self) -> list[RunType]:
        return [_run_type(d) for d in _runs_dicts()]


schema = strawberry.Schema(query=Query)
app.include_router(GraphQLRouter(schema), prefix="/graphql")
