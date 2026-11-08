"""Command-line interface for Chronoscope for QNX 8.0.

    python3 -m chronoscope --help
    python3 -m chronoscope init --out my-system.toml
    python3 -m chronoscope audit models/brake-by-wire.toml
    python3 -m chronoscope fix models/brake-by-wire.toml --out fixed.toml --report demo/report.html
    python3 -m chronoscope import examples/traces/qnx-kernel-trace.txt --out trace.json
    python3 -m chronoscope inspect target/images/chronoscope.build --model models/...
    python3 -m chronoscope verify

Everything works offline with Python 3.11+ and the standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .audit import AuditResult, audit, summarize as audit_summary
from .autopilot import autopilot, summarize as ap_summary
from .buildfile import lint_buildfile, summarize as bf_summary
from .importers import load_trace, parse_pidin, parse_slog2info, parse_traceprinter, reconcile
from .kernel import simulate
from .model import EXAMPLE_MODEL, ModelError, dump_model, load_model
from .report import render_html, render_markdown
from .trace import Trace

BANNER = r"""
   ____ _   _  ___  _   _  ___  ____   ___   ____ ____   ___  ____  _____
  / ___| | | |/ _ \| \ | |/ _ \|  _ \ / _ \ / ___|  _ \ / _ \|  _ \| ____|
 | |   | |_| | | | |  \| | | | | |_) | | | | |   | |_) | | | | |_) |  _|
 | |___|  _  | |_| | |\  | |_| |  _ <| |_| | |___|  __/| |_| |  __/| |___
  \____|_| |_|\___/|_| \_|\___/|_| \_\\___/ \____|_|    \___/|_|   |_____|
        Q N X   8 . 0   D E T E R M I N I S M   W O R K B E N C H
"""

ROOT = Path(__file__).resolve().parent.parent


def _out(path: str | Path, text: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    print(f"  wrote {p}")


def _load(path: str):
    try:
        return load_model(path)
    except (ModelError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(2) from exc


def _title(model, path: str) -> str:
    return f"{model.name} · QNX 8.0 determinism"


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_init(args: argparse.Namespace) -> int:
    dest = Path(args.out)
    if dest.exists() and not args.force:
        print(f"error: {dest} already exists (use --force)", file=sys.stderr)
        return 2
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(EXAMPLE_MODEL, encoding="utf-8")
    print(f"Created {dest}")
    print("Edit it, then run: python3 -m chronoscope audit", str(dest))
    return 0


def cmd_simulate(args: argparse.Namespace) -> int:
    model = _load(args.model)
    trace = simulate(model)
    print(f"Simulated {model.name}: {len(trace.jobs)} jobs, "
          f"{len(trace.events)} kernel events, {len(trace.segments)} segments")
    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        trace.to_json(args.out)
        print(f"  wrote {args.out}")
    if args.traceprinter:
        _out(args.traceprinter, trace.to_traceprinter_text())
    if args.summary:
        print(audit_summary(audit(trace, model), model.name))
    return 0


def _audit_model(args: argparse.Namespace):
    model = _load(args.model)
    trace = simulate(model)
    result = audit(trace, model)
    return model, trace, result


def cmd_audit(args: argparse.Namespace) -> int:
    model, trace, result = _audit_model(args)
    if not args.quiet:
        print(audit_summary(result, model.name))
    if args.json_out:
        _out(args.json_out, json.dumps(result.to_dict(), indent=2) + "\n")
    if args.html:
        _out(args.html, render_html(trace, model, result, title=_title(model, args.model)))
    if args.markdown:
        _out(args.markdown, render_markdown(trace, model, result) + "\n")
    return 1 if (args.strict and result.kpis["deadline_misses"]) else 0


def cmd_fix(args: argparse.Namespace) -> int:
    model = _load(args.model)
    progress = (lambda s: print("  " + s)) if not args.quiet else None
    res = autopilot(model, max_rounds=args.max_rounds,
                    max_candidates=args.max_candidates, progress=progress)
    if not args.quiet:
        print(ap_summary(res))
    if res.fixed_model is not None and args.out:
        dump_model(res.fixed_model, args.out)
        print(f"  wrote fixed model: {args.out}")
    if args.json_out:
        _out(args.json_out, json.dumps(res.to_dict(), indent=2) + "\n")
    if args.report:
        assert res.after_trace is not None and res.fixed is not None
        _out(args.report, render_html(res.after_trace, res.fixed_model, res.fixed,
                                     res, title=f"{model.name} · verified fix"))
    return 0 if res.verified else (1 if args.strict else 0)


def cmd_report(args: argparse.Namespace) -> int:
    model, trace, result = _audit_model(args)
    ap = None
    if args.fix:
        ap = autopilot(model, max_rounds=3)
        if ap.after_trace is not None and ap.fixed is not None:
            trace, result = ap.after_trace, ap.fixed
            model = ap.fixed_model or model
    _out(args.out, render_html(trace, model, result, ap,
                               title=_title(model, args.model)))
    if args.markdown:
        _out(args.markdown, render_markdown(trace, model, result, ap) + "\n")
    if not args.quiet:
        print(audit_summary(result, model.name))
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    p = Path(args.file)
    if not p.is_file():
        print(f"error: file not found: {p}", file=sys.stderr)
        return 2
    text = p.read_text(encoding="utf-8", errors="replace")
    if args.format == "traceprinter":
        tr = parse_traceprinter(text, name=p.stem)
    elif args.format == "slog2":
        tr = parse_slog2info(text, name=p.stem)
    elif args.format == "pidin":
        tr = parse_pidin(text, name=p.stem)
    else:
        tr = load_trace(p)
    print(f"Imported {tr.source} from {p}: {len(tr.events)} events, "
          f"{len(tr.segments)} segments, {len(tr.jobs)} jobs "
          f"(QNX {tr.meta.get('qnx_version', '8.0')})")
    if args.out:
        tr.to_json(args.out)
        print(f"  wrote {args.out}")
    if args.audit:
        print(audit_summary(audit(tr), tr.name))
    return 0


def cmd_inspect(args: argparse.Namespace) -> int:
    model = _load(args.model) if args.model else None
    rep = lint_buildfile(args.buildfile, model=model)
    if not args.quiet:
        print(bf_summary(rep))
    if args.json_out:
        _out(args.json_out, json.dumps(rep.to_dict(), indent=2) + "\n")
    return 1 if (args.strict and rep.score < 100) else 0


def cmd_reconcile(args: argparse.Namespace) -> int:
    sim = Trace.from_json(args.simulated)
    real = load_trace(args.measured)
    rep = reconcile(sim, real)
    print(f"Model vs QNX target: {rep['verdict']}  "
          f"(sim={rep['simulated_jobs']} jobs, target={rep['measured_jobs']} jobs, "
          f"drift={rep['drift_count']})")
    print(f"  {'thread':<22}{'sim WCET':>12}{'QNX WCET':>12}{'delta':>10}  verdict")
    for row in rep["rows"]:
        print(f"  {row['thread']:<22}{str(row.get('sim_wcet_us')):>12}"
              f"{str(row.get('qnx_wcet_us')):>12}"
              f"{str(row.get('wcet_delta_pct')):>10}  {row['verdict']}")
    if args.json_out:
        _out(args.json_out, json.dumps(rep, indent=2) + "\n")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Run the bundled unittest suite -- the project's self-verification.

    The suite contains a test that itself invokes ``verify`` in a subprocess.
    CHRONOSCOPE_VERIFY breaks that recursion: the nested suite skips the
    re-entrant test instead of spawning another verifier forever.
    """
    os.environ["CHRONOSCOPE_VERIFY"] = "1"
    suite = unittest.defaultTestLoader.discover(
        str(ROOT / "tests"), pattern="test_*.py", top_level_dir=str(ROOT))
    runner = unittest.TextTestRunner(verbosity=2 if args.verbose else 1)
    result = runner.run(suite)
    skipped = len(getattr(result, "skipped", []))
    print(f"\nSelf-verification: {result.testsRun} tests, "
          f"{len(result.failures)} failures, {len(result.errors)} errors"
          + (f", {skipped} re-entrancy guard"
             f"{'s' if skipped != 1 else ''} skipped" if skipped else ""))
    if skipped:
        print("  (run `python3 -m unittest discover -s tests` to execute all "
              "69 cases including the CLI verifier test)")
    return 0 if result.wasSuccessful() else 1


def cmd_demo(args: argparse.Namespace) -> int:
    """Generate the full demo artifact set used in the submission video."""
    outdir = Path(args.out_dir)
    outdir.mkdir(parents=True, exist_ok=True)
    models_dir = ROOT / "models"
    flagship = models_dir / "brake-by-wire.toml"
    if not flagship.is_file():
        flagship = models_dir / "hello-qnx.toml"
    rows: list[str] = []
    for path in sorted(models_dir.glob("*.toml")):
        model = load_model(path)
        trace = simulate(model)
        result = audit(trace, model)
        name = path.stem
        html = outdir / f"{name}.html"
        html.write_text(render_html(trace, model, result,
                                    title=f"{model.name} · QNX 8.0"),
                        encoding="utf-8")
        (outdir / f"{name}.json").write_text(
            json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")
        rows.append(f"| {name} | {result.verdict} | {result.score} | "
                    f"{result.kpis['deadline_misses']} | "
                    f"{result.kpis['priority_inversion_episodes']} | "
                    f"[{name}.html]({name}.html) |")
        print(f"  {name:28s} {result.verdict:18s} "
              f"misses={result.kpis['deadline_misses']:3d}")
    # Flagship: autopilot proof + fixed model + QNX trace export.
    model = load_model(flagship)
    ap = autopilot(model, progress=None)
    if ap.fixed_model is not None:
        dump_model(ap.fixed_model, outdir / "brake-by-wire.fixed.toml")
    if ap.after_trace is not None and ap.fixed is not None:
        (outdir / "proof.html").write_text(
            render_html(ap.after_trace, ap.fixed_model, ap.fixed, ap,
                        title="Brake-by-wire · verified fix"), encoding="utf-8")
    (outdir / "qnx-kernel-trace.txt").write_text(
        simulate(model).to_traceprinter_text(), encoding="utf-8")
    (outdir / "proof.json").write_text(
        json.dumps(ap.to_dict(), indent=2) + "\n", encoding="utf-8")
    summary = "\n".join([
        "# Chronoscope demo artifacts", "",
        "Generated with `python3 -m chronoscope demo`. All reports are "
        "standalone, offline HTML (no CDN).", "",
        "| Model | Verdict | Score | Misses | Inversions | Report |",
        "| --- | --- | ---: | ---: | ---: | --- |", *rows, "",
        "## Flagship proof", "",
        f"- Baseline: **{ap.proof['before']['verdict']}**, "
        f"{ap.proof['before']['deadline_misses']} deadline misses",
        f"- Patched: **{ap.proof['after']['verdict']}**, "
        f"{ap.proof['after']['deadline_misses']} deadline misses",
        f"- {ap.candidates_evaluated} QNX configurations simulated; "
        f"{len(ap.patches)} patches kept",
        "- See `proof.html` and `proof.json`.",
        "- `qnx-kernel-trace.txt` is the simulated trace exported in "
        "`traceprinter` format, readable by `chronoscope import`.",
    ]) + "\n"
    (outdir / "README.md").write_text(summary, encoding="utf-8")
    print(f"\nDemo artifacts written to {outdir}")
    print(f"Flagship proof: {ap.proof['before']['deadline_misses']} -> "
          f"{ap.proof['after']['deadline_misses']} deadline misses, "
          f"{ap.candidates_evaluated} configurations simulated")
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(f"Chronoscope for QNX 8.0 v{__version__} (Python {sys.version.split()[0]})")
    print("Standard library only. QNX SDP 8.0 / QNX Everywhere.")
    return 0


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="chronoscope",
        description="Chronoscope for QNX 8.0 -- prove exactly when your code runs.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="Docs: https://www.qnx.com/developers/docs/8.0/")
    p.add_argument("--version", action="version",
                   version=f"chronoscope {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add(name: str, help_: str, fn) -> argparse.ArgumentParser:
        sp = sub.add_parser(name, help=help_, description=help_)
        sp.set_defaults(func=fn)
        return sp

    sp = add("init", "Write a starter QNX system model (TOML)", cmd_init)
    sp.add_argument("--out", default="my-system.toml")
    sp.add_argument("--force", action="store_true")

    sp = add("simulate", "Simulate a QNX 8.0 system model", cmd_simulate)
    sp.add_argument("model")
    sp.add_argument("--out", help="write Chronoscope JSON trace")
    sp.add_argument("--traceprinter", help="export traceprinter-compatible text")
    sp.add_argument("--summary", action="store_true", help="also print an audit")

    sp = add("audit", "Audit determinism of a model", cmd_audit)
    sp.add_argument("model")
    sp.add_argument("--json", dest="json_out")
    sp.add_argument("--html")
    sp.add_argument("--markdown")
    sp.add_argument("--quiet", action="store_true")
    sp.add_argument("--strict", action="store_true",
                    help="exit 1 when deadline misses remain (CI gate)")

    sp = add("fix", "Derive and verify a minimal QNX configuration fix", cmd_fix)
    sp.add_argument("model")
    sp.add_argument("--out", help="write the patched TOML model")
    sp.add_argument("--report", help="write the before/after HTML proof")
    sp.add_argument("--json", dest="json_out")
    sp.add_argument("--max-rounds", type=int, default=3)
    sp.add_argument("--max-candidates", type=int, default=110)
    sp.add_argument("--quiet", action="store_true")
    sp.add_argument("--strict", action="store_true")

    sp = add("report", "Render a standalone HTML determinism report", cmd_report)
    sp.add_argument("model")
    sp.add_argument("--out", default="chronoscope-report.html")
    sp.add_argument("--markdown")
    sp.add_argument("--fix", action="store_true",
                    help="include the autopilot proof and patched timeline")
    sp.add_argument("--quiet", action="store_true")

    sp = add("import", "Import a real QNX artifact (traceprinter/slog2info/pidin)", cmd_import)
    sp.add_argument("file")
    sp.add_argument("--out", help="write Chronoscope JSON trace")
    sp.add_argument("--format", choices=["auto", "traceprinter", "slog2", "pidin"],
                    default="auto")
    sp.add_argument("--audit", action="store_true")

    sp = add("inspect", "Lint a QNX mkifs .build image buildfile", cmd_inspect)
    sp.add_argument("buildfile")
    sp.add_argument("--model", help="cross-check priorities against a model")
    sp.add_argument("--json", dest="json_out")
    sp.add_argument("--quiet", action="store_true")
    sp.add_argument("--strict", action="store_true")

    sp = add("reconcile", "Compare a simulated trace against a real QNX trace",
             cmd_reconcile)
    sp.add_argument("simulated", help="simulated .json trace")
    sp.add_argument("measured", help="real QNX traceprinter/slog2/pidin artifact")
    sp.add_argument("--json", dest="json_out")

    sp = add("demo", "Generate all demo artifacts (HTML reports, proof, QNX trace)",
             cmd_demo)
    sp.add_argument("--out-dir", default="demo")

    add("verify", "Run the built-in self-verification test suite",
        cmd_verify).add_argument("--verbose", action="store_true")
    add("version", "Print version information", cmd_version)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "cmd", None) in (None, "version") or "--help" in (argv or []):
        pass
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except (ModelError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
