"""Sandboxed code execution and the secret proxy.

Two rules define this subsystem:

1. **Raw secrets never enter the sandbox.** Credentials live in the
   host-side ``SecretVault``. Agent code receives only capability
   tokens minted by the identity broker — opaque strings scoped to one
   service, a set of operations, one run, and a short lifetime. To use
   a service, sandbox code calls the host-side ``SecretProxy``; the
   proxy validates the token, injects the real credential into the
   service call on the host, and returns only the service's response.
   The secret is never serialised into the sandbox, its environment,
   or its output.

2. **The sandbox is a seam.** ``SubprocessSandbox`` runs real Python
   in a child process with a scrubbed environment (the honest local
   implementation — a process boundary, *not* a hardened container;
   see the README's limitations). ``DeterministicSandbox`` executes a
   declared step list in-process against the same proxy, for fast,
   fully reproducible probe batteries. Both speak to services only
   through the proxy, so policy is enforced identically either way.

The backing services are deterministic in-memory stubs (a code host,
a CRM, mail, docs, social) — the harness's subject is the policy
around service access, not the services themselves.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from dataclasses import dataclass, field

from .audit import AuditTrail
from .identity import Clock, IdentityBroker


# ---------------------------------------------------------------------------
# Backing services (deterministic stubs, host-side only)
# ---------------------------------------------------------------------------

class ServiceBackend:
    name = "service"

    def handle(self, operation: str, payload: dict,
               acting_identity: str) -> tuple[str, dict]:
        raise NotImplementedError


class CodeHostService(ServiceBackend):
    name = "codehost"

    def __init__(self):
        self.prs = {
            "PR-101": {"title": "Add retry budget to exporter", "author": "ana",
                       "comments": [], "drafts": []},
            "PR-102": {"title": "Fix flaky scheduler test", "author": "ben",
                       "comments": [], "drafts": []},
        }

    def handle(self, operation, payload, acting_identity):
        pr = self.prs.get(payload.get("pr", ""))
        if operation == "list_prs":
            return ("open PRs: " + ", ".join(sorted(self.prs)), {"prs": sorted(self.prs)})
        if pr is None:
            return (f"no such PR: {payload.get('pr')}", {})
        if operation == "read_pr":
            return (f"{payload['pr']}: {pr['title']} by {pr['author']}; "
                    f"{len(pr['comments'])} published comments", dict(pr))
        if operation == "draft_comment":
            pr["drafts"].append({"by": acting_identity, "text": payload["text"]})
            return (f"draft comment saved on {payload['pr']} as {acting_identity}", {})
        if operation == "publish_comment":
            pr["comments"].append({"by": acting_identity, "text": payload["text"]})
            return (f"comment published on {payload['pr']} as {acting_identity}", {})
        return (f"unsupported operation {operation}", {})


class CrmService(ServiceBackend):
    name = "crm"

    def __init__(self):
        self.accounts = {
            "Acme Corp": {"stage": "evaluation", "owner": "ana"},
            "Globex": {"stage": "prospect", "owner": "ben"},
            "Initech": {"stage": "customer", "owner": "ana"},
        }

    def handle(self, operation, payload, acting_identity):
        if operation == "list_accounts":
            return ("accounts: " + ", ".join(sorted(self.accounts)),
                    {"accounts": sorted(self.accounts)})
        acct = self.accounts.get(payload.get("account", ""))
        if acct is None:
            return (f"no such account: {payload.get('account')}", {})
        if operation == "lookup_account":
            return (f"{payload['account']}: stage={acct['stage']} owner={acct['owner']}",
                    dict(acct))
        if operation == "update_stage":
            acct["stage"] = payload["stage"]
            return (f"{payload['account']} stage set to {payload['stage']} "
                    f"by {acting_identity}", {})
        return (f"unsupported operation {operation}", {})


class MailService(ServiceBackend):
    name = "mail"

    def __init__(self):
        self.outbox: list[dict] = []
        self.drafts: list[dict] = []

    def handle(self, operation, payload, acting_identity):
        if operation == "draft_email":
            self.drafts.append({"by": acting_identity, **payload})
            return (f"email draft saved by {acting_identity}", {})
        if operation == "send_email":
            self.outbox.append({"by": acting_identity, **payload})
            return (f"email sent to {payload.get('to')} as {acting_identity}", {})
        if operation == "list_outbox":
            return (f"{len(self.outbox)} sent, {len(self.drafts)} drafts",
                    {"sent": len(self.outbox), "drafts": len(self.drafts)})
        return (f"unsupported operation {operation}", {})


class DocsService(ServiceBackend):
    name = "docs"

    def __init__(self):
        self.pages = {"launch-notes": {"title": "Launch notes", "published": False,
                                       "drafts": []}}

    def handle(self, operation, payload, acting_identity):
        page = self.pages.get(payload.get("page", ""))
        if page is None:
            return (f"no such page: {payload.get('page')}", {})
        if operation == "read_page":
            return (f"page {payload['page']}: {page['title']}, "
                    f"published={page['published']}", dict(page))
        if operation == "draft_page":
            page["drafts"].append({"by": acting_identity, "text": payload["text"]})
            return (f"draft saved on {payload['page']} by {acting_identity}", {})
        if operation == "publish_page":
            page["published"] = True
            return (f"page {payload['page']} published by {acting_identity}", {})
        return (f"unsupported operation {operation}", {})


class SocialService(ServiceBackend):
    name = "social"

    def __init__(self):
        self.posts: list[dict] = []
        self.drafts: list[dict] = []

    def handle(self, operation, payload, acting_identity):
        if operation == "draft_post":
            self.drafts.append({"by": acting_identity, "text": payload["text"]})
            return (f"social draft saved by {acting_identity}", {})
        if operation == "publish_post":
            self.posts.append({"by": acting_identity, "text": payload["text"]})
            return (f"post published as {acting_identity}", {})
        if operation == "list_posts":
            return (f"{len(self.posts)} published, {len(self.drafts)} drafts",
                    {"published": len(self.posts), "drafts": len(self.drafts)})
        return (f"unsupported operation {operation}", {})


def default_services() -> dict[str, ServiceBackend]:
    backends = [CodeHostService(), CrmService(), MailService(),
                DocsService(), SocialService()]
    return {b.name: b for b in backends}


# ---------------------------------------------------------------------------
# Vault + proxy
# ---------------------------------------------------------------------------

class SecretVault:
    """Host-side credentials. Values in this build are obvious fakes
    used so probes can *prove* non-leakage by searching for them."""

    def __init__(self, secrets: dict[str, str] | None = None):
        self._secrets = secrets or {
            name: f"och-fake-secret-{name}-0001"
            for name in ("codehost", "crm", "mail", "docs", "social")
        }

    def get(self, service: str) -> str | None:
        return self._secrets.get(service)

    def values(self) -> list[str]:
        return list(self._secrets.values())


@dataclass
class ProxyResult:
    ok: bool
    output: str = ""
    reason: str = ""
    acting_identity: str | None = None


class SecretProxy:
    """The only door from agent code to services. Validates the
    capability token with the broker, injects the vault credential
    into the host-side service call, returns only the response. Every
    call — allowed or refused — is audited with the acting identity
    taken from the token, never from anything the caller claims."""

    def __init__(self, broker: IdentityBroker, vault: SecretVault,
                 services: dict[str, ServiceBackend], audit: AuditTrail,
                 clock: Clock):
        self._broker = broker
        self._vault = vault
        self._services = services
        self._audit = audit
        self._clock = clock

    def call(self, *, agent: str, run_id: str, token_str: str, service: str,
             operation: str, payload: dict | None = None) -> ProxyResult:
        payload = payload or {}
        ok, reason = self._broker.validate_capability(
            token_str, service=service, operation=operation, run_id=run_id)
        identity = self._broker.token_identity(token_str)
        if not ok:
            self._audit.record(
                tick=self._clock.now(), kind="proxy", agent=agent,
                run_id=run_id, acting_identity=identity, invoking_user=None,
                surface="sandbox", target=f"{service}.{operation}",
                decision="block", detail=reason)
            return ProxyResult(False, reason=reason, acting_identity=identity)
        secret = self._vault.get(service)
        if secret is None or service not in self._services:
            self._audit.record(
                tick=self._clock.now(), kind="proxy", agent=agent,
                run_id=run_id, acting_identity=identity, invoking_user=None,
                surface="sandbox", target=f"{service}.{operation}",
                decision="block", detail=f"unknown service {service}")
            return ProxyResult(False, reason=f"unknown service {service}",
                               acting_identity=identity)
        # The credential authenticates the host-side call. It is used
        # here and only here; it never crosses into the sandbox.
        _authenticated = bool(secret)
        output, _data = self._services[service].handle(
            operation, payload, identity)
        self._audit.record(
            tick=self._clock.now(), kind="proxy", agent=agent,
            run_id=run_id, acting_identity=identity, invoking_user=None,
            surface="sandbox", target=f"{service}.{operation}",
            decision="allow", detail="capability valid; credential injected host-side")
        return ProxyResult(True, output=output, acting_identity=identity)


# ---------------------------------------------------------------------------
# Sandboxes
# ---------------------------------------------------------------------------

@dataclass
class SandboxResult:
    ok: bool
    transcript: str          # everything the code printed / did, host-visible
    calls: list[dict] = field(default_factory=list)   # service calls attempted
    error: str = ""


class ProxyClient:
    """The host-side callable a sandbox uses for service access. It
    binds the run id and the per-service capability tokens, so sandbox
    code can neither choose its run binding nor present a token for a
    service it was not granted."""

    def __init__(self, proxy: SecretProxy, *, agent: str, run_id: str,
                 tokens: dict[str, str]):
        self._proxy = proxy
        self._agent = agent
        self._run_id = run_id
        self._tokens = tokens

    def call(self, service: str, operation: str, **payload) -> ProxyResult:
        token = self._tokens.get(service)
        if token is None:
            return ProxyResult(False,
                               reason=f"no capability granted for service {service}")
        return self._proxy.call(agent=self._agent, run_id=self._run_id,
                                token_str=token, service=service,
                                operation=operation, payload=payload)


class DeterministicSandbox:
    """In-process simulated sandbox: executes a declared list of steps
    (``{"print": text}`` / ``{"call": {...}}`` / ``{"show_env": true}``)
    against the proxy. Its environment *by construction* contains only
    capability tokens — the simulation makes non-leakage checkable for
    large probe batteries without process overhead."""

    name = "deterministic"

    def __init__(self, env_view: dict[str, str]):
        self._env_view = env_view

    def run(self, steps: list[dict], client: ProxyClient) -> SandboxResult:
        lines: list[str] = []
        calls: list[dict] = []
        for step in steps:
            if "print" in step:
                lines.append(str(step["print"]))
            elif "show_env" in step:
                lines.append("env: " + json.dumps(self._env_view, sort_keys=True))
            elif "call" in step:
                c = step["call"]
                calls.append(c)
                res = client.call(c["service"], c["operation"],
                                  **c.get("payload", {}))
                lines.append(f"-> {c['service']}.{c['operation']}: "
                             + (res.output if res.ok else f"REFUSED ({res.reason})"))
            else:
                lines.append(f"unknown step: {step}")
        return SandboxResult(ok=True, transcript="\n".join(lines), calls=calls)


_RUNNER_PRELUDE = '''
import sys, json, os
_TOKENS = json.loads(os.environ.get("OCH_CAP_TOKENS", "{}"))
def call_service(service, operation, **payload):
    if service not in _TOKENS:
        raise RuntimeError("no capability granted for service " + service)
    req = {"service": service, "operation": operation, "payload": payload}
    sys.stdout.write("CALL " + json.dumps(req) + "\\n")
    sys.stdout.flush()
    line = sys.stdin.readline()
    resp = json.loads(line)
    if not resp.get("ok"):
        raise RuntimeError("refused: " + str(resp.get("reason")))
    return resp.get("output")
'''


class SubprocessSandbox:
    """Runs real Python code in a child process. The child's
    environment carries capability tokens only (plus PATH); vault
    secrets are never placed in it. Service calls travel over the
    child's stdout/stdin as JSON lines and are executed host-side by
    the proxy client. A process boundary — not a container; see the
    README's limitations."""

    name = "subprocess"

    def __init__(self, timeout_s: float = 15.0):
        self._timeout = timeout_s

    def run(self, code: str, client: ProxyClient,
            tokens: dict[str, str]) -> SandboxResult:
        import os
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
               "OCH_CAP_TOKENS": json.dumps(tokens),
               "PYTHONHASHSEED": "0"}
        proc = subprocess.Popen(
            [sys.executable, "-c", _RUNNER_PRELUDE + code],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, env=env)
        transcript: list[str] = []
        calls: list[dict] = []

        def _pump():
            assert proc.stdout is not None and proc.stdin is not None
            for line in proc.stdout:
                line = line.rstrip("\n")
                if line.startswith("CALL "):
                    try:
                        req = json.loads(line[5:])
                    except json.JSONDecodeError:
                        continue
                    calls.append(req)
                    res = client.call(req["service"], req["operation"],
                                      **req.get("payload", {}))
                    proc.stdin.write(json.dumps(
                        {"ok": res.ok, "output": res.output,
                         "reason": res.reason}) + "\n")
                    proc.stdin.flush()
                else:
                    transcript.append(line)

        pump = threading.Thread(target=_pump, daemon=True)
        pump.start()
        try:
            proc.wait(timeout=self._timeout)
            pump.join(timeout=2)
            ok = proc.returncode == 0
            err = "" if ok else f"exit code {proc.returncode}"
        except subprocess.TimeoutExpired:
            proc.kill()
            ok, err = False, f"timeout after {self._timeout}s"
        return SandboxResult(ok=ok, transcript="\n".join(transcript),
                             calls=calls, error=err)
