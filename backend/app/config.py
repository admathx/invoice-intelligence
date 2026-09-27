from pathlib import Path
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Anchored to backend/ regardless of the process's cwd. `make up` starts the API
# and worker from the repo root; pytest and manual runs use backend/ as cwd —
# a relative ".env"/"./uploads" resolved to two different locations depending
# on which, so backend/.env was silently never being read by `make up`'s
# processes (ANTHROPIC_API_KEY, if only set there, was invisible to them; other
# settings happened to have defaults matching docker-compose's values, so
# nothing looked wrong until API key selection exposed it).
BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=str(BACKEND_DIR / ".env"), extra="ignore")

    # "production" turns the settings that are only safe on a developer's
    # machine into startup errors (see _production_safety below), so a
    # deployment can't quietly run with them.
    app_env: Literal["development", "production"] = "development"

    database_url: str = "postgresql+psycopg://invoice:invoice@localhost:5432/invoice_intelligence"
    redis_url: str = "redis://localhost:6379/0"
    anthropic_api_key: str = ""

    # --- file storage (app/storage.py) ---
    # "local" keeps files under upload_dir; "s3" puts them in a bucket, which
    # any deployment where the API and worker don't share a disk needs.
    storage_backend: Literal["local", "s3"] = "local"
    upload_dir: str = str(BACKEND_DIR / "uploads")
    s3_bucket: str = ""
    # Optional key prefix inside the bucket, to share one bucket between
    # environments ("staging/", "prod/").
    s3_prefix: str = ""
    # For S3-compatible services (Cloudflare R2, MinIO); empty means AWS.
    s3_endpoint_url: str = ""
    s3_region: str = ""
    inbox_dir: str = str(BACKEND_DIR.parent / "inbox")
    # A multi-page scanned invoice is a few MB; 25 MB is generous headroom
    # while still refusing to read a runaway file into API memory.
    max_upload_bytes: int = 25 * 1024 * 1024
    # Domain half of a tenant's forwarding address (see
    # app/ingest/email_stub.py's inbox_address_for). Configurable because the
    # real deployment's inbound domain won't be this placeholder.
    inbox_domain: str = "invoices.example.com"
    # Shared secret the mail provider presents when posting to
    # /inbound/email (app/api/inbound.py). Empty disables the endpoint.
    inbound_email_secret: str = ""
    # Several PDFs per email is normal; a whole mailbox in one is not.
    max_inbound_email_bytes: int = 60 * 1024 * 1024

    extraction_model: str = "claude-sonnet-4-6"

    # --- login (app/auth.py) ---
    # The browser origins allowed to make credentialed (cookie-carrying)
    # requests. Explicit, not a pattern: with credentials allowed, any origin
    # matched here can act as a signed-in user, and "any localhost port" would
    # include whatever else happens to be running on the machine.
    frontend_origins: list[str] = ["http://localhost:3000", "http://localhost:3001"]
    session_ttl_days: int = 14
    # Secure cookies aren't sent over plain http, which is what local dev
    # runs on. Must be True anywhere served over https.
    session_cookie_secure: bool = False
    # Failed sign-ins allowed per email address inside the window before
    # further attempts are refused, whatever the password.
    login_max_failures: int = 10
    login_failure_window_minutes: int = 15
    # Expired or revoked sessions are deleted this long after they stop
    # working (app.auth.prune_sessions). Kept a while rather than at once so a
    # "why was I signed out?" question can still be answered from the table.
    session_retention_days: int = 30

    @model_validator(mode="after")
    def _production_safety(self) -> "Settings":
        if self.app_env == "production":
            problems = []
            if not self.session_cookie_secure:
                problems.append("SESSION_COOKIE_SECURE must be true (session cookies would travel over plain http)")
            if any(origin.startswith("http://") for origin in self.frontend_origins):
                problems.append("FRONTEND_ORIGINS must all be https")
            if problems:
                raise ValueError("refusing to start with APP_ENV=production: " + "; ".join(problems))
        return self


settings = Settings()
