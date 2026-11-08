"""End-to-end pipeline: trace export/import, audit, autopilot, reports, CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests import ROOT
from chronoscope.anomaly import cusum, detect_anomalies, mad, median
from chronoscope.audit import _is_contended_block, audit
from chronoscope.autopilot import autopilot, objective
from chronoscope.buildfile import lint_buildfile
from chronoscope.importers import (
    parse_pidin, parse_slog2info, parse_traceprinter, reconcile,
)
from chronoscope.kernel import simulate
from chronoscope.model import load_model
from chronoscope.report import render_html, render_markdown, report_json
from chronoscope.trace import Trace, TraceEvent

MODELS = ROOT / "models"
FLAGSHIP = MODELS / "brake-by-wire.toml"


class TestTraceRoundTrip(unittest.TestCase):
    def test_traceprinter_export_reimports(self):
        tr = simulate(load_model(MODELS / "hello-qnx.toml"))
        text = tr.to_traceprinter_text()
        self.assertIn("TRACE_SYS_RELEASE:: 8.0", text)
        self.assertIn("THREAD  :THRUNNING", text)
        self.assertIn("KER_CALL :MsgSend(", text)
        back = parse_traceprinter(text, name="roundtrip")
        self.assertEqual(back.meta["qnx_version"], "8.0")
        self.assertEqual(len(back.jobs), len(tr.jobs))
        self.assertTrue(back.segments)

    def test_priority_tags_survive_import(self):
        tr = simulate(load_model(FLAGSHIP))
        back = parse_traceprinter(tr.to_traceprinter_text())
        run_prios = {s.thread: s.priority for s in back.segments
                     if s.kind == "run" and s.priority}
        self.assertIn("pedal_sampler", run_prios)
        self.assertEqual(run_prios["pedal_sampler"], 50)

    def test_process_names_survive_import(self):
        tr = simulate(load_model(FLAGSHIP))
        back = parse_traceprinter(tr.to_traceprinter_text())
        names = set(back.meta.get("thread_priorities", {}))
        self.assertIn("brake_server", names)

    def test_json_round_trip(self):
        tr = simulate(load_model(MODELS / "hello-qnx.toml"))
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "t.json"
            tr.to_json(p)
            self.assertEqual(Trace.from_json(p).to_dict(), tr.to_dict())

    def test_chrono_annotations_use_their_own_class(self):
        tr = simulate(load_model(MODELS / "hello-qnx.toml"))
        text = tr.to_traceprinter_text()
        self.assertIn("CHRONO  :JOB_RELEASE", text)
        self.assertNotIn("THREAD  :JOB_RELEASE", text)


class TestAudit(unittest.TestCase):
    def test_contended_block_classifier(self):
        for reason in ("STATE_SEND on brake", "STATE_REPLY on brake",
                       "pthread_mutex_lock(m)", "SEND", "MUTEX", "SEM"):
            with self.subTest(reason=reason):
                self.assertTrue(_is_contended_block(reason))
        for reason in ("waiting for worker work", "nanosleep(100us)",
                       "InterruptWait(irq=7)", "RECEIVE", "NANOSLEEP",
                       "THINTR", ""):
            with self.subTest(reason=reason):
                self.assertFalse(_is_contended_block(reason))

    def test_hello_qnx_is_deterministic(self):
        m = load_model(MODELS / "hello-qnx.toml")
        r = audit(simulate(m), m)
        self.assertEqual(r.verdict, "DETERMINISTIC")
        self.assertEqual(r.kpis["deadline_misses"], 0)
        self.assertEqual(r.kpis["priority_inversion_episodes"], 0)

    def test_flagship_detects_inversion_and_misses(self):
        m = load_model(FLAGSHIP)
        r = audit(simulate(m), m)
        self.assertEqual(r.verdict, "NON-DETERMINISTIC")
        self.assertGreater(r.kpis["deadline_misses"], 0)
        self.assertGreater(r.kpis["priority_inversion_episodes"], 0)
        codes = {f.code for f in r.findings}
        self.assertIn("DEADLINE_MISS", codes)
        self.assertIn("PRIORITY_INVERSION", codes)
        self.assertIn("FIXED_PRIORITY_CHANNEL", codes)

    def test_simulated_and_imported_audits_agree(self):
        m = load_model(FLAGSHIP)
        sim = simulate(m)
        back = parse_traceprinter(sim.to_traceprinter_text())
        a, b = audit(sim, m), audit(back)
        self.assertEqual(a.verdict, b.verdict)
        self.assertEqual(a.kpis["deadline_misses"], b.kpis["deadline_misses"])
        self.assertEqual(a.kpis["priority_inversion_episodes"],
                         b.kpis["priority_inversion_episodes"])

    def test_smp_unrelated_low_priority_runs_are_not_inversions(self):
        m = load_model(MODELS / "multicore-affinity.toml")
        r = audit(simulate(m), m)
        self.assertEqual(r.kpis["priority_inversion_episodes"], 0)

    def test_timing_evidence_is_structured(self):
        m = load_model(FLAGSHIP)
        r = audit(simulate(m), m)
        ev = r.timing_evidence
        self.assertEqual(ev["qnx_version"], "8.0")
        self.assertIn("wcet_and_jitter_per_thread", ev)
        self.assertIn("priority_assignment", ev)
        self.assertIn("NOT a certification artifact", ev["purpose"])
        self.assertTrue(all(f.qnx_reference for f in r.findings))

    def test_findings_are_json_serialisable(self):
        r = audit(simulate(load_model(FLAGSHIP)), load_model(FLAGSHIP))
        self.assertEqual(json.loads(report_json(r))["verdict"], r.verdict)


class TestAutopilot(unittest.TestCase):
    def test_fix_is_counterfactually_verified(self):
        m = load_model(FLAGSHIP)
        res = autopilot(m)
        self.assertTrue(res.verified)
        self.assertEqual(res.baseline.kpis["deadline_misses"], 2)
        self.assertEqual(res.fixed.kpis["deadline_misses"], 0)
        self.assertEqual(res.fixed.kpis["priority_inversion_episodes"], 0)
        self.assertGreater(res.candidates_evaluated, 0)
        self.assertTrue(res.patches)

    def test_winning_patch_is_the_qnx_channel_flag(self):
        res = autopilot(load_model(FLAGSHIP))
        self.assertEqual(res.patches[0].kind, "channel")
        self.assertEqual(res.patches[0].field, "fixed_priority")
        self.assertEqual((res.patches[0].old, res.patches[0].new), (True, False))
        self.assertIn("ChannelCreate", res.patches[0].qnx_reference)

    def test_fixed_model_is_loadable_and_deterministic(self):
        from chronoscope.model import dump_model, model_from_dict
        res = autopilot(load_model(FLAGSHIP))
        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "fixed.toml"
            dump_model(res.fixed_model, p)
            m2 = load_model(p)
        self.assertFalse([c for c in m2.channels if c.fixed_priority])
        self.assertEqual(audit(simulate(m2), m2).kpis["deadline_misses"], 0)
        # also check in-memory round-trip used by the search
        self.assertEqual(model_from_dict(res.fixed_model.to_dict()).name, m2.name)

    def test_a_clean_model_needs_no_patches(self):
        res = autopilot(load_model(MODELS / "hello-qnx.toml"))
        self.assertEqual(res.patches, [])
        self.assertFalse(res.verified)

    def test_objective_penalises_misses(self):
        m = load_model(FLAGSHIP)
        before = objective(audit(simulate(m), m))
        after = objective(autopilot(m).fixed)
        self.assertLess(after, before)


class TestImporters(unittest.TestCase):
    def test_slog2_chrono_lines(self):
        text = "\n".join([
            "CHRONO 1000.000 pedal_sampler JOB_RELEASE pedal_cycle#0 deadline=2200",
            "CHRONO 1700.000 pedal_sampler JOB_COMPLETE pedal_cycle#0 status=OK response=700",
        ])
        tr = parse_slog2info(text)
        self.assertEqual(len(tr.events), 2)
        self.assertEqual(tr.events[0].kind, "job_release")

    def test_slog2_reconstructs_jobs_and_deadlines(self):
        """A real QNX slogger2 capture must yield audit-able JobRecords."""
        bare = "\n".join([
            "CHRONO 1000 pedal_sampler JOB_RELEASE pedal_cycle#0 deadline=2200",
            "CHRONO 1700 pedal_sampler JOB_COMPLETE pedal_cycle#0 status=OK response=700",
            "CHRONO 3000 pedal_sampler JOB_RELEASE pedal_cycle#1 deadline=4200",
            "CHRONO 4500 pedal_sampler JOB_COMPLETE pedal_cycle#1 status=MISS response=1500",
        ])
        embedded = "\n".join([
            "Jan 01 00:00:00.000 chronoscope 6 " + ln for ln in bare.splitlines()
        ])
        for text in (bare, embedded):
            with self.subTest(kind="bare" if text is bare else "slog2info"):
                tr = parse_slog2info(text)
                self.assertEqual(len(tr.jobs), 2)
                self.assertEqual(tr.jobs[0].status, "OK")
                self.assertEqual(tr.jobs[0].response_us, 700)
                self.assertEqual(tr.jobs[1].status, "MISS")
                self.assertEqual(tr.meta["parsed_jobs"], 2)
        # Integer microseconds (the C probe's wire format) must not be
        # misread as seconds.milliseconds.
        self.assertEqual(parse_slog2info(bare).jobs[0].release_us, 1000.0)

    def test_pidin_snapshot(self):
        text = "pid tid name pri state\n101 1 pedal_sampler 50 RUNNING\n102 2 brake_server 20 RECEIVE\n"
        tr = parse_pidin(text)
        self.assertEqual(tr.meta["thread_count"], 2)
        self.assertEqual(tr.meta["threads"][0]["priority"], 50)

    def test_reconcile_reports_drift(self):
        sim = simulate(load_model(MODELS / "hello-qnx.toml"))
        rep = reconcile(sim, sim)
        self.assertEqual(rep["verdict"], "ALIGNED")
        self.assertEqual(rep["drift_count"], 0)
        self.assertTrue(rep["rows"])

    def test_trace_event_defensive_coercion(self):
        tr = Trace()
        tr.event(1.5, "X", cpu="2", priority="7", pid="9", tid="3")
        self.assertEqual(tr.events[0].cpu, 2)
        self.assertEqual(tr.events[0].priority, 7)


class TestAnomaly(unittest.TestCase):
    def test_median_and_mad(self):
        self.assertEqual(median([1, 2, 3, 4]), 2.5)
        self.assertEqual(mad([5, 5, 5, 5]), 0.0)

    def test_cusum_finds_sustained_shift(self):
        xs = [10.0] * 20 + [40.0] * 20
        self.assertTrue(cusum(xs))

    def test_detect_anomalies_on_spike(self):
        tr = Trace(horizon_us=10000)
        for i, v in enumerate([100] * 12 + [900]):
            tr.jobs.append(type("J", (), {})()) if False else None
        from chronoscope.trace import JobRecord
        tr.jobs = [JobRecord("t", "j", float(i * 100), float(i * 100 + 500),
                             complete_us=float(i * 100 + v))
                   for i, v in enumerate([100] * 12 + [900])]
        self.assertTrue(detect_anomalies(tr, z_limit=2.0))


class TestReports(unittest.TestCase):
    def test_html_is_standalone_and_offline(self):
        m = load_model(FLAGSHIP)
        tr = simulate(m)
        html = render_html(tr, m, audit(tr, m))
        self.assertTrue(html.startswith("<!DOCTYPE html>"))
        self.assertIn("<style>", html)
        self.assertNotIn("http://", html.replace("http://www.w3.org", ""))
        self.assertIn("QNX", html)

    def test_markdown_and_json(self):
        m = load_model(MODELS / "hello-qnx.toml")
        r = audit(simulate(m), m)
        self.assertIn("Chronoscope", render_markdown(simulate(m), m, r))
        self.assertIn("verdict", json.loads(report_json(r)))


class TestBuildfile(unittest.TestCase):
    def test_shipped_buildfile_scores_100(self):
        rep = lint_buildfile(ROOT / "target" / "images" / "chronoscope.build",
                             model=load_model(FLAGSHIP))
        self.assertEqual(rep.score, 100.0)
        self.assertFalse([c for c in rep.checks if c.status != "PASS"])

    def test_missing_kernel_is_detected(self):
        rep = lint_buildfile("[virtual=x86_64,elf] .bootstrap = {\n"
                             "startup-x86\nprocnto-smp\n}\n", is_text=True)
        codes = {c.code for c in rep.checks if c.status != "PASS"}
        self.assertIn("INSTRUMENTED_KERNEL", codes)


class TestCLI(unittest.TestCase):
    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "chronoscope", *map(str, args)],
                             cwd=ROOT, capture_output=True, text=True, timeout=60)

    def test_version(self):
        r = self.run_cli("version")
        self.assertEqual(r.returncode, 0)
        self.assertIn("QNX 8.0", r.stdout)

    def test_audit_cli(self):
        r = self.run_cli("audit", FLAGSHIP)
        self.assertEqual(r.returncode, 0)
        self.assertIn("NON-DETERMINISTIC", r.stdout)

    def test_fix_cli(self):
        with tempfile.TemporaryDirectory() as td:
            out = Path(td) / "fixed.toml"
            r = self.run_cli("fix", FLAGSHIP, "--out", out, "--quiet")
            self.assertEqual(r.returncode, 0)
            self.assertTrue(out.is_file())
            self.assertEqual(load_model(out).channels[0].fixed_priority, False)

    def test_inspect_cli(self):
        r = self.run_cli("inspect", ROOT / "target/images/chronoscope.build",
                         "--model", FLAGSHIP)
        self.assertIn("(100.0/100)", r.stdout)

    def test_import_cli_audit(self):
        with tempfile.TemporaryDirectory() as td:
            trace = Path(td) / "qnx.txt"
            trace.write_text(simulate(load_model(FLAGSHIP)).to_traceprinter_text(),
                             encoding="utf-8")
            r = self.run_cli("import", trace, "--audit")
            self.assertEqual(r.returncode, 0)
            self.assertIn("NON-DETERMINISTIC", r.stdout)

    @unittest.skipIf(os.environ.get("CHRONOSCOPE_VERIFY") == "1",
                     "nested verifier: skip to avoid infinite recursion")
    def test_verify_cli_runs_this_suite(self):
        r = self.run_cli("verify")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("failures,", r.stdout)

    def test_demo_cli_generates_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            r = self.run_cli("demo", "--out-dir", td)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            for name in ("proof.html", "proof.json", "qnx-kernel-trace.txt",
                         "README.md", "brake-by-wire.html"):
                self.assertTrue((Path(td) / name).is_file(), name)


if __name__ == "__main__":
    unittest.main()
