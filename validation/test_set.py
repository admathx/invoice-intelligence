"""Run a generated test set (documents + answer keys + manifest.csv) through
the app. In order (each: PYTHONPATH=backend:. python -m validation.test_set ...):

    prepare SET_DIR WORK_DIR       free: the gate, and a check of the set itself
    python -m validation.real_invoice_report WORK_DIR --extract --workers 6    costs API money
    split WORK_DIR                 free: the other invoices found inside files
    python -m validation.real_invoice_report WORK_DIR --extract --workers 6    the few split out
    score WORK_DIR                 free: readings against the answer keys
    app2 WORK_DIR SET_DIR          free: the replay through the app
    cleanup                        removes the test locations

prepare puts every document through the same gate an upload goes through
(app/ingest/upload.py): photos become one PDF per invoice (a multi-photo
set, NAME-p1.jpg, NAME-p2.jpg..., is one invoice), an email is read from its
attachments as intake takes them, and anything the app refuses or can't
open is recorded as that outcome instead of being sent to the model. What's
left is written to WORK_DIR as NAME.pdf beside its answer key
NAME.truth.json. It prints what reading will cost, and what in the set's
own files would mislead (check_set).

app2 replays the saved readings through the app's real pipeline (upload
endpoint, email intake, worker, arithmetic check, matcher, price history,
alerts) as a business would use it: a location per restaurant, a person
deleting held copies, adding local vendors and accepting suggestions. It
compares each document's outcome with the manifest's expected_outcome
(known and find-out cases apart), and checks the planned sets against their
plans: price alerts (series_plan.csv, changes_plan.csv, and year_plan.csv,
whose invoices are added in its upload_order), comparisons with other
restaurants (peers_plan.csv), pack sizes (packs_plan.csv) and matching
(products_plan.csv). Every plan is optional. Writes WORK_DIR/app_results.json.

`app` is the first set's simpler replay, kept for that set.
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

from app.ingest.render import MAX_PAGE_IMAGES, render_pdf_to_pngs  # noqa: E402
from app.ingest.upload import InvalidInvoiceFileError, invoice_pdf_from_upload  # noqa: E402

_PAGE = re.compile(r"^(?P<stem>.+)-p(?P<n>\d+)$")
TEST_LOCATION_PREFIX = "Test Set "
# What a read has cost per document, for the estimate prepare prints.
USD_PER_DOCUMENT = 0.032
OUTCOMES = {"Ready", "Needs a look", "Couldn't read", "Rejected", "Refused as a copy"}


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
    # Every key the email has, including those for invoices the app has to
    # separate out of one attachment (two invoices photographed together).
    for key in sorted(set_dir.glob(f"{name}-att*.truth.json")):
        shutil.copyfile(key, work_dir / key.name)
    try:
        parsed = parse_email((set_dir / entry["files"][0]).read_bytes())
    except Exception:
        return  # a message that can't be parsed: intake rejects it; nothing to read
    pdfs = [a.content for a in parsed.pdf_attachments]
    if not pdfs and parsed.photo_attachments:
        try:
            pdfs = [invoice_pdf_from_upload([a.content for a in parsed.photo_attachments])[0]]
        except InvalidInvoiceFileError:
            pdfs = []  # too many photos: intake refuses the email
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
            # Fourth set on: where it's uploaded when that isn't who is
            # billed, and whether the expected outcome is known or a guess.
            "upload_to": rows[0].get("upload_to") or None,
            "certainty": rows[0].get("certainty") or "known",
            "notes": rows[0].get("notes") or "",
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
        except Exception as exc:
            # Not a refusal: the upload itself broke, which a person would see
            # as an error page. A bug to hear about, whatever was expected.
            entry.update(outcome="Error", reason=f"adding it raised {type(exc).__name__}: {exc}"[:200])
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
        if pages > MAX_PAGE_IMAGES:
            # As the worker does (app/workers/tasks.py): more page images
            # than one read takes, so it stops before calling the model.
            entry.update(outcome="Couldn't read", reason=f"{pages} page images is more than can be read at once")
            gate[name] = entry
            continue
        (work_dir / f"{name}.pdf").write_bytes(pdf)
        truth = set_dir / f"{name}.truth.json"
        if truth.exists():
            shutil.copyfile(truth, work_dir / f"{name}.truth.json")
        # A file holding several invoices has a key for each (NAME.1, NAME.2
        # ...). The first is the invoice the file itself becomes; the others
        # are matched to what the app separates out of it (split()).
        for part_key in sorted(set_dir.glob(f"{name}.*.truth.json")):
            shutil.copyfile(part_key, work_dir / part_key.name)
        first = work_dir / f"{name}.1.truth.json"
        if first.exists() and not truth.exists():
            shutil.copyfile(first, work_dir / f"{name}.truth.json")
        entry.update(outcome=None, pages=pages)
        gate[name] = entry
    (work_dir / "gate.json").write_text(json.dumps(gate, indent=2))
    sent = sum(1 for e in gate.values() if e["outcome"] is None and e.get("kind") != "email")
    stopped = {k: e["outcome"] for k, e in gate.items() if e["outcome"]}
    print(f"{len(gate)} entries: {sent} documents to read, {len(stopped)} stopped before reading: {stopped}")
    print(f"pages to read: {sum(e.get('pages', 0) for e in gate.values())}; about ${sent * USD_PER_DOCUMENT:.2f} at ${USD_PER_DOCUMENT} a document")
    for problem in check_set(set_dir):
        print(f"  check: {problem}")


def check_set(set_dir: Path) -> list[str]:
    """What in a set's own files would make its results misleading, found
    before anything is read: a manifest row with no file or no answer key,
    an outcome the runner doesn't know, a local vendor's first invoice at a
    restaurant expected Ready (the app holds it for a person to add the
    vendor), a restaurant meant to be in one set that is in two."""
    problems: list[str] = []
    manifest = list(csv.DictReader((set_dir / "manifest.csv").open()))
    by_file = {row["file"]: row for row in manifest}
    for row in manifest:
        if not (set_dir / row["file"]).exists():
            problems.append(f"{row['file']}: in the manifest, not in the folder")
        for outcome in row["expected_outcome"].split(" or "):
            if outcome.strip() not in OUTCOMES:
                problems.append(f"{row['file']}: expected_outcome {row['expected_outcome']!r} isn't one the runner knows")
        if (row.get("certainty") or "known") not in ("known", "find_out"):
            problems.append(f"{row['file']}: certainty {row['certainty']!r}")
    for name, rows in _groups(set_dir).items():
        row = rows[0]
        keyed = (set_dir / f"{name}.truth.json").exists() or any(set_dir.glob(f"{name}.*.truth.json")) or any(set_dir.glob(f"{name}-att*.truth.json"))
        if not keyed and "Ready" in row["expected_outcome"] and not row["file"].endswith(".eml"):
            problems.append(f"{name}: expected {row['expected_outcome']} but has no answer key")
    for row in _plan(set_dir, "vendors_plan.csv"):
        first = by_file.get(row.get("first_invoice_file", ""))
        if first is None:
            problems.append(f"vendors_plan: {row.get('first_invoice_file')!r} isn't in the manifest")
        elif "Needs a look" not in first["expected_outcome"]:
            problems.append(
                f"{first['file']}: {row.get('vendor')}'s first invoice at {row.get('restaurant')} is expected "
                f"{first['expected_outcome']}; the app holds a new vendor's first invoice"
            )
    alone = {"H", "K", "O", "U", "V"}
    for row in _plan(set_dir, "restaurants.csv"):
        sets = {x.strip() for x in (row.get("sets") or "").replace(",", ";").split(";") if x.strip()}
        if sets & alone and len(sets) > 1:
            problems.append(f"{row.get('customer')}: in sets {sorted(sets)}, but a set {sorted(sets & alone)} restaurant should be in no other")
    order: dict[str, set] = defaultdict(set)
    for row in _plan(set_dir, "year_plan.csv"):
        order[row.get("invoice_file", "")].add((row.get("upload_order") or "").strip())
    bad = sorted(f for f, values in order.items() if len(values) != 1 or not next(iter(values)).isdigit())
    if bad:
        problems.append(f"year_plan: {len(bad)} files without one whole-number upload_order, e.g. {bad[:3]}")
    taken = [next(iter(values)) for f, values in order.items() if f not in bad]
    if len(taken) != len(set(taken)):
        problems.append("year_plan: two files share an upload_order")
    for plan in ("changes_plan.csv", "year_plan.csv"):
        expected: dict[str, set] = defaultdict(set)
        for row in _plan(set_dir, plan):
            expected[row.get("behavior", "")].add(row.get("expected_alert", ""))
        for behavior, values in expected.items():
            if len(values) > 1:
                problems.append(f"{plan}: {behavior} rows disagree on expected_alert ({sorted(values)}); they are graded apart")
            if values - {"yes", "no"}:
                problems.append(f"{plan}: {behavior} has expected_alert {sorted(values - {'yes', 'no'})}; it must be yes or no")
    for plan, column in (("year_plan.csv", "invoice_file"), ("changes_plan.csv", "invoice_file"), ("products_plan.csv", "invoice_file")):
        missing = sorted({row[column] for row in _plan(set_dir, plan) if row.get(column) and row[column] not in by_file})
        if missing:
            problems.append(f"{plan}: {len(missing)} files not in the manifest, e.g. {missing[:3]}")
    return problems


def split(work_dir: Path) -> None:
    """After the first read: where the reader found several invoices in one
    file, write each of the others as its own PDF (the same pages the app
    would take, app/splitting.py), to be read in a second pass. A part of
    NAME is NAME.2, NAME.3 ...; for an email's photos, which arrive as one
    PDF (NAME-att1), the parts take the email's next keys (NAME-att2 ...)."""
    from app import splitting
    from app.ingest.render import pdf_of_pages, render_pages

    gate = json.loads((work_dir / "gate.json").read_text())
    made = 0
    for name, entry in sorted(gate.items()):
        record_path = work_dir / f"{name}.extracted.json"
        if entry.get("outcome") or entry.get("kind") == "part" or not record_path.exists():
            continue
        extraction = json.loads(record_path.read_text())["extraction"]
        if not extraction:
            continue
        pdf = (work_dir / f"{name}.pdf").read_bytes()
        with tempfile.TemporaryDirectory() as scratch:
            pdf_pages = [page.pdf_page for page in render_pages(pdf, Path(scratch))]
        plan = splitting.plan(extraction.get("page_invoices") or [], pdf_pages)
        if not plan:
            continue
        entry["split"] = {str(number): pages for number, pages in plan.items()}
        for position, number in enumerate(sorted(plan)[1:], start=2):
            # An email's photos arrive as one PDF (NAME-att1), and its keys
            # for the invoices in them are NAME-att2 ...: taken only when no
            # real attachment has that name.
            part = f"{name}.{position}"
            if entry.get("kind") == "attachment" and name.endswith("-att1") and f"{name[:-1]}{position}" not in gate:
                part = f"{name[:-1]}{position}"
            (work_dir / f"{part}.pdf").write_bytes(pdf_of_pages(pdf, plan[number]))
            truth = work_dir / f"{part}.truth.json"
            gate[part] = {**entry, "kind": "part", "part_of": name, "pages_of_file": plan[number], "files": [f"{part}.pdf"],
                          "outcome": None, "pages": len(plan[number]), "has_key": truth.exists()}  # fmt: skip
            gate[part].pop("split", None)
            made += 1
    (work_dir / "gate.json").write_text(json.dumps(gate, indent=2))
    print(f"{made} more invoices found inside files; read them with real_invoice_report --extract")


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
        if truth.get("_reviewed") is False:
            continue  # no key came with it: that's the reader's own answer, saved as a template
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
        swapped: list[int] = []
        for n in sorted(both):
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
            # The misreading arithmetic can't catch: the two numbers that
            # multiply, each in the other's place.
            if (
                not _same_money(t.get("quantity"), t.get("unit_price"))
                and _same_money(g.get("quantity"), t.get("unit_price"))
                and _same_money(g.get("unit_price"), t.get("quantity"))
            ):
                swapped.append(n)
        row["fields"] = fields
        row["swapped_lines"] = swapped
        row["wrong_money_lines"] = wrong_money_lines
        # Wrong in a way that matters to the numbers: a total, a price or
        # quantity, or a missing or extra line.
        # The same rows in another order (pages scanned out of order, read
        # in the order they came) are not a misreading.
        def rows(lines) -> list:
            # With the item each row is for: two items' numbers exchanged
            # are a misreading, however well the totals still add up.
            return sorted(
                (_norm(li.get("raw_description")), *(str(_num(li.get(f))) for f in _MONEY_FIELDS)) for li in lines
            )

        row["same_rows_any_order"] = rows(truth["line_items"]) == rows(got["line_items"])
        row["money_mistake"] = (
            not row["header_money_right"]
            or (wrong_money_lines > 0 and not row["same_rows_any_order"])
            or len(t_lines) != len(g_lines)
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


def _plan(set_dir: Path, name: str) -> list[dict]:
    """A plan file's rows; none when the set doesn't have it (not every
    set includes every part)."""
    path = set_dir / name
    return list(csv.DictReader(path.open())) if path.exists() else []


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
            # Fine only if it was settled as a repeat of one confirmed just
            # before; any other conflict is a failure to hear about.
            db.expire_all()
            bind_tenant(db, tenant_id)
            if db.get(InvoiceLineItem, line_id).review_status != ReviewStatus.pending:
                continue
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
# The domain the sets' forwarding addresses are at.
INBOX_DOMAIN = "invoices.example.com"


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
    from app.ingest.email_stub import ingest_email_bytes, parse_email
    from app.main import app
    from app.models import Account, AuditEvent, CanonicalSku, Distributor, Invoice, InvoiceLineItem, PriceAlert
    from app.models.enums import AlertStatus
    from app.storage import read_uri
    from app.workers import tasks

    cleanup()
    gate = json.loads((work_dir / "gate.json").read_text())
    manifest = list(csv.DictReader((set_dir / "manifest.csv").open()))
    replay = _Replay()
    tasks.extractor = replay
    queue_module.invoice_queue.enqueue = lambda *a, **k: None  # read here, not by a worker

    # Byte-identical documents share a hash, so each hash keeps every name.
    by_hash: dict[str, list[str]] = defaultdict(list)
    for pdf in sorted(work_dir.glob("*.pdf")):
        if (work_dir / f"{pdf.stem}.extracted.json").exists():
            by_hash[file_hash(pdf.read_bytes())].append(pdf.stem)

    db = SessionLocal()
    reviewer = _reviewer(db)
    app.dependency_overrides[current_user] = lambda: reviewer
    client = TestClient(app, headers={CSRF_HEADER: CSRF_HEADER_VALUE})
    customers = sorted(
        {e["customer"] for e in gate.values() if e.get("customer")}
        | {e["upload_to"] for e in gate.values() if e.get("upload_to")}
    )
    # The six compared with each other (set I) share a city; every other
    # restaurant is alone in its own, so it can't pass for a local business.
    compared = {r["customer"] for r in _plan(set_dir, "peers_plan.csv")}
    tenants = {
        c: _location(db, f"{TEST_LOCATION_PREFIX}{c}", TEST_METRO if c in compared else f"{c} (test set)") for c in customers
    }
    series_customer = next((r["customer"] for r in manifest if r["set"] == "H"), None)
    packs_customer = next((r["customer"] for r in manifest if r["set"] == "K"), None)
    changes_customer = next((r["customer"] for r in manifest if r["set"] == "O"), None)
    year_customer = next((r["customer"] for r in manifest if r["set"] == "U"), None)
    # Set U arrives in the order its plan says (a year of paperwork out of
    # a box), every other document in order of its name.
    upload_order = {
        Path(r["invoice_file"]).stem: int(r["upload_order"])
        for r in _plan(set_dir, "year_plan.csv")
        if (r.get("upload_order") or "").strip().isdigit()  # anything else is reported by check_set at prepare
    }
    # Set V: what each line was matched to before a person touched it.
    as_matched: dict[str, list[dict]] = {}
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
    # Each emailed-to restaurant gets the address its emails are sent to:
    # the forwarding address, wherever in the headers it is (an auto-forward
    # leaves the owner's own address in To). An email to two restaurants'
    # addresses gives the second to the restaurant in upload_to.
    for entry in gate.values():
        if entry.get("kind") == "email" and (entry.get("expected_outcome") != "Rejected" or entry.get("upload_to")):
            to = list(dict.fromkeys(parse_email((set_dir / entry["files"][0]).read_bytes()).recipients))
            to = [address for address in to if address.endswith(f"@{INBOX_DOMAIN}")]
            if to and tenants[entry["customer"]].inbox_address is None:
                tenants[entry["customer"]].inbox_address = to[0]
            if len(to) > 1 and entry.get("upload_to") and tenants[entry["upload_to"]].inbox_address is None:
                tenants[entry["upload_to"]].inbox_address = to[1]
    db.commit()
    tenant_ids = {c: t.id for c, t in tenants.items()}

    packs_plan = {
        (Path(r["invoice_file"]).stem, r["item_code"]): r["real_pack_size"]
        for r in _plan(set_dir, "packs_plan.csv")
    }
    packs_entered: set[str] = set()
    dismissed: list[dict] = []
    changes_path = set_dir / "changes_plan.csv"
    two_stage_codes = (
        {r["item_code"] for r in csv.DictReader(changes_path.open()) if r["behavior"] == "two_stage"} if changes_path.exists() else set()
    )
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
        result["printed_customer"] = invoice.printed_customer
        # Other invoices the app took out of this one's file: each is read
        # with the extraction of the part with the same pages.
        parts = []
        for other in db.scalars(select(Invoice).where(Invoice.split_from_id == invoice_id)):
            details = db.scalar(
                select(AuditEvent.details).where(AuditEvent.action == "invoice.split_out", AuditEvent.entity_id == other.id)
            )
            pages = list((details or {}).get("pages") or [])
            part = next(
                (k for k, e in gate.items() if e.get("part_of") == name and [n + 1 for n in e["pages_of_file"]] == pages),
                None,
            )
            parts.append((part or "", other.id))
        for part, other_id in sorted(parts):
            if part and (work_dir / f"{part}.extracted.json").exists():
                read(part, other_id, tenant_id)
            else:
                results.setdefault(f"{name} (an unread part)", {"kind": "part", "part_of": name, "app_outcome": "not read"})
        if parts:
            result["parts"] = [part for part, _ in sorted(parts)]
            db.expire_all()
            bind_tenant(db, tenant_id)
            invoice = db.get(Invoice, invoice_id)
        if invoice.is_held:
            result["held_as"] = (
                "copy" if invoice.duplicate_of_id else invoice.document_type or f"billed to {invoice.printed_customer}"
            )
            # What a person does with a held one: delete it; or, where the
            # set says it is a real invoice of its own held as a copy (a
            # credit memo carrying its invoice's number), say so.
            if invoice.duplicate_of_id and not invoice.document_type and result.get("expected_outcome") == "Ready":
                client.post(f"/invoices/{invoice_id}/keep", params={"tenant_id": str(tenant_id)})
                result["kept"] = True
            else:
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
        if result.get("set") == "V":
            # Matching traps: the app's own decision is what's judged, so no
            # one accepts its suggestions here.
            db.expire_all()
            bind_tenant(db, tenant_id)
            products = dict(db.execute(select(CanonicalSku.id, CanonicalSku.name)).all())
            as_matched[name] = [
                {
                    "item_code": (line.raw_sku or "").strip(),
                    "description": line.raw_description,
                    "status": line.review_status.value,
                    "product": products.get(line.canonical_sku_id),
                    "confidence": str(line.match_confidence) if line.match_confidence is not None else None,
                    "pack": line.raw_pack_size,
                    "uom": line.uom,
                    "price_per_unit": str(line.normalized_unit_price) if line.normalized_unit_price is not None else None,
                    "unit": line.base_uom.value if line.base_uom else None,
                }
                for line in db.scalars(
                    select(InvoiceLineItem).where(InvoiceLineItem.invoice_id == invoice_id).order_by(InvoiceLineItem.line_number)
                )
            ]
            return result
        result["accepted"] = _accept_suggestions(client, db, tenant_id, invoice_id)
        if result.get("set") == "O" and not any(d["dismissed"] for d in dismissed):
            # Set O: the person deals with the two-stage riser's alert the
            # first time it's open; it should come back when the price climbs
            # again.
            db.expire_all()
            bind_tenant(db, tenant_id)
            two_stage = set(
                db.scalars(
                    select(InvoiceLineItem.canonical_sku_id).where(
                        InvoiceLineItem.tenant_id == tenant_id, InvoiceLineItem.raw_sku.in_(two_stage_codes)
                    )
                )
            )
            for alert in db.scalars(select(PriceAlert).where(PriceAlert.tenant_id == tenant_id, PriceAlert.status == AlertStatus.open)):
                if alert.canonical_sku_id in two_stage:
                    resp = client.post(f"/insights/{alert.id}/dismiss", params={"tenant_id": str(tenant_id)})
                    dismissed.append(
                        {"sku": str(alert.canonical_sku_id), "at": str(alert.current_price), "after": name, "dismissed": resp.status_code == 204}
                    )
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

    def arrival(item) -> tuple:
        name, entry = item
        return (entry.get("set") or name[:1], upload_order.get(name, 0), name)

    for name, entry in sorted(gate.items(), key=arrival):
        if entry.get("kind") in ("attachment", "part"):
            continue  # read when its email, or the file it's in, arrives
        if entry.get("outcome"):
            results[name] = {**entry, "app_outcome": entry["outcome"]}
            continue
        # Where it's sent, which is who it's billed to unless the set says
        # otherwise; a document billed to nobody (a blank page) goes to the first.
        tenant_id = tenant_ids[entry.get("upload_to") or entry.get("customer") or customers[0]]
        if entry.get("kind") == "email":
            raw = (set_dir / entry["files"][0]).read_bytes()
            ingest = ingest_email_bytes(db, raw, name)
            tenant_id = ingest.tenant_id or tenant_id
            result = results.setdefault(name, {**entry})
            result["sent_to"] = next((c for c, t in tenant_ids.items() if t == ingest.tenant_id), None)
            if ingest.status == "quarantined":
                result["app_outcome"] = "Rejected"
                result["reason"] = ingest.reason
                continue
            if ingest.status == "duplicate":
                result["app_outcome"] = "Refused as a copy"
                continue
            outcomes = []
            attachments = [k for k, e in gate.items() if e.get("email") == name and e.get("kind") == "attachment"]
            for invoice_id in ingest.invoice_ids:
                bind_tenant(db, tenant_id)
                stored = db.get(Invoice, invoice_id)
                same_file = by_hash.get(file_hash(read_uri(stored.original_file_uri)), [])
                # This email's own attachment first, when the same file also
                # exists under another name in the set.
                att = next((n for n in same_file if n in attachments), same_file[0] if same_file else None)
                if att is None and len(attachments) == 1 and len(ingest.invoice_ids) == 1:
                    # Photos become a PDF afresh each time (not byte-identical);
                    # one invoice from one attachment is that attachment.
                    att = attachments[0]
                if att is None or not (work_dir / f"{att}.extracted.json").exists():
                    outcomes.append("not read yet")
                    continue
                outcomes.append(read(att, invoice_id, tenant_id)["app_outcome"])
            result["app_outcome"] = (
                "Needs a look" if "Needs a look" in outcomes else outcomes[0] if len(set(outcomes)) == 1 else "/".join(outcomes)
            )
            result["each"] = outcomes
            result["invoices"] = len(ingest.invoice_ids)
            continue
        if not (work_dir / f"{name}.extracted.json").exists():
            results[name] = {**entry, "app_outcome": "not read yet"}
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
    if series_customer:
        report["series"] = _series_v2(db, tenant_ids[series_customer])
    if compared:
        report["peers"] = _peers_v2(db, set_dir, tenants, gate)
    if packs_customer:
        report["packs"] = _packs_v2(db, tenant_ids[packs_customer], packs_plan, invoice_names, len(packs_entered))
    if changes_customer:
        report["changes"] = _changes(db, _plan(set_dir, "changes_plan.csv"), tenant_ids[changes_customer], dismissed)
    if year_customer:
        report["year"] = _changes(db, _plan(set_dir, "year_plan.csv"), tenant_ids[year_customer], [])
        report["year"]["added_in_order"] = [n for n, _ in sorted(upload_order.items(), key=lambda kv: kv[1])][:5]
    if as_matched:
        report["matching"] = _matching(_plan(set_dir, "products_plan.csv"), as_matched)
    report["errors"] = {k: r["error"] for k, r in results.items() if r.get("error")}
    score_path = work_dir / "score.json"
    if score_path.exists():
        scores = json.loads(score_path.read_text())
        scores = scores.get("invoices", scores)
        report["misread"] = {
            k: results.get(k, {}).get("app_outcome")
            for k, v in scores.items()
            if isinstance(v, dict) and (v.get("money_mistake") or v.get("header_money_right") is False)
        }
        report["swapped"] = {
            k: {"lines": v["swapped_lines"], "outcome": results.get(k, {}).get("app_outcome")}
            for k, v in scores.items()
            if isinstance(v, dict) and v.get("swapped_lines")
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

    peer_customers = sorted({r["customer"] for r in _plan(set_dir, "peers_plan.csv")})
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


def _changes(db, plan_rows: list[dict], tenant_id, dismissed: list[dict]) -> dict:
    """A planned series (set O's changes_plan.csv, set U's year_plan.csv):
    for each planned behavior, whether each of its products has an open
    alert from its distributor at the end, against the plan's
    expected_alert. Also whether the alerts that are open are the ones the
    prices as they now stand call for, whatever order the invoices came in."""
    from sqlalchemy import select

    from app.analytics.price_creep import detect_price_creep
    from app.db import bind_tenant
    from app.models import CanonicalSku, Distributor, Invoice, InvoiceLineItem, PriceAlert

    # A behavior whose rows disagree on what's expected (the old item no, its
    # replacement yes) is graded as two, one for each answer.
    answers: dict[str, set] = defaultdict(set)
    for row in plan_rows:
        answers[row["behavior"]].add(row["expected_alert"])
    plan: dict[str, dict] = {}
    for row in plan_rows:
        key = row["behavior"] if len(answers[row["behavior"]]) == 1 else f"{row['behavior']} (expected {row['expected_alert']})"
        b = plan.setdefault(key, {"expected_alert": row["expected_alert"], "codes": set(), "descriptions": set()})
        b["codes"].add(row["item_code"])
        b["descriptions"].add(row["description"])
    bind_tenant(db, tenant_id)
    lines = db.execute(
        select(InvoiceLineItem, Invoice.distributor_id)
        .join(Invoice, Invoice.id == InvoiceLineItem.invoice_id)
        .where(InvoiceLineItem.tenant_id == tenant_id)
    ).all()
    alerts = list(db.scalars(select(PriceAlert).where(PriceAlert.tenant_id == tenant_id)))
    names = dict(db.execute(select(CanonicalSku.id, CanonicalSku.name)).all())
    distributors = dict(db.execute(select(Distributor.id, Distributor.name)).all())

    def of(series: set) -> list:
        # An alert from before alerts had a distributor covers the product.
        return [a for a in alerts if (a.canonical_sku_id, a.distributor_id) in series or (a.distributor_id is None and a.canonical_sku_id in {s for s, _ in series})]

    out: dict[str, dict] = {}
    special: set = set()
    for behavior, b in plan.items():
        if behavior == "stable":
            continue
        mine = [(li, d) for li, d in lines if (li.raw_sku or "").strip() in b["codes"]]
        series = {(li.canonical_sku_id, d) for li, d in mine if li.canonical_sku_id}
        special |= series
        theirs = of(series)
        alerting = {(a.canonical_sku_id, a.distributor_id) for a in theirs if a.status.value == "open"}
        expected = b["expected_alert"]
        out[behavior] = {
            "expected_alert": expected,
            "items": sorted(b["descriptions"]),
            "lines": len(mine),
            "priced": sum(1 for li, _ in mine if li.normalized_unit_price is not None),
            "unmatched_lines": sum(1 for li, _ in mine if li.canonical_sku_id is None),
            "products": sorted(f"{names[sku]} ({distributors.get(d)})" for sku, d in series),
            "products_alerting": len(alerting),
            # As planned: every product alerting where one is expected, none
            # where none is.
            "open_alert": bool(series) and len(alerting) == len(series) if expected == "yes" else bool(alerting),
            "alerts": [
                {"product": names[a.canonical_sku_id], "status": a.status.value, "pct": str(a.pct_change),
                 "from": str(a.baseline_price), "to": str(a.current_price)}
                for a in theirs
            ],  # fmt: skip
        }
    out["stable"] = {
        "expected_alert": "no",
        "alerts": [
            {"product": names[a.canonical_sku_id], "status": a.status.value, "pct": str(a.pct_change)}
            for a in alerts
            if (a.canonical_sku_id, a.distributor_id) not in special and a.status.value == "open"
        ],
    }
    out["open_after_week_10"] = [{**a, "product": names.get(uuid.UUID(a["sku"]))} for a in dismissed]
    # The same alerts as a fresh look at the prices gives: what order the
    # invoices arrived in, and what was open along the way, left no trace.
    fresh = {(f.canonical_sku_id, f.distributor_id) for f in detect_price_creep(db, tenant_id)}
    standing = {(a.canonical_sku_id, a.distributor_id) for a in alerts if a.status.value in ("open", "dismissed")}
    open_now = {(a.canonical_sku_id, a.distributor_id) for a in alerts if a.status.value == "open"}
    out["matches_a_fresh_look"] = open_now <= fresh <= standing
    return out


# Words that don't tell one product from another.
_PRODUCT_NOISE = {"and", "of", "the", "a", "with", "in", "for", "fresh", "pack", "case"}


def _product_words(name: str) -> set[str]:
    words = re.findall(r"[a-z]+|\d+(?:\.\d+)?%?", (name or "").lower())
    return {w[:-1] if len(w) > 3 and w.endswith("s") and not w.endswith("ss") else w for w in words} - _PRODUCT_NOISE


def same_product(matched: str | None, true: str) -> bool:
    """Whether the catalog product a line was matched to is the product the
    plan says it is: one name's words all in the other's. "Chicken Breast
    Boneless Skinless" is "chicken breast"; it is not "chicken thigh
    boneless skinless", though they share three words of four."""
    a, b = _product_words(matched or ""), _product_words(true)
    return bool(a) and bool(b) and (a <= b or b <= a)


def _matching(plan_rows: list[dict], as_matched: dict[str, list[dict]]) -> dict:
    """Set V against products_plan.csv: each line matched to its true
    product, left for a person, or matched to something else. A wrong match
    made automatically is the failure; a wrong suggestion waits for a person
    who may catch it. `true_product` none means it's in no catalog."""
    # A row is found by its code and description together; by either alone
    # only when that is unambiguous on its invoice (one code on two rows, a
    # contract price and a market price, mustn't be judged as each other).
    exact: dict[tuple, dict] = {}
    by_code: dict[tuple, list] = defaultdict(list)
    by_description: dict[tuple, list] = defaultdict(list)
    for row in plan_rows:
        name = Path(row["invoice_file"]).stem
        code, description = (row.get("item_code") or "").strip(), _norm(row.get("description"))
        exact[(name, code, description)] = row
        if code:
            by_code[(name, code)].append(row)
        by_description[(name, description)].append(row)

    def planned(name: str, line: dict) -> dict | None:
        code, description = line["item_code"], _norm(line["description"])
        if (name, code, description) in exact:
            return exact[(name, code, description)]
        for candidates in (by_code.get((name, code), []) if code else [], by_description.get((name, description), [])):
            if len(candidates) == 1:
                return candidates[0]
        return None

    out = {"lines": 0, "right": 0, "left_for_a_person": 0, "wrong_automatic": [], "wrong_suggested": [], "not_in_plan": 0,
           "unclear_pack_priced": [], "packs": []}  # fmt: skip
    for name, lines in sorted(as_matched.items()):
        for line in lines:
            row = planned(name, line)
            if row is None:
                out["not_in_plan"] += 1
                continue
            out["lines"] += 1
            true = (row.get("true_product") or "").strip()
            said = f"{name}: {line['description']!r} is {true or 'none'}, matched to {line['product']!r} ({line['status']}, {line['confidence']})"
            if line["status"] == "not_product":
                out["right" if true.lower() in ("", "none") else "left_for_a_person"] += 1
            elif line["product"] is None:
                out["left_for_a_person"] += 1
            elif true.lower() not in ("", "none") and same_product(line["product"], true):
                out["right"] += 1
            else:
                out["wrong_automatic" if line["status"] == "auto" else "wrong_suggested"].append(said)
            true_pack = (row.get("true_pack") or "").strip()
            if true_pack:
                out["packs"].append(
                    {"file": name, "pack": line["pack"], "uom": line["uom"], "true_pack": true_pack,
                     "priced": f"{line['price_per_unit']}/{line['unit']}" if line["price_per_unit"] else None}
                )  # fmt: skip
                if true_pack.lower() == "unclear" and line["price_per_unit"]:
                    out["unclear_pack_priced"].append(f"{name}: {line['description']!r} [{line['pack']}] priced {line['price_per_unit']}/{line['unit']}")
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

    graded = {k: r for k, r in results.items() if r.get("kind") not in ("attachment", "part")}
    for certainty in ("known", "find_out"):
        group = {k: r for k, r in graded.items() if (r.get("certainty") or "known") == certainty}
        if not group:
            continue
        label = "documents with a known expected outcome" if certainty == "known" else "find-out documents (the expectation was a guess)"
        print(f"\n{sum(agrees(r) for r in group.values())}/{len(group)} {label} came out as expected")
        for k, r in sorted(group.items()):
            if not agrees(r):
                extra = f" held as {r['held_as']}" if r.get("held_as") else ""
                print(f"  {k}: expected {r.get('expected_outcome')}, got {r.get('app_outcome')}{extra}  {r.get('reason') or ''}"[:220])
    split_files = {k: r["parts"] for k, r in results.items() if r.get("parts")}
    print(f"\nfiles the app separated into several invoices: {len(split_files)}")
    for k, parts in sorted(split_files.items()):
        print(f"  {k}: {results[k].get('app_outcome')} + " + ", ".join(f"{p or 'unread part'}: {results.get(p, {}).get('app_outcome')}" for p in parts))
    print(f"held, then deleted: { {k: r['held_as'] for k, r in results.items() if r.get('held_as')} }")
    print(f"local distributors added: { {k: r['distributor_added'] for k, r in results.items() if r.get('distributor_added')} }")
    print(
        "recognized by name: "
        f"{ {k: r['distributor_name'] for k, r in results.items() if r.get('distributor') == 'other' and r.get('distributor_name') not in (None, 'Other') and not r.get('distributor_added') and not r.get('held_as')} }"
    )
    print(f"fee lines kept off Match items: {sum(r.get('fees', 0) for r in results.values())}")
    if "series" in report:
        print(f"\nSet H alerts ({len(report['series']['alerts'])}):")
    for a in report.get("series", {}).get("alerts", []):
        print(f"  {a['product']} from {a['distributor']}: {a['from']} -> {a['to']} ({float(a['pct_change']):+.1%})")
    if "peers" in report:
        print("\nSet I, what each restaurant is shown:")
    for customer, rows in report.get("peers", {}).items():
        scopes = Counter(r["scope"] for r in rows)
        high = [r["product"] for r in rows if r["percentile"] and float(r["percentile"]) >= 0.9 and r["scope"] == "metro"]
        print(f"  {customer}: {dict(scopes)}; paying the most nearby for: {high}")
    if "misread" in report:
        print(f"\nDocuments read with a wrong amount, and what the app did: {report['misread']}")
    if report.get("errors"):
        print(f"\nReading these raised an error (the invoice is marked Couldn't read): {report['errors']}")
    wrong_and_ready = {k: v for k, v in report.get("misread", {}).items() if v == "Ready"}
    print(f"\nRead with a wrong amount and still Ready (the failure that matters): {wrong_and_ready or 'none'}")
    if report.get("swapped"):
        print(f"Quantity and price exchanged on a line: {report['swapped']}")
    for key, title in (("changes", "Set O"), ("year", "Set U, a year added in random order")):
        if key not in report:
            continue
        print(f"\n{title}: an open alert at the end, by planned behavior:")
        for behavior, c in report[key].items():
            if not isinstance(c, dict) or behavior == "stable":
                continue
            got = "yes" if c["open_alert"] else "no"
            mark = "ok " if got == c["expected_alert"] else "DIFF"
            detail = "; ".join(f"{a['product']} {float(a['pct']):+.1%} ({a['status']})" for a in c["alerts"])
            unmatched = f"; {c['unmatched_lines']} lines unmatched" if c["unmatched_lines"] else ""
            print(
                f"  {mark} {behavior}: expected {c['expected_alert']}, got {got} ({c['products_alerting']} of {len(c['products'])} "
                f"products alerting); {c['priced']}/{c['lines']} lines priced{unmatched}; {c['products']}; {detail}"
            )
        print(f"  alerts on steady items: {report[key]['stable']['alerts']}")
        if report[key]["open_after_week_10"]:
            print(f"  dismissed along the way: {[(a['product'], a['after'], a['at']) for a in report[key]['open_after_week_10']]}")
        print(f"  open alerts are what a fresh look at the prices gives: {'yes' if report[key]['matches_a_fresh_look'] else 'NO'}")
    if "matching" in report:
        m = report["matching"]
        print(
            f"\nSet V, matching traps: {m['lines']} planned lines; {m['right']} matched to the true product, "
            f"{m['left_for_a_person']} left for a person, {len(m['wrong_suggested'])} wrong suggestions, "
            f"{len(m['wrong_automatic'])} WRONG AUTOMATIC MATCHES ({m['not_in_plan']} lines not in the plan)"
        )
        for line in m["wrong_automatic"]:
            print(f"  wrong, automatic: {line}")
        for line in m["wrong_suggested"][:25]:
            print(f"  wrong suggestion: {line}")
        for line in m["unclear_pack_priced"]:
            print(f"  priced though the pack is unclear: {line}")
        if m["packs"]:
            print("  packs as read (pack, unit -> price per unit | what it truly is):")
            for pk in m["packs"]:
                print(f"    {str(pk['pack']):16} {str(pk['uom']):4} -> {str(pk['priced']):18} | {pk['true_pack']}")
    if "packs" not in report:
        return
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
    sp = sub.add_parser("split", help="after the first read: write out the other invoices found inside files")
    sp.add_argument("work_dir", type=Path)
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
    elif args.command == "split":
        split(args.work_dir)
    else:
        cleanup()


if __name__ == "__main__":
    main()
