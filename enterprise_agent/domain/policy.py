from __future__ import annotations
from dataclasses import dataclass
import math


@dataclass(frozen=True)
class ReplayQualification:
    """Evidence supplied by an offline evaluator; not proof of production safety."""
    policy_id: str
    source: str
    cases: int
    baseline: dict[str, float]
    candidate: dict[str, float]

    def eligible(self, policy_id: str) -> bool:
        metrics = {"blocked_priority", "weighted_tardiness", "schedule_churn_minutes"}
        return (self.policy_id == policy_id and self.source in {"simulation", "historical_replay"}
                and type(self.cases) is int and self.cases > 0
                and all(type(x.get(k)) in {int, float} and math.isfinite(x[k]) and x[k] >= 0
                        for x in (self.baseline, self.candidate) for k in metrics)
                and all(self.candidate[k] <= self.baseline[k] for k in metrics))
