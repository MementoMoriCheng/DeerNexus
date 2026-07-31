"""Tests for the run lifecycle state machine (PR-070).

Locks the legal-transition table + terminal immutability so a future 6→9
status-vocabulary rename (deferred follow-up) must update these expectations
deliberately, and so the store/manager CAS paths have a frozen contract to key
on.

Covers :mod:`deerflow.runtime.runs.transitions`.
"""

from __future__ import annotations

import pytest

from deerflow.runtime.runs.transitions import (
    TERMINAL_RUN_STATUSES,
    IllegalRunTransitionError,
    assert_run_transition,
    is_terminal_run_status,
)

# The current 6-state vocabulary (PR-070 deliberately does not rename).
STATES = {"pending", "running", "success", "error", "timeout", "interrupted"}
TERMINAL = {"success", "error", "timeout", "interrupted"}
NON_TERMINAL = {"pending", "running"}

# Legal edges (source → targets), frozen from the module.
LEGAL: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "success", "error", "timeout", "interrupted"}),
    "running": frozenset({"success", "error", "timeout", "interrupted"}),
    "interrupted": frozenset({"error"}),  # rollback re-classification (see module docstring)
}


class TestTerminalSet:
    def test_terminal_set_matches_current_vocabulary(self):
        """Terminal = the 4 non-active statuses in the 6-state vocabulary."""
        assert TERMINAL_RUN_STATUSES == TERMINAL

    def test_is_terminal_for_each_terminal(self):
        for s in TERMINAL:
            assert is_terminal_run_status(s) is True

    def test_is_terminal_false_for_active(self):
        for s in NON_TERMINAL:
            assert is_terminal_run_status(s) is False


class TestLegalTransitions:
    @pytest.mark.parametrize(
        ("current", "target"),
        [
            ("pending", "running"),
            ("pending", "success"),
            ("pending", "error"),
            ("pending", "timeout"),
            ("pending", "interrupted"),
            ("running", "success"),
            ("running", "error"),
            ("running", "timeout"),
            ("running", "interrupted"),
        ],
    )
    def test_legal_edge_passes(self, current, target):
        # Must not raise.
        assert_run_transition(current, target)

    def test_all_legal_edges_covered(self):
        """Every legal edge enumerated in the module is tested above."""
        for current, targets in LEGAL.items():
            for target in targets:
                assert_run_transition(current, target)  # no raise


class TestIllegalTransitions:
    @pytest.mark.parametrize(
        "terminal",
        ["success", "error", "timeout"],
    )
    def test_terminal_cannot_transition_anywhere(self, terminal):
        """Terminal immutability (data-model §12.3): no outgoing transitions.

        Self-transitions are allowed (idempotent re-confirmation), so only
        *other* states must be rejected from a terminal status.
        """
        for target in STATES - {terminal}:
            with pytest.raises(IllegalRunTransitionError):
                assert_run_transition(terminal, target)

    @pytest.mark.parametrize(
        "target",
        ["pending", "running", "success", "timeout", "interrupted"],
    )
    def test_interrupted_only_reclassifies_to_error(self, target):
        """``interrupted`` (cancel terminal) may only re-classify to ``error``
        (rollback path). All other transitions are rejected; ``interrupted →
        interrupted`` is an allowed self-transition (idempotent)."""
        if target == "error":
            assert_run_transition("interrupted", target)  # legal rollback re-classification
            return
        if target == "interrupted":
            assert_run_transition("interrupted", target)  # legal self-transition
            return
        with pytest.raises(IllegalRunTransitionError):
            assert_run_transition("interrupted", target)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            # Self-loops are legal (idempotent re-confirmation, not a state change).
            ("pending", "pending"),
            ("running", "running"),
        ],
    )
    def test_self_transition_is_allowed(self, current, target):
        # Must not raise (idempotent re-confirmation).
        assert_run_transition(current, target)

    @pytest.mark.parametrize(
        ("current", "target"),
        [
            # Backwards / cross transitions that are not in the table.
            ("running", "pending"),
            ("success", "running"),
            ("error", "running"),
            ("interrupted", "running"),
            ("timeout", "running"),
            # success → error (terminal → terminal) forbidden.
            ("success", "error"),
            ("error", "success"),
        ],
    )
    def test_illegal_edge_raises(self, current, target):
        with pytest.raises(IllegalRunTransitionError):
            assert_run_transition(current, target)

    def test_unknown_source_status_raises(self):
        """An unrecognised source status is rejected defensively, not silently allowed."""
        with pytest.raises(IllegalRunTransitionError):
            assert_run_transition("cancelled", "running")  # 9-state name not in 6-state vocab yet

    def test_unknown_target_status_raises(self):
        with pytest.raises(IllegalRunTransitionError):
            assert_run_transition("running", "cancelled")


class TestErrorShape:
    def test_error_message_names_both_states(self):
        with pytest.raises(IllegalRunTransitionError, match="success.*running"):
            assert_run_transition("success", "running")

    def test_is_exception_subclass(self):
        err = IllegalRunTransitionError("x")
        assert isinstance(err, Exception)
