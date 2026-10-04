"""The v0.1 capture vocabulary; quantity and governance execution remain deferred."""

from enum import StrEnum


class CaptureOperationKind(StrEnum):
    """Only the seven opened capture verbs; no material-quantity producer."""

    RECEIVE_SCAN = "RECEIVE_SCAN"
    MOVE = "MOVE"
    ASSIGN = "ASSIGN"
    UNASSIGN = "UNASSIGN"
    TRANSITION = "TRANSITION"
    ATTACH_EVIDENCE = "ATTACH_EVIDENCE"
    RESOLVE = "RESOLVE"


VERSIONED = frozenset({"MOVE", "ASSIGN", "UNASSIGN", "TRANSITION"})
