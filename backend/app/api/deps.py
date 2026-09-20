"""Shared API-layer helpers.

`get_tenant_or_404` exists because `get_db_for_tenant` only *binds* a
tenant_id for query scoping (app/db.py) — it never checks that the tenant
actually exists. Endpoints that skipped their own check behaved
inconsistently: some returned a clean 404, others a degenerate 200, and the
one write endpoint (invoices.upload_invoice) would have raised an unhandled
IntegrityError on the FK after already writing the uploaded file to disk.
"""
import uuid

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import Tenant


def get_tenant_or_404(db: Session, tenant_id: uuid.UUID) -> Tenant:
    tenant = db.get(Tenant, tenant_id)
    if tenant is None:
        raise HTTPException(status_code=404, detail="tenant not found")
    return tenant
