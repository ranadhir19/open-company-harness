"""Sandbox and secret proxy: capability scope, expiry, non-leakage."""

import json

import pytest

from open_company_harness import Action, RunContext, Surface, build_harness

RUN_ID = "run-sandbox"


@pytest.fixture()
def harness():
    return build_harness()


def _mint(harness, operations=("read_pr",)):
    ctx = RunContext(agent="reviewer", invoking_user="ana",
                     surface=Surface.CHANNEL, channel_id="eng", run_id=RUN_ID)
    tool = harness.registry.tool("codehost")
    decision = harness.broker.decide(ctx, tool, operations[0])
    assert decision.allowed
    return harness.broker.mint_capability(ctx, decision, tool, operations)


def _call(harness, token_str, service="codehost", operation="read_pr",
          run_id=RUN_ID, payload=None):
    return harness.proxy.call(agent="reviewer", run_id=run_id,
                              token_str=token_str, service=service,
                              operation=operation,
                              payload=payload or {"pr": "PR-101"})


def _with_task(harness, name, actions):
    harness.provider.task_plans = {
        **harness.provider.task_plans, name: lambda p: actions}


def test_valid_capability_call_succeeds(harness):
    token = _mint(harness)
    res = _call(harness, token.token)
    assert res.ok
    assert res.acting_identity == "user:ana"
    assert "PR-101" in res.output


def test_forged_token_refused(harness):
    res = _call(harness, "cap_000000000000000000000000")
    assert not res.ok
    assert "forged" in res.reason


def test_expired_token_refused(harness):
    token = _mint(harness)
    harness.clock.advance(51)  # default ttl is 50 ticks
    res = _call(harness, token.token)
    assert not res.ok
    assert "expired" in res.reason


def test_wrong_service_refused(harness):
    token = _mint(harness)
    res = _call(harness, token.token, service="crm",
                operation="list_accounts", payload={})
    assert not res.ok
    assert "scoped to service codehost" in res.reason


def test_out_of_scope_operation_refused(harness):
    token = _mint(harness, operations=("read_pr",))
    res = _call(harness, token.token, operation="list_prs", payload={})
    assert not res.ok
    assert "outside token scope" in res.reason


def test_cross_run_replay_refused(harness):
    token = _mint(harness)
    res = _call(harness, token.token, run_id="run-someone-else")
    assert not res.ok
    assert "different run" in res.reason


def test_subprocess_env_contains_no_vault_secret(harness):
    code = "import os, json\nprint(json.dumps(dict(os.environ)))\n"
    _with_task(harness, "dump_env", [
        Action(kind="sandbox_exec", payload={
            "engine": "subprocess", "services": ["codehost"], "code": code}),
        Action(kind="respond", text="done"),
    ])
    result = harness.invoke("reviewer", "dump_env", user="ana",
                            surface=Surface.CHANNEL, channel_id="eng")
    step = result.steps[0]
    assert step.status == "ok"
    assert "OCH_CAP_TOKENS" in step.output
    for secret in harness.vault.values():
        assert secret not in step.output
        assert secret not in result.output


def test_subprocess_out_of_scope_call_refused(harness):
    # publish_comment is approval-gated, so the sandbox token minted for
    # the run deliberately excludes it.
    code = (
        "try:\n"
        "    call_service('codehost', 'publish_comment', "
        "pr='PR-101', text='sneaky')\n"
        "    print('UNEXPECTED-OK')\n"
        "except Exception as exc:\n"
        "    print('REFUSED:', exc)\n"
    )
    _with_task(harness, "sneaky_publish", [
        Action(kind="sandbox_exec", payload={
            "engine": "subprocess", "services": ["codehost"], "code": code}),
        Action(kind="respond", text="done"),
    ])
    result = harness.invoke("reviewer", "sneaky_publish", user="ana",
                            surface=Surface.CHANNEL, channel_id="eng")
    step = result.steps[0]
    assert "REFUSED:" in step.output
    assert "outside token scope" in step.output
    assert "UNEXPECTED-OK" not in step.output
    backend = harness.services["codehost"]
    assert backend.prs["PR-101"]["comments"] == []


def test_deterministic_show_env_exposes_only_tokens(harness):
    result = harness.invoke("studio", "exec_digest", user="ana",
                            surface=Surface.CHANNEL, channel_id="studio")
    assert result.status == "completed"
    step = result.steps[0]
    assert step.status == "ok"
    env_line = next(line for line in step.output.splitlines()
                    if line.startswith("env: "))
    env = json.loads(env_line[len("env: "):])
    assert set(env) == {"TOKEN[docs]", "TOKEN[social]"}
    assert all(value.startswith("cap_") for value in env.values())
    for secret in harness.vault.values():
        assert secret not in step.output
    assert "social.list_posts" in step.output
    assert "docs.read_page" in step.output
