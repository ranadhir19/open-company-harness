#!/usr/bin/env python3
"""Uniform validation harness for the open-company-harness repository.

Usage:  python3 validate.py     (deterministic, offline, no network)

Stages, in order, each printing one PASS/FAIL line:
  1. import/compile  - compileall over the tree's Python sources, then
                       import the package(s) declared in pyproject.toml
                       (plus every module) in a clean subprocess.
  2. test suite      - pytest over tests/ in a subprocess.
  3. employer scan   - the standing gate scanner over the repo tree;
                       every candidate is judged in context below.
                       A real reference FAILs this stage.
  4. docs/results    - every results/demo/entry-point path referenced
                       by the README exists (existence only).

Final line is exactly "HARNESS GREEN" (exit 0) or
"HARNESS RED: <stages>" (exit 1). See VALIDATION.md for what this
harness does and does not verify.
"""

import compileall
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
GATE_SCANNER = Path(
    os.environ.get(
        "FIS_SCAN",
        Path.home() / "workspace" / "github-fis-watch" / "fis_scan.py",
    )
)
# This harness and its companion doc must name the patterns they enforce,
# so they are excluded from the tree scan and reviewed by hand instead
# (judgment recorded in the harness notes). Nothing else is excluded
# beyond the scanner's own skips (.git, __pycache__, binaries).
SCAN_EXCLUDE = {"validate.py", "VALIDATION.md"}

FAILED = []


def report(stage, ok, detail=""):
    print(f"[{'PASS' if ok else 'FAIL'}] {stage}" + (f" - {detail}" if detail else ""))
    if not ok:
        FAILED.append(stage)
    return ok


def project_packages():
    with open(ROOT / "pyproject.toml", "rb") as fh:
        name = tomllib.load(fh)["project"]["name"]
    top = name.replace("-", "_")
    pkgs = [top]
    base = SRC / top
    for mod in sorted(base.glob("*.py")):
        if mod.name != "__init__.py":
            pkgs.append(f"{top}.{mod.stem}")
    return name, pkgs


def stage_import_compile():
    targets = [SRC, ROOT / "tests", ROOT / "examples", ROOT / "experiments",
                 ROOT / "demo"]
    files = [ROOT / "demo.py"]
    ok = True
    for t in targets:
        if t.exists() and not compileall.compile_dir(str(t), quiet=1):
            ok = False
            print(f"    compile failed under {t.relative_to(ROOT)}")
    for f in files:
        if f.exists() and not compileall.compile_file(str(f), quiet=1):
            ok = False
            print(f"    compile failed: {f.name}")
    name, pkgs = project_packages()
    env = dict(os.environ, PYTHONPATH=str(SRC))
    code = "; ".join(f"import {p}" for p in pkgs)
    proc = subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True, env=env, cwd=str(ROOT))
    if proc.returncode != 0:
        ok = False
        print("    import errors:\n" + proc.stderr[-1500:])
    return report("1. import/compile", ok,
                  f"project {name}; imported {len(pkgs)} packages/modules")


def stage_tests():
    env = dict(os.environ, PYTHONPATH=str(SRC))
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "-q", "--no-header"],
        capture_output=True, text=True, env=env, cwd=str(ROOT), timeout=1800)
    tail = (proc.stdout.strip().splitlines() or [""])[-1]
    print("    pytest: " + tail)
    if proc.returncode != 0:
        print(proc.stdout[-3000:])
        print(proc.stderr[-1500:])
    return report("2. test suite", proc.returncode == 0, tail)


def judge(hit):
    """Judge one scanner candidate in context. Returns (real, label)."""
    pattern, text = hit["pattern"], hit["text"]
    low = text.lower()
    if pattern in ("fisglobal", "fis-global", "fisglobal-domain"):
        return True, "REAL"
    if pattern == "Zenith-codename":
        if any(w in low for w in ("telescope", "astronom", "star", "sky")) \
                and not any(w in low for w in ("ai", "platform", "product")):
            return False, "BENIGN (astronomy)"
        return True, "REAL"
    if pattern == "FIS-word":
        if "affiliation" in low:
            return True, "USER-DECISION (affiliation)"
        return True, "REAL"
    # GOS-word and fis-path-segment have no benign use in this repo.
    return True, "REAL"


def stage_employer_scan():
    if not GATE_SCANNER.exists():
        return report("3. employer-reference scan", False,
                      f"scanner not found at {GATE_SCANNER}")
    proc = subprocess.run([sys.executable, str(GATE_SCANNER), str(ROOT)],
                          capture_output=True, text=True)
    hits, excluded = [], 0
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.startswith("{"):
            h = json.loads(line)
            rel = os.path.relpath(h["file"], ROOT)
            if rel in SCAN_EXCLUDE:
                excluded += 1
            else:
                hits.append((rel, h))
    if excluded:
        print(f"    note: {excluded} candidate(s) inside validate.py / "
              "VALIDATION.md name the gate itself (pattern names, the "
              "scanner path) - excluded, judged benign, see VALIDATION.md")
    real = []
    for rel, h in hits:
        is_real, label = judge(h)
        print(f"    candidate {rel}:{h['line']} [{h['pattern']}] "
              f"-> {label}: {h['text'][:120]}")
        if is_real:
            real.append((rel, h, label))
    ok = not real
    detail = (f"{len(hits)} candidate(s), {len(real)} real"
              if hits else "0 candidates")
    return report("3. employer-reference scan", ok, detail)


REF_RE = re.compile(
    r"(?<![\w./-])((?:results|examples|experiments|docs|src|demo)/[\w./<>*-]+"
    r"|demo\.py)(?![\w/-])")


# ---------------------------------------------------------------------------
# Deployment-config validation (part of stage 4).
#
# Standing rule: a broken deployment configuration is a harness failure,
# not a deploy-time surprise. The tree is scanned for the usual deployment
# files; when none is present the step passes with nothing to validate,
# and any file found is parsed and schema-checked offline. YAML parsing
# uses PyYAML when importable; otherwise only a structural (non-empty)
# check is possible and that limitation is reported, never hidden.
# ---------------------------------------------------------------------------

DEPLOY_FILE_NAMES = {"render.yaml", "render.yml", "vercel.json", "Procfile",
                     "netlify.toml", "fly.toml"}
DEPLOY_SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv",
                    ".pytest_cache"}


def find_deploy_configs():
    found = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = sorted(d for d in dirnames if d not in DEPLOY_SKIP_DIRS)
        for fn in sorted(filenames):
            p = Path(dirpath) / fn
            rel = p.relative_to(ROOT)
            if fn in DEPLOY_FILE_NAMES or fn.startswith("Dockerfile"):
                found.append(p)
            elif fn.startswith("docker-compose") and p.suffix in (".yml", ".yaml"):
                found.append(p)
            elif (len(rel.parts) == 3 and rel.parts[0] == ".github"
                  and rel.parts[1] == "workflows"
                  and p.suffix in (".yml", ".yaml")):
                found.append(p)
    return sorted(found)


def _load_yaml_config(path, notes):
    """Return (data, error). data is None when no parser is available."""
    try:
        import yaml
    except ImportError:
        notes.append("parser unavailable (PyYAML not importable) - "
                     "structural check only")
        if not path.read_text(errors="replace").strip():
            return None, "empty file"
        return None, None
    try:
        return yaml.safe_load(path.read_text(errors="replace")), None
    except Exception as exc:  # YAML syntax error
        return None, f"YAML does not parse: {exc}"


def _check_render(path, rel, problems, notes):
    data, err = _load_yaml_config(path, notes)
    if err:
        problems.append(f"{rel}: {err}")
        return
    if data is None:
        return
    if not isinstance(data, dict):
        problems.append(f"{rel}: top level must be a mapping")
        return
    services = data.get("services")
    if not isinstance(services, list) or not services:
        problems.append(f"{rel}: 'services' must be a non-empty list")
        return
    valid_types = {"web", "worker", "cron", "pserv", "static"}
    for i, svc in enumerate(services):
        where = f"{rel}: services[{i}]"
        if not isinstance(svc, dict):
            problems.append(f"{where} must be a mapping")
            continue
        if not svc.get("name"):
            problems.append(f"{where} has no 'name'")
        typ, env = svc.get("type"), svc.get("env")
        if typ not in valid_types:
            problems.append(f"{where} type {typ!r} not in {sorted(valid_types)}")
        if typ == "static":
            problems.append(f"{where} 'type: static' is invalid - a static "
                            "site is type: web with env: static")
        if env == "static" and typ != "web":
            problems.append(f"{where} env: static requires type: web")
        if env == "docker":
            root_dir = svc.get("rootDir") or "."
            dockerfile = svc.get("dockerfilePath") or "./Dockerfile"
            if not (ROOT / root_dir / dockerfile).exists():
                problems.append(f"{where} env: docker but dockerfilePath "
                                f"{dockerfile!r} does not exist")


def _check_vercel(path, rel, problems, notes):
    try:
        data = json.loads(path.read_text(errors="replace"))
    except Exception as exc:
        problems.append(f"{rel}: JSON does not parse: {exc}")
        return
    if not isinstance(data, dict):
        problems.append(f"{rel}: top level must be an object")
        return
    if "builds" in data and not isinstance(data["builds"], list):
        problems.append(f"{rel}: 'builds' must be a list")
    if "functions" in data and not isinstance(data["functions"], dict):
        problems.append(f"{rel}: 'functions' must be an object")


def _check_dockerfile(path, rel, problems, notes):
    lines = [ln.strip() for ln in path.read_text(errors="replace").splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        problems.append(f"{rel}: empty Dockerfile")
        return
    heads = [ln.split(None, 1)[0].upper() for ln in lines]
    if "FROM" not in heads:
        problems.append(f"{rel}: no FROM instruction")
    if "CMD" not in heads and "ENTRYPOINT" not in heads:
        problems.append(f"{rel}: no CMD or ENTRYPOINT instruction")


def _check_compose(path, rel, problems, notes):
    data, err = _load_yaml_config(path, notes)
    if err:
        problems.append(f"{rel}: {err}")
        return
    if data is None:
        return
    services = data.get("services") if isinstance(data, dict) else None
    if not isinstance(services, dict) or not services:
        problems.append(f"{rel}: 'services' must be a non-empty mapping")


def _check_workflow(path, rel, problems, notes):
    data, err = _load_yaml_config(path, notes)
    if err:
        problems.append(f"{rel}: {err}")
        return
    if data is None:
        return
    if not isinstance(data, dict):
        problems.append(f"{rel}: top level must be a mapping")
        return
    # YAML 1.1 parses the bare key `on` as boolean True.
    if "on" not in data and True not in data:
        problems.append(f"{rel}: missing 'on' trigger")
    if "jobs" not in data:
        problems.append(f"{rel}: missing 'jobs'")


def _check_procfile(path, rel, problems, notes):
    lines = [ln for ln in path.read_text(errors="replace").splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        problems.append(f"{rel}: empty Procfile")
        return
    for ln in lines:
        if not re.match(r"^[\w.-]+:\s*\S+", ln):
            problems.append(f"{rel}: line does not match 'name: command': {ln!r}")


def _check_toml(path, rel, problems, notes, need_app=False):
    try:
        data = tomllib.loads(path.read_text(errors="replace"))
    except Exception as exc:
        problems.append(f"{rel}: TOML does not parse: {exc}")
        return
    if need_app and "app" not in data:
        problems.append(f"{rel}: missing 'app' key")


def check_deploy_configs():
    """Return (problems, found_count); prints one line per file checked."""
    configs = find_deploy_configs()
    problems = []
    for p in configs:
        rel = str(p.relative_to(ROOT))
        notes = []
        if p.name in ("render.yaml", "render.yml"):
            _check_render(p, rel, problems, notes)
        elif p.name == "vercel.json":
            _check_vercel(p, rel, problems, notes)
        elif p.name.startswith("Dockerfile"):
            _check_dockerfile(p, rel, problems, notes)
        elif p.name.startswith("docker-compose"):
            _check_compose(p, rel, problems, notes)
        elif p.name == "Procfile":
            _check_procfile(p, rel, problems, notes)
        elif p.name == "fly.toml":
            _check_toml(p, rel, problems, notes, need_app=True)
        elif p.name == "netlify.toml":
            _check_toml(p, rel, problems, notes)
        elif p.suffix in (".yml", ".yaml"):
            _check_workflow(p, rel, problems, notes)
        for n in notes:
            print(f"  deployment-config: {rel}: {n}")
    return problems, len(configs)


def stage_docs_results():
    readmes = [ROOT / "README.md"] + sorted(ROOT.glob("*/README.md"))
    refs, missing = set(), []
    for rm in readmes:
        if not rm.exists():
            continue
        for m in REF_RE.finditer(rm.read_text(errors="replace")):
            refs.add(m.group(1).rstrip(".,;:"))
    for ref in sorted(refs):
        if "<" in ref or "*" in ref:
            pat = ref.replace("<slug>", "*").replace("NN", "*")
            if not list(ROOT.glob(pat)):
                missing.append(ref + " (no expansion exists)")
            continue
        if not (ROOT / ref).exists():
            missing.append(ref)
    # Entry points and result artefacts the README documents.
    for rel in ("demo.py", "examples/quickstart.py",
                "experiments/run_experiments.py", "results/results.md",
                "results/results.json", "results/demo_run.json",
                "results/charts.png", "docs/index.html",
                "demo/backend/app.py", "demo/web/package.json",
                "render.yaml"):
        if not (ROOT / rel).exists():
            missing.append(rel)
    for m in missing:
        print(f"    missing: {m}")
    deploy_problems, deploy_found = check_deploy_configs()
    if not deploy_found:
        print("[PASS] deployment-config: none present \u2014 nothing to validate")
    for dp in deploy_problems:
        print(f"    INVALID deployment-config: {dp}")
    detail = (f"{len(refs)} README references checked, "
              f"{len(missing)} missing")
    if deploy_found:
        detail += (f"; {deploy_found} deployment config(s) checked, "
                   f"{len(deploy_problems)} invalid")
    return report("4. results/docs consistency",
                  not missing and not deploy_problems, detail)


def main():
    print(f"validate.py - {ROOT.name} ({ROOT})")
    stage_import_compile()
    stage_tests()
    stage_employer_scan()
    stage_docs_results()
    if FAILED:
        print("HARNESS RED: " + ", ".join(FAILED))
        return 1
    print("HARNESS GREEN")
    return 0


if __name__ == "__main__":
    sys.exit(main())
