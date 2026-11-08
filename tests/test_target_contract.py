"""Static contract checks for the QNX 8.0 target-side layer.

The C sources can only be *compiled* with QNX SDP 8.0's qcc, which is not
present in a generic CI environment. These tests still verify the parts that
matter to the host/target contract: the CHRONO wire format, the QNX API
inventory, the mkifs buildfile, and POSIX shell syntax of the deployment
scripts. They catch the class of bug where the C probe and the Python importer
silently drift apart.
"""

from __future__ import annotations

import re
import subprocess
import unittest
from pathlib import Path

from tests import ROOT
from chronoscope.buildfile import lint_buildfile
from chronoscope.importers import parse_slog2info

TARGET = ROOT / "target"
LIB = TARGET / "lib" / "chronoscope.c"
HEADER = TARGET / "include" / "chronoscope.h"
DEMO = TARGET / "demo" / "chronoscope_demo.c"
RESMGR = TARGET / "resmgr" / "chronoscope_resmgr.c"
BUILDFILE = TARGET / "images" / "chronoscope.build"

QNX_APIS = (
    "ClockTime", "slog2f", "slog2_register", "procmgr_ability",
    "PROCMGR_AID_PRIORITY", "ChannelCreate", "ConnectAttach", "MsgSend",
    "MsgReceive", "MsgReply", "dispatch_create", "resmgr_attach",
    "_DEVCTL_DATA", "SIGEV_PULSE_INIT", "pthread_setschedparam",
    "timer_create", "TraceEvent",
)


class TestTargetContract(unittest.TestCase):
    def test_target_sources_exist(self):
        for p in (LIB, HEADER, DEMO, RESMGR, BUILDFILE,
                  TARGET / "Makefile", TARGET / "common.mk",
                  TARGET / "README.md"):
            with self.subTest(file=str(p.relative_to(ROOT))):
                self.assertTrue(p.is_file())
                self.assertGreater(p.stat().st_size, 100)

    def test_chrono_wire_format_is_integer_microseconds(self):
        """The C probe and importers.py must agree on the timestamp unit."""
        c = LIB.read_text(encoding="utf-8")
        self.assertIn('"CHRONO %llu %s %s %s"', c,
                      "C probe must emit integer CLOCK_MONOTONIC microseconds")
        self.assertNotIn("%llu.%03llu", c,
                         "seconds.milliseconds would be 1000x off for the host")
        h = HEADER.read_text(encoding="utf-8")
        self.assertIn("INTEGER", h)
        self.assertIn("ABSOLUTE", h)

    def test_c_probe_lines_are_importable(self):
        """Lines shaped exactly like chrono_emit() output reconstruct jobs."""
        lines = [
            "CHRONO 1000000 pedal_sampler THREAD_REGISTER pedal_sampler prio=50",
            "CHRONO 1000200 pedal_sampler JOB_RELEASE pedal_cycle#0 deadline=1001400",
            "CHRONO 1000700 brake_server MSG_REPLY channel=brake server_prio=20",
            "CHRONO 1001600 pedal_sampler JOB_COMPLETE pedal_cycle#0 status=MISS response=1400",
        ]
        tr = parse_slog2info("\n".join(lines))
        self.assertEqual(len(tr.jobs), 1)
        self.assertEqual(tr.jobs[0].release_us, 1000200.0)
        self.assertEqual(tr.jobs[0].deadline_us, 1001400.0)
        self.assertEqual(tr.jobs[0].response_us, 1400.0)
        self.assertEqual(tr.jobs[0].status, "MISS")
        self.assertEqual(tr.horizon_us, 1001600.0)
        kinds = {e.kind for e in tr.events}
        self.assertIn("thread_register", kinds)
        self.assertIn("msg_reply", kinds)

    def test_qnx_api_inventory(self):
        sources = "\n".join(p.read_text(encoding="utf-8")
                            for p in (LIB, HEADER, DEMO, RESMGR))
        missing = [api for api in QNX_APIS if api not in sources]
        self.assertFalse(missing, f"QNX 8.0 APIs missing from target/: {missing}")

    def test_buildfile_scores_100_and_names_qnx8(self):
        rep = lint_buildfile(BUILDFILE)
        self.assertEqual(rep.score, 100.0)
        text = BUILDFILE.read_text(encoding="utf-8")
        self.assertIn("procnto-smp-instr -P 64", text)
        self.assertIn("tracelogger", text)
        self.assertIn("slogger2", text)
        self.assertTrue(all(c.status == "PASS" for c in rep.checks))

    def test_shell_scripts_are_valid_posix_sh(self):
        for p in sorted((TARGET / "scripts").glob("*.sh")):
            with self.subTest(script=p.name):
                r = subprocess.run(["sh", "-n", str(p)], capture_output=True,
                                   text=True, timeout=15)
                self.assertEqual(r.returncode, 0, r.stderr)

    def test_c_sources_have_balanced_braces(self):
        """Cheap syntax smoke test that needs no QNX headers."""
        for p in (LIB, DEMO, RESMGR, HEADER):
            with self.subTest(file=p.name):
                text = p.read_text(encoding="utf-8")
                stripped = re.sub(r"//[^\n]*|/\*.*?\*/|\"(?:\\.|[^\"\\])*\"",
                                  "", text, flags=re.S)
                self.assertEqual(stripped.count("{"), stripped.count("}"))
                self.assertEqual(stripped.count("("), stripped.count(")"))


if __name__ == "__main__":
    unittest.main()
