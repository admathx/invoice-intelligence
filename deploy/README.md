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
  run --rm api python scripts/manage_users.py create you@yourcompany.com "Your Name" --operator
```

It prompts for the password. Then sign in at `https://DOMAIN`.

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

## 7. Storage

`STORAGE_BACKEND=local` keeps PDFs and page images in the `uploads` volume.
Set `STORAGE_BACKEND=s3` with `S3_BUCKET` (and `S3_ENDPOINT_URL` for R2 or
MinIO) to use a bucket; credentials come from the standard AWS variables or
an instance role. Invoices stored before a switch keep opening from where
they were written, as long as that location is still reachable.

## 8. Backups

The database is the thing to back up; everything else can be rebuilt.

```
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.production \
  exec postgres pg_dump -U invoice -Fc invoice_intelligence > backup-$(date +%F).dump
```

With local storage, also back up the `uploads` volume (original PDFs).

## 9. Updating

```
git pull
docker compose -f deploy/docker-compose.prod.yml --env-file deploy/.env.production up -d --build
```

Migrations run automatically before the new API starts.
