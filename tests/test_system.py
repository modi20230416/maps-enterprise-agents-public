import json
import random
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from enterprise_agent.cli import main, run_scenario
from enterprise_agent.experiments import benchmark
from enterprise_agent.models import Assignment, Event, Plan, Snapshot
from enterprise_agent.planning import OrchestratorAgent
from enterprise_agent.scenarios import base_snapshot, order, scenarios
from enterprise_agent.scheduler import OptimizationAgent, earliest_slot, validate_plan
from enterprise_agent.store import StateStore
from enterprise_agent.warehouse import MessageBus, WarehouseAgent


class ConstraintsTests(unittest.TestCase):
    def test_slot_finds_gaps_and_respects_half_open_boundaries(self):
        self.assertEqual(earliest_slot(0, 10, [(10, 20)]), 0)
        self.assertEqual(earliest_slot(5, 10, [(10, 20), (25, 30)]), 30)
        self.assertEqual(earliest_slot(20, 5, [(10, 20), (25, 30)]), 20)

    def test_input_rejects_bad_duration_machine_ids_and_duplicate_orders(self):
        for mutation in ("duration", "machine", "duplicate", "bool", "negative", "window"):
            data = base_snapshot()
            if mutation == "duration":
                data["orders"][0]["operations"][0]["duration"] = 0
            elif mutation == "machine":
                data["orders"][0]["operations"][0]["machines"] = ["UNKNOWN"]
            elif mutation == "duplicate":
                data["orders"].append(deepcopy(data["orders"][0]))
            elif mutation == "bool":
                data["orders"][0]["priority"] = True
            elif mutation == "negative":
                data["stock"]["steel"] = -1
            else:
                data["machines"][0]["unavailable"] = [[20, 10]]
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                Snapshot.parse(data)

    def test_round_trip_snapshot(self):
        s = Snapshot.parse(base_snapshot())
        self.assertEqual(s, Snapshot.parse(s.to_dict()))

    def test_warehouse_atomic_kit_rejection_and_lead_time(self):
        data = base_snapshot()
        data["stock"] = {"steel": 1}
        data["receipts"] = [{"id": "R", "material": "other", "quantity": 1, "eta": 50}]
        s = Snapshot.parse(data)
        warehouse = WarehouseAgent(s, MessageBus("test"))
        bad = order("BAD", 10, 100, 10)
        bad["materials"] = {"steel": 1, "missing": 1}
        self.assertIsNone(warehouse.reserve(Snapshot.parse({**data, "orders": [bad]}).orders[0])[0])
        ready, allocations, _ = warehouse.reserve(s.orders[0])
        self.assertEqual(ready, 10)
        self.assertEqual(sum(a["quantity"] for a in allocations), 1)
        other = order("OTHER", 1, 200, 10, "other")
        self.assertEqual(warehouse.reserve(Snapshot.parse({**data, "orders": [other]}).orders[0])[0], 60)

    def test_validator_catches_conflicts_and_material_overdraw(self):
        data = base_snapshot()
        data["stock"]["steel"] = 1
        s = Snapshot.parse(data)
        assignments = []
        for o in s.orders:
            assignments.extend([Assignment(o.id, "cut", "CUT-1", 10, 10 + o.operations[0].duration),
                                Assignment(o.id, "assemble", "ASSEMBLY-1", 12, 32)])
        errors = validate_plan(s, Plan(assignments=assignments))
        self.assertTrue(any("overlap" in e for e in errors))
        self.assertTrue(any("precedence" in e for e in errors))
        self.assertTrue(any("overdraw" in e for e in errors))

    def test_randomized_feasibility_and_determinism(self):
        rng = random.Random(20261014)
        optimizer = OptimizationAgent()
        for index in range(35):
            data = base_snapshot()
            count = rng.randint(0, 5)
            data["stock"]["steel"] = rng.randint(0, 5)
            data["receipts"] = [{"id": "R", "material": "steel", "quantity": 2,
                                 "eta": rng.randint(0, 120)}]
            data["machines"][0]["unavailable"] = [[20, 80]]
            data["orders"] = [order(f"J{i}", rng.randint(1, 10), rng.randint(40, 220),
                                    rng.randint(10, 60)) for i in range(count)]
            s = Snapshot.parse(data)
            plan, _, candidates = optimizer.solve(s)
            with self.subTest(index=index):
                self.assertEqual(validate_plan(s, plan), [])
                self.assertLessEqual(len(candidates), 120)
                self.assertEqual(plan.to_dict(), optimizer.solve(s)[0].to_dict())


class ClosedLoopTests(unittest.TestCase):
    def run_case(self, name):
        with StateStore(":memory:") as store:
            return run_scenario(scenarios()[name], store, name)

    def test_all_scenarios_valid(self):
        for name in scenarios():
            with self.subTest(name=name):
                result = self.run_case(name)
                self.assertEqual(result["decisions"][0]["validation_errors"], [])
                self.assertEqual(result["version"], 1)

    def test_urgent_order_can_meet_deadline(self):
        decision = self.run_case("rush_order")["decisions"][0]
        self.assertEqual(decision["action"], "REPLAN")
        self.assertEqual(decision["after_metrics"]["tardiness"]["URGENT-1"], 0)
        self.assertEqual(decision["after_metrics"]["blocked_orders"], 0)

    def test_future_downtime_detected_before_it_occurs(self):
        decision = self.run_case("future_downtime")["decisions"][0]
        risks = [r for r in decision["risks"] if r["kind"] == "capacity_conflict"]
        self.assertTrue(risks)
        self.assertTrue(all(r["proactive"] and r["lead_minutes"] == 60 for r in risks))
        self.assertFalse(decision["before_feasible"])
        self.assertEqual(decision["validation_errors"], [])

    def test_material_delay_accounts_for_preparation(self):
        result = self.run_case("receipt_delay")
        decision = result["decisions"][0]
        self.assertTrue(any(r["kind"] == "material_wait" for r in decision["risks"]))
        allocations = [r for r in result["final_plan"]["reservations"] if r["lot"] == "PO-1"]
        self.assertTrue(allocations)
        self.assertTrue(all(r["ready"] == 130 and r["consume_at"] >= 130 for r in allocations))
        kinds = {m["kind"] for m in decision["messages"]}
        self.assertTrue({"material.request", "material.offered", "reservation.selected"} <= kinds)

    def test_no_change_skips_optimization_and_preserves_assignments(self):
        result = self.run_case("no_change")
        d = result["decisions"][0]
        self.assertEqual(d["action"], "KEEP")
        self.assertEqual(d["candidate_count"], 0)
        self.assertEqual(d["after_metrics"]["schedule_churn_minutes"], 0)
        self.assertEqual(result["initial_plan"]["assignments"], result["final_plan"]["assignments"])

    def test_shortage_protects_high_priority_order(self):
        result = self.run_case("material_shortage")
        self.assertEqual(set(result["final_plan"]["blocked"]), {"NORMAL-2"})
        self.assertEqual(result["final_plan"]["metrics"]["blocked_priority"], 1)
        d = result["decisions"][0]
        self.assertTrue(any(m["kind"] == "material.rejected" for m in d["messages"]))

    def test_unrelated_downtime_keeps_plan(self):
        s = Snapshot.parse(base_snapshot())
        engine = OrchestratorAgent()
        baseline = engine.initialize(s)
        e = Event.parse({"id": "unrelated", "at": 0, "kind": "machine.unavailable",
                         "payload": {"machine_id": "CUT-1", "start": 1000, "end": 1100}})
        _, _, result = engine.process(s, baseline, e)
        self.assertEqual(result["action"], "KEEP")
        self.assertEqual(result["candidate_count"], 0)

    def test_blocked_order_can_recover_after_inventory_update(self):
        data = base_snapshot()
        data["stock"]["steel"] = 0
        s = Snapshot.parse(data)
        engine = OrchestratorAgent()
        baseline = engine.initialize(s)
        self.assertEqual(len(baseline.blocked), 2)
        e = Event("restore", "inventory.corrected", 0, {"material": "steel", "quantity": 2})
        _, plan, result = engine.process(s, baseline, e)
        self.assertEqual(result["action"], "REPLAN")
        self.assertFalse(plan.blocked)


class PersistenceTests(unittest.TestCase):
    def test_duplicate_event_persists_across_reopen_and_conflict_rejected(self):
        case = scenarios()["rush_order"]
        s = Snapshot.parse(case["snapshot"])
        e = Event.parse(case["events"][0])
        engine = OrchestratorAgent()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.sqlite3"
            with StateStore(path) as store:
                store.initialize("s", s, engine)
                original = store.process("s", e, engine)
            with StateStore(path) as store:
                replayed = store.process("s", e, engine)
                self.assertEqual(original, replayed)
                self.assertEqual(store.read("s")[2], 1)
                bad = Event(e.id, "planning.tick", 0, {})
                with self.assertRaisesRegex(ValueError, "reused"):
                    store.process("s", bad, engine)
                self.assertEqual(store.read("s")[2], 1)

    def test_invalid_event_rolls_back_state_and_audit(self):
        with StateStore(":memory:") as store:
            engine = OrchestratorAgent()
            s = Snapshot.parse(base_snapshot())
            store.initialize("s", s, engine)
            before = store.read("s")
            for e in (Event("bad", "order.created", 0, {"order": s.to_dict()["orders"][0]}),
                      Event("bad2", "machine.unavailable", 0,
                            {"machine_id": "unknown", "start": 1, "end": 2}),
                      Event("bad3", "planning.tick", 20, {})):
                with self.assertRaises(ValueError):
                    store.process("s", e, engine)
                self.assertEqual(before, store.read("s"))
            count = store.connection.execute("SELECT count(*) FROM decisions").fetchone()[0]
            self.assertEqual(count, 0)

    def test_session_identity_rejects_different_initial_snapshot(self):
        with StateStore(":memory:") as store:
            engine = OrchestratorAgent()
            store.initialize("s", Snapshot.parse(base_snapshot()), engine)
            changed = base_snapshot()
            changed["stock"]["steel"] = 99
            with self.assertRaisesRegex(ValueError, "different"):
                store.initialize("s", Snapshot.parse(changed), engine)

    def test_cli_report_and_idempotent_rerun(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            output = Path(directory) / "report.json"
            db = Path(directory) / "state.sqlite3"
            source.write_text(json.dumps(scenarios()["rush_order"]), encoding="utf-8")
            argv = ["run", str(source), "--output", str(output), "--db", str(db), "--session", "demo",
                    "--summary", str(Path(directory) / "summary.md")]
            self.assertEqual(main(argv), 0)
            original = json.loads(output.read_text(encoding="utf-8"))["custom"]
            self.assertEqual(main(argv), 0)
            report = json.loads(output.read_text(encoding="utf-8"))["custom"]
            self.assertEqual(report["version"], 1)
            self.assertEqual(original["initial_plan"], report["initial_plan"])

    def test_benchmark_is_reproducible_and_search_contains_baselines(self):
        first = benchmark(cases=8, seed=42)
        second = benchmark(cases=8, seed=42)
        for a, b in zip(first["records"], second["records"]):
            self.assertEqual(a["snapshot"], b["snapshot"])
            for name in a["policies"]:
                self.assertTrue(a["policies"][name]["feasible"])
                self.assertEqual(a["policies"][name]["metrics"], b["policies"][name]["metrics"])


if __name__ == "__main__":
    unittest.main()
