"""Health checks for uptime monitors, and errors people hit in the dashboard."""
from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import ops
from app.auth import signed_in_user
from app.db import get_db
from app.models import User

router = APIRouter(tags=["ops"])


@router.get("/health/ready")
def ready(db: Session = Depends(get_db)) -> JSONResponse:
    """Whether the API can do its job, not just whether it's running: the
    database answers and the queue is reachable. For an uptime monitor
    (it's public, and says nothing but ok or which part isn't)."""
    problems = []
    try:
        db.execute(text("select 1"))
    except Exception:
        problems.append("database")
    try:
        from app.queue import redis_conn

        redis_conn.ping()
    except Exception:
        problems.append("queue")
    if problems:
        return JSONResponse({"status": "unavailable", "failing": problems}, status_code=503)
    return JSONResponse({"status": "ok"})


class ClientError(BaseModel):
    message: str = Field(max_length=2000)
    page: str = Field(max_length=500)
    digest: str | None = Field(default=None, max_length=200)


@router.post("/ops/client-error", status_code=204)
def client_error(body: ClientError, user: User = Depends(signed_in_user)) -> None:
    """A page broke in someone's browser (frontend app/error.tsx). Signed-in
    people only, so it can't be used to fill an inbox."""
    ops.alert(
        f"client:{body.page.split('?')[0]}:{body.message[:80]}",
        f"A page broke for someone: {body.page.split('?')[0]}",
        f"{body.message}\n\nSigned in as {user.email}." + (f"\nServer reference: {body.digest}" if body.digest else ""),
    )
