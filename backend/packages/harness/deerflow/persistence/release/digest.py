"""Artifact content digest (PR-052).

Computes the immutable execution identity for an ``AgentVersion``:
``sha256:<hex>`` over the raw artifact bytes (ADR-0004 §3.2, §11.1).

This is distinct from the backup per-table ``content_digest``
(``persistence/backup/snapshot.py``), which is a *bare hex* sha256 over a
normalised JSON row projection. The artifact digest:

* carries the ``sha256:`` algorithm prefix (so future algorithms —
  ``sha512:``, multihash — can coexist without a column migration);
* is computed over the **raw artifact bytes**, not a re-serialised
  structure — the same bytes the executor will load must be the same bytes
  the digest pinned;
* is the value stored in ``AgentVersionRow.digest`` and surfaced as
  ``ReleaseRef.digest`` (the execution identity; SemVer ``version`` is
  display-only).
"""

from __future__ import annotations

import hashlib

#: Algorithm prefix carried by every artifact digest. Future algorithms
#: (``sha512:``, multihash) coexist by carrying a distinct prefix — no column
#: migration needed because the digest is a free-form ``String(80)``.
DIGEST_ALGORITHM = "sha256"


def compute_artifact_digest(content: bytes | str) -> str:
    """Return ``sha256:<hex>`` over the artifact content.

    A ``str`` payload is encoded as UTF-8 (matching how ``content_inline``
    is stored). The caller MUST pass the exact bytes the executor will
    re-read — re-serialisation would break the digest's identity guarantee.
    """
    raw = content.encode("utf-8") if isinstance(content, str) else content
    hexdigest = hashlib.sha256(raw).hexdigest()
    return f"{DIGEST_ALGORITHM}:{hexdigest}"
