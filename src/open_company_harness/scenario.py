"""The seeded company: declarations and task templates.

A fictional firm, **Meridian Labs**, with four people, three agents
and five tools — the same shape as the three internal agents in the
LangChain post that framed this project (an engineering agent, a
go-to-market agent, a content agent), rebuilt generically:

- ``reviewer`` — engineering agent over the code host. Its tool is
  DUAL: invoked by a person it acts as that person; ambient (a PR was
  opened) it acts as the ``codehost-bot`` service identity. Publishing
  a review comment is approval-gated.
- ``pipeline`` — go-to-market agent. The CRM tool is a fixed SERVICE
  identity (nobody logs in per-person); the mail tool is
  on-behalf-of and **DM-only**, exactly the blog's rule that
  personal-capability tools disappear in shared channels.
- ``studio`` — content agent. Docs are on-behalf-of; publishing a page
  or a social post is whitelist-only (Dee, the content lead), while
  drafting is open — the blog's read/draft-for-all, publish-for-some.

Task templates are data: a task id maps to a function of the run
parameters returning the plan. The scripted provider executes them;
the same plan in a different context (DM vs channel, invoked vs
ambient) is supposed to produce different allow/block outcomes — that
difference is what the experiments measure.
"""

from __future__ import annotations

from .channels import Channel
from .models import Action, RunContext, Surface
from .providers import ScriptedProvider
from .registry import AgentSpec, OpPolicy, Registry, ToolSpec
from .models import IdentityMode
from .runtime import Harness

USERS = ("ana", "ben", "cy", "dee")
PUBLISHERS = frozenset({"dee"})


def build_registry() -> Registry:
    reg = Registry()
    reg.register_tool(ToolSpec(
        name="codehost", service="codehost",
        identity_mode=IdentityMode.DUAL, service_account="codehost-bot",
        description="Code hosting: read PRs, draft and publish review comments.",
        operations=("list_prs", "read_pr", "draft_comment", "publish_comment"),
        op_policy={"publish_comment": OpPolicy(approval_required=True)}))
    reg.register_tool(ToolSpec(
        name="crm", service="crm",
        identity_mode=IdentityMode.SERVICE, service_account="crm-sync",
        description="Customer accounts under a fixed sync identity.",
        operations=("list_accounts", "lookup_account", "update_stage")))
    reg.register_tool(ToolSpec(
        name="mail", service="mail",
        identity_mode=IdentityMode.ON_BEHALF_OF, service_account="mail-obo",
        description="Send mail as the invoking user. DM-only: personal "
                    "capability, never available in shared channels.",
        surfaces=(Surface.DM,),
        operations=("draft_email", "send_email", "list_outbox")))
    reg.register_tool(ToolSpec(
        name="docs", service="docs",
        identity_mode=IdentityMode.ON_BEHALF_OF, service_account="docs-obo",
        description="Documents as the invoking user; publishing is "
                    "restricted to the content lead.",
        operations=("read_page", "draft_page", "publish_page"),
        op_policy={"publish_page": OpPolicy(whitelist_only=True)},
        whitelist=PUBLISHERS))
    reg.register_tool(ToolSpec(
        name="social", service="social",
        identity_mode=IdentityMode.SERVICE, service_account="brand-voice",
        description="The company social voice. Anyone may draft; only "
                    "whitelisted humans may publish.",
        operations=("draft_post", "publish_post", "list_posts"),
        op_policy={"publish_post": OpPolicy(whitelist_only=True)},
        whitelist=PUBLISHERS))

    reg.register_agent(AgentSpec(
        name="reviewer", role="engineering agent",
        instructions="Review pull requests: read them, check them "
                     "against the team's conventions, draft comments; "
                     "publish only with human approval.",
        tools=("codehost",), can_execute_code=True))
    reg.register_agent(AgentSpec(
        name="pipeline", role="go-to-market agent",
        instructions="Keep account knowledge current and run follow-up "
                     "motions; mail only ever as the user, in DMs.",
        tools=("crm", "mail")))
    reg.register_agent(AgentSpec(
        name="studio", role="content agent",
        instructions="Draft and publish content in the company voice; "
                     "may state org-wide facts into global memory.",
        tools=("docs", "social"), can_execute_code=True,
        can_write_global=True))
    return reg


# ---------------------------------------------------------------------------
# Task templates
# ---------------------------------------------------------------------------

def _review_pr(p: dict) -> list[Action]:
    pr = p.get("pr", "PR-101")
    return [
        Action(kind="memory_recall", text="review conventions retry budget"),
        Action(kind="tool_call", tool="codehost", operation="read_pr",
               payload={"pr": pr}),
        Action(kind="tool_call", tool="codehost", operation="draft_comment",
               payload={"pr": pr,
                        "text": "Review drafted: checked against the retry-"
                                "budget convention; no blocking issues."}),
        Action(kind="respond", text=f"Review of {pr} drafted."),
    ]


def _publish_review(p: dict) -> list[Action]:
    pr = p.get("pr", "PR-101")
    return [
        Action(kind="tool_call", tool="codehost", operation="read_pr",
               payload={"pr": pr}),
        Action(kind="tool_call", tool="codehost", operation="publish_comment",
               payload={"pr": pr,
                        "text": "Review published: no blocking issues."}),
        Action(kind="respond", text=f"Review of {pr} published."),
    ]


def _account_brief(p: dict) -> list[Action]:
    account = p.get("account", "Acme Corp")
    return [
        Action(kind="tool_call", tool="crm", operation="lookup_account",
               payload={"account": account}),
        Action(kind="memory_recall", text=f"{account} notes renewal"),
        Action(kind="respond", text=f"Brief for {account}."),
    ]


def _update_stage(p: dict) -> list[Action]:
    return [
        Action(kind="tool_call", tool="crm", operation="update_stage",
               payload={"account": p.get("account", "Globex"),
                        "stage": p.get("stage", "negotiation")}),
        Action(kind="respond", text="Stage updated."),
    ]


def _followup_email(p: dict) -> list[Action]:
    return [
        Action(kind="tool_call", tool="mail", operation="draft_email",
               payload={"to": p.get("to", "sam@acme.example"),
                        "subject": "Follow-up",
                        "body": "Checking in on the evaluation."}),
        Action(kind="tool_call", tool="mail", operation="send_email",
               payload={"to": p.get("to", "sam@acme.example"),
                        "subject": "Follow-up",
                        "body": "Checking in on the evaluation."}),
        Action(kind="respond", text="Follow-up sent."),
    ]


def _draft_announcement(p: dict) -> list[Action]:
    page = p.get("page", "launch-notes")
    return [
        Action(kind="tool_call", tool="docs", operation="read_page",
               payload={"page": page}),
        Action(kind="tool_call", tool="social", operation="draft_post",
               payload={"text": "Draft announcement for the launch."}),
        Action(kind="respond", text="Announcement drafted."),
    ]


def _publish_announcement(p: dict) -> list[Action]:
    return [
        Action(kind="tool_call", tool="social", operation="publish_post",
               payload={"text": p.get("text", "We shipped the new harness.")}),
        Action(kind="respond", text="Announcement published."),
    ]


def _publish_page(p: dict) -> list[Action]:
    return [
        Action(kind="tool_call", tool="docs", operation="publish_page",
               payload={"page": p.get("page", "launch-notes")}),
        Action(kind="respond", text="Page published."),
    ]


def _exec_review_stats(p: dict) -> list[Action]:
    pr = p.get("pr", "PR-101")
    code = (
        "import os\n"
        "print('env keys:', sorted(os.environ.keys()))\n"
        f"out = call_service('codehost', 'read_pr', pr='{pr}')\n"
        "print('service said:', out)\n"
    )
    return [
        Action(kind="sandbox_exec",
               payload={"engine": "subprocess", "services": ["codehost"],
                        "code": code}),
        Action(kind="respond", text="Review stats computed in the sandbox."),
    ]


def _exec_digest(p: dict) -> list[Action]:
    return [
        Action(kind="sandbox_exec",
               payload={"engine": "deterministic", "services": ["social", "docs"],
                        "steps": [
                            {"show_env": True},
                            {"call": {"service": "social",
                                      "operation": "list_posts", "payload": {}}},
                            {"call": {"service": "docs",
                                      "operation": "read_page",
                                      "payload": {"page": "launch-notes"}}},
                        ]}),
        Action(kind="respond", text="Digest computed."),
    ]


def _remember_preference(p: dict) -> list[Action]:
    return [
        Action(kind="memory_write", text=p.get("text", "Prefers concise briefs."),
               payload={"tier": "user"}),
        Action(kind="respond", text="Noted."),
    ]


def _self_schedule(p: dict) -> list[Action]:
    return [
        Action(kind="schedule_create",
               payload={"task": p.get("task", "account_brief"),
                        "every_ticks": p.get("every_ticks", 25)}),
        Action(kind="respond", text="Scheduled."),
    ]


def _daily_digest(p: dict) -> list[Action]:
    return [
        Action(kind="tool_call", tool="crm", operation="list_accounts"),
        Action(kind="respond", text="Daily digest ready."),
    ]


TASK_PLANS = {
    "review_pr": _review_pr,
    "publish_review": _publish_review,
    "account_brief": _account_brief,
    "update_stage": _update_stage,
    "followup_email": _followup_email,
    "draft_announcement": _draft_announcement,
    "publish_announcement": _publish_announcement,
    "publish_page": _publish_page,
    "exec_review_stats": _exec_review_stats,
    "exec_digest": _exec_digest,
    "remember_preference": _remember_preference,
    "self_schedule": _self_schedule,
    "daily_digest": _daily_digest,
}


# ---------------------------------------------------------------------------
# Seeded harness
# ---------------------------------------------------------------------------

def build_harness(tier: str = "stub-pro") -> Harness:
    """A ready-to-run harness: declarations, provider, workspace
    channels, and seed memory (one global convention, three private
    user facts used across the experiments)."""
    provider = ScriptedProvider(tier, task_plans=TASK_PLANS)
    harness = Harness(build_registry(), provider)
    ws = harness.workspace
    ws.add_channel(Channel(id="eng", name="#eng", members=("ana", "ben")))
    ws.add_channel(Channel(id="gtm", name="#gtm", members=("ana", "cy")))
    ws.add_channel(Channel(id="studio", name="#studio", members=("ana", "dee")))
    ws.add_channel(Channel(id="dm-ana", name="DM ana", members=("ana",), kind="dm"))
    ws.add_channel(Channel(id="dm-cy", name="DM cy", members=("cy",), kind="dm"))

    seed_ctx = RunContext(agent="reviewer", invoking_user=None,
                          surface=Surface.AMBIENT, run_id="seed")
    harness.memory.remember_declared_global(
        seed_ctx, "Review convention: flag any PR that changes retry or "
                  "timeout behaviour.", source_user=None)
    for user, text in (
            ("ana", "Ana prefers review summaries of at most five lines."),
            ("ben", "Ben is evaluating a data-residency requirement for Globex."),
            ("cy", "Cy notes Acme Corp renews in March and is expanding seats.")):
        ctx = RunContext(agent="pipeline", invoking_user=user,
                         surface=Surface.DM, run_id="seed")
        harness.memory.remember_user_fact(ctx, user, text)
    return harness
