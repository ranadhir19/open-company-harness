"""Identity broker policy: attribution, surface gating, whitelists."""

import pytest

from open_company_harness import RunContext, Surface, build_harness


@pytest.fixture()
def harness():
    return build_harness()


def _ctx(agent="reviewer", user=None, surface=Surface.CHANNEL, on_behalf_of=None):
    return RunContext(agent=agent, invoking_user=user, surface=surface,
                      on_behalf_of=on_behalf_of, run_id="run-identity")


def _decide(harness, agent, tool_name, operation, **ctx_kwargs):
    tool = harness.registry.tool(tool_name)
    return harness.broker.decide(_ctx(agent=agent, **ctx_kwargs), tool, operation)


def test_dual_invoked_acts_as_user(harness):
    d = _decide(harness, "reviewer", "codehost", "read_pr",
                user="ana", surface=Surface.CHANNEL)
    assert d.allowed
    assert d.acting_identity == "user:ana"


def test_dual_ambient_acts_as_service(harness):
    d = _decide(harness, "reviewer", "codehost", "read_pr",
                user=None, surface=Surface.AMBIENT)
    assert d.allowed
    assert d.acting_identity == "service:codehost-bot"


def test_service_mode_always_service_identity(harness):
    invoked = _decide(harness, "pipeline", "crm", "lookup_account",
                      user="ben", surface=Surface.CHANNEL)
    ambient = _decide(harness, "pipeline", "crm", "lookup_account",
                      user=None, surface=Surface.AMBIENT)
    assert invoked.allowed and invoked.acting_identity == "service:crm-sync"
    assert ambient.allowed and ambient.acting_identity == "service:crm-sync"


def test_obo_ambient_blocked(harness):
    # docs is on-behalf-of and allows the ambient surface, so the block
    # must come from there being nobody to act for.
    d = _decide(harness, "studio", "docs", "read_page",
                user=None, surface=Surface.AMBIENT)
    assert not d.allowed
    assert d.acting_identity is None
    assert "ambient" in d.reason


@pytest.mark.parametrize("surface,user", [
    (Surface.CHANNEL, "cy"),
    (Surface.THREAD, "cy"),
    (Surface.AMBIENT, None),
])
def test_mail_surface_blocked_outside_dm(harness, surface, user):
    d = _decide(harness, "pipeline", "mail", "send_email",
                user=user, surface=surface)
    assert not d.allowed
    assert "not available on surface" in d.reason


def test_mail_allowed_in_dm_as_user(harness):
    d = _decide(harness, "pipeline", "mail", "send_email",
                user="cy", surface=Surface.DM)
    assert d.allowed
    assert d.acting_identity == "user:cy"


def test_whitelist_publish_allowed_for_dee(harness):
    social = _decide(harness, "studio", "social", "publish_post",
                     user="dee", surface=Surface.CHANNEL)
    docs = _decide(harness, "studio", "docs", "publish_page",
                   user="dee", surface=Surface.CHANNEL)
    assert social.allowed and social.acting_identity == "service:brand-voice"
    assert docs.allowed and docs.acting_identity == "user:dee"


@pytest.mark.parametrize("user", ["cy", "ana", "ben"])
def test_whitelist_publish_blocked_for_others(harness, user):
    d = _decide(harness, "studio", "social", "publish_post",
                user=user, surface=Surface.CHANNEL)
    assert not d.allowed
    assert "whitelist" in d.reason
    assert user in d.reason


def test_whitelist_ambient_commissioned_by_dee_allowed(harness):
    d = _decide(harness, "studio", "social", "publish_post",
                user=None, surface=Surface.AMBIENT, on_behalf_of="dee")
    assert d.allowed
    assert d.acting_identity == "service:brand-voice"


def test_whitelist_ambient_commissioned_by_cy_blocked(harness):
    d = _decide(harness, "studio", "social", "publish_post",
                user=None, surface=Surface.AMBIENT, on_behalf_of="cy")
    assert not d.allowed
    assert "whitelist" in d.reason


def test_undeclared_operation_blocked(harness):
    d = _decide(harness, "reviewer", "codehost", "delete_everything",
                user="ana", surface=Surface.CHANNEL)
    assert not d.allowed
    assert d.acting_identity is None
    assert "not declared" in d.reason


def test_approval_required_op_reports_needs_approval(harness):
    gated = _decide(harness, "reviewer", "codehost", "publish_comment",
                    user="ana", surface=Surface.CHANNEL)
    plain = _decide(harness, "reviewer", "codehost", "read_pr",
                    user="ana", surface=Surface.CHANNEL)
    assert gated.allowed and gated.needs_approval is True
    assert plain.allowed and plain.needs_approval is False
