"""A bounded, dependency-free environment for dispatch-rule selection.

An episode constructs a static schedule, one ORDER dispatch at a time. It is not
a production execution simulator or a future-event oracle.
"""

from __future__ import annotations

import random
from dataclasses import replace

from .models import Plan, Snapshot
from .scheduler import OptimizationAgent, measure, validate_plan
from .warehouse import MessageBus, WarehouseAgent

ACTION_NAMES = ("priority_edd", "edd", "shortest_processing", "weighted_slack")
ENV_VERSION = "dispatch-rule-v1"


class DispatchEnv:
    def __init__(self, snapshot: Snapshot, max_orders: int = 8, baseline: Plan | None = None):
        if not 1 <= max_orders <= 64 or len(snapshot.orders) > max_orders:
            raise ValueError("snapshot exceeds bounded RL order capacity")
        self.snapshot, self.max_orders, self.baseline = snapshot, max_orders, baseline
        self.orders = tuple(sorted(snapshot.orders, key=lambda o: o.id))
        self.optimizer = OptimizationAgent()
        self.observation_size = 10 + 8 * max_orders
        self.time_scale = max(1, max([snapshot.now,
            *(o.release for o in self.orders), *(r.eta + snapshot.preparation_minutes for r in snapshot.receipts),
            *(end for m in snapshot.machines for _, end in m.unavailable)]) - snapshot.now
            + snapshot.preparation_minutes + sum(p.duration for o in self.orders for p in o.operations))
        self.priority_scale = max(1, sum(o.priority for o in self.orders))
        # Finite-episode bounds make one unit of blocked priority dominate all
        # possible secondary cost. Makespan is a fractional tie breaker.
        op_count = sum(len(o.operations) for o in self.orders)
        old_max = max((a.end for a in baseline.assignments), default=snapshot.now) if baseline else snapshot.now
        churn_bound = op_count * (self.time_scale + max(0, old_max - snapshot.now) + 10)
        self.block_penalty = 10 * self.priority_scale * (snapshot.now + self.time_scale) + churn_bound + 2
        self.selected = []
        self.plan = Plan()
        self.terminated = not self.orders
        self.cost = 0.0
        self.material_ready = {}
        for order in self.orders:
            ready, _, _ = WarehouseAgent(snapshot, MessageBus("rl-quote")).reserve(order)
            self.material_ready[order.id] = ready

    def reset(self, *, seed: int | None = None, options: dict | None = None):
        # No stochastic transitions within a static episode; scenario seeds are
        # controlled by the caller. Signature matches Gymnasium reset semantics.
        self.selected = []
        self.plan = Plan()
        self.terminated = not self.orders
        self.cost = 0.0
        return self.observe(), self.info()

    def observe(self) -> list[float]:
        s, t = self.snapshot, self.time_scale
        done_ids = {o.id for o in self.selected}
        metrics = measure(replace(s, orders=tuple(self.selected)), self.plan, self.baseline)
        completion = metrics["completion"]
        downtime = [end - start for m in s.machines for start, end in m.unavailable]
        obs = [(len(self.orders) - len(self.selected)) / self.max_orders,
               len(self.selected) / self.max_orders,
               metrics["weighted_tardiness"] / (self.priority_scale * t),
               metrics["blocked_priority"] / self.priority_scale,
               metrics["makespan"] / t,
               max((r.eta - s.now for r in s.receipts), default=0) / t,
               sum(s.stock.values()) / max(1, sum(sum(o.materials.values()) for o in self.orders)),
               sum(p.duration for o in self.orders if o.id not in done_ids for p in o.operations) / t,
               sum(downtime) / (t * len(s.machines)),
               metrics["schedule_churn_minutes"] / max(1, t * sum(len(o.operations) for o in self.orders))]
        for order in self.orders:
            ready = self.material_ready[order.id]
            position = next((i + 1 for i, o in enumerate(self.selected) if o.id == order.id), 0)
            obs.extend([float(order.id not in done_ids), order.priority / self.priority_scale,
                        (order.due - s.now) / t, (order.release - s.now) / t,
                        sum(op.duration for op in order.operations) / t,
                        (completion[order.id] - s.now) / t if order.id in completion else 0,
                        position / self.max_orders, (ready - s.now) / t if ready is not None else 2.0])
        obs.extend([0.0] * (self.observation_size - len(obs)))
        return obs

    def info(self) -> dict:
        return {"environment_version": ENV_VERSION, "terminated": self.terminated,
                "action_names": list(ACTION_NAMES), "action_mask": [not self.terminated] * 4,
                "selected_orders": [o.id for o in self.selected], "metrics": self.plan.metrics,
                "cost": self.cost, "time_advances": False}

    def step(self, action: int):
        if self.terminated:
            raise ValueError("episode terminated; reset before taking another action")
        if type(action) is not int or not 0 <= action < len(ACTION_NAMES):
            raise ValueError("invalid dispatch-rule action")
        selected_ids = {o.id for o in self.selected}
        remaining = [o for o in self.orders if o.id not in selected_ids]
        keys = [lambda o: (-o.priority, o.due, o.id), lambda o: (o.due, o.id),
                lambda o: (sum(p.duration for p in o.operations), -o.priority, o.id),
                lambda o: ((o.due - max(self.snapshot.now, o.release)
                            - sum(p.duration for p in o.operations)) / o.priority, -o.priority, o.id)]
        self.selected.append(min(remaining, key=keys[action]))
        partial = replace(self.snapshot, orders=tuple(self.selected))
        plan, _ = self.optimizer.construct(partial, tuple(self.selected), "drl_dispatch", "rl")
        plan.metrics = measure(partial, plan, self.baseline)
        if validate_plan(partial, plan):
            raise RuntimeError("RL environment generated an invalid partial schedule")
        metrics = plan.metrics
        cost = (metrics["blocked_priority"] * self.block_penalty + 10 * metrics["weighted_tardiness"]
                + metrics["schedule_churn_minutes"] + metrics["makespan"] / (self.time_scale + 1))
        reward = -(cost - self.cost) / self.block_penalty
        self.cost = cost
        self.plan = plan
        self.terminated = len(self.selected) == len(self.orders)
        if self.terminated and validate_plan(self.snapshot, plan):
            raise RuntimeError("RL terminal schedule failed validation")
        return self.observe(), reward, self.terminated, False, self.info()


def training_snapshot(seed: int, max_orders: int = 8) -> Snapshot:
    from .scenarios import base_snapshot, order
    rng = random.Random(seed)
    data = base_snapshot()
    count = rng.randint(3, min(6, max_orders))
    data["orders"] = [order(f"J{i}", rng.choice([1, 2, 5, 10]), rng.randint(55, 220),
                            rng.randint(15, 65)) for i in range(count)]
    data["stock"]["steel"] = rng.randint(1, count)
    data["receipts"] = [{"id": "PO", "material": "steel", "quantity": rng.randint(1, count),
                         "eta": rng.randint(20, 140)}]
    data["machines"][2]["unavailable"] = [[rng.randint(60, 90), 130]]
    return Snapshot.parse(data)


def gymnasium_environment(snapshot: Snapshot, max_orders: int = 8):
    """Optional real Gymnasium wrapper; the core environment needs no packages."""
    try:
        import gymnasium as gym
        import numpy as np
    except ImportError:
        raise ImportError("install the optional gym extra for Gymnasium integration") from None

    class GymDispatch(gym.Env):
        metadata = {"render_modes": []}

        def __init__(self):
            self.core = DispatchEnv(snapshot, max_orders)
            self.action_space = gym.spaces.Discrete(4)
            self.observation_space = gym.spaces.Box(-np.inf, np.inf,
                                                    shape=(self.core.observation_size,), dtype=np.float32)

        def reset(self, *, seed=None, options=None):
            super().reset(seed=seed)
            obs, info = self.core.reset(seed=seed, options=options)
            return np.asarray(obs, dtype=np.float32), info

        def step(self, action):
            obs, reward, done, truncated, info = self.core.step(int(action))
            return np.asarray(obs, dtype=np.float32), reward, done, truncated, info

    return GymDispatch()
