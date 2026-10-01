"""Spotting an invoice that's a copy of one already added.

On a realistic test set, one invoice arrived as a PDF, a byte-identical copy
and a phone rescan, and all three were counted: its spending three times
over, its prices three times in the history. An identical file is refused at
upload (app/api/invoices.py, by hash). A rescan is a different file, so once
it's been read it's compared by distributor and invoice number, and held as a
likely copy for a person to delete or keep.
"""
import hashlib
import re

from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session

from app.business_distributors import name_key
from app.models import Distributor, Invoice
from app.models.distributor import UNRECOGNIZED_SLUG
from app.models.enums import InvoiceStatus


BEING_READ = (InvoiceStatus.received, InvoiceStatus.rendering, InvoiceStatus.extracting)


def file_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def same_file(tenant_id, digest: str):
    """This location's invoices from the identical file, except one that
    couldn't be read: sending it again is how a person retries."""
    return (Invoice.tenant_id == tenant_id, Invoice.file_sha256 == digest, Invoice.status != InvoiceStatus.failed)


def number_key(number: str | None) -> str:
    """An invoice number as a rescan would read it too: no spaces,
    punctuation, case or leading zeros ("# 0088214" is "88214")."""
    return re.sub(r"[^A-Z0-9]", "", (number or "").upper()).lstrip("0")


# How a reissued invoice is numbered: the original's number and a suffix
# that says so, sometimes counted ("904718242 R1"). A bare R needs its
# separator or its count: "1234R" says no more than "1234A" does.
_REISSUE = re.compile(
    r"(.*\d)\s*[-/. ]?\s*(RB|REV|REVISED|REBILL|CORR|CORRECTED|R(?=\d)|(?<=[-/. ])R)\s*\d{0,2}"
)
# The original's number and one letter ("904718243A", "88214-B"). That is
# also how the parts of a split shipment are numbered, which are different
# invoices, so it counts only with the same total (find_original).
_LETTERED = re.compile(r"(.*\d)\s*[-/. ]?\s*[A-Z]")


def reissue_of(number: str | None) -> str | None:
    """The key of the invoice number this one reissues ("904718271-R" is a
    rebill of 904718271), or None. A rebill came out Ready beside its
    original, and the delivery was counted twice."""
    match = _REISSUE.fullmatch((number or "").strip().upper())
    return number_key(match.group(1)) or None if match else None


def lettered_from(number: str | None) -> str | None:
    """The key of the number this one adds a single letter to, or None."""
    match = _LETTERED.fullmatch((number or "").strip().upper())
    return number_key(match.group(1)) or None if match else None


def _is_credit(invoice: Invoice) -> bool:
    return invoice.total is not None and invoice.total < 0


def one_at_a_time(db: Session, tenant_id) -> None:
    """Hold this location's copy check until the caller's transaction ends.

    With several workers, an invoice and a rescan of it can finish reading
    at the same moment; each would look for the other before either was
    saved, find nothing, and both would be kept. Taking this first makes the
    second wait for the first to commit, and then see it. An advisory lock
    on the location: nothing else contends for it, and it lasts only for
    the saving, not the reading."""
    db.execute(select(func.pg_advisory_xact_lock(func.hashtextextended(f"duplicates|{tenant_id}", 0))))


def find_original(db: Session, invoice: Invoice) -> Invoice | None:
    """The earliest invoice added before this one that it looks like a copy
    of: same location, same distributor, same number, or one the reissue of
    the other ("904718271-R"), whichever of those arrived first; or one the
    other's number with a letter added and the same total ("904718243A", a
    rebill; "88214-A" and "88214-B" with their own totals are the two parts
    of a split shipment). None without a number or a distributor to go on.
    For an unattributed invoice, the printed distributor name has to match
    as well.

    A credit and a charge are never copies of each other: a credit memo
    often carries the number of the invoice it credits."""
    key = number_key(invoice.invoice_number)
    if not key or invoice.distributor_id is None:
        return None
    reissued = reissue_of(invoice.invoice_number)
    lettered = lettered_from(invoice.invoice_number)
    keys = [k for k in (key, reissued, lettered) if k]
    stored_key = func.ltrim(func.regexp_replace(func.upper(Invoice.invoice_number), "[^A-Z0-9]", "", "g"), "0")
    candidates = db.scalars(
        select(Invoice)
        .where(
            Invoice.tenant_id == invoice.tenant_id,
            Invoice.id != invoice.id,
            # The original is the one added first. Attachments of one email
            # share a timestamp (the transaction's), so ties go by id.
            or_(
                Invoice.created_at < invoice.created_at,
                and_(Invoice.created_at == invoice.created_at, Invoice.id < invoice.id),
            ),
            Invoice.distributor_id == invoice.distributor_id,
            # Its own number, the one it reissues or adds a letter to, or
            # (narrowed below) one that does that to it or reissues the same
            # one: a suffix after the same number.
            or_(stored_key.in_(keys), *(stored_key.like(f"{prefix}%") for prefix in (key, reissued) if prefix)),
            Invoice.status != InvoiceStatus.failed,
            # The original, not another copy of it.
            Invoice.duplicate_of_id.is_(None),
        )
        .order_by(Invoice.created_at)
    ).all()
    distributor = db.get(Distributor, invoice.distributor_id)
    if distributor is not None and distributor.slug == UNRECOGNIZED_SLUG:
        printed = name_key(invoice.printed_distributor or "")
        candidates = [c for c in candidates if printed and name_key(c.printed_distributor or "") == printed]

    def same_invoice(other: Invoice) -> bool:
        theirs = number_key(other.invoice_number)
        # The same number, one a reissue of the other, or both reissues of
        # one original ("R1" and "R2", when the original has been deleted).
        their_original = reissue_of(other.invoice_number)
        if theirs == key or theirs == reissued or (their_original and their_original in (key, reissued)):
            return True
        same_total = invoice.total is not None and other.total == invoice.total
        return same_total and (theirs == lettered or lettered_from(other.invoice_number) == key)

    candidates = [c for c in candidates if same_invoice(c) and _is_credit(c) == _is_credit(invoice)]
    return candidates[0] if candidates else None
