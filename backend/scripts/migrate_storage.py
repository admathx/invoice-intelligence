"""Move invoice files into the storage backend the deployment now uses.

Run once after changing STORAGE_BACKEND (typically local disk -> an S3
bucket). Originals would keep opening without it (each invoice row records
where its file was written), but rendered pages are looked up in the current
backend only, so every existing invoice would lose its page images on the
review screen, and the old disk could never be retired.

    python scripts/migrate_storage.py --source-dir /srv/backend/uploads [--dry-run]

For each invoice: copies its original into the current backend and repoints
the row at it, and copies its rendered pages. Safe to re-run: anything
already in the current backend is skipped. Nothing is deleted from the
source; remove it once you've checked the result.
"""
import argparse
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import TENANT_SCOPE_BYPASS, SessionLocal  # noqa: E402
from app.models import Invoice  # noqa: E402
from app.storage import (  # noqa: E402
    LocalStorage,
    S3Storage,
    Storage,
    StorageError,
    get_storage,
    original_key,
    read_uri,
    renders_prefix,
)


@dataclass
class MigrationReport:
    invoices: int = 0
    originals_copied: int = 0
    originals_already_there: int = 0
    originals_missing: int = 0
    pages_copied: int = 0


def _in_target(uri: str, target: Storage) -> bool:
    if isinstance(target, S3Storage):
        return uri.startswith(f"s3://{target.bucket}/")
    return uri.startswith(target.root.as_uri() + "/")


def migrate(
    db, source: LocalStorage, target: Storage, *, dry_run: bool = False, only: list | None = None
) -> MigrationReport:
    """`only` restricts it to those invoice ids (for trying it on a few first)."""
    report = MigrationReport()
    query = select(Invoice).order_by(Invoice.created_at).execution_options(**{TENANT_SCOPE_BYPASS: True})
    if only is not None:
        query = query.where(Invoice.id.in_(only))
    invoices = db.scalars(query)
    for invoice in invoices:
        report.invoices += 1
        if _in_target(invoice.original_file_uri, target):
            report.originals_already_there += 1
        else:
            try:
                data = read_uri(invoice.original_file_uri)
            except StorageError:
                # Placeholder rows (file:///dev/null) and files already gone:
                # nothing to move, and the row is left pointing where it did.
                report.originals_missing += 1
                data = None
            if data is not None:
                if not dry_run:
                    invoice.original_file_uri = target.put(original_key(invoice.id, ".pdf"), data)
                report.originals_copied += 1

        prefix = renders_prefix(invoice.id)
        present = set(target.list(prefix))
        for key in source.list(prefix):
            if key in present:
                continue
            if not dry_run:
                target.put(key, source.get(key))
            report.pages_copied += 1
        if not dry_run and report.invoices % 100 == 0:
            db.commit()  # keep what's done if a long run is interrupted
    if not dry_run:
        db.commit()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--source-dir", default=settings.upload_dir, help="the old local upload directory")
    parser.add_argument("--dry-run", action="store_true", help="count what would move, change nothing")
    args = parser.parse_args()

    target = get_storage()
    source = LocalStorage(args.source_dir)
    if isinstance(target, LocalStorage) and target.root == source.root:
        sys.exit("the source directory is the current storage; set STORAGE_BACKEND to where files should go")
    db = SessionLocal()
    try:
        report = migrate(db, source, target, dry_run=args.dry_run)
    finally:
        db.close()
    for name, value in asdict(report).items():
        print(f"{name.replace('_', ' '):<26} {value}")
    if args.dry_run:
        print("DRY RUN: nothing changed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
