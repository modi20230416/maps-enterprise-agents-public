import importlib.util
import json
import math
import random
import tempfile
import unittest
from pathlib import Path

from enterprise_agent.experiments.dqn import NeuralDispatchPolicy, dispatch_with_checkpoint, train_dqn
from enterprise_agent.domain.models import Snapshot
from enterprise_agent.experiments.dispatch_env import ACTION_NAMES, ENV_VERSION, DispatchEnv, training_snapshot
from enterprise_agent.experiments.scenarios import base_snapshot
from enterprise_agent.capabilities.scheduling import validate_plan


class RLEnvironmentTests(unittest.TestCase):
    def test_seeded_rule_actions_produce_valid_plans(self):
        for seed in range(20):
            snapshot = training_snapshot(seed)
            env = DispatchEnv(snapshot)
            obs, info = env.reset(seed=seed)
            self.assertEqual(len(obs), 74)
            rng = random.Random(seed)
            total = 0
            steps = 0
            while not info["terminated"]:
                self.assertTrue(all(math.isfinite(x) for x in obs))
                obs, reward, done, truncated, info = env.step(rng.randrange(4))
                total += reward
                steps += 1
                self.assertFalse(truncated)
            self.assertEqual(steps, len(snapshot.orders))
            self.assertEqual(validate_plan(snapshot, env.plan), [])
            self.assertAlmostEqual(total, -env.cost / env.block_penalty)
            self.assertEqual(info["action_mask"], [False] * 4)
            with self.assertRaises(ValueError):
                env.step(0)

    def test_reset_and_action_contract(self):
        snapshot = training_snapshot(10)
        env = DispatchEnv(snapshot)
        first, _ = env.reset()
        for action in [-1, 4, True, 1.2]:
            with self.assertRaises(ValueError):
                env.step(action)
        env.step(0)
        again, _ = env.reset()
        self.assertEqual(first, again)
        with self.assertRaises(ValueError):
            DispatchEnv(snapshot, max_orders=1)

    def test_empty_snapshot_is_terminal(self):
        data = base_snapshot()
        data["orders"] = []
        env = DispatchEnv(Snapshot.parse(data))
        self.assertTrue(env.reset()[1]["terminated"])

    def test_json_neural_policy_inference_and_contract(self):
        layers = [74, 32, 32, 4]
        model = {"format": "maps-dqn-v1", "environment_version": ENV_VERSION,
                 "actions": list(ACTION_NAMES), "max_orders": 8, "layers": layers,
                 "weights": [[[0.0] * b for _ in range(a)] for a, b in zip(layers, layers[1:])],
                 "biases": [[0.0] * 32, [0.0] * 32, [0.0, 1.0, 0.0, 0.0]]}
        policy = NeuralDispatchPolicy(model)
        self.assertEqual(policy.action([0.0] * 74), 1)
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "model.json"
            target.write_text(json.dumps(model), encoding="utf-8")
            snapshot = training_snapshot(5)
            plan = dispatch_with_checkpoint(snapshot, str(target))
            self.assertEqual(validate_plan(snapshot, plan), [])
        model["weights"][0][0][0] = float("nan")
        with self.assertRaises(ValueError):
            NeuralDispatchPolicy(model)

    @unittest.skipUnless(importlib.util.find_spec("numpy"), "optional NumPy training runtime unavailable")
    def test_dqn_training_updates_weights_and_evaluates_disjoint_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "model.json"
            report = train_dqn(12, 42, str(target))
            self.assertGreater(report["gradient_updates"], 0)
            self.assertGreaterEqual(report["mean_last_100_td_loss"], 0)
            self.assertTrue(target.exists())
            import numpy as np
            initial = np.random.default_rng(42).normal(0, (2 / 74) ** .5, (74, 32))
            trained = json.loads(target.read_text(encoding="utf-8"))
            self.assertFalse(np.allclose(initial, np.asarray(trained["weights"][0])))
            self.assertEqual(report["evaluation"]["cases"], 24)
            self.assertGreater(report["evaluation"]["test_seed_start"], 42 + 12)
            for outcome in report["evaluation"]["summary"].values():
                self.assertEqual(outcome["feasible_cases"], 24)


if __name__ == "__main__":
    unittest.main()
