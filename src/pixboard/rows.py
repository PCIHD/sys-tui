"""Provenance tags for a value shown about a board part."""

from __future__ import annotations

from dataclasses import dataclass

LIVE, DERIVED, SPEC = "live", "derived", "spec"


@dataclass
class Row:
    key: str
    value: str
    kind: str  # LIVE | DERIVED | SPEC
