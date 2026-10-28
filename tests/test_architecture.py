"""Keep the production-facing framework independent of experimental drivers."""

import ast
from pathlib import Path
import subprocess
import sys
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ArchitectureTests(unittest.TestCase):
    def test_framework_does_not_import_experiments(self):
        for layer in ("agents", "capabilities", "domain", "infrastructure", "application"):
            for source in (ROOT / "enterprise_agent" / layer).glob("*.py"):
                tree = ast.parse(source.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if isinstance(node, ast.ImportFrom):
                        self.assertEqual(node.level, 0, f"Use explicit imports in {source.name}")
                        names = [node.module or ""]
                    elif isinstance(node, ast.Import):
                        names = [alias.name for alias in node.names]
                    else:
                        continue
                    self.assertFalse(any(name.startswith("enterprise_agent.experiments") for name in names),
                                     f"Framework depends on experiment code: {source.name}")

    def test_framework_entrypoint_does_not_load_experiment_engines(self):
        script = """
import sys
from enterprise_agent.agents.orchestrate import OrchestrateAgent
from enterprise_agent.agents.planning import PlanningAgent
from enterprise_agent.agents.scheduling import SchedulingAgent
from enterprise_agent.agents.warehouse import WarehouseAgent
from enterprise_agent.application.demo import run_demo
assert not any(name.startswith('enterprise_agent.experiments') for name in sys.modules)
"""
        result = subprocess.run([sys.executable, "-c", script], cwd=ROOT,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
