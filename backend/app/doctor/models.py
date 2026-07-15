"""Stable output models for production preflight checks."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class DoctorStatus(StrEnum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class DoctorCheckResult:
    check_id: str
    status: DoctorStatus
    component: str
    message: str
    remediation: str | None
    config_source: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "status": self.status.value,
            "component": self.component,
            "message": self.message,
            "remediation": self.remediation,
            "config_source": self.config_source,
            "timestamp": self.timestamp.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class DoctorReport:
    profile: str
    config_path: str
    checks: tuple[DoctorCheckResult, ...]
    schema_version: str = "v1alpha1"

    @property
    def fail_count(self) -> int:
        return sum(check.status is DoctorStatus.FAIL for check in self.checks)

    @property
    def warn_count(self) -> int:
        return sum(check.status is DoctorStatus.WARN for check in self.checks)

    @property
    def pass_count(self) -> int:
        return sum(check.status is DoctorStatus.PASS for check in self.checks)

    @property
    def ready(self) -> bool:
        return self.fail_count == 0

    @property
    def exit_code(self) -> int:
        return 0 if self.ready else 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "profile": self.profile,
            "config_path": self.config_path,
            "ready": self.ready,
            "summary": {
                "pass": self.pass_count,
                "warn": self.warn_count,
                "fail": self.fail_count,
            },
            "checks": [check.to_dict() for check in self.checks],
        }
