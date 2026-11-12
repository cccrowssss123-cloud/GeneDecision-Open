"""Normalization of raw evidence into the canonical ``EvidenceRecord`` schema."""

from genedecision.normalization.evidence_normalizer import (
    COLUMN_ALIASES,
    MISSING_TOKENS,
    normalize_evidence_row,
    normalize_evidence_rows,
)

__all__ = [
    "COLUMN_ALIASES",
    "MISSING_TOKENS",
    "normalize_evidence_row",
    "normalize_evidence_rows",
]
