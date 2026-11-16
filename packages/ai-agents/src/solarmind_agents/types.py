"""Shared value types for the agents."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

Severity = Literal["INFO", "WARNING", "HIGH", "CRITICAL"]
# How a statement was obtained. "statistical" marks unvalidated model output.
Basis = Literal["measured", "calculated", "estimated", "statistical"]


@dataclass(frozen=True)
class Reading:
    time: datetime
    value: float
    quality: str = "valid"


@dataclass(frozen=True)
class Finding:
    """A conclusion with the evidence and assumptions it rests on."""

    code: str
    severity: Severity
    title: str
    detail: str
    basis: Basis
    evidence: dict[str, Any] = field(default_factory=dict)
    assumptions: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "basis": self.basis,
            "evidence": self.evidence,
            "assumptions": list(self.assumptions),
        }
