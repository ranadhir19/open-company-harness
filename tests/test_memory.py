"""Two-tier memory: the wall between users, and the promotion routes."""

import pytest

from open_company_harness import RunContext, Surface, Tier, build_harness
from open_company_harness.memory import MemoryError


@pytest.fixture()
def harness():
    return build_harness()


def _ctx(user, agent="pipeline", surface=Surface.DM):
    return RunContext(agent=agent, invoking_user=user, surface=surface,
                      run_id="run-memory")


def test_per_user_recall_isolation(harness):
    ana_ctx = _ctx("ana")
    fact = harness.memory.remember_user_fact(
        ana_ctx, "ana", "Zqxwv marmalade telescope violet ledger note")
    ben_facts = harness.memory.recall(
        _ctx("ben"), "zqxwv marmalade telescope violet ledger")
    assert fact.id not in [f.id for f in ben_facts]
    assert all(f.owner != "ana" for f in ben_facts)
    assert all("Zqxwv" not in f.text for f in ben_facts)
    # The owner herself can still find it with the same query.
    ana_facts = harness.memory.recall(ana_ctx, "zqxwv marmalade telescope")
    assert fact.id in [f.id for f in ana_facts]


def test_global_facts_visible_to_all_users(harness):
    for user in ("ana", "ben", "cy", "dee"):
        facts = harness.memory.recall(
            _ctx(user), "review convention retry timeout")
        assert any(f.tier is Tier.GLOBAL and "Review convention" in f.text
                   for f in facts), user


def test_global_facts_visible_to_ambient(harness):
    ctx = RunContext(agent="reviewer", invoking_user=None,
                     surface=Surface.AMBIENT, run_id="run-memory")
    facts = harness.memory.recall(ctx, "review convention retry timeout")
    assert facts
    assert all(f.tier is Tier.GLOBAL for f in facts)
    assert any("Review convention" in f.text for f in facts)


def test_consent_promotion_by_owner_works(harness):
    ctx = _ctx("ana")
    fact = harness.memory.remember_user_fact(
        ctx, "ana", "Ana approves sharing the launch checklist wording")
    promoted = harness.memory.promote_by_consent(ctx, fact.id, "ana")
    assert promoted.tier is Tier.GLOBAL
    assert promoted.provenance.route == "consent"
    assert promoted.id in [f.id for f in harness.memory.visible_to("ben")]


def test_consent_promotion_by_non_owner_raises(harness):
    ctx = _ctx("ana")
    fact = harness.memory.remember_user_fact(
        ctx, "ana", "Ana private planning note for the offsite")
    with pytest.raises(MemoryError):
        harness.memory.promote_by_consent(_ctx("ben"), fact.id, "ben")


def test_corroboration_same_owner_refused(harness):
    ctx = _ctx("ana")
    text = "The support rota rotates every Monday morning"
    f1 = harness.memory.remember_user_fact(ctx, "ana", text)
    f2 = harness.memory.remember_user_fact(ctx, "ana", text)
    with pytest.raises(MemoryError):
        harness.memory.promote_by_corroboration(ctx, (f1.id, f2.id))


def test_corroboration_paraphrase_refused(harness):
    text_a = "The deployment window opens on Friday evening"
    text_b = "The deployment window opens on Friday night"
    fa = harness.memory.remember_user_fact(_ctx("ana"), "ana", text_a)
    fb = harness.memory.remember_user_fact(_ctx("ben"), "ben", text_b)
    with pytest.raises(MemoryError):
        harness.memory.promote_by_corroboration(_ctx("ana"), (fa.id, fb.id))


def test_corroboration_two_owners_identical_succeeds(harness):
    text = "The office kitchen closes early on Fridays"
    fa = harness.memory.remember_user_fact(_ctx("ana"), "ana", text)
    fb = harness.memory.remember_user_fact(_ctx("ben"), "ben", text)
    derived = harness.memory.promote_by_corroboration(
        _ctx("ana"), (fa.id, fb.id))
    assert derived.tier is Tier.GLOBAL
    assert derived.owner is None
    assert derived.provenance.route == "corroboration"
    assert derived.provenance.source_user is None
    assert derived.id in [f.id for f in harness.memory.visible_to("cy")]


def test_declared_global_with_source_user_refused(harness):
    ctx = RunContext(agent="studio", invoking_user="dee",
                     surface=Surface.CHANNEL, run_id="run-memory")
    with pytest.raises(MemoryError):
        harness.memory.remember_declared_global(
            ctx, "A fact lifted from one person's private input",
            source_user="dee")


def test_user_fact_without_owner_refused(harness):
    with pytest.raises(MemoryError):
        harness.memory.remember_user_fact(_ctx("ana"), "", "Ownerless text")
