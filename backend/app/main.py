from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import invoices

app = FastAPI(title="Invoice Intelligence")

app.add_middleware(
    CORSMiddleware,
    # Regex (not a fixed port) because the frontend dev port varies by machine
    # (see frontend/.claude/launch.json). Dev-only; v0 has no auth in front of this.
    allow_origin_regex=r"http://localhost:\d+",
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(invoices.router)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}
