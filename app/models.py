from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class EntityType(str, Enum):
    CLIENT = "client"
    CONTRACT = "contract"
    CONTACT = "contact"


@dataclass
class AquiraClientRecord:
    aquira_id: int | None = None
    name: str = ""
    version: int | None = None


@dataclass
class SyncRunItem:
    entity_type: str
    aquira_id: str | int | None
    hubspot_id: str | None
    action: str
    diff_json: dict | None = None
    error: str | None = None


@dataclass
class SyncRun:
    id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    trigger: str = "manual"
    whatif: bool = False
    status: str = "pending"
    summary_json: dict | None = None
    error: str | None = None
