"""Connector-neutral records and complete/incremental sync batches."""

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class PartyInput:
    external_id: str
    name: str
    party_type: str = "company"
    role: str = "customer"
    alt_names: list[str] = field(default_factory=list)
    address_lines: list[str] = field(default_factory=list)
    city: str | None = None
    region: str | None = None
    postal_code: str | None = None
    country_iso2: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)
    record_hash: str = ""
    deleted: bool = False

    def fields(self) -> dict[str, Any]:
        return {key: getattr(self, key) for key in self.__dataclass_fields__ if key != "deleted"}


@dataclass(frozen=True)
class RejectedRow:
    row_number: int
    reason: str

    def payload(self) -> dict[str, Any]:
        return {"row_number": self.row_number, "reason": self.reason}


@dataclass(frozen=True)
class SyncBatch:
    parties: list[PartyInput]
    rejected: list[RejectedRow] = field(default_factory=list)
    cursor: str | None = None
    complete: bool = True
    rejected_count: int = 0


class Connector(Protocol):
    def full_sync(self) -> SyncBatch: ...
    def incremental_sync(self, cursor: str | None) -> SyncBatch: ...
    def normalize(self, raw: dict[str, Any]) -> PartyInput: ...
