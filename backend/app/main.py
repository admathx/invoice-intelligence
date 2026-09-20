from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from app.api import accounts, insights, invoices, negotiation, review, skus, tenants
from app.config import settings

app = FastAPI(title="Invoice Intelligence")

app.add_middleware(
    CORSMiddleware,
    # Regex (not a fixed port) because the frontend dev port varies by machine
    # (see frontend/.claude/launch.json). Dev-only; v0 has no auth in front of this.
    allow_origin_regex=r"http://localhost:\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)

# SPEC.md §9: Invoices page shows the page image side-by-side with extracted
# lines. Renders live on disk at <upload_dir>/renders/<invoice_id>/page_NNN.png
# (app/ingest/render.py) — served directly rather than proxied through an
# endpoint, since they're static once extraction finishes and tenant scoping
# doesn't apply to raw file bytes any differently than a signed-URL proxy
# would provide in this no-auth v0.
renders_dir = Path(settings.upload_dir) / "renders"
renders_dir.mkdir(parents=True, exist_ok=True)
app.mount("/renders", StaticFiles(directory=renders_dir), name="renders")

app.include_router(invoices.router)
app.include_router(skus.router)
app.include_router(review.router)
app.include_router(insights.router)
app.include_router(negotiation.router)
app.include_router(accounts.router)
app.include_router(tenants.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
