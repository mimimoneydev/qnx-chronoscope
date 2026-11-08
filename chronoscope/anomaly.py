"""Dependency-free statistical anomaly detection on QNX timing traces.

Chronoscope does not bolt an LLM onto a scheduling problem. Instead it ships
small, explainable, deterministic detectors that an engineer can audit:

* robust z-score (median / MAD) -- resistant to the very outliers we seek,
* CUSUM changepoint detection -- catches a *sustained* latency regression,
  which is what matters for a real-time system that used to meet its deadlines,
* EWMA drift -- a lightweight online control chart for streamed slogger2 data.

All are O(n), need no training data, and are fully reproducible -- important
when the output is used as timing evidence in a safety case.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable

from .trace import Trace


@dataclass
class Anomaly:
    thread: str
    index: int
    kind: str
    value_us: float
    score: float
    explanation: str


def median(xs: list[float]) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def mad(xs: list[float]) -> float:
    """Median absolute deviation (scaled to be a consistent sigma estimator)."""
    if len(xs) < 2:
        return 0.0
    med = median(xs)
    return 1.4826 * median([abs(x - med) for x in xs])


def ewma(xs: Iterable[float], alpha: float = 0.2) -> list[float]:
    out: list[float] = []
    prev: float | None = None
    for x in xs:
        prev = x if prev is None else alpha * x + (1 - alpha) * prev
        out.append(prev)
    return out


def scale(xs: list[float]) -> float:
    """Robust sigma with a quantization floor.

    Perfectly repeatable QNX timings give MAD == 0. A zero denominator would
    hide every genuine spike, so sigma is floored at 0.1% of the median (and
    at 1 ns, the resolution of ClockTime(CLOCK_MONOTONIC)). The floor is small
    enough not to mask ordinary jitter, but large enough that a step change on
    a noise-free baseline is still detectable.
    """
    med = median(xs)
    return max(mad(xs), 1e-3 * abs(med), 1e-6)


def cusum(xs: list[float], threshold: float = 5.0, drift: float = 0.5,
          min_run: int = 3) -> list[int]:
    """Return indices at which a *sustained* mean shift is detected.

    ``threshold`` and ``drift`` are expressed in robust sigma units.

    Two details matter for real-time timing evidence:

    * sigma is estimated from the baseline window only. Estimating it across
      the whole series (which may contain two regimes) inflates it enough to
      hide the very shift we are looking for.
    * a threshold crossing is only reported when the following ``min_run``
      samples stay shifted. A single deadline-busting spike is an outlier for
      the robust z-score detector to report, not a changepoint.
    """
    n = len(xs)
    if n < 2 * min_run + 3:
        return []
    w = min(max(5, n // 5), n - min_run)
    baseline = median(xs[:w])
    sigma = scale(xs[:w])
    z = [(x - baseline) / sigma for x in xs]
    s_pos, s_neg = 0.0, 0.0
    hits: list[int] = []
    refractory = max(min_run, n // 5)
    for i, v in enumerate(z):
        s_pos = max(0.0, s_pos + v - drift)
        s_neg = max(0.0, s_neg - v - drift)
        if max(s_pos, s_neg) <= threshold:
            continue
        direction = 1 if s_pos >= s_neg else -1
        ahead = z[i + 1:i + 1 + min_run]
        sustained = (len(ahead) == min_run
                     and direction * median(ahead) > drift)
        if sustained and (not hits or i - hits[-1] >= refractory):
            hits.append(i)
        # Reset on every crossing so one long shift is not re-reported at
        # every subsequent sample.
        s_pos = s_neg = 0.0
    return hits


def detect_anomalies(trace: Trace, z_limit: float = 3.5) -> list[Anomaly]:
    """Flag latency outliers and sustained regressions in a QNX trace."""
    by_thread: dict[str, list[tuple[int, float]]] = {}
    for i, j in enumerate(trace.jobs):
        r = j.response_us
        if r is None:
            continue
        by_thread.setdefault(j.thread, []).append((i, r))
    out: list[Anomaly] = []
    for thread, pairs in sorted(by_thread.items()):
        xs = [v for _, v in pairs]
        if len(xs) < 5:
            continue
        med, sigma = median(xs), scale(xs)
        for (idx, v) in pairs:
            z = (v - med) / sigma
            if z > z_limit:
                out.append(Anomaly(
                    thread=thread, index=idx, kind="latency_spike",
                    value_us=round(v, 3), score=round(z, 2),
                    explanation=(f"robust z={z:.2f} (median={med:.1f}us, "
                                 f"MAD-sigma={sigma:.3f}us); response time "
                                 f"is {(v / max(med, 1e-9)):.1f}x baseline")))
        for idx in cusum(xs):
            global_idx, value = pairs[idx]
            out.append(Anomaly(
                thread=thread, index=global_idx, kind="sustained_regression",
                value_us=round(value, 3), score=round((value - med) / max(sigma, 1e-9), 2),
                explanation=("CUSUM changepoint: latency distribution shifted "
                             "upward from this sample; re-measure WCET on the "
                             "QNX 8.0 target and check for a new low-priority "
                             "workload or partition budget change")))
    return out


def ewma_control_chart(trace: Trace, alpha: float = 0.2,
                       sigma_limit: float = 3.0) -> dict[str, Any]:
    """Return EWMA series and out-of-control indices for each thread."""
    by_thread: dict[str, list[float]] = {}
    for j in trace.jobs:
        r = j.response_us
        if r is None:
            continue
        by_thread.setdefault(j.thread, []).append(r)
    result: dict[str, Any] = {}
    for thread, xs in sorted(by_thread.items()):
        if len(xs) < 3:
            continue
        series = ewma(xs, alpha)
        sigma = mad(xs)
        center = median(xs)
        limit = center + sigma_limit * sigma
        result[thread] = {
            "center_us": round(center, 3),
            "upper_limit_us": round(limit, 3),
            "ewma_us": [round(x, 3) for x in series],
            "out_of_control": [i for i, x in enumerate(series)
                               if sigma > 0 and x > limit],
        }
    return result
