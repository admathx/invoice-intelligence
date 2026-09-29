"""Where invoice files live: the original PDFs and their rendered pages.

Two backends behind one interface, chosen by settings.storage_backend:

- local: a directory on disk (settings.upload_dir). Development, and a
  single-server deployment with a persistent volume.
- s3: any S3-compatible bucket (AWS S3, Cloudflare R2, MinIO, ...). Needed as
  soon as the API and the worker run on different machines, since each only
  sees its own disk.

Files are addressed by key ("originals/<invoice>.pdf",
"renders/<invoice>/page_001.png"). What gets stored on the invoice row is a
URI ("file:///..." or "s3://bucket/key"), and read_uri dispatches on its
scheme rather than on the current backend, so invoices recorded before a
switch still open.
"""
import functools
import mimetypes
from pathlib import Path
from typing import Protocol
from urllib.parse import unquote, urlparse

from app.config import settings


class StorageError(RuntimeError):
    pass


class Storage(Protocol):
    def put(self, key: str, data: bytes) -> str:
        """Store data at key, replacing anything there. Returns its URI."""

    def put_file(self, key: str, path: Path) -> str:
        """Store a file at key without reading it all into memory (database
        backups run to gigabytes). Returns its URI."""

    def get(self, key: str) -> bytes | None:
        """The bytes at key, or None if there are none."""

    def list(self, prefix: str) -> list[str]:
        """Keys under prefix, sorted."""

    def delete_prefix(self, prefix: str) -> None:
        """Remove every key under prefix (a retried extraction's old renders)."""

    def delete(self, key: str) -> None:
        """Remove one key; nothing if it isn't there."""


def _check_key(key: str) -> str:
    # Keys are built by this codebase, but a key that climbs out of its root
    # is never legitimate on either backend, so it's refused outright.
    if key.startswith("/") or ".." in key.split("/"):
        raise StorageError(f"invalid storage key: {key!r}")
    return key


class LocalStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def _path(self, key: str) -> Path:
        return self.root / _check_key(key)

    def put(self, key: str, data: bytes) -> str:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Written aside and renamed, so a reader never sees half a file.
        partial = path.with_name(path.name + ".partial")
        partial.write_bytes(data)
        partial.replace(path)
        return path.as_uri()

    def put_file(self, key: str, source: Path) -> str:
        import shutil

        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_name(path.name + ".partial")
        shutil.copyfile(source, partial)
        partial.replace(path)
        return path.as_uri()

    def get(self, key: str) -> bytes | None:
        path = self._path(key)
        return path.read_bytes() if path.is_file() else None

    def list(self, prefix: str) -> list[str]:
        base = self._path(prefix.rstrip("/"))
        if not base.is_dir():
            return []
        return sorted(
            str(p.relative_to(self.root)) for p in base.rglob("*") if p.is_file() and not p.name.endswith(".partial")
        )

    def delete_prefix(self, prefix: str) -> None:
        for key in self.list(prefix):
            self._path(key).unlink(missing_ok=True)

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)


class S3Storage:
    def __init__(self, bucket: str, prefix: str = "", endpoint_url: str | None = None, region: str | None = None):
        import boto3  # only needed when this backend is configured

        if not bucket:
            raise StorageError("STORAGE_BACKEND=s3 needs S3_BUCKET")
        self.bucket = bucket
        self.prefix = prefix.strip("/") + "/" if prefix.strip("/") else ""
        # Credentials come from the standard AWS chain (env vars, instance
        # role, ~/.aws), never from this app's own settings.
        self.client = boto3.client("s3", endpoint_url=endpoint_url or None, region_name=region or None)

    def _key(self, key: str) -> str:
        return self.prefix + _check_key(key)

    def put(self, key: str, data: bytes) -> str:
        content_type = mimetypes.guess_type(key)[0] or "application/octet-stream"
        self.client.put_object(Bucket=self.bucket, Key=self._key(key), Body=data, ContentType=content_type)
        return f"s3://{self.bucket}/{self._key(key)}"

    def put_file(self, key: str, source: Path) -> str:
        # Streamed, and split into parts past a few MB: no whole file in
        # memory, and no 5 GB single-upload ceiling.
        self.client.upload_file(str(source), self.bucket, self._key(key))
        return f"s3://{self.bucket}/{self._key(key)}"

    def get(self, key: str) -> bytes | None:
        try:
            return self.client.get_object(Bucket=self.bucket, Key=self._key(key))["Body"].read()
        except self.client.exceptions.NoSuchKey:
            return None

    def list(self, prefix: str) -> list[str]:
        keys = []
        paginator = self.client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=self._key(prefix)):
            keys.extend(obj["Key"][len(self.prefix) :] for obj in page.get("Contents", []))
        return sorted(keys)

    def delete_prefix(self, prefix: str) -> None:
        keys = self.list(prefix)
        for start in range(0, len(keys), 1000):  # the API's per-request cap
            batch = [{"Key": self._key(k)} for k in keys[start : start + 1000]]
            self.client.delete_objects(Bucket=self.bucket, Delete={"Objects": batch, "Quiet": True})

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=self._key(key))


@functools.lru_cache(maxsize=1)
def get_storage() -> Storage:
    if settings.storage_backend == "s3":
        return S3Storage(settings.s3_bucket, settings.s3_prefix, settings.s3_endpoint_url, settings.s3_region)
    return LocalStorage(settings.upload_dir)


def read_uri(uri: str) -> bytes:
    """The bytes an invoice row's original_file_uri points at, whichever
    backend wrote it."""
    parsed = urlparse(uri)
    if parsed.scheme == "file":
        # Decoded: LocalStorage.put writes Path.as_uri(), which percent-encodes
        # ("/My Projects" -> "/My%20Projects"). Reading the encoded form
        # literally meant any upload directory with a space in its path could
        # never read back its own originals.
        path = Path(unquote(parsed.path))
        if not path.is_file():
            raise StorageError(f"file not found: {uri}")
        return path.read_bytes()
    if parsed.scheme == "s3":
        storage = get_storage()
        if not isinstance(storage, S3Storage) or storage.bucket != parsed.netloc:
            raise StorageError(f"{uri} is in a bucket this deployment isn't configured for")
        data = storage.client.get_object(Bucket=parsed.netloc, Key=parsed.path.lstrip("/"))["Body"].read()
        return data
    raise StorageError(f"unsupported storage URI: {uri}")


# --- The keys this app uses ---------------------------------------------------


def original_key(invoice_id, suffix: str = ".pdf") -> str:
    return f"originals/{invoice_id}{suffix}"


def forget_original(invoice_id) -> None:
    """Remove a stored original whose invoice row never got committed, so a
    failed write doesn't leave a file nothing points to."""
    try:
        get_storage().delete(original_key(invoice_id, ".pdf"))
    except Exception:  # best effort: the caller is already handling a failure
        pass


def renders_prefix(invoice_id) -> str:
    return f"renders/{invoice_id}/"


def render_key(invoice_id, page_name: str) -> str:
    return f"{renders_prefix(invoice_id)}{page_name}"


def page_names(invoice_id) -> list[str]:
    """The invoice's rendered page files, in page order."""
    return [key.rsplit("/", 1)[1] for key in get_storage().list(renders_prefix(invoice_id)) if key.endswith(".png")]
