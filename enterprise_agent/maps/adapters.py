"""In-memory MES test adapter; no enterprise network writes."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import hashlib
import json
from threading import Lock

from ..models import Plan, Snapshot
from ..scheduler import validate_plan
from .contracts import MESBaseline, MESPatch, Priority, WorkOrder
from .ports import StaleMESVersion
from .validation import safety_status, validate_candidate


def fingerprint(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def parse_baseline(data: dict) -> MESBaseline:
    catalog = tuple(WorkOrder(w["mo_id"], w["wo_id"], Priority(w["priority"]),
                             w.get("status", "planned"), w.get("interruptible", False))
                    for w in data["work_orders"])
    return MESBaseline(data["version"], Snapshot.parse(data["snapshot"]),
                       Plan.parse(data["plan"]), catalog)


class InMemoryMES:
    """Authoritative fixture state, atomic version CAS and idempotent writes.

    Rejected patches never update state. This simulates the gateway protocol;
    neither this state nor the orchestration trace survives process restart.
    """

    def __init__(self, baseline: MESBaseline):
        errors = validate_plan(baseline.snapshot, baseline.plan)
        if errors:
            raise ValueError(f"invalid imported MES baseline: {errors}")
        self._baseline = deepcopy(baseline)
        self._lock = Lock()
        self._applied: dict[str, tuple[str, int]] = {}

    def read_baseline(self) -> MESBaseline:
        with self._lock:
            return deepcopy(self._baseline)

    def submit_patch(self, patch: MESPatch) -> int:
        patch = deepcopy(patch)
        signature = fingerprint(asdict(patch))
        with self._lock:
            if patch.patch_id in self._applied:
                prior_signature, version = self._applied[patch.patch_id]
                if signature != prior_signature:
                    raise ValueError("patch id reused with different content")
                return version
            if patch.expected_version != self._baseline.version:
                raise StaleMESVersion("MES baseline changed; recollect and reassess")
            if asdict(patch.context.baseline) != asdict(self._baseline):
                raise ValueError("patch baseline does not match authoritative MES state")
            # Rebuild observed context from the authoritative baseline/event,
            # rather than trusting caller-supplied frozen status or snapshot.
            from .planning import DataCollect, QuickRespond
            authoritative = DataCollect().collect(self._baseline, patch.context.event)
            if asdict(authoritative) != asdict(patch.context):
                raise ValueError("patch context differs from validated source event")
            gate = QuickRespond().assess(authoritative)
            if set(patch.scope) != set(gate.scope):
                raise ValueError("writeback scope differs from gate scope")
            safety = safety_status(authoritative)
            if safety:
                raise ValueError(safety[1])
            errors = validate_candidate(authoritative, patch.plan, patch.scope)
            if errors:
                raise ValueError("patch validation failed: " + "; ".join(errors))
            from .scheduling import WarehouseCoordinator
            reservations, _, conflicts = WarehouseCoordinator().confirm(authoritative, patch.plan)
            if conflicts or reservations != patch.plan.reservations:
                raise ValueError("patch material confirmations do not match independent warehouse check")
            self._baseline = MESBaseline(self._baseline.version + 1,
                                         authoritative.snapshot, patch.plan, authoritative.work_orders)
            self._applied[patch.patch_id] = signature, self._baseline.version
            return self._baseline.version
