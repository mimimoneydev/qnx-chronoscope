# Chronoscope demo artifacts

Generated with `python3 -m chronoscope demo`. All reports are standalone, offline HTML (no CDN).

| Artifact | What it is |
| --- | --- |
| `chronoscope-demo.mp4` | The Devpost demo video (1920×1080, under 3 minutes, original narration) |
| `build_video.py` | Reproducible builder: live CLI captures + headless-Chrome report screenshots + local TTS |
| `VIDEO.md` | YouTube title, description, tags, transcript and upload instructions |
| `proof.html` / `proof.json` | Before/after Autopilot proof shown in the video |
| `*.html` / `*.json` | One offline report per shipped model |
| `qnx-kernel-trace.txt` | Simulated trace exported in `traceprinter` format |
| `brake-by-wire.fixed.toml` | The Autopilot's verified patch, as a runnable model |

`qnx-kernel-trace.txt` and the files in `examples/traces/` are simulated
round-trip samples, clearly labelled — they are **not** a live QNX capture.
See [`examples/traces/README.md`](../examples/traces/README.md) and
[`target/README.md`](../target/README.md) for the honest verification status.

| Model | Verdict | Score | Misses | Inversions | Report |
| --- | --- | ---: | ---: | ---: | --- |
| adaptive-partition | NON-DETERMINISTIC | 75.0 | 2 | 0 | [adaptive-partition.html](adaptive-partition.html) |
| brake-by-wire | NON-DETERMINISTIC | 57.0 | 2 | 12 | [brake-by-wire.html](brake-by-wire.html) |
| hello-qnx | DETERMINISTIC | 100.0 | 0 | 0 | [hello-qnx.html](hello-qnx.html) |
| infusion-pump | DETERMINISTIC | 100.0 | 0 | 0 | [infusion-pump.html](infusion-pump.html) |
| multicore-affinity | DETERMINISTIC | 100.0 | 0 | 0 | [multicore-affinity.html](multicore-affinity.html) |
| sporadic-server | NON-DETERMINISTIC | 65.5 | 3 | 0 | [sporadic-server.html](sporadic-server.html) |

## Flagship proof

- Baseline: **NON-DETERMINISTIC**, 2 deadline misses
- Patched: **DETERMINISTIC**, 0 deadline misses
- 18 QNX configurations simulated; 1 patches kept
- See `proof.html` and `proof.json`.
- `qnx-kernel-trace.txt` is the simulated trace exported in `traceprinter` format, readable by `chronoscope import`.
