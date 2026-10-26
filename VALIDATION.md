# Validation

`python3 validate.py` runs the repository harness. It is deterministic
and offline (the test suite uses the scripted provider and in-memory
service stubs; no network, no API keys).

Stages:

1. **import/compile** — `compileall` over the tree's Python sources,
   then imports of the `open_company_harness` package (every module)
   in a clean subprocess.
2. **test suite** — `pytest` over `tests/` (53 tests: identity broker,
   two-tier memory, channels, sandbox + secret proxy, scheduler,
   runtime).
3. **employer-reference scan** — the standing gate scanner
   (`~/workspace/github-fis-watch/fis_scan.py`) over the repo's sources;
   every candidate is judged in context by the rules in `validate.py`.
   A real employer reference FAILs this stage. This project is
   independent open-source work and must contain none. `validate.py`
   and this file name the gate they enforce, so they are excluded from
   the scan and reviewed by hand.
4. **results/docs consistency** — every path the README references
   exists, and the committed experiment outputs (`results/results.md`,
   `results/results.json`, `results/demo_run.json`,
   `results/charts.png`), entry points (`demo.py`,
   `examples/quickstart.py`, `experiments/run_experiments.py`), the
   docs page, the demo backend, and `render.yaml` are present and the
   deployment config parses.

Final line is exactly `HARNESS GREEN` (exit 0) or `HARNESS RED:
<stages>` (exit 1).

What this harness does **not** verify: the experiment suite is not
re-run by the harness (run it directly:
`PYTHONPATH=src python3 experiments/run_experiments.py` — it is
deterministic; two consecutive runs produce byte-identical
`results/results.json`); `demo.py` is not executed by the harness; the
hosted-provider adapters (OpenAI/Anthropic REST, direct-completion
mode) are never exercised offline; token and cost figures are
deterministic estimates from the scripted provider, not billed usage;
the content scan covers text files only and excludes `validate.py`
and this file as gate self-references; the demo is deploy-ready but
not deployed, so no hosted behaviour is verified.
