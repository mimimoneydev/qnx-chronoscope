"""Static checker for QNX OS image ``.build`` files (mkifs buildfiles).

This is the "startup and boot customization" half of the QNX connection: a
perfectly scheduled application still misses deadlines if the bootable image
runs an uninstrumented kernel, omits slogger2, starts the probe after the
control loop, or boots with a ``procnto -P`` ceiling lower than the priorities
the design assumes.

Reference: QNX SDP 8.0, Building Embedded Systems > OS Image Buildfiles, and
Utilities Reference > mkifs / procnto.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .model import SystemModel


@dataclass
class Check:
    code: str
    status: str           # PASS | WARN | FAIL
    message: str
    evidence: str
    fix: str
    qnx_reference: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class BuildfileReport:
    path: str
    score: float
    checks: list[Check] = field(default_factory=list)
    parsed: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "score": self.score,
                "checks": [c.to_dict() for c in self.checks],
                "parsed": self.parsed}


def parse_buildfile(text: str) -> dict[str, Any]:
    """Extract the structural parts of a mkifs buildfile."""
    virtual: list[str] = []
    bootstrap: list[str] = []
    script: list[str] = []
    files: list[str] = []
    in_script = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[virtual=") or line.startswith("[ image="):
            virtual.append(line)
            continue
        if line.startswith("[+script") or line.startswith("[script"):
            in_script = True
            continue
        if line.startswith("["):
            in_script = False
            continue
        if in_script:
            script.append(line)
        elif line.startswith("[bootstrap") or "startup-" in line:
            bootstrap.append(line)
        else:
            files.append(line)
    return {
        "virtual": virtual,
        "bootstrap": bootstrap,
        "script": script,
        "files": files,
        "script_text": "\n".join(script),
        "file_text": "\n".join(files),
    }


def lint_buildfile(path_or_text: str | Path, model: SystemModel | None = None,
                   is_text: bool = False) -> BuildfileReport:
    """Validate a QNX 8.0 OS image buildfile for determinism readiness."""
    if is_text:
        text, name = str(path_or_text), "<inline>"
    else:
        p = Path(path_or_text)
        if not p.is_file():
            raise FileNotFoundError(f"buildfile not found: {p}")
        text, name = p.read_text(encoding="utf-8"), str(p)
    parsed = parse_buildfile(text)
    low = text.lower()
    script = parsed["script_text"]
    files = parsed["file_text"]
    checks: list[Check] = []

    def chk(code: str, ok: bool | None, msg: str, evidence: str, fix: str,
            ref: str = "", warn_only: bool = False) -> None:
        status = "PASS" if ok else ("WARN" if warn_only else "FAIL")
        checks.append(Check(code, status, msg, evidence, fix, ref))

    chk("VIRTUAL", bool(parsed["virtual"]),
        "Image declares a [virtual=...] bootstrap line",
        parsed["virtual"][0] if parsed["virtual"] else "(missing)",
        "Add e.g. [virtual=x86_64,elf] .bootstrap = { startup-x86 procnto-smp-instr }",
        "Building Embedded Systems > OS Image Buildfiles")
    chk("ARCH_64", any(a in low for a in ("x86_64", "aarch64le")),
        "Targets a QNX 8.0 64-bit architecture (x86_64 or aarch64le)",
        " or ".join(parsed["virtual"][:1]),
        "QNX OS 8.0 is 64-bit: use x86_64 or aarch64le.",
        "QNX SDP 8.0 Quickstart Guide")
    chk("INSTRUMENTED_KERNEL", "procnto-smp-instr" in low,
        "Instrumented microkernel procnto-smp-instr is booted",
        "procnto-smp-instr" if "procnto-smp-instr" in low else "(missing)",
        "tracelogger requires the instrumented kernel; ship procnto-smp-instr.",
        "Utilities Reference > tracelogger")
    chk("STARTUP_SCRIPT", bool(parsed["script"]),
        "Startup script orders system services",
        f"{len(parsed['script'])} script line(s)",
        "Add a [+script] startup-script section.",
        "Building Embedded Systems > OS Images")
    chk("SLOGGER2", "slogger2" in script.lower() or "slogger2" in files.lower(),
        "slogger2 is started/included for structured event capture",
        next((l for l in script.splitlines() if "slogger2" in l.lower()),
             "(missing)"),
        "Add slogger2 to the image and start it before the probe.",
        "Utilities Reference > slogger2")
    chk("LIBC", "libc.so" in low,
        "C runtime shared objects are present in the image",
        next((l for l in files.splitlines() if "libc.so" in l.lower()), "(missing)"),
        "Include libc.so (and the libcpp/libm your binaries need).",
        "Building Embedded Systems > Sample Buildfiles")
    chk("ENV_PATH", "path=" in script.lower() or "path=" in low,
        "PATH / LD_LIBRARY_PATH configured for the target",
        next((l for l in script.splitlines() if "path=" in l.lower()), "(missing)"),
        "Set PATH and LD_LIBRARY_PATH in the startup script.",
        "Building Embedded Systems > Sample Buildfiles")
    chk("TRACELOGGER", "tracelogger" in low,
        "tracelogger present for kernel-event capture",
        next((l for l in text.splitlines() if "tracelogger" in l.lower()), "(missing)"),
        "Add tracelogger (or use TraceEvent() from the probe in daemon mode).",
        "Utilities Reference > tracelogger")
    chk("PROBE", "chronoscope" in low,
        "Chronoscope QNX-side probe / resource manager is in the image",
        next((l for l in text.splitlines() if "chronoscope" in l.lower()), "(missing)"),
        "Add chronoscope_resmgr and chronoscope_demo to the image.",
        "target/resmgr/chronoscope_resmgr.c", warn_only=True)
    chk("PRIORITY_CEILING", "-p " in low or "-p\t" in low,
        "procnto -P ceiling is explicit (non-default priorities)",
        next((l for l in text.splitlines() if "-p " in l.lower()), "(default 63)"),
        "Set procnto-smp-instr -P <ceiling> when the design uses priorities > 62.",
        "System Architecture > Scheduling priority", warn_only=True)
    chk("ABILITY", "ability" in low or "secpol" in low or "procmgr_ability" in low,
        "Security policy / procmgr abilities declared",
        next((l for l in text.splitlines()
              if any(k in l.lower() for k in ("ability", "secpol"))), "(missing)"),
        "Use a secpol policy or procmgr_ability() for PROCMGR_AID_PRIORITY.",
        "System Security Guide", warn_only=True)

    if model is not None:
        maxprio = max((t.priority for t in model.threads), default=0)
        pm = re.search(r"procnto\S*\s+-[Pp]\s*(\d+)", text)
        ceiling = int(pm.group(1)) if pm else 63
        if maxprio >= ceiling:
            checks.append(Check(
                "MODEL_PRIORITY_VS_BOOT", "FAIL",
                f"Model uses priority {maxprio} but boot ceiling is {ceiling}",
                f"threads with priority >= {ceiling}: "
                + ", ".join(t.name for t in model.threads if t.priority >= ceiling),
                f"Boot with procnto-smp-instr -P {maxprio + 1} or lower the "
                "model priorities / grant PROCMGR_AID_PRIORITY.",
                "System Architecture > Scheduling priority"))
        else:
            checks.append(Check(
                "MODEL_PRIORITY_VS_BOOT", "PASS",
                f"All model priorities ({maxprio} max) fit boot ceiling {ceiling}",
                "models/*.toml vs target/images/chronoscope.build", "No action.",
                "System Architecture > Scheduling priority"))

    fails = sum(1 for c in checks if c.status == "FAIL")
    warns = sum(1 for c in checks if c.status == "WARN")
    score = round(max(0.0, 100.0 - 12.0 * fails - 4.0 * warns), 1)
    return BuildfileReport(path=name, score=score, checks=checks, parsed=parsed)


def summarize(report: BuildfileReport) -> str:
    lines = [
        "=" * 68,
        f"  QNX 8.0 BUILDFILE DETERMINISM CHECK  ({report.score}/100)",
        "=" * 68,
    ]
    for c in report.checks:
        lines.append(f"  [{c.status:<4}] {c.code:<26} {c.message}")
        if c.status != "PASS":
            lines.append(f"          fix: {c.fix}")
    lines.append("=" * 68)
    return "\n".join(lines)
