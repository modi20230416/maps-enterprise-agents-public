"""Replaceable enterprise/model boundaries, without claiming live integrations."""

from __future__ import annotations

from typing import Protocol

from enterprise_agent.domain.contracts import DecisionContext, GateDecision, MESBaseline, MESPatch, Proposal, ScheduleCandidate, WorkPackage


class MESReader(Protocol):
    def read_baseline(self) -> MESBaseline: ...


class MESWriter(Protocol):
    """Single writeback channel; must CAS the MES version and revalidate."""
    def submit_patch(self, patch: MESPatch) -> int: ...


class ERPReader(Protocol):
    """Normalized MO/WO records; production field mapping is still required."""
    def read_orders(self) -> list[dict]: ...


class WMSReader(Protocol):
    def read_inventory(self) -> dict: ...


class EquipmentReader(Protocol):
    def read_equipment(self) -> list[dict]: ...


class ForecastProvider(Protocol):
    def forecast(self, context: DecisionContext) -> list[dict]: ...


class ProposalProvider(Protocol):
    def propose(self, context: DecisionContext, gate: GateDecision,
                evidence: list[dict]) -> Proposal: ...


class ConstraintSolver(Protocol):
    """Future CP/MILP adapter implements this same deterministic interface."""
    def solve(self, context: DecisionContext, package: WorkPackage) -> ScheduleCandidate: ...


class DiagnosisProvider(Protocol):
    """Health/RAG evidence only. This port has no MES writer or actuation API."""
    def observations(self, context: DecisionContext) -> list[dict]: ...


class MARLPolicy(Protocol):
    """Optional sequence proposals; never permission to bypass constraints."""
    policy_id: str

    def propose(self, context: DecisionContext, package: WorkPackage) -> ScheduleCandidate: ...


class StaleMESVersion(ValueError):
    pass
