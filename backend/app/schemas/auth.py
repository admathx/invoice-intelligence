import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class LocationOut(BaseModel):
    id: uuid.UUID
    name: str
    metro: str


class MeOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    is_operator: bool
    # Every location this person may open, for the location switcher.
    # Operators get all of them.
    locations: list[LocationOut]


class AuditEventOut(BaseModel):
    id: uuid.UUID
    occurred_at: datetime
    # None when the system did it (extraction, email intake).
    actor_name: str | None
    actor_email: str | None
    action: str
    entity_type: str
    entity_id: uuid.UUID | None
    details: dict[str, Any]
