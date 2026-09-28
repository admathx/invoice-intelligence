# Deploying Invoice Intelligence

One host running Docker holds the whole service: Caddy (HTTPS, with
certificates it obtains and renews itself) in front of the Next.js dashboard,
which proxies `/api` to the FastAPI backend; the extraction worker; Postgres
(with pgvector); Redis. Only ports 80 and 443 are published.

```
browser / mail provider ──https──> caddy ──> frontend (Next.js) ──/api──> api (FastAPI)
                                                                    worker ──> Anthropic API
                                       postgres, redis, uploads volume (or an S3 bucket)
```

## 1. Before you start

- A server with Docker and the compose plugin (2 GB RAM is enough; the
  embedding model and two API workers use about 1 GB).
- A hostname for the dashboard (`DOMAIN`) with a DNS A/AAAA record pointing
  at the server, and ports 80 and 443 open (Caddy needs 80 to get the
  certificate).
- For email intake: a domain for forwarding addresses (`INBOX_DOMAIN`, e.g.
  `invoices.yourcompany.com`) whose MX records point at an inbound mail
  provider (Postmark, SendGrid, Mailgun).

## 2. Configure

```
cp deploy/.env.production.example deploy/.env.production
```

Fill in `DOMAIN`, a strong `POSTGRES_PASSWORD`, `ANTHROPIC_API_KEY`,
`INBOX_DOMAIN` and a long random `INBOUND_EMAIL_SECRET`. The file is
gitignored; keep it that way.

The compose file sets `APP_ENV=production`, under which the backend refuses
to start with insecure session cookies or a non-https origin.

## 3. Start

```
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.production up -d --build
```

The `migrate` service runs first on every start: database migrations, then
the canonical SKU catalog (both idempotent). The API and worker wait for it.

## 4. Create the first operator

There's no sign-up page. The first operator is made from the command line;
after that, operators add everyone else on the Users page.

```
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.production \
  run --rm api python scripts/manage_users.py create you@yourcompany.com "Your Name" --operator --permanent
```

It prompts for the password (`--permanent`: it's your own, so you aren't
asked to replace it at first sign-in, as someone given a password would be).
Then sign in at `https://DOMAIN`.

## 5. Onboard a restaurant

1. **Businesses → Add a location**: name, metro (pick an existing one where
   it fits; benchmarks compare within a metro), annual food spend. The page
   shows the location's forwarding address.
2. **Users → Add user**: give them that location. A temporary password is
   shown once; hand it over directly.
3. The restaurant forwards distributor invoices to the forwarding address,
   or uploads PDFs on the Invoices page.

## 6. Inbound email

Point the provider's inbound webhook at

```
https://inbound:<INBOUND_EMAIL_SECRET>@<DOMAIN>/api/inbound/email
```

and have it send the raw message:

| Provider | Setting |
|---|---|
| Postmark | Inbound webhook, "Include raw email content" on |
| SendGrid | Inbound Parse, "POST the raw, full MIME message" on |
| Mailgun | Route action `forward("https://...")` to a URL ending in `mime` |
| Anything else | POST the raw message with `Content-Type: message/rfc822` |

Emails that can't become invoices (unknown address, no PDF) are not dropped:
they're stored under `inbound/rejected/` and appear in the Audit log
("Rejected emails") with the reason.

## 7. Outgoing email

Three kinds, all through your provider's SMTP relay (`SMTP_*` and `MAIL_FROM`
in `.env.production`):

- **The weekly summary**, described below.
- **Price-increase emails**: within minutes of a price alert opening for an
  increase of 10% or more (`ALERT_EMAIL_MIN_PCT_CHANGE`, default `0.10`), to
  everyone at that location who wants them. Smaller increases wait for the
  weekly summary. The `scheduler` service sends them, checking every five
  minutes; each alert is emailed to each person once.
- **Password reset links** ("Forgot your password?" on the sign-in page):
  single use, valid for an hour, at most three per address per hour.

People turn the first two off separately, from their account page or the
unsubscribe link in each email.

### The weekly summary

Every Monday at 12:00 UTC the `scheduler` service emails each person a
summary of their locations' week: new price increases, invoices to check,
the review queue, what arrived, and the biggest savings on the negotiation
sheet. Nothing is sent to someone whose locations had a quiet week.

It sends through your provider's SMTP relay (`SMTP_*` and `MAIL_FROM` in
`.env.production`); set up SPF and DKIM for the `MAIL_FROM` domain as your
provider describes, or the digests will land in spam. People turn it off
from their account page or the unsubscribe link in any digest. To see what
someone would get, without sending it:

```
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.production \
  run --rm api python scripts/send_digests.py --preview someone@example.com
```

(It's written to the `outbox` folder inside the container.) No provider yet:
set `DIGESTS_ENABLED=false`, `ALERT_EMAILS_ENABLED=false` and
`PASSWORD_RESET_ENABLED=false` (operators then reset passwords from the Users
screen); the API refuses to start in production with any of them on and no
SMTP configured, rather than silently sending nothing.

## 8. Storage

`STORAGE_BACKEND=local` keeps PDFs and page images in the `uploads` volume.
Set `STORAGE_BACKEND=s3` with `S3_BUCKET` (and `S3_ENDPOINT_URL` for R2 or
MinIO) to use a bucket; credentials come from the standard AWS variables or
an instance role.

Switching an existing deployment from local to S3: set the new variables,
then move what's already stored (originals and rendered pages; re-runnable,
deletes nothing):

```
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.production \
  run --rm api python scripts/migrate_storage.py --dry-run
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.production \
  run --rm api python scripts/migrate_storage.py
```

Skipping it, invoices recorded earlier still open their original PDF, but
their page images disappear from the review screen.

## 9. Backups

The database is the thing to back up; everything else can be rebuilt.

```
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.production \
  exec -T postgres pg_dump -U invoice -Fc invoice_intelligence > backup-$(date +%F).dump
```

`-T` matters: without it compose attaches a terminal, which rewrites line
endings in the binary dump, and the file only turns out to be corrupt when
you try to restore it. Check a backup is readable with
`pg_restore --list backup-....dump > /dev/null`. To restore into a fresh
deployment (stop `api` and `worker` first):

```
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.production \
  exec -T postgres pg_restore -U invoice -d invoice_intelligence --clean --if-exists < backup-....dump
```

With local storage, also back up the `uploads` volume (original PDFs).

## 10. Updating

```
git pull
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.production up -d --build
```

Migrations run automatically before the new API starts.
