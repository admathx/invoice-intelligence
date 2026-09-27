"""Real inbound email: the mail provider posts each message here.

Point the invoice domain's MX at a provider (Postmark, SendGrid, Mailgun, SES
via a relay...) and its inbound webhook at `https://<host>/api/inbound/email`.
Accepted bodies, whichever the provider sends:

- the raw message itself (Content-Type message/rfc822 or application/octet-stream);
- SendGrid Inbound Parse with "POST the raw, full MIME message": form field `email`;
- Mailgun routes forwarding to a URL ending in "mime": form field `body-mime`;
- Postmark with "include raw email content": JSON field `RawEmail`.

Authentication is the shared secret in settings.inbound_email_secret, as the
password of HTTP Basic auth (which every one of those providers can put in
the webhook URL: https://inbound:SECRET@host/...) or as a Bearer token.
There is no session: this is a machine calling, not a person.

Nothing is silently dropped. An email that can't become invoices (unknown
recipient, no PDF, a file too large) is kept in storage under
inbound/rejected/ and recorded in the audit trail with the reason, and the
provider gets a 200 so it doesn't retry something that can never succeed.
Anything else going wrong is a 5xx, so the provider retries; a retry of a
message that did get in comes back as a duplicate, never twice.
"""
import base64
import json
import secrets
import uuid
from datetime import datetime, timezone
from email import policy
from email.parser import BytesParser
from urllib.parse import parse_qs

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app import audit
from app.config import settings
from app.db import get_db
from app.ingest.email_stub import ingest_email_bytes, parse_email
from app.storage import get_storage

router = APIRouter(prefix="/inbound", tags=["inbound"])


def _authorized(request: Request) -> bool:
    secret = settings.inbound_email_secret.encode()
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer":
        return secrets.compare_digest(value.strip().encode(), secret)
    if scheme.lower() == "basic":
        try:
            _, _, password = base64.b64decode(value.strip()).partition(b":")
        except ValueError:
            return False
        return secrets.compare_digest(password, secret)
    return False


async def _read_body(request: Request) -> bytes:
    """The body, refusing it as soon as it passes the limit rather than after
    holding all of it."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > settings.max_inbound_email_bytes:
        raise HTTPException(status_code=413, detail="email too large")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > settings.max_inbound_email_bytes:
            raise HTTPException(status_code=413, detail="email too large")
    return bytes(body)


def _raw_message(content_type_header: str, body: bytes) -> bytes:
    """The RFC 822 message inside whatever envelope the provider used."""
    content_type = content_type_header.split(";")[0].strip().lower()
    if content_type == "multipart/form-data":
        # Parsed with the email package rather than the web framework's form
        # parser, whose limits on a text field's size are version-dependent
        # (newer Starlette caps them at 1 MB), and SendGrid sends the whole
        # message, PDFs and all, as a text field.
        envelope = BytesParser(policy=policy.default).parsebytes(
            f"Content-Type: {content_type_header}\r\n\r\n".encode() + body
        )
        for part in envelope.iter_parts():
            if part.get_param("name", header="content-disposition") in ("email", "body-mime"):
                return part.get_payload(decode=True) or b""
        raise HTTPException(status_code=422, detail="form has no raw message (expected field 'email' or 'body-mime')")
    if content_type == "application/x-www-form-urlencoded":
        fields = parse_qs(body.decode("utf-8", "replace"))
        for name in ("email", "body-mime"):
            if fields.get(name):
                return fields[name][0].encode()
        raise HTTPException(status_code=422, detail="form has no raw message (expected field 'email' or 'body-mime')")
    if content_type == "application/json":
        try:
            raw = json.loads(body).get("RawEmail")
        except (ValueError, AttributeError) as exc:
            raise HTTPException(status_code=422, detail="invalid JSON") from exc
        if not raw:
            raise HTTPException(status_code=422, detail="JSON has no RawEmail (enable raw content on the webhook)")
        return raw.encode()
    return body


def _ingest(db: Session, raw: bytes) -> dict:
    received_at = datetime.now(timezone.utc)
    source_name = f"webhook-{received_at:%Y%m%dT%H%M%S}-{uuid.uuid4().hex[:8]}.eml"
    result = ingest_email_bytes(db, raw, source_name)

    if result.status == "quarantined":
        key = f"inbound/rejected/{received_at:%Y/%m/%d}/{source_name}"
        get_storage().put(key, raw)
        try:
            parsed = parse_email(raw)
            recipients, subject = parsed.recipients, parsed.subject
        except Exception:
            recipients, subject = [], None
        audit.record(
            db,
            None,
            "email.rejected",
            "email",
            None,
            result.tenant_id,
            reason=result.reason,
            recipients=recipients or None,
            subject=subject,
            stored_at=key,
        )
        db.commit()
        return {"status": "rejected", "reason": result.reason}

    response = {"status": result.status, "invoice_ids": [str(i) for i in result.invoice_ids]}
    if result.reason:
        response["warning" if result.status == "ingested" else "reason"] = result.reason
    return response


@router.post("/email")
async def receive_email(request: Request, db: Session = Depends(get_db)) -> dict:
    if not settings.inbound_email_secret:
        raise HTTPException(status_code=404, detail="inbound email is not configured")
    if not _authorized(request):
        raise HTTPException(status_code=401, detail="bad inbound credentials")
    body = await _read_body(request)
    # Parsing, storage and the database are all blocking: off the event loop.
    raw = await run_in_threadpool(_raw_message, request.headers.get("content-type", ""), body)
    return await run_in_threadpool(_ingest, db, raw)
