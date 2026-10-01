"""Run a generated test set (documents + answer keys + manifest.csv) through
the app: python -m validation.test_set prepare|app SET_DIR WORK_DIR

1. prepare: puts every document through the same gate an upload goes
   through (app/ingest/upload.py): photos become one PDF per invoice (a
   multi-photo set, NAME-p1.jpg, NAME-p2.jpg..., is one invoice), and
   anything the app refuses or can't open is recorded as that outcome
   instead of being sent to the model. What's left is written to WORK_DIR as
   NAME.pdf beside its answer key NAME.truth.json, the layout
   validation/real_invoice_report.py reads:

       python -m validation.real_invoice_report WORK_DIR --extract   # costs API money, once
       python -m validation.real_invoice_report WORK_DIR             # accuracy, free

2. app: replays those saved extractions through the app's real pipeline
   (worker, arithmetic check, matcher, price observations, alerts) into
   test locations, without calling the model again, and compares each
   invoice's outcome with the manifest's expected_outcome. Set H (a weekly
   series) goes into its own location, and its price alerts are compared
   with series_plan.csv. Writes WORK_DIR/app_results.json.

   python -m validation.test_set cleanup   # removes the test locations
"""
import argparse
import csv
import json
import re
import shutil
import sys
import tempfile
import uuid
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.ingest.render import render_pdf_to_pngs  # noqa: E402
from app.ingest.upload import InvalidInvoiceFileError, invoice_pdf_from_upload  # noqa: E402

_PAGE = re.compile(r"^(?P<stem>.+)-p(?P<n>\d+)$")
TEST_LOCATION_PREFIX = "Test Set "


def _groups(set_dir: Path) -> dict[str, list[dict]]:
    """manifest rows grouped by invoice: a photo set is one invoice."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in csv.DictReader((set_dir / "manifest.csv").open()):
        stem = Path(row["file"]).stem
        match = _PAGE.match(stem)
        key = match["stem"] if match and (set_dir / f"{match['stem']}.truth.json").exists() else stem
        groups[key].append(row)
    for rows in groups.values():
        rows.sort(key=lambda r: int(m["n"]) if (m := _PAGE.match(Path(r["file"]).stem)) else 0)
    return dict(groups)


def _prepare_email(set_dir: Path, work_dir: Path, name: str, entry: dict, gate: dict) -> None:
    """An emailed invoice is read from its attachments, as email intake
    takes them: each PDF is an invoice; with no PDF, the photos are the pages
    of one. Each becomes NAME-attN (its answer key's name); the email itself
    is replayed through intake (app_run)."""
    from app.ingest.email_stub import parse_email
    from app.ingest.upload import validate_invoice_bytes

    entry.update(kind="email", outcome=None)
    gate[name] = entry
    parsed = parse_email((set_dir / entry["files"][0]).read_bytes())
    pdfs = [a.content for a in parsed.pdf_attachments]
    if not pdfs and parsed.photo_attachments:
        pdfs = [invoice_pdf_from_upload([a.content for a in parsed.photo_attachments])[0]]
    for i, data in enumerate(pdfs, 1):
        att = f"{name}-att{i}"
        try:
            validate_invoice_bytes(data)
        except InvalidInvoiceFileError:
            continue  # intake refuses it; nothing to read
        (work_dir / f"{att}.pdf").write_bytes(data)
        truth = set_dir / f"{att}.truth.json"
        if truth.exists():
            shutil.copyfile(truth, work_dir / f"{att}.truth.json")
        gate[att] = {**entry, "kind": "attachment", "email": name, "files": [f"{att}.pdf"], "outcome": None, "pages": 1}


def prepare(set_dir: Path, work_dir: Path) -> None:
    work_dir.mkdir(parents=True, exist_ok=True)
    gate: dict[str, dict] = {}
    for name, rows in _groups(set_dir).items():
        files = [set_dir / r["file"] for r in rows]
        entry = {
            "files": [f.name for f in files],
            "set": rows[0]["set"],
            "distributor": rows[0]["distributor"],
            "expected_outcome": rows[0]["expected_outcome"],
            "expected_reason": rows[0]["expected_reason"],
            # The restaurant billed (second set on): its own location.
            "customer": rows[0].get("customer") or None,
        }
        if files[0].suffix == ".eml":
            _prepare_email(set_dir, work_dir, name, entry, gate)
            continue
        try:
            pdf, _ = invoice_pdf_from_upload([f.read_bytes() for f in files])
        except InvalidInvoiceFileError as exc:
            entry.update(outcome="Rejected", reason=str(exc))
            gate[name] = entry
            continue
        try:
            with tempfile.TemporaryDirectory() as scratch:
                pages = len(render_pdf_to_pngs(pdf, Path(scratch)))
        except Exception as exc:
            # The worker marks these failed: "Couldn't read" in the app.
            entry.update(outcome="Couldn't read", reason=f"couldn't open the file: {type(exc).__name__}: {exc}"[:200])
            gate[name] = entry
            continue
        (work_dir / f"{name}.pdf").write_bytes(pdf)
        truth = set_dir / f"{name}.truth.json"
        if truth.exists():
            shutil.copyfile(truth, work_dir / f"{name}.truth.json")
        entry.update(outcome=None, pages=pages)
        gate[name] = entry
    (work_dir / "gate.json").write_text(json.dumps(gate, indent=2))
    sent = sum(1 for e in gate.values() if e["outcome"] is None)
    stopped = {k: e["outcome"] for k, e in gate.items() if e["outcome"]}
    print(f"{len(gate)} invoices: {sent} ready to extract, {len(stopped)} stopped at the gate: {stopped}")
    print(f"pages to extract: {sum(e.get('pages', 0) for e in gate.values())}")


# --- scoring ---------------------------------------------------------------------

_TEXT_FIELDS = ("raw_description", "raw_sku", "raw_pack_size", "uom")
_MONEY_FIELDS = ("quantity", "unit_price", "extended_price")


def _num(value):
    from decimal import Decimal, InvalidOperation

    if value is None or value == "":
        return None
    try:
        return Decimal(str(value).replace(",", "").replace("$", ""))
    except InvalidOperation:
        return None


def _same_money(a, b) -> bool:
    x, y = _num(a), _num(b)
    return x is not None and y is not None and abs(x - y) < 0.01


def _norm(text) -> str:
    return " ".join(str(text or "").upper().split())


def score(work_dir: Path) -> dict:
    """Readings against answer keys, comparing only what each key states."""
    from app.extract.dates import parse_invoice_date

    gate = json.loads((work_dir / "gate.json").read_text())
    per_invoice = {}
    for name, entry in sorted(gate.items()):
        extracted_path, truth_path = work_dir / f"{name}.extracted.json", work_dir / f"{name}.truth.json"
        if entry["outcome"] or not (extracted_path.exists() and truth_path.exists()):
            continue
        record = json.loads(extracted_path.read_text())
        truth = json.loads(truth_path.read_text())
        got = record["extraction"] or {"line_items": []}
        row = {"set": entry["set"], "cost_usd": record["cost_usd"], "failed": record["failed"]}
        row["distributor"] = got.get("distributor") == truth.get("distributor")
        row["invoice_number"] = _norm(got.get("invoice_number")) == _norm(truth.get("invoice_number"))
        row["invoice_date"] = parse_invoice_date(got.get("invoice_date")) == parse_invoice_date(truth.get("invoice_date"))
        header_money = [f for f in ("subtotal", "tax", "total") if _num(truth.get(f)) is not None]
        row["header_money_right"] = all(_same_money(got.get(f), truth.get(f)) for f in header_money)
        t_lines = {li["line_number"]: li for li in truth["line_items"]}
        g_lines = {li["line_number"]: li for li in got["line_items"]}
        both = set(t_lines) & set(g_lines)
        row["lines_truth"], row["lines_read"], row["lines_matched"] = len(t_lines), len(g_lines), len(both)
        fields = {f: [0, 0] for f in _TEXT_FIELDS + _MONEY_FIELDS + ("raw_description_loose",)}
        wrong_money_lines = 0
        for n in both:
            t, g = t_lines[n], g_lines[n]
            for f in _TEXT_FIELDS:
                if f == "uom" and not t.get(f):
                    continue
                fields[f][1] += 1
                fields[f][0] += (t.get(f) or None) == (g.get(f) or None)
            fields["raw_description_loose"][1] += 1
            fields["raw_description_loose"][0] += _norm(t.get("raw_description")) == _norm(g.get("raw_description"))
            line_ok = True
            for f in _MONEY_FIELDS:
                if _num(t.get(f)) is None:
                    continue
                fields[f][1] += 1
                ok = _same_money(t.get(f), g.get(f))
                fields[f][0] += ok
                line_ok &= ok
            wrong_money_lines += not line_ok
        row["fields"] = fields
        row["wrong_money_lines"] = wrong_money_lines
        # Wrong in a way that matters to the numbers: a total, a price or
        # quantity, or a missing or extra line.
        row["money_mistake"] = (
            not row["header_money_right"] or wrong_money_lines > 0 or len(t_lines) != len(g_lines)
        )
        per_invoice[name] = row
    (work_dir / "score.json").write_text(json.dumps(per_invoice, indent=2, default=str))
    _print_score(per_invoice)
    return per_invoice


def _print_score(per_invoice: dict) -> None:
    by_set = defaultdict(list)
    for name, row in per_invoice.items():
        by_set[row["set"]].append(row)
    total_cost = sum(r["cost_usd"] for r in per_invoice.values())
    print(f"{len(per_invoice)} invoices read, ${total_cost:.2f} (${total_cost / max(len(per_invoice), 1):.3f} each)")
    print(f"{'set':<4}{'n':>3} {'dist':>6} {'inv#':>6} {'date':>6} {'totals':>7} {'lines':>7} {'desc':>6} {'sku':>6} {'pack':>6} {'qty':>6} {'price':>6} {'ext':>6} {'all $ right':>12}")
    for group in sorted(by_set) + ["ALL"]:
        rows = list(per_invoice.values()) if group == "ALL" else by_set[group]
        pct = lambda xs: f"{100 * sum(xs) / len(xs):.0f}%" if xs else "-"
        field = lambda f: (
            f"{100 * sum(r['fields'][f][0] for r in rows) / t:.0f}%"
            if (t := sum(r["fields"][f][1] for r in rows))
            else "-"
        )
        lines = sum(r["lines_matched"] for r in rows) / max(sum(r["lines_truth"] for r in rows), 1)
        print(
            f"{group:<4}{len(rows):>3} {pct([r['distributor'] for r in rows]):>6} {pct([r['invoice_number'] for r in rows]):>6}"
            f" {pct([r['invoice_date'] for r in rows]):>6} {pct([r['header_money_right'] for r in rows]):>7} {100 * lines:>6.0f}%"
            f" {field('raw_description_loose'):>6} {field('raw_sku'):>6} {field('raw_pack_size'):>6} {field('quantity'):>6}"
            f" {field('unit_price'):>6} {field('extended_price'):>6} {pct([not r['money_mistake'] for r in rows]):>12}"
        )


# --- the app run --------------------------------------------------------------------


class _Replay:
    """Stands in for the model: returns the extraction already paid for."""

    def __init__(self) -> None:
        self.next = None

    def extract(self, pages):
        from app.extract.client import ExtractionFailedError
        from app.extract.schema import ExtractedInvoice

        record = self.next
        if record["extraction"] is None:
            raise ExtractionFailedError(record["failed"] or "extraction failed", cost_usd=record["cost_usd"])
        return ExtractedInvoice.model_validate(record["extraction"]), record["cost_usd"]


_OUTCOME = {
    "extracted": "Ready",
    "confirmed": "Ready",
    "needs_review": "Needs a look",
    "failed": "Couldn't read",
}


def _location(db, name: str, metro: str):
    from sqlalchemy import select

    from app.models import Tenant
    from app.models.enums import VolumeTier

    tenant = db.scalar(select(Tenant).where(Tenant.name == name))
    if tenant is None:
        tenant = Tenant(id=uuid.uuid4(), name=name, metro=metro, volume_tier=VolumeTier.tier_500k_1m)
        db.add(tenant)
        db.commit()
    return tenant


REVIEWER_EMAIL = "test-set-reviewer@test.invalid"


def _reviewer(db):
    """A login for the simulated person on Match items."""
    from sqlalchemy import select

    from app.auth import hash_password
    from app.models import User

    user = db.scalar(select(User).where(User.email == REVIEWER_EMAIL))
    if user is None:
        user = User(
            id=uuid.uuid4(), email=REVIEWER_EMAIL, name="Test Set Reviewer",
            password_hash=hash_password(uuid.uuid4().hex), is_operator=True,
        )  # fmt: skip
        db.add(user)
        db.commit()
    return user


def _accept_suggestions(client, db, tenant_id, invoice_id) -> int:
    """What a person does on Match items when the suggestion looks right:
    press Enter. Through the real endpoint, so aliases and price history are
    written exactly as they would be. Items with no suggestion are left, as
    a person who didn't search would leave them."""
    from sqlalchemy import select

    from app.db import bind_tenant
    from app.models import InvoiceLineItem
    from app.models.enums import ReviewStatus

    bind_tenant(db, tenant_id)
    pending = db.scalars(
        select(InvoiceLineItem.id).where(
            InvoiceLineItem.invoice_id == invoice_id,
            InvoiceLineItem.review_status == ReviewStatus.pending,
            InvoiceLineItem.canonical_sku_id.is_not(None),
        )
    ).all()
    done = 0
    for line_id in pending:
        res = client.post(f"/review/{line_id}/confirm?tenant_id={tenant_id}")
        if res.status_code == 409:
            continue  # settled already, as a repeat of one confirmed just before
        if res.status_code != 200:
            raise RuntimeError(f"confirm failed: {res.status_code} {res.text[:200]}")
        done += 1
    return done


def app_run(work_dir: Path, accept_suggestions: bool = True) -> None:
    from sqlalchemy import func, select

    from app.db import SessionLocal, bind_tenant
    from app.duplicates import file_hash
    from app.ingest.upload import save_invoice_bytes
    from app.models import CanonicalSku, Distributor, Invoice, InvoiceLineItem, PriceAlert, PriceObservation
    from app.models.enums import AlertStatus, InvoiceSource, InvoiceStatus
    from app.workers import tasks

    cleanup()
    gate = json.loads((work_dir / "gate.json").read_text())
    replay = _Replay()
    tasks.extractor = replay

    from fastapi.testclient import TestClient

    from app.auth import CSRF_HEADER, CSRF_HEADER_VALUE, current_user
    from app.main import app

    db = SessionLocal()
    reviewer = _reviewer(db)
    app.dependency_overrides[current_user] = lambda: reviewer
    client = TestClient(app, headers={CSRF_HEADER: CSRF_HEADER_VALUE})
    general = _location(db, f"{TEST_LOCATION_PREFIX}Kitchen", "Charlotte, NC")
    series = _location(db, f"{TEST_LOCATION_PREFIX}Harbor & Pine", "Charlotte, NC")
    # Set E are scans, faxes and photos OF invoices in sets A-C, with the same
    # numbers: in one location they're rightly held as copies. Their own
    # location tests how well each is read.
    scans = _location(db, f"{TEST_LOCATION_PREFIX}Scans", "Charlotte, NC")
    results: dict[str, dict] = {}
    for name, entry in sorted(gate.items()):
        result = {**entry, "app_outcome": entry["outcome"]}
        results[name] = result
        if entry["outcome"]:
            continue
        extracted = work_dir / f"{name}.extracted.json"
        if not extracted.exists():
            result["app_outcome"] = "not extracted yet"
            continue
        replay.next = json.loads(extracted.read_text())
        tenant = {"H": series, "E": scans}.get(entry["set"], general)
        data = (work_dir / f"{name}.pdf").read_bytes()
        bind_tenant(db, tenant.id)
        # As the upload endpoint does: the very same file is refused.
        digest = file_hash(data)
        if db.scalar(select(Invoice.id).where(Invoice.tenant_id == tenant.id, Invoice.file_sha256 == digest)):
            result["app_outcome"] = "Refused: already added"
            continue
        invoice = Invoice(
            id=uuid.uuid4(),
            tenant_id=tenant.id,
            source=InvoiceSource.upload,
            status=InvoiceStatus.received,
            original_file_uri="",
            file_sha256=digest,
        )
        db.add(invoice)
        db.flush()
        invoice.original_file_uri = save_invoice_bytes(invoice.id, f"{name}.pdf", data)
        db.commit()
        invoice_id = invoice.id
        try:
            tasks.process_invoice(str(invoice_id))
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"[:200]
        db.expire_all()
        bind_tenant(db, tenant.id)
        invoice = db.get(Invoice, invoice_id)
        result["invoice_id"] = str(invoice_id)
        result["status"] = invoice.status.value
        result["app_outcome"] = _OUTCOME.get(invoice.status.value, invoice.status.value)
        lines = list(db.scalars(select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == invoice_id)))
        result["lines"] = len(lines)
        result["matched"] = sum(1 for li in lines if li.canonical_sku_id is not None)
        result["auto_matched"] = sum(1 for li in lines if li.review_status.value == "auto")
        result["priced"] = sum(1 for li in lines if li.normalized_unit_price is not None)
        result["fees"] = sum(1 for li in lines if li.review_status.value == "not_product")
        distributor = db.get(Distributor, invoice.distributor_id) if invoice.distributor_id else None
        result["distributor_name"] = distributor.name if distributor else None
        if not accept_suggestions:
            continue
        # What a person on the invoice page does with a held one: delete a
        # copy or something that isn't an invoice ...
        if invoice.duplicate_of_id or invoice.document_type:
            result["held_as"] = "copy" if invoice.duplicate_of_id else invoice.document_type
            resp = client.delete(f"/invoices/{invoice_id}", params={"tenant_id": str(tenant.id)})
            result["deleted"] = resp.status_code == 204
            continue
        # ... and add a local distributor the app doesn't know yet, by the
        # name printed on the invoice.
        if distributor is not None and distributor.slug == "other" and invoice.printed_distributor:
            added = client.post(
                "/distributors", params={"tenant_id": str(tenant.id)}, json={"name": invoice.printed_distributor}
            ).json()
            client.patch(f"/invoices/{invoice_id}", params={"tenant_id": str(tenant.id)}, json={"distributor_id": added["id"]})
            result["distributor_added"] = added["name"]
            db.expire_all()
            bind_tenant(db, tenant.id)
        result["accepted"] = _accept_suggestions(client, db, tenant.id, invoice_id)

    # Set H: which products opened alerts, against what the plan says should.
    bind_tenant(db, series.id)
    alerts = db.execute(
        select(PriceAlert, CanonicalSku.name, Distributor.name)
        .join(CanonicalSku, CanonicalSku.id == PriceAlert.canonical_sku_id)
        .outerjoin(Distributor, Distributor.id == PriceAlert.distributor_id)
        .where(PriceAlert.tenant_id == series.id, PriceAlert.status == AlertStatus.open)
    ).all()
    series_result = {
        "alerts": [
            {
                "product": name,
                "distributor": distributor,
                "pct_change": str(a.pct_change),
                "from": str(a.baseline_price),
                "to": str(a.current_price),
            }
            for a, name, distributor in alerts
        ]
    }
    series_result["observations"] = db.scalar(
        select(func.count(PriceObservation.id)).where(PriceObservation.tenant_id == series.id)
    )
    app.dependency_overrides.pop(current_user, None)
    db.close()
    (work_dir / "app_results.json").write_text(json.dumps({"invoices": results, "series": series_result}, indent=2))
    _summarize(results, series_result)


def _summarize(results: dict, series: dict) -> None:
    agree = [k for k, r in results.items() if r["app_outcome"] == r["expected_outcome"]]
    print(f"\n{len(agree)}/{len(results)} invoices had the expected outcome")
    for k, r in sorted(results.items()):
        if r["app_outcome"] != r["expected_outcome"]:
            print(f"  {k}: expected {r['expected_outcome']}, got {r['app_outcome']}  {r.get('reason') or ''}")
    lines = sum(r.get("lines", 0) for r in results.values())
    print(
        f"\nitems: {lines}, priced {sum(r.get('priced', 0) for r in results.values())}, matched automatically "
        f"{sum(r.get('auto_matched', 0) for r in results.values())}, suggestions accepted "
        f"{sum(r.get('accepted', 0) for r in results.values())}"
    )
    held = {k: r["held_as"] for k, r in results.items() if r.get("held_as")}
    refused = [k for k, r in results.items() if r["app_outcome"].startswith("Refused")]
    added = {k: r["distributor_added"] for k, r in results.items() if r.get("distributor_added")}
    recognized = {
        k: r["distributor_name"]
        for k, r in results.items()
        if r.get("distributor_name") not in (None, "Other")
        and r["distributor"] == "other"
        and not r.get("distributor_added")
        and not r.get("held_as")
    }
    print(f"refused as the same file: {refused}")
    print(f"held, then deleted: {held}")
    print(f"local distributors added: {added}")
    print(f"recognized by name on a later invoice: {recognized}")
    print(f"fee lines kept off Match items: {sum(r.get('fees', 0) for r in results.values())}")
    print(f"Set H: {series.get('observations')} prices recorded; price alerts: {len(series['alerts'])}")
    for a in series["alerts"]:
        print(f"  {a['product']} from {a['distributor']}: {a['from']} -> {a['to']} ({float(a['pct_change']):+.1%})")


# The second set's restaurants share a city apart from the demo businesses, so
# price comparisons use only them.
TEST_METRO = "Test City (test set)"
HARBOR_MAILBOX = "harbor-and-pine@invoices.example.com"


def app_run_v2(work_dir: Path, set_dir: Path) -> None:
    """The second set through the app as a business would use it: a location
    per restaurant (two sharing an owner under one account), uploads through
    the upload endpoint (so the same file is refused there), emails through
    email intake, and a person doing what Match items and the invoice page
    ask: deleting held copies and non-invoices, adding local vendors,
    accepting suggestions, and, for set K, entering each missing pack size
    once. Then checks set H's alerts, set I's comparisons and set K's prices."""
    from fastapi.testclient import TestClient
    from sqlalchemy import select

    import app.queue as queue_module
    from app.auth import CSRF_HEADER, CSRF_HEADER_VALUE, current_user
    from app.db import SessionLocal, bind_tenant
    from app.duplicates import file_hash
    from app.ingest.email_stub import ingest_email_bytes
    from app.main import app
    from app.models import Account, Distributor, Invoice, InvoiceLineItem
    from app.storage import read_uri
    from app.workers import tasks

    cleanup()
    gate = json.loads((work_dir / "gate.json").read_text())
    manifest = list(csv.DictReader((set_dir / "manifest.csv").open()))
    replay = _Replay()
    tasks.extractor = replay
    queue_module.invoice_queue.enqueue = lambda *a, **k: None  # read here, not by a worker

    by_hash = {
        file_hash(pdf.read_bytes()): pdf.stem
        for pdf in work_dir.glob("*.pdf")
        if (work_dir / f"{pdf.stem}.extracted.json").exists()
    }

    db = SessionLocal()
    reviewer = _reviewer(db)
    app.dependency_overrides[current_user] = lambda: reviewer
    client = TestClient(app, headers={CSRF_HEADER: CSRF_HEADER_VALUE})
    customers = sorted({e["customer"] for e in gate.values() if e.get("customer")})
    # The six compared with each other (set I) share a city; every other
    # restaurant is alone in its own, so it can't pass for a local business.
    compared = {r["customer"] for r in csv.DictReader((set_dir / "peers_plan.csv").open())}
    tenants = {
        c: _location(db, f"{TEST_LOCATION_PREFIX}{c}", TEST_METRO if c in compared else f"{c} (test set)") for c in customers
    }
    series_customer = next(r["customer"] for r in manifest if r["set"] == "H")
    packs_customer = next(r["customer"] for r in manifest if r["set"] == "K")
    owners: dict[str, set] = defaultdict(set)
    for row in manifest:
        if row["account"] and row["customer"]:
            owners[row["account"]].add(row["customer"])
    shared = {number: names for number, names in owners.items() if len(names) > 1}
    for number, names in shared.items():
        account = Account(id=uuid.uuid4(), name=f"{TEST_LOCATION_PREFIX}owner {number}")
        db.add(account)
        db.flush()
        for name in names:
            tenants[name].account_id = account.id
    tenants["Harbor & Pine Kitchen"].inbox_address = HARBOR_MAILBOX
    db.commit()
    tenant_ids = {c: t.id for c, t in tenants.items()}

    packs_plan = {
        (Path(r["invoice_file"]).stem, r["item_code"]): r["real_pack_size"]
        for r in csv.DictReader((set_dir / "packs_plan.csv").open())
    }
    packs_entered: set[str] = set()
    invoice_names: dict = {}
    results: dict[str, dict] = {}

    def read(name: str, invoice_id, tenant_id) -> dict:
        """Read one stored invoice with its paid-for extraction, then do what
        a person would with it."""
        result = results.setdefault(name, {**gate.get(name, {}), "app_outcome": None})
        replay.next = json.loads((work_dir / f"{name}.extracted.json").read_text())
        try:
            tasks.process_invoice(str(invoice_id))
        except Exception as exc:
            result["error"] = f"{type(exc).__name__}: {exc}"[:200]
        db.expire_all()
        bind_tenant(db, tenant_id)
        invoice = db.get(Invoice, invoice_id)
        invoice_names[invoice_id] = name
        result["status"] = invoice.status.value
        result["app_outcome"] = _OUTCOME.get(invoice.status.value, invoice.status.value)
        lines = list(db.scalars(select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == invoice_id)))
        result["lines"] = len(lines)
        result["auto_matched"] = sum(1 for li in lines if li.review_status.value == "auto")
        result["fees"] = sum(1 for li in lines if li.review_status.value == "not_product")
        distributor = db.get(Distributor, invoice.distributor_id) if invoice.distributor_id else None
        result["distributor_name"] = distributor.name if distributor else None
        if invoice.duplicate_of_id or invoice.document_type:
            result["held_as"] = "copy" if invoice.duplicate_of_id else invoice.document_type
            client.delete(f"/invoices/{invoice_id}", params={"tenant_id": str(tenant_id)})
            return result
        if distributor is not None and distributor.slug == "other":
            # What a person looking at the page does: choose the big
            # distributor it's from when the name couldn't be read (a crumpled
            # scan printed "Virginia Foods - Forwarding Foods" for PFG), or add
            # the local vendor whose name it prints.
            big = db.scalar(select(Distributor).where(Distributor.slug == result.get("distributor")))
            if big is not None and big.slug != "other":
                chosen = {"id": str(big.id), "name": big.name}
                result["distributor_chosen"] = big.name
            elif invoice.printed_distributor:
                chosen = client.post(
                    "/distributors", params={"tenant_id": str(tenant_id)}, json={"name": invoice.printed_distributor}
                ).json()
                result["distributor_added"] = chosen["name"]
            else:
                chosen = None
            if chosen:
                client.patch(f"/invoices/{invoice_id}", params={"tenant_id": str(tenant_id)}, json={"distributor_id": chosen["id"]})
                db.expire_all()
                bind_tenant(db, tenant_id)
        result["accepted"] = _accept_suggestions(client, db, tenant_id, invoice_id)
        if result.get("set") == "K":
            # The person fills in each missing pack size as Match items asks:
            # once per item, the first time it can't be priced.
            db.expire_all()
            bind_tenant(db, tenant_id)
            for line in db.scalars(select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == invoice_id)):
                real = packs_plan.get((name, (line.raw_sku or "").strip()))
                if (
                    real
                    and line.normalized_unit_price is None
                    and line.review_status.value != "not_product"
                    and line.raw_sku not in packs_entered
                ):
                    resp = client.post(f"/review/{line.id}/pack", params={"tenant_id": str(tenant_id)}, json={"pack_size": real})
                    if resp.status_code == 200:
                        packs_entered.add(line.raw_sku)
        return result

    for name, entry in sorted(gate.items()):
        if entry.get("kind") == "attachment":
            continue  # read when its email arrives
        if entry.get("outcome"):
            results[name] = {**entry, "app_outcome": entry["outcome"]}
            continue
        tenant_id = tenant_ids[entry["customer"]]
        if entry.get("kind") == "email":
            raw = (set_dir / entry["files"][0]).read_bytes()
            ingest = ingest_email_bytes(db, raw, name)
            result = results.setdefault(name, {**entry})
            if ingest.status == "quarantined":
                result["app_outcome"] = "Rejected"
                result["reason"] = ingest.reason
                continue
            if ingest.status == "duplicate":
                result["app_outcome"] = "Refused as a copy"
                continue
            outcomes = []
            attachments = [k for k, e in gate.items() if e.get("email") == name]
            for invoice_id in ingest.invoice_ids:
                bind_tenant(db, tenant_id)
                stored = db.get(Invoice, invoice_id)
                att = by_hash.get(file_hash(read_uri(stored.original_file_uri)))
                if att is None and len(attachments) == 1 and len(ingest.invoice_ids) == 1:
                    # Photos become a PDF afresh each time (not byte-identical);
                    # one invoice from one attachment is that attachment.
                    att = attachments[0]
                if att is None:
                    outcomes.append("not read")
                    continue
                outcomes.append(read(att, invoice_id, tenant_id)["app_outcome"])
            result["app_outcome"] = (
                "Needs a look" if "Needs a look" in outcomes else outcomes[0] if len(set(outcomes)) == 1 else "/".join(outcomes)
            )
            result["invoices"] = len(ingest.invoice_ids)
            continue
        data = (work_dir / f"{name}.pdf").read_bytes()
        resp = client.post(
            f"/invoices?tenant_id={tenant_id}", files={"file": (f"{name}.pdf", data, "application/pdf")}
        )
        if resp.status_code == 409:
            results[name] = {**entry, "app_outcome": "Refused as a copy", "reason": resp.json()["detail"]}
            continue
        if resp.status_code != 201:
            results[name] = {**entry, "app_outcome": "Rejected", "reason": resp.json().get("detail")}
            continue
        read(name, uuid.UUID(resp.json()["id"]), tenant_id)

    report = {"invoices": results}
    report["series"] = _series_v2(db, tenant_ids[series_customer])
    report["peers"] = _peers_v2(db, set_dir, tenants, gate)
    report["packs"] = _packs_v2(db, tenant_ids[packs_customer], packs_plan, invoice_names, len(packs_entered))
    score_path = work_dir / "score.json"
    if score_path.exists():
        scores = json.loads(score_path.read_text())
        scores = scores.get("invoices", scores)
        report["misread"] = {
            k: results.get(k, {}).get("app_outcome")
            for k, v in scores.items()
            if isinstance(v, dict) and (v.get("money_mistake") or v.get("header_money_right") is False)
        }
    app.dependency_overrides.pop(current_user, None)
    db.close()
    (work_dir / "app_results.json").write_text(json.dumps(report, indent=2, default=str))
    _summarize_v2(report)


def _series_v2(db, tenant_id) -> dict:
    from sqlalchemy import select

    from app.db import bind_tenant
    from app.models import CanonicalSku, Distributor, PriceAlert
    from app.models.enums import AlertStatus

    bind_tenant(db, tenant_id)
    alerts = db.execute(
        select(PriceAlert, CanonicalSku.name, Distributor.name)
        .join(CanonicalSku, CanonicalSku.id == PriceAlert.canonical_sku_id)
        .outerjoin(Distributor, Distributor.id == PriceAlert.distributor_id)
        .where(PriceAlert.tenant_id == tenant_id, PriceAlert.status == AlertStatus.open)
    ).all()
    return {
        "alerts": [
            {"product": name, "distributor": d, "pct_change": str(a.pct_change), "from": str(a.baseline_price), "to": str(a.current_price)}
            for a, name, d in alerts
        ]
    }


def _peers_v2(db, set_dir: Path, tenants: dict, gate: dict) -> dict:
    """What each set I restaurant is told about how its prices compare."""
    from sqlalchemy import func, select

    from app.analytics.benchmark import account_key_for, compute_benchmark
    from app.db import bind_tenant
    from app.models import CanonicalSku, PriceObservation

    peer_customers = sorted({r["customer"] for r in csv.DictReader((set_dir / "peers_plan.csv").open())})
    out: dict[str, list] = {}
    for customer in peer_customers:
        tenant = tenants[customer]
        bind_tenant(db, tenant.id)
        latest = db.execute(
            select(PriceObservation.canonical_sku_id, func.max(PriceObservation.observed_on))
            .where(PriceObservation.tenant_id == tenant.id)
            .group_by(PriceObservation.canonical_sku_id)
        ).all()
        rows = []
        for sku_id, as_of in latest:
            price = db.scalar(
                select(PriceObservation.unit_price_base)
                .where(PriceObservation.tenant_id == tenant.id, PriceObservation.canonical_sku_id == sku_id)
                .order_by(PriceObservation.observed_on.desc())
                .limit(1)
            )
            bench = compute_benchmark(
                db, sku_id, tenant.metro, as_of, exclude_account_key=account_key_for(db, tenant.id), subject_price=price
            )
            rows.append(
                {
                    "product": db.scalar(select(CanonicalSku.name).where(CanonicalSku.id == sku_id)),
                    "scope": bench.scope if bench else None,
                    "businesses": bench.distinct_account_count if bench else None,
                    "percentile": str(bench.subject_percentile) if bench and bench.subject_percentile is not None else None,
                }
            )
        out[customer] = rows
    return out


def _packs_v2(db, tenant_id, packs_plan: dict, invoice_names: dict, entered: int) -> dict:
    """Set K's prices against the real pack sizes: each line priced as the
    real pack would price it."""
    from sqlalchemy import select

    from decimal import Decimal

    from app.db import bind_tenant
    from app.models import CanonicalSku, InvoiceLineItem
    from app.normalize.matcher import normalize_price

    bind_tenant(db, tenant_id)
    counts = {"lines": 0, "priced": 0, "right": 0, "wrong": [], "remembered": 0, "entered": entered}
    for line in db.scalars(select(InvoiceLineItem).where(InvoiceLineItem.tenant_id == tenant_id)):
        name = invoice_names.get(line.invoice_id)
        real = packs_plan.get((name, (line.raw_sku or "").strip()))
        if real is None or not name.startswith("K-"):
            continue
        counts["lines"] += 1
        counts["remembered"] += int(line.pack_size_remembered)
        if line.normalized_unit_price is None:
            continue
        counts["priced"] += 1
        product = db.get(CanonicalSku, line.canonical_sku_id) if line.canonical_sku_id else None
        _, expected = normalize_price(real, line.quantity, line.unit_price, line.uom, product)
        if expected is not None and abs(expected - line.normalized_unit_price) <= Decimal("0.0002"):
            counts["right"] += 1
        else:
            counts["wrong"].append(f"{name} {line.raw_description} [{line.raw_pack_size}] {line.normalized_unit_price} vs {expected}")
    return counts


def _summarize_v2(report: dict) -> None:
    from collections import Counter

    results = report["invoices"]

    def agrees(r) -> bool:
        expected = r.get("expected_outcome") or ""
        return r.get("app_outcome") in [e.strip() for e in expected.split(" or ")]

    graded = {k: r for k, r in results.items() if r.get("kind") != "attachment"}
    print(f"\n{sum(agrees(r) for r in graded.values())}/{len(graded)} documents had the expected outcome")
    for k, r in sorted(graded.items()):
        if not agrees(r):
            print(f"  {k}: expected {r.get('expected_outcome')}, got {r.get('app_outcome')}  {r.get('reason') or ''}"[:200])
    print(f"held, then deleted: { {k: r['held_as'] for k, r in results.items() if r.get('held_as')} }")
    print(f"local distributors added: { {k: r['distributor_added'] for k, r in results.items() if r.get('distributor_added')} }")
    print(
        "recognized by name: "
        f"{ {k: r['distributor_name'] for k, r in results.items() if r.get('distributor') == 'other' and r.get('distributor_name') not in (None, 'Other') and not r.get('distributor_added') and not r.get('held_as')} }"
    )
    print(f"fee lines kept off Match items: {sum(r.get('fees', 0) for r in results.values())}")
    print(f"\nSet H alerts ({len(report['series']['alerts'])}):")
    for a in report["series"]["alerts"]:
        print(f"  {a['product']} from {a['distributor']}: {a['from']} -> {a['to']} ({float(a['pct_change']):+.1%})")
    print("\nSet I, what each restaurant is shown:")
    for customer, rows in report["peers"].items():
        scopes = Counter(r["scope"] for r in rows)
        high = [r["product"] for r in rows if r["percentile"] and float(r["percentile"]) >= 0.9 and r["scope"] == "metro"]
        print(f"  {customer}: {dict(scopes)}; paying the most nearby for: {high}")
    if "misread" in report:
        print(f"\nDocuments read with a wrong amount, and what the app did: {report['misread']}")
    k = report["packs"]
    print(
        f"\nSet K: {k['entered']} pack sizes entered by hand; {k['priced']}/{k['lines']} lines priced, "
        f"{k['right']} at the real pack's price, {k['remembered']} filled in from memory"
    )
    for w in k["wrong"][:10]:
        print(f"  wrong: {w}")


def cleanup() -> None:
    """Remove the test locations and everything in them."""
    from sqlalchemy import delete, select

    from app.db import SessionLocal, bind_tenant
    from app.models import (
        Account,
        AuditEvent,
        Distributor,
        Invoice,
        InvoiceLineItem,
        PriceAlert,
        PriceObservation,
        SkuAlias,
        Tenant,
        User,
    )
    from app.storage import forget_original, get_storage, renders_prefix

    db = SessionLocal()
    tenant_ids = list(db.scalars(select(Tenant.id).where(Tenant.name.like(f"{TEST_LOCATION_PREFIX}%"))))
    for tenant_id in tenant_ids:
        bind_tenant(db, tenant_id)
        invoice_ids = list(db.scalars(select(Invoice.id).where(Invoice.tenant_id == tenant_id)))
        line_ids = select(InvoiceLineItem.id).where(InvoiceLineItem.tenant_id == tenant_id)
        db.execute(delete(PriceObservation).where(PriceObservation.invoice_line_item_id.in_(line_ids)))
        db.execute(delete(PriceObservation).where(PriceObservation.tenant_id == tenant_id))
        db.execute(delete(PriceAlert).where(PriceAlert.tenant_id == tenant_id))
        db.execute(delete(SkuAlias).where(SkuAlias.tenant_id == tenant_id))
        db.execute(delete(InvoiceLineItem).where(InvoiceLineItem.tenant_id == tenant_id))
        db.execute(delete(Invoice).where(Invoice.tenant_id == tenant_id))
        db.execute(delete(AuditEvent).where(AuditEvent.tenant_id == tenant_id))
        db.execute(delete(Distributor).where(Distributor.account_key == tenant_id))
        db.execute(delete(Tenant).where(Tenant.id == tenant_id))
        db.commit()
        for invoice_id in invoice_ids:
            forget_original(invoice_id)
            get_storage().delete_prefix(renders_prefix(invoice_id))
    db.execute(delete(User).where(User.email == REVIEWER_EMAIL))
    db.execute(delete(Account).where(Account.name.like(f"{TEST_LOCATION_PREFIX}owner %")))
    db.commit()
    db.close()
    if tenant_ids:
        print(f"removed {len(tenant_ids)} test location(s)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("prepare")
    p.add_argument("set_dir", type=Path)
    p.add_argument("work_dir", type=Path)
    a = sub.add_parser("app")
    a.add_argument("work_dir", type=Path)
    a2 = sub.add_parser("app2", help="the second set: locations per restaurant, emails, peers, packs")
    a2.add_argument("work_dir", type=Path)
    a2.add_argument("set_dir", type=Path)
    sc = sub.add_parser("score")
    sc.add_argument("work_dir", type=Path)
    sub.add_parser("cleanup")
    args = parser.parse_args()
    if args.command == "prepare":
        prepare(args.set_dir, args.work_dir)
    elif args.command == "app":
        app_run(args.work_dir)
    elif args.command == "app2":
        app_run_v2(args.work_dir, args.set_dir)
    elif args.command == "score":
        score(args.work_dir)
    else:
        cleanup()


if __name__ == "__main__":
    main()
