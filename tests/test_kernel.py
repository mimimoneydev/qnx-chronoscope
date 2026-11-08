"""QNX Neutrino 8.0 scheduler semantics exercised by the simulator."""

from __future__ import annotations

import unittest

from tests import ROOT
from chronoscope.kernel import simulate
from chronoscope.model import load_model

MODELS = ROOT / "models"


def flag(name: str, fixed: bool | None = None, ability: bool | None = None):
    m = load_model(MODELS / name)
    for c in m.channels:
        if fixed is not None:
            c.fixed_priority = fixed
    for t in m.threads:
        if t.channel:
            if fixed is not None:
                t.fixed_priority = fixed
            if ability is not None and t.role == "server":
                t.ability_priority = ability
    return m


class TestScheduler(unittest.TestCase):
    def test_priority_inheritance_boosts_server_at_send_time(self):
        m = load_model(MODELS / "hello-qnx.toml")
        tr = simulate(m)
        boosts = [e for e in tr.events if e.kind == "priority_boost"]
        self.assertTrue(boosts, "QNX message-driven PI must fire at MsgSend()")
        self.assertEqual(boosts[0].thread, "actuator_server")
        self.assertEqual(boosts[0].priority, 40)  # client priority

    def test_fixed_priority_channel_disables_inheritance(self):
        m = load_model(MODELS / "brake-by-wire.toml")
        tr = simulate(m)
        self.assertFalse([e for e in tr.events if e.kind == "priority_boost"])

    def test_removing_fixed_priority_flag_enables_inheritance(self):
        m = flag("brake-by-wire.toml", fixed=False)
        tr = simulate(m)
        self.assertTrue([e for e in tr.events if e.kind == "priority_boost"])

    def test_native_message_passing_counts(self):
        m = load_model(MODELS / "brake-by-wire.toml")
        tr = simulate(m)
        # Count kernel calls only: state-transition events also carry a
        # "MsgReply()" detail, but they are wakeups, not IPC transactions.
        calls = [e for e in tr.events if e.kind == "kernel_call"]
        sends = [e for e in calls if e.detail.startswith("MsgSend(")]
        replies = [e for e in calls if e.detail.startswith("MsgReply(")]
        receives = [e for e in calls if e.detail.startswith("MsgReceive(")]
        self.assertEqual(len(sends), 10)
        self.assertEqual(len(replies), 10)
        self.assertEqual(len(receives), 10)

    def test_send_and_receive_states_are_traced(self):
        tr = simulate(load_model(MODELS / "hello-qnx.toml"))
        kinds = {e.kind for e in tr.events}
        for state in ("SEND", "RECEIVE", "REPLY", "READY", "RUNNING"):
            self.assertIn(state, kinds)

    def test_jobs_have_deadline_and_response(self):
        tr = simulate(load_model(MODELS / "hello-qnx.toml"))
        self.assertTrue(tr.jobs)
        for j in tr.jobs:
            self.assertIsNotNone(j.response_us)
            self.assertGreater(j.deadline_us, j.release_us)

    def test_seeded_defect_produces_deadline_misses(self):
        tr = simulate(load_model(MODELS / "brake-by-wire.toml"))
        self.assertGreaterEqual(sum(1 for j in tr.jobs if j.missed), 2)

    def test_isr_to_ist_events_present(self):
        tr = simulate(load_model(MODELS / "brake-by-wire.toml"))
        self.assertTrue([s for s in tr.segments if s.kind == "isr"])
        self.assertTrue([e for e in tr.events if e.kind == "interrupt_entry"])
        self.assertTrue([e for e in tr.events if e.thread.endswith("_ist")])

    def test_smp_affinity_is_honoured(self):
        m = load_model(MODELS / "multicore-affinity.toml")
        tr = simulate(m)
        self.assertEqual(tr.cpus, 2)
        by_thread = {}
        for s in tr.segments:
            if s.kind == "run":
                by_thread.setdefault(s.thread, set()).add(s.cpu)
        for t in m.threads:
            if t.affinity and t.name in by_thread:
                self.assertTrue(
                    by_thread[t.name].issubset(set(t.affinity)),
                    f"{t.name} escaped its runmask: {by_thread[t.name]}")

    def test_sporadic_budget_drop_is_traced(self):
        tr = simulate(load_model(MODELS / "sporadic-server.toml"))
        self.assertTrue([e for e in tr.events if e.kind == "sporadic_drop"])

    def test_aps_bankruptcy_is_traced(self):
        tr = simulate(load_model(MODELS / "adaptive-partition.toml"))
        self.assertTrue(tr.meta.get("aps_bankruptcies"))

    def test_mutex_inheritance_model_is_deterministic(self):
        tr = simulate(load_model(MODELS / "infusion-pump.toml"))
        self.assertFalse([j for j in tr.jobs if j.missed])

    def test_simulation_is_reproducible(self):
        m = load_model(MODELS / "brake-by-wire.toml")
        a, b = simulate(m), simulate(m)
        self.assertEqual(a.to_dict(), b.to_dict())


if __name__ == "__main__":
    unittest.main()
