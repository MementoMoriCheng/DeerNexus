"""Run lifecycle state machine: terminal set, legal transitions, guard (PR-070).

Freezes the run-status transition semantics that ``RunManager.set_status`` and
``cancel`` previously enforced only implicitly (and that the store layer could
not enforce at all under concurrent writers). The legal edges here reflect the
**current 6-state vocabulary** actually in use (``pending | running | success |
error | timeout | interrupted``), not the data-model §12.3 9-state target — the
6→9 rename (``interrupted``→``cancelled`` etc. + ``cancelling``/
``clarification_required``/``approval_required``) is an explicitly-deferred
follow-up tracked in runtime-contracts.md §16, because the status strings are
hard-coded in several SQL aggregations (``aggregate_tokens`` /
``aggregate_stats_by_org`` / ``list_inflight``), the metrics label cardinality
(PR-063 ``runs_status_total``), and the Admin Console runs filter (PR-061).

Terminal states (``success`` / ``error`` / ``timeout`` / ``interrupted``) have
an empty outgoing edge set: a terminal run may never return to ``running``.
This is the immutability rule the CAS column (``runs.row_version``) enforces at
the store layer — a concurrent cancel racing a completion yields exactly one
winner because only the first ``WHERE row_version = :expected`` matches
(TM-027 mitigation).
"""

from __future__ import annotations


class IllegalRunTransitionError(Exception):
    """Raised when a run-status transition is not permitted by the state machine."""


# The four terminal statuses in the current 6-state vocabulary. ``interrupted``
# is the cancel terminal (the 9-state rename would call it ``cancelled``).
TERMINAL_RUN_STATUSES: frozenset[str] = frozenset(
    {"success", "error", "timeout", "interrupted"},
)

# Legal source → target transitions. Edges reflect the transitions the runtime
# actually performs today — NOT the data-model §12.3 9-state target (see module
# docstring). Terminal states map to an empty set so any outgoing transition
# raises (terminal immutability).
_LEGAL_RUN_TRANSITIONS: dict[str, frozenset[str]] = {
    # A pending run starts (→ running) or can reach any terminal state without
    # passing through running (a run that completes/times out/is cancelled
    # before its body marks it running, or that fails to start).
    "pending": frozenset({"running", "success", "error", "timeout", "interrupted"}),
    # A running run completes normally (→ success), fails (→ error), times out
    # (→ timeout), or is cancelled (→ interrupted).
    "running": frozenset({"success", "error", "timeout", "interrupted"}),
    # ``interrupted`` is the cancel terminal, but it may be re-classified to
    # ``error`` when the cancel carried a rollback action: the worker's final
    # status step re-marks the cancelled run as ``error`` ("Rolled back by
    # user") so the run is recorded as having ended in a rollback failure
    # rather than a plain cancel. Both endpoints are terminal, so this is a
    # documented terminal→terminal re-classification, NOT a return to running
    # (it does not violate terminal immutability). The 9-state vocabulary
    # (deferred follow-up) would model this as cancelling → failed instead.
    "interrupted": frozenset({"error"}),
    # Terminal states: no outgoing transitions (immutable).
    "success": frozenset(),
    "error": frozenset(),
    "timeout": frozenset(),
}


def assert_run_transition(current: str, target: str) -> None:
    """Raise :class:`IllegalRunTransitionError` if ``current → target`` is illegal.

    Mirrors the release layer's ``_assert_transition``
    (``persistence/release/repository.py``). A transition is legal iff ``target``
    is in ``_LEGAL_RUN_TRANSITIONS[current]``. Any transition out of a terminal
    state raises (terminal immutability, data-model §12.3).

    A self-transition (``current == target``) is **allowed**: it is an idempotent
    re-confirmation of the same status (e.g. a resumed run re-marking itself
    ``running``), not a state change. Under CAS such a write still goes through
    the ``expected_row_version`` predicate, so it is safe.
    """
    if current == target:
        return
    allowed = _LEGAL_RUN_TRANSITIONS.get(current)
    if allowed is None:
        # Unknown source state — defensive: reject rather than silently allow.
        raise IllegalRunTransitionError(f"Unknown run status {current!r}; cannot transition to {target!r}.")
    if target not in allowed:
        raise IllegalRunTransitionError(f"Illegal run transition {current!r} → {target!r} (run state machine, PR-070).")


def is_terminal_run_status(status: str) -> bool:
    """Return ``True`` if ``status`` is a terminal run status (no outgoing transitions)."""
    return status in TERMINAL_RUN_STATUSES


__all__ = [
    "TERMINAL_RUN_STATUSES",
    "IllegalRunTransitionError",
    "assert_run_transition",
    "is_terminal_run_status",
]
