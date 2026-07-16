"""Schema versioning for DeerNexus runtime contracts.

The contracts follow the compatibility policy in
``docs/architecture/runtime-contracts.md`` §13. ``v1alpha1`` is the initial
schema version. During the alpha phase field changes are allowed, but every
change must update the spec, the canonical JSON fixtures and the producer /
consumer contract tests in the same PR (§13.1).

This module intentionally has no contract dependencies so every other contract
module can import the current version without risking an import cycle.
"""

CURRENT_SCHEMA_VERSION = "v1alpha1"
