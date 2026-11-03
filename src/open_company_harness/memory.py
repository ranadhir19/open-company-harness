"""Two-tier memory: a global tier everyone may read, a user tier only
its owner may read — and a hard wall between them.

The failure mode this design exists to prevent is quiet cross-user
leakage: a fact one user told an agent in a DM resurfacing, via the
global tier, in another user's channel. So the rules are structural,
not advisory:

- A user-tier fact is stored with an owner and is returned *only* to
  that owner's contexts. Recall enforces it; there is no query that
  returns another user's facts.
- A fact reaches the global tier only by an explicit route: the
  owning user consents to share it (``consent`` promotion), two or
  more distinct users independently assert an equivalent fact
  (``corroboration`` promotion, which stores a *derived* global fact
  attributed to the rule, not either user's wording), or an agent
  whose declaration grants global-write states a fact directly (and
  such facts may not be sourced from a single user's private input —
  the runtime passes provenance and the store refuses user-sourced
  text on that route).
- Every fact carries provenance: who supplied it, in which run, on
  which surface, at which tick. A fact without provenance is not
  stored.

Recall is lexical and deterministic (token overlap, then recency) —
the same approach as the sibling memory project; semantic ranking is
a provider concern, not a store concern.
"""

from __future__ import annotations

import itertools
import re
from dataclasses import dataclass, field

from .audit import AuditTrail
from .identity import Clock
from .models import RunContext, Tier


@dataclass
class Provenance:
    source_user: str | None      # user who supplied it, None for agent-declared
    source_agent: str
    run_id: str
    surface: str
    tick: int
    route: str                   # direct | consent | corroboration | declared

    def to_dict(self) -> dict:
        return {"source_user": self.source_user,
                "source_agent": self.source_agent, "run_id": self.run_id,
                "surface": self.surface, "tick": self.tick,
                "route": self.route}


@dataclass
class Fact:
    id: str
    text: str
    tier: Tier
    owner: str | None            # user id for USER tier; None for GLOBAL
    provenance: Provenance
    tags: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        return {"id": self.id, "text": self.text, "tier": self.tier.value,
                "owner": self.owner, "tags": list(self.tags),
                "provenance": self.provenance.to_dict()}


class MemoryError(Exception):
    """A memory operation policy refused (distinct from a broker block:
    this is the store protecting its own invariants)."""


_WORD = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    return set(_WORD.findall(text.lower()))


@dataclass
class MemoryStore:
    audit: AuditTrail
    clock: Clock
    _facts: dict = field(default_factory=dict)
    _ids: itertools.count = field(default_factory=lambda: itertools.count(1))

    def _new_id(self) -> str:
        return f"fact-{next(self._ids):04d}"

    # -- writes ---------------------------------------------------------------
    def remember_user_fact(self, ctx: RunContext, owner: str, text: str,
                           tags: tuple[str, ...] = ()) -> Fact:
        if not owner:
            raise MemoryError("a user-tier fact needs an owner")
        fact = Fact(id=self._new_id(), text=text, tier=Tier.USER, owner=owner,
                    provenance=Provenance(
                        source_user=ctx.invoking_user, source_agent=ctx.agent,
                        run_id=ctx.run_id, surface=ctx.surface.value,
                        tick=self.clock.now(), route="direct"),
                    tags=tags)
        self._facts[fact.id] = fact
        self._log(ctx, fact, "user fact stored")
        return fact

    def remember_declared_global(self, ctx: RunContext, text: str,
                                 source_user: str | None,
                                 tags: tuple[str, ...] = ()) -> Fact:
        """Direct global write by an agent whose declaration allows it.
        Refused when the content is sourced from one user's input —
        that content must travel by consent or corroboration instead."""
        if source_user is not None:
            raise MemoryError(
                "user-sourced content may not be written straight to the "
                "global tier; use consent or corroboration promotion")
        fact = Fact(id=self._new_id(), text=text, tier=Tier.GLOBAL, owner=None,
                    provenance=Provenance(
                        source_user=None, source_agent=ctx.agent,
                        run_id=ctx.run_id, surface=ctx.surface.value,
                        tick=self.clock.now(), route="declared"),
                    tags=tags)
        self._facts[fact.id] = fact
        self._log(ctx, fact, "declared global fact stored")
        return fact

    def promote_by_consent(self, ctx: RunContext, fact_id: str,
                           consenting_user: str) -> Fact:
        src = self._get(fact_id)
        if src.tier is not Tier.USER:
            raise MemoryError(f"{fact_id} is not a user-tier fact")
        if src.owner != consenting_user:
            raise MemoryError(
                f"only the owner ({src.owner}) may consent to share {fact_id}; "
                f"{consenting_user} may not")
        fact = Fact(id=self._new_id(), text=src.text, tier=Tier.GLOBAL,
                    owner=None,
                    provenance=Provenance(
                        source_user=src.owner, source_agent=ctx.agent,
                        run_id=ctx.run_id, surface=ctx.surface.value,
                        tick=self.clock.now(), route="consent"),
                    tags=src.tags)
        self._facts[fact.id] = fact
        self._log(ctx, fact, f"promoted from {src.id} by owner consent")
        return fact

    def promote_by_corroboration(self, ctx: RunContext,
                                 fact_ids: tuple[str, ...]) -> Fact:
        """Two or more distinct owners asserting equivalent facts.
        Equivalence here is token-set equality after normalisation —
        deliberately strict, so near-miss paraphrases do NOT promote
        (that strictness is measured in E1)."""
        if len(fact_ids) < 2:
            raise MemoryError("corroboration needs at least two facts")
        facts = [self._get(fid) for fid in fact_ids]
        owners = {f.owner for f in facts}
        if any(f.tier is not Tier.USER for f in facts):
            raise MemoryError("corroboration applies to user-tier facts")
        if len(owners) < 2:
            raise MemoryError("corroboration needs facts from distinct owners")
        norm = [frozenset(_tokens(f.text)) for f in facts]
        if any(n != norm[0] for n in norm):
            raise MemoryError("facts are not equivalent; corroboration refused")
        fact = Fact(id=self._new_id(), text=facts[0].text, tier=Tier.GLOBAL,
                    owner=None,
                    provenance=Provenance(
                        source_user=None, source_agent=ctx.agent,
                        run_id=ctx.run_id, surface=ctx.surface.value,
                        tick=self.clock.now(), route="corroboration"),
                    tags=facts[0].tags)
        self._facts[fact.id] = fact
        self._log(ctx, fact,
                  f"promoted by corroboration of {[f.id for f in facts]} "
                  f"from owners {sorted(owners)}")
        return fact

    # -- reads ------------------------------------------------------------------
    def recall(self, ctx: RunContext, query: str, limit: int = 5) -> list[Fact]:
        """Global facts + the invoking user's own facts, ranked by
        token overlap then recency. For ambient runs there is no
        invoking user, so only the global tier is visible."""
        visible = [f for f in self._facts.values()
                   if f.tier is Tier.GLOBAL
                   or (ctx.invoking_user is not None
                       and f.owner == ctx.invoking_user)]
        q = _tokens(query)
        scored = []
        for f in visible:
            overlap = len(q & _tokens(f.text))
            scored.append((overlap, f.provenance.tick, f))
        scored.sort(key=lambda t: (-t[0], -t[1], t[2].id))
        out = [f for _, _, f in scored[:limit]]
        self.audit.record(
            tick=self.clock.now(), kind="memory", agent=ctx.agent,
            run_id=ctx.run_id,
            acting_identity=(f"user:{ctx.invoking_user}"
                             if ctx.invoking_user else None),
            invoking_user=ctx.invoking_user, surface=ctx.surface.value,
            target=f"recall:{query[:40]}", decision="n/a",
            detail=f"returned {[f.id for f in out]}")
        return out

    def visible_to(self, user_id: str | None) -> list[Fact]:
        """Everything a user could ever see — used by probes to test
        the wall from the outside."""
        return [f for f in self._facts.values()
                if f.tier is Tier.GLOBAL
                or (user_id is not None and f.owner == user_id)]

    def get(self, fact_id: str) -> Fact:
        return self._get(fact_id)

    def all_facts(self) -> list[Fact]:
        return list(self._facts.values())

    # -- plumbing ---------------------------------------------------------------
    def _get(self, fact_id: str) -> Fact:
        if fact_id not in self._facts:
            raise MemoryError(f"unknown fact: {fact_id}")
        return self._facts[fact_id]

    def _log(self, ctx: RunContext, fact: Fact, detail: str):
        self.audit.record(
            tick=self.clock.now(), kind="memory", agent=ctx.agent,
            run_id=ctx.run_id,
            acting_identity=(f"user:{fact.owner}" if fact.tier is Tier.USER
                             else None),
            invoking_user=ctx.invoking_user, surface=ctx.surface.value,
            target=fact.id, decision="n/a",
            detail=f"{detail} (tier={fact.tier.value})")
