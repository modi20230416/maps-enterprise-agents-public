"""Local human review ledger; no production identity/authentication claims."""
from copy import deepcopy
from dataclasses import asdict

from enterprise_agent.infrastructure.mes import fingerprint

REVIEW_MODULES = ("orchestrate", "planning", "scheduling", "warehouse")


def decision_binding(context, plan):
    return fingerprint({"context": asdict(context), "plan": plan.to_dict()})


class ReviewBoard:
    def __init__(self, decider=None, *, simulated=False):
        self.decider = decider
        self.simulated = simulated
        self.requests = {}

    def request(self, module, binding, summary):
        if module not in REVIEW_MODULES:
            raise ValueError("unknown review module")
        key = fingerprint({"module": module, "binding": binding})
        if key not in self.requests:
            self.requests[key] = {"id": key, "module": module, "binding": binding,
                                  "summary": deepcopy(summary), "status": "pending",
                                  "reviewer": None, "simulated": self.simulated}
        item = self.requests[key]
        if item["status"] == "pending" and self.decider:
            answer = self.decider(deepcopy(item))
            if answer is not None:
                self.decide(key, **answer)
        return deepcopy(item)

    def decide(self, request_id, *, approved, reviewer):
        if type(approved) is not bool or not isinstance(reviewer, str) or not reviewer.strip():
            raise ValueError("explicit decision and local reviewer name required")
        item = self.requests[request_id]
        if item["status"] != "pending":
            raise ValueError("review already decided")
        item.update(status="approved" if approved else "rejected", reviewer=reviewer.strip())

    def verify(self, ids, binding, *, allow_simulated=False):
        items = [self.requests.get(key, {}) for key in ids]
        return (len(ids) == len(set(ids)) == len(REVIEW_MODULES)
                and {item.get("module") for item in items} == set(REVIEW_MODULES)
                and all(item.get("status") == "approved" and item.get("binding") == binding
                        and (allow_simulated or not item.get("simulated")) for item in items))
