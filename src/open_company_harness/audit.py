"""The audit trail: every decision and action, identity-stamped.

The harness's central promise is that nothing acts anonymously. The
identity broker, the memory store, the secret proxy and the scheduler
all append to one append-only trail. Each entry records *who acted*
(the acting identity the broker resolved), *who asked* (the invoking
user, or the commissioning user for ambient work), *where* (surface),
and *what was decided*. Blocked attempts are recorded with the same
care as allowed ones — a refusal you cannot see is a policy you cannot
audit.
"""

from __future__ import annotations

import itertools
import json
from dataclasses import dataclass, field


@dataclass
class AuditEntry:
    seq: int
    tick: int
    kind: str                    # decision | action | memory | proxy | schedule | run | approval
    agent: str
    run_id: str
    acting_identity: str | None  # e.g. "user:ana", "service:codehost-bot"
    invoking_user: str | None
    surface: str
    target: str                  # tool/op, fact id, service/op, schedule id
    decision: str                # allow | block | n/a
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "seq": self.seq, "tick": self.tick, "kind": self.kind,
            "agent": self.agent, "run_id": self.run_id,
            "acting_identity": self.acting_identity,
            "invoking_user": self.invoking_user, "surface": self.surface,
            "target": self.target, "decision": self.decision,
            "detail": self.detail,
        }


@dataclass
class AuditTrail:
    entries: list[AuditEntry] = field(default_factory=list)
    _seq: itertools.count = field(default_factory=lambda: itertools.count(1))

    def record(self, *, tick: int, kind: str, agent: str, run_id: str,
               acting_identity: str | None, invoking_user: str | None,
               surface: str, target: str, decision: str,
               detail: str = "") -> AuditEntry:
        entry = AuditEntry(seq=next(self._seq), tick=tick, kind=kind,
                           agent=agent, run_id=run_id,
                           acting_identity=acting_identity,
                           invoking_user=invoking_user, surface=surface,
                           target=target, decision=decision, detail=detail)
        self.entries.append(entry)
        return entry

    def for_run(self, run_id: str) -> list[AuditEntry]:
        return [e for e in self.entries if e.run_id == run_id]

    def decisions(self) -> list[AuditEntry]:
        return [e for e in self.entries if e.kind == "decision"]

    def blocked(self) -> list[AuditEntry]:
        return [e for e in self.entries if e.decision == "block"]

    def stamp_coverage(self) -> tuple[int, int]:
        """(stamped, total) over entries that record an *executed*
        action or a proxied service call — the entries E5 verifies for
        coverage. (Decision entries, including refusals where no
        identity could ever act, are verified separately against the
        policy table.)"""
        relevant = [e for e in self.entries
                    if e.kind in ("action", "proxy")]
        stamped = [e for e in relevant if e.acting_identity]
        return len(stamped), len(relevant)

    def to_json(self) -> str:
        return json.dumps([e.to_dict() for e in self.entries], indent=1)
