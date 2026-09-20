import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TenantSummary(BaseModel):
    id: uuid.UUID
    name: str
    metro: str
    volume_tier: str
    account_id: uuid.UUID | None


class AccountSummary(BaseModel):
    id: uuid.UUID
    name: str
    created_at: datetime
    # How many locations belong to this business. It is also, exactly, how many
    # votes this account does NOT get: one, no matter how many locations —
    # see app/models/account.py.
    location_count: int


class AccountDetail(AccountSummary):
    locations: list[TenantSummary]


class AttachTenantRequest(BaseModel):
    tenant_id: uuid.UUID
