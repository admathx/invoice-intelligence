import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    email: str = Field(min_length=1, max_length=320)
    password: str = Field(min_length=1, max_length=1024)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=1, max_length=1024)


class LocationOut(BaseModel):
    id: uuid.UUID
    name: str
    metro: str


class MeOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    is_operator: bool
    # An operator issued their password; the screens send them to change it.
    password_change_required: bool = False
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
    # The location it happened at, if any (not every event has one: creating
    # a business, a failed sign-in).
    tenant_id: uuid.UUID | None = None
    tenant_name: str | None = None
    details: dict[str, Any]


class AuditPage(BaseModel):
    events: list[AuditEventOut]
    # Pass back as `cursor` for the next (older) page; None when there's no more.
    next_cursor: str | None


class UserOut(BaseModel):
    id: uuid.UUID
    email: str
    name: str
    is_operator: bool
    is_active: bool
    created_at: datetime
    last_sign_in: datetime | None
    locations: list[LocationOut]


class UserCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    name: str = Field(min_length=1, max_length=200)
    is_operator: bool = False
    location_ids: list[uuid.UUID] = []
    # Omitted: one is generated and returned once, for the operator to hand over.
    password: str | None = Field(default=None, max_length=1024)


class UserUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    is_operator: bool | None = None
    is_active: bool | None = None


class PasswordReset(BaseModel):
    password: str | None = Field(default=None, max_length=1024)


class UserWithPassword(BaseModel):
    user: UserOut
    # Only when the server generated it. Shown to the operator this once;
    # never stored anywhere but as a hash.
    generated_password: str | None
