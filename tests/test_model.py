"""Declarative model: QNX 8.0 constants, validation and TOML round-trips."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from tests import ROOT
from chronoscope.model import (
    DEFAULT_UNPRIVILEGED_CEILING, PRIORITY_LEVELS, PRIVILEGED_MAX,
    ModelError, dump_model, load_model, model_from_dict,
)

MODELS = ROOT / "models"


class TestQNXConstants(unittest.TestCase):
    def test_256_priority_levels(self):
        self.assertEqual(PRIORITY_LEVELS, 256)
        self.assertEqual(PRIVILEGED_MAX, 253)
        self.assertEqual(DEFAULT_UNPRIVILEGED_CEILING, 63)

    def test_models_directory_has_flagship(self):
        names = {p.name for p in MODELS.glob("*.toml")}
        self.assertIn("brake-by-wire.toml", names)
        self.assertIn("hello-qnx.toml", names)
        self.assertGreaterEqual(len(names), 5)

    def test_every_shipped_model_loads(self):
        paths = sorted(MODELS.glob("*.toml"))
        self.assertTrue(paths)
        for p in paths:
            with self.subTest(model=p.name):
                m = load_model(p)
                self.assertTrue(m.name)
                self.assertTrue(m.threads)
                self.assertGreater(m.horizon_us, 0)


class TestValidation(unittest.TestCase):
    def base(self):
        return {
            "system": {"name": "unit", "horizon_us": 5000},
            "channels": [], "mutexes": [], "partitions": [],
            "threads": [{"name": "t", "priority": 10, "policy": "FIFO",
                         "jobs": []}],
        }

    def test_priority_zero_is_rejected(self):
        d = self.base()
        d["threads"][0]["priority"] = 0
        with self.assertRaises(ModelError):
            model_from_dict(d)

    def test_reserved_ist_priorities_rejected(self):
        for prio in (254, 255):
            d = self.base()
            d["threads"][0]["priority"] = prio
            with self.subTest(prio=prio), self.assertRaises(ModelError):
                model_from_dict(d)

    def test_unknown_policy_rejected(self):
        d = self.base()
        d["threads"][0]["policy"] = "DEADLINE"
        with self.assertRaises(ModelError):
            model_from_dict(d)

    def test_unknown_channel_reference_rejected(self):
        d = self.base()
        d["threads"][0]["jobs"] = [{
            "name": "j", "period_us": 1000, "deadline_us": 900,
            "steps": [{"op": "send", "channel": "nope", "service_us": 10}],
        }]
        with self.assertRaises(ModelError):
            model_from_dict(d)

    def test_channel_without_server_rejected(self):
        d = self.base()
        d["channels"] = [{"name": "c", "server": "t"}]
        d["threads"][0]["jobs"] = [{
            "name": "j", "period_us": 1000, "deadline_us": 900,
            "steps": [{"op": "send", "channel": "c", "service_us": 10}],
        }]
        with self.assertRaises(ModelError):
            model_from_dict(d)

    def test_aps_budgets_must_sum_to_100(self):
        d = self.base()
        d["system"]["aps_enabled"] = True
        d["partitions"] = [
            {"name": "system", "budget_pct": 40.0},
            {"name": "other", "budget_pct": 40.0},
        ]
        with self.assertRaises(ModelError):
            model_from_dict(d)

    def test_sporadic_requires_budget_and_period(self):
        d = self.base()
        d["threads"][0]["policy"] = "SPORADIC"
        with self.assertRaises(ModelError):
            model_from_dict(d)

    def test_fixed_priority_channel_syncs_to_server(self):
        d = self.base()
        d["channels"] = [{"name": "c", "server": "srv",
                          "fixed_priority": True}]
        d["threads"].append({"name": "srv", "priority": 20, "role": "server",
                             "channel": "c", "jobs": []})
        m = model_from_dict(d)
        self.assertTrue(m.thread("srv").fixed_priority)


class TestTomlRoundTrip(unittest.TestCase):
    def test_dump_then_load_is_lossless(self):
        for p in sorted(MODELS.glob("*.toml")):
            with self.subTest(model=p.name), tempfile.TemporaryDirectory() as td:
                original = load_model(p)
                dest = Path(td) / "round.toml"
                dump_model(original, dest)
                restored = load_model(dest)
                self.assertEqual(original.to_dict(), restored.to_dict())

    def test_dump_is_valid_toml_with_expected_sections(self):
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "m.toml"
            dump_model(load_model(MODELS / "brake-by-wire.toml"), dest)
            text = dest.read_text(encoding="utf-8")
            for token in ("[system]", "[[threads]]", "[[channels]]",
                          "[[interrupts]]"):
                self.assertIn(token, text)


if __name__ == "__main__":
    unittest.main()
