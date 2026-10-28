"""Seeded offline benchmark; measures heuristics, not enterprise effectiveness."""

from __future__ import annotations

import random
from statistics import mean
from time import perf_counter

from .models import Snapshot
from .scenarios import base_snapshot, order
from .scheduler import OptimizationAgent, measure, score, validate_plan


def benchmark(cases: int = 30, seed: int = 20261014) -> dict:
    if not 1 <= cases <= 1000:
        raise ValueError("cases must be between 1 and 1000")
    rng = random.Random(seed)
    optimizer = OptimizationAgent()
    records = []
    for case in range(cases):
        data = base_snapshot()
        count = rng.randint(3, 5)
        data["stock"]["steel"] = rng.randint(0, count)
        data["receipts"] = [{"id": "PO", "material": "steel", "quantity": rng.randint(1, count),
                             "eta": rng.randint(20, 150)}]
        data["orders"] = [order(f"J{i}", rng.choice([1, 2, 5, 10]), rng.randint(60, 220),
                                rng.randint(15, 70)) for i in range(count)]
        data["machines"][2]["unavailable"] = [[80, 130]]
        snapshot = Snapshot.parse(data)
        policies = {}
        for name, key in [("EDD", lambda o: (o.due, o.id)),
                          ("Priority_EDD", lambda o: (-o.priority, o.due, o.id))]:
            started = perf_counter()
            plan, _ = optimizer.construct(snapshot, tuple(sorted(snapshot.orders, key=key)), name, str(case))
            plan.metrics = measure(snapshot, plan)
            policies[name] = {"metrics": plan.metrics, "score": list(score(plan.metrics)),
                              "feasible": not validate_plan(snapshot, plan),
                              "elapsed_seconds": perf_counter() - started}
        started = perf_counter()
        plan, _, candidates = optimizer.solve(snapshot)
        policies["Multi_candidate"] = {"metrics": plan.metrics, "score": list(score(plan.metrics)),
                                       "feasible": not validate_plan(snapshot, plan),
                                       "candidates": len(candidates),
                                       "elapsed_seconds": perf_counter() - started}
        # These are dispatch-sequence baselines contained in the bounded search.
        if any(tuple(policies["Multi_candidate"]["score"]) > tuple(policies[name]["score"])
               for name in ("EDD", "Priority_EDD")):
            raise RuntimeError("search regressed relative to an included baseline")
        records.append({"case": case, "snapshot": snapshot.to_dict(), "policies": policies})
    summary = {}
    for policy in ["EDD", "Priority_EDD", "Multi_candidate"]:
        results = [r["policies"][policy] for r in records]
        times = sorted(r["elapsed_seconds"] for r in results)
        summary[policy] = {
            "feasible_cases": sum(r["feasible"] for r in results),
            "mean_blocked_priority": mean(r["metrics"]["blocked_priority"] for r in results),
            "mean_weighted_tardiness": mean(r["metrics"]["weighted_tardiness"] for r in results),
            "mean_makespan": mean(r["metrics"]["makespan"] for r in results),
            "p95_seconds": times[min(len(times) - 1, (95 * len(times) + 99) // 100 - 1)]}
    return {"seed": seed, "cases": cases, "summary": summary, "records": records,
            "limitations": ["synthetic single-epoch cases; no real enterprise data",
                            "no training, statistical significance or global optimality claim",
                            "different blocked orders make standalone tardiness incomparable",
                            "timing is machine-dependent; no production latency guarantee"]}
