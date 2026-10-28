from __future__ import annotations
from copy import deepcopy


class TraceLog:
    """Synchronous local protocol; envelopes can later move to a message broker."""

    def __init__(self, correlation_id: str):
        self.correlation_id = correlation_id
        self.messages: list[dict] = []

    def send(self, sender: str, receiver: str, kind: str, payload: dict) -> None:
        self.messages.append({"sequence": len(self.messages) + 1,
                              "correlation_id": self.correlation_id,
                              "sender": sender, "receiver": receiver,
                              "kind": kind, "payload": deepcopy(payload)})
