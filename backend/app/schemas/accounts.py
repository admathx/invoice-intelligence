import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.models.enums import VolumeTier


class AccountCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)


class TenantSummary(BaseModel):
    id: uuid.UUID
    name: str
    metro: str
    volume_tier: str
    account_id: uuid.UUID | None
    # Where the restaurant forwards its invoices (app/api/inbound.py).
    inbox_address: str | None = None

    @classmethod
    def from_tenant(cls, tenant) -> "TenantSummary":
        return cls(
            id=tenant.id,
            name=tenant.name,
            metro=tenant.metro,
            volume_tier=tenant.volume_tier.value,
            account_id=tenant.account_id,
            inbox_address=tenant.inbox_address,
        )


class TenantCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    # Benchmark cells are per metro, so spelling matters: "Austin, TX" and
    # "austin tx" would be two thin cells instead of one. The screen offers
    # the existing ones to pick from.
    metro: str = Field(min_length=1, max_length=100)
    volume_tier: VolumeTier


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
