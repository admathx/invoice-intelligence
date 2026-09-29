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
    # How long one invoice's extraction job may run before the worker gives
    # up on it. RQ's own default is 180 s, and a large invoice streams up to
    # 64k output tokens, possibly twice (the retry): minutes, not seconds.
    extraction_job_timeout_seconds: int = 1800

    # --- login (app/auth.py) ---
    # The browser origins allowed to make credentialed (cookie-carrying)
    # requests. Explicit, not a pattern: with credentials allowed, any origin
    # matched here can act as a signed-in user, and "any localhost port" would
    # include whatever else happens to be running on the machine.
    frontend_origins: list[str] = ["http://localhost:3000", "http://localhost:3001"]
    # A session ends after this long unused; every use pushes it back
    # (app.auth.user_for_token), so someone working daily isn't signed out
    # mid-task. session_max_age_days caps it regardless, so a stolen cookie
    # can't be kept alive forever by using it.
    session_ttl_days: int = 14
    session_max_age_days: int = 90
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

    # --- outgoing mail and the weekly digest (app/mail.py, app/digest.py) ---
    # Where links in emails point: the dashboard's public address.
    public_base_url: str = "http://localhost:3000"
    # Signs unsubscribe links, so one can't be forged for someone else.
    # Development falls back to a fixed value; production must set its own.
    secret_key: str = ""
    # "outbox" writes each message as an .eml file into outbox_dir (open it in
    # any mail client); "smtp" sends it. Postmark, SendGrid and Mailgun all
    # accept SMTP, with the credentials their dashboards give you.
    mail_backend: Literal["outbox", "smtp"] = "outbox"
    outbox_dir: str = str(BACKEND_DIR / "outbox")
    mail_from: str = "Invoice Intelligence <digest@invoices.example.com>"
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_starttls: bool = True
    digests_enabled: bool = True
    # When the weekly digest goes out (scripts/digest_scheduler.py): Monday,
    # 12:00 UTC, which is morning across the US.
    digest_weekday: int = 0
    digest_hour_utc: int = 12
    # Price-increase emails (app/alert_emails.py): sent within minutes of a
    # price alert opening, for increases of at least this much. Smaller ones
    # wait for the weekly digest.
    alert_emails_enabled: bool = True
    alert_email_min_pct_change: float = 0.10
    # Where problems are emailed (app/ops.py): errors, extraction failing,
    # backups going stale. Empty: logged only.
    ops_email: str = ""
    # Where the backup service writes database dumps (deploy/backup.sh), for
    # the scheduler to check they keep coming and, with S3 storage, to copy
    # them off the server (app/backups.py). Empty: not checked.
    backup_dir: str = ""
    backup_max_age_hours: int = 26
    # "Forgot your password?" (app/api/auth.py): how long an emailed link
    # works, and how many one address can be sent per hour.
    password_reset_enabled: bool = True
    password_reset_ttl_minutes: int = 60
    password_reset_max_per_hour: int = 3

    @property
    def signing_key(self) -> bytes:
        return (self.secret_key or "development-only-signing-key").encode()

    @model_validator(mode="after")
    def _production_safety(self) -> "Settings":
        if self.app_env == "production":
            problems = []
            if not self.session_cookie_secure:
                problems.append("SESSION_COOKIE_SECURE must be true (session cookies would travel over plain http)")
            if any(origin.startswith("http://") for origin in self.frontend_origins):
                problems.append("FRONTEND_ORIGINS must all be https")
            if len(self.secret_key) < 32:
                problems.append("SECRET_KEY must be set (32+ characters): it signs unsubscribe links")
            if not self.public_base_url.startswith("https://"):
                problems.append("PUBLIC_BASE_URL must be https (links in emails point there)")
            if self.mail_backend != "smtp":
                # Outbox mail in production is mail written to the server's
                # disk and never delivered, with nothing to say so.
                needing = [
                    name
                    for name, on in (
                        ("DIGESTS_ENABLED", self.digests_enabled),
                        ("ALERT_EMAILS_ENABLED", self.alert_emails_enabled),
                        ("PASSWORD_RESET_ENABLED", self.password_reset_enabled),
                    )
                    if on
                ]
                if needing:
                    problems.append(
                        "sending email needs MAIL_BACKEND=smtp (or set " + ", ".join(f"{n}=false" for n in needing) + ")"
                    )
            if problems:
                raise ValueError("refusing to start with APP_ENV=production: " + "; ".join(problems))
        return self


settings = Settings()
