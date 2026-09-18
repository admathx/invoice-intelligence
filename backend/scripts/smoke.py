"""make smoke: curl-equivalent upload against the real API + RQ worker, poll until extracted.

Exits non-zero on failure or timeout, per SPEC.md §10 rule 2 (every gate exits non-zero).
"""
import io
import sys
import time

import httpx
from reportlab.pdfgen import canvas

from app.db import SessionLocal
from app.models import Tenant
from app.models.enums import VolumeTier

API_BASE = "http://localhost:8000"
TIMEOUT_SECONDS = 30


def make_test_pdf() -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf)
    c.drawString(72, 720, "Smoke Test Invoice")
    c.showPage()
    c.save()
    return buf.getvalue()


def get_or_create_dev_tenant() -> str:
    db = SessionLocal()
    try:
        tenant = db.query(Tenant).filter(Tenant.name == "Dev Smoke Tenant").first()
        if tenant is None:
            tenant = Tenant(name="Dev Smoke Tenant", metro="dev-metro", volume_tier=VolumeTier.under_500k)
            db.add(tenant)
            db.commit()
            db.refresh(tenant)
        return str(tenant.id)
    finally:
        db.close()


def main() -> int:
    tenant_id = get_or_create_dev_tenant()
    print(f"Using tenant {tenant_id}")

    with httpx.Client(base_url=API_BASE, timeout=10.0) as client:
        resp = client.post(
            "/invoices",
            params={"tenant_id": tenant_id},
            files={"file": ("smoke.pdf", make_test_pdf(), "application/pdf")},
        )
        if resp.status_code != 201:
            print(f"FAIL: upload returned {resp.status_code}: {resp.text}")
            return 1
        invoice_id = resp.json()["id"]
        print(f"Uploaded invoice {invoice_id}, status={resp.json()['status']}")

        start = time.monotonic()
        while time.monotonic() - start < TIMEOUT_SECONDS:
            resp = client.get(f"/invoices/{invoice_id}")
            status = resp.json()["status"]
            if status == "extracted":
                line_items = resp.json()["line_items"]
                elapsed = time.monotonic() - start
                print(f"PASS: extracted in {elapsed:.1f}s with {len(line_items)} line items")
                return 0
            if status == "failed":
                print(f"FAIL: invoice processing failed: {resp.json()}")
                return 1
            time.sleep(0.5)

    print(f"FAIL: timed out after {TIMEOUT_SECONDS}s waiting for extraction")
    return 1


if __name__ == "__main__":
    sys.exit(main())
