"""Small real DQN: replay, target network, two hidden layers and Huber TD loss.

Training uses optional NumPy; JSON checkpoint inference uses only the standard
library. This is an experimental static dispatcher, not a factory controller.
"""

from __future__ import annotations

import json
import math
import random
from collections import deque
from copy import deepcopy
from pathlib import Path
from statistics import mean

from enterprise_agent.domain.models import Plan, Snapshot
from enterprise_agent.experiments.dispatch_env import ACTION_NAMES, ENV_VERSION, DispatchEnv, training_snapshot
from enterprise_agent.capabilities.scheduling import BoundedSearchOptimizer, measure, score, validate_plan


class NeuralDispatchPolicy:
    def __init__(self, checkpoint: dict):
        if (checkpoint.get("format") != "maps-dqn-v1" or checkpoint.get("environment_version") != ENV_VERSION
                or checkpoint.get("actions") != list(ACTION_NAMES)):
            raise ValueError("incompatible DRL checkpoint contract")
        self.max_orders = checkpoint["max_orders"]
        if type(self.max_orders) is not int or not 3 <= self.max_orders <= 64:
            raise ValueError("invalid checkpoint order capacity")
        expected = [10 + 8 * self.max_orders, 32, 32, 4]
        if checkpoint.get("layers") != expected:
            raise ValueError("incompatible network shape")
        self.weights = checkpoint["weights"]
        self.biases = checkpoint["biases"]
        if len(self.weights) != 3 or len(self.biases) != 3:
            raise ValueError("invalid network layers")
        for index, (input_dim, output_dim) in enumerate(zip(expected, expected[1:])):
            matrix, bias = self.weights[index], self.biases[index]
            if (len(matrix) != input_dim or any(len(row) != output_dim for row in matrix)
                    or len(bias) != output_dim):
                raise ValueError("invalid checkpoint tensor dimensions")
            if any(type(v) not in (int, float) or not math.isfinite(v)
                   for row in matrix for v in row) or any(
                       type(v) not in (int, float) or not math.isfinite(v) for v in bias):
                raise ValueError("nonfinite checkpoint weights")

    @classmethod
    def load(cls, path: str):
        source = Path(path)
        if source.stat().st_size > 2_000_000:
            raise ValueError("checkpoint exceeds size limit")
        return cls(json.loads(source.read_text(encoding="utf-8")))

    def action(self, observation: list[float]) -> int:
        if len(observation) != 10 + 8 * self.max_orders:
            raise ValueError("checkpoint observation size mismatch")
        hidden = observation
        for index, (weights, biases) in enumerate(zip(self.weights, self.biases)):
            hidden = [bias + sum(x * row[j] for x, row in zip(hidden, weights))
                      for j, bias in enumerate(biases)]
            if index < 2:
                hidden = [max(0, value) for value in hidden]
        return max(range(4), key=lambda i: hidden[i])


def dispatch_with_checkpoint(snapshot: Snapshot, path: str, baseline: Plan | None = None) -> Plan:
    policy = NeuralDispatchPolicy.load(path)
    env = DispatchEnv(snapshot, policy.max_orders, baseline)
    obs, info = env.reset()
    while not info["terminated"]:
        obs, _, _, _, info = env.step(policy.action(obs))
    env.plan.strategy = "dqn_checkpoint_dispatch"
    env.plan.metrics = measure(snapshot, env.plan, baseline)
    if validate_plan(snapshot, env.plan):
        raise RuntimeError("DRL plan failed independent validation")
    return env.plan


def evaluate(policy: NeuralDispatchPolicy, seed: int, cases: int = 24) -> dict:
    records = []
    rng = random.Random(seed)
    optimizer = BoundedSearchOptimizer()
    for index in range(cases):
        snapshot = training_snapshot(seed + index, policy.max_orders)
        outcomes = {}
        for name in ("dqn", "random", "priority_edd", "edd"):
            env = DispatchEnv(snapshot, policy.max_orders)
            obs, info = env.reset()
            while not info["terminated"]:
                action = (policy.action(obs) if name == "dqn" else rng.randrange(4)
                          if name == "random" else 0 if name == "priority_edd" else 1)
                obs, _, _, _, info = env.step(action)
            outcomes[name] = {"metrics": env.plan.metrics, "score": list(score(env.plan.metrics)),
                              "feasible": not validate_plan(snapshot, env.plan)}
        search, _, _ = optimizer.solve(snapshot)
        outcomes["bounded_search"] = {"metrics": search.metrics, "score": list(score(search.metrics)),
                                       "feasible": not validate_plan(snapshot, search)}
        records.append({"seed": seed + index, "outcomes": outcomes})
    summary = {}
    for name in records[0]["outcomes"]:
        values = [record["outcomes"][name] for record in records]
        summary[name] = {"feasible_cases": sum(v["feasible"] for v in values),
                         "mean_blocked_priority": mean(v["metrics"]["blocked_priority"] for v in values),
                         "mean_weighted_tardiness": mean(v["metrics"]["weighted_tardiness"] for v in values),
                         "mean_makespan": mean(v["metrics"]["makespan"] for v in values)}
    return {"cases": cases, "test_seed_start": seed, "summary": summary, "records": records}


def train_dqn(episodes: int, seed: int, checkpoint_path: str) -> dict:
    if not 1 <= episodes <= 100_000:
        raise ValueError("episodes must be between 1 and 100000")
    try:
        import numpy as np
    except ImportError:
        raise ImportError("DQN training needs NumPy: install the drl extra, or use a NumPy-enabled runtime") from None
    rng = random.Random(seed)
    generator = np.random.default_rng(seed)
    max_orders = 8
    layers = [10 + 8 * max_orders, 32, 32, 4]
    weights = [generator.normal(0, (2 / dim) ** .5, (dim, out)).astype(np.float64)
               for dim, out in zip(layers, layers[1:])]
    biases = [np.zeros(out, dtype=np.float64) for out in layers[1:]]
    target_w, target_b = deepcopy(weights), deepcopy(biases)

    def checkpoint():
        return {"format": "maps-dqn-v1", "environment_version": ENV_VERSION,
                "actions": list(ACTION_NAMES), "max_orders": max_orders, "layers": layers,
                "weights": [w.tolist() for w in weights], "biases": [b.tolist() for b in biases],
                "training_seed": seed, "episodes": episodes, "algorithm": "DQN"}

    # A deterministic, disjoint synthetic test set; never used for selecting a checkpoint.
    test_seed = seed + episodes + 1_000_000
    before = evaluate(NeuralDispatchPolicy(checkpoint()), test_seed)
    memory = deque(maxlen=5000)
    losses, returns = [], []
    updates = 0
    for episode in range(episodes):
        env = DispatchEnv(training_snapshot(seed + episode, max_orders), max_orders)
        obs, info = env.reset()
        total = 0.0
        epsilon = .8 + (.05 - .8) * min(1, episode / max(1, episodes * .8))
        while not info["terminated"]:
            x = np.asarray(obs)
            hidden = np.maximum(0, x @ weights[0] + biases[0])
            hidden = np.maximum(0, hidden @ weights[1] + biases[1])
            action = rng.randrange(4) if rng.random() < epsilon else int(np.argmax(hidden @ weights[2] + biases[2]))
            next_obs, reward, done, _, info = env.step(action)
            memory.append((obs, action, reward, next_obs, done))
            obs = next_obs
            total += reward
            if len(memory) < 32:
                continue
            batch = rng.sample(list(memory), 32)
            states = np.asarray([b[0] for b in batch])
            actions = np.asarray([b[1] for b in batch], dtype=np.int64)
            rewards = np.asarray([b[2] for b in batch])
            next_states = np.asarray([b[3] for b in batch])
            dones = np.asarray([b[4] for b in batch])
            z1 = states @ weights[0] + biases[0]
            h1 = np.maximum(0, z1)
            z2 = h1 @ weights[1] + biases[1]
            h2 = np.maximum(0, z2)
            predicted = h2 @ weights[2] + biases[2]
            next_h = np.maximum(0, next_states @ target_w[0] + target_b[0])
            next_h = np.maximum(0, next_h @ target_w[1] + target_b[1])
            target = rewards + .99 * (1 - dones) * np.max(next_h @ target_w[2] + target_b[2], axis=1)
            error = predicted[np.arange(32), actions] - target
            losses.append(float(np.mean(np.where(abs(error) <= 1, .5 * error ** 2, abs(error) - .5))))
            gradient = np.zeros_like(predicted)
            gradient[np.arange(32), actions] = np.clip(error, -1, 1) / 32
            gw2, gb2 = h2.T @ gradient, gradient.sum(axis=0)
            d2 = (gradient @ weights[2].T) * (z2 > 0)
            gw1, gb1 = h1.T @ d2, d2.sum(axis=0)
            d1 = (d2 @ weights[1].T) * (z1 > 0)
            gw0, gb0 = states.T @ d1, d1.sum(axis=0)
            grads_w, grads_b = [gw0, gw1, gw2], [gb0, gb1, gb2]
            norm = float(np.sqrt(sum(np.sum(g * g) for g in grads_w + grads_b)))
            scale = min(1.0, 10 / max(norm, 1e-12))
            for index in range(3):
                weights[index] -= .003 * scale * grads_w[index]
                biases[index] -= .003 * scale * grads_b[index]
            updates += 1
            if updates % 100 == 0:
                target_w, target_b = deepcopy(weights), deepcopy(biases)
        returns.append(total)
    model = checkpoint()
    if not all(math.isfinite(v) for matrix in model["weights"] for row in matrix for v in row):
        raise RuntimeError("nonfinite network weights after training")
    policy = NeuralDispatchPolicy(model)
    after = evaluate(policy, test_seed)
    target_path = Path(checkpoint_path)
    target_path.parent.mkdir(parents=True, exist_ok=True)
    target_path.write_text(json.dumps(model, allow_nan=False), encoding="utf-8")
    return {"algorithm": "DQN", "backend": "NumPy, CPU", "episodes": episodes,
            "seed": seed, "gradient_updates": updates, "checkpoint": str(target_path.resolve()),
            "mean_last_50_return": mean(returns[-50:]),
            "mean_last_100_td_loss": mean(losses[-100:]) if losses else None,
            "initial_network_evaluation": before, "evaluation": after,
            "limitations": ["small synthetic static dispatch environment; no execution-clock advancement",
                            "compact features abstract resource state; not a complete factory observation",
                            "no guaranteed improvement over fixed rules or search",
                            "not a multi-agent RL or production-trained policy"]}
