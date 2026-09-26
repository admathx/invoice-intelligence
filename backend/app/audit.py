"""Recording who changed what (AuditEvent).

record() only adds the event to the session; the caller's own commit writes
it. That's deliberate: an event exists if and only if the change it describes
was committed, in the same transaction, so the log can't claim a change that
rolled back or miss one that landed.
"""
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from app.models import AuditEvent, User


def _jsonable(value: Any) -> Any:
    # Money stays exact in the log: Decimal as its string, never a float. Scale
    # is normalized (to at least cents) so a stored 20.0000 and a typed 20.00
    # read the same in a before/after pair; digits past the cent are kept.
    if isinstance(value, Decimal):
        cents = value.quantize(Decimal("0.01"))
        return str(cents) if cents == value else format(value.normalize(), "f")
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):
        return value.value  # enums
    return value


def record(
    db: Session,
    actor: User | None,
    action: str,
    entity_type: str,
    entity_id: uuid.UUID | None = None,
    tenant_id: uuid.UUID | None = None,
    **details: Any,
) -> None:
    """actor=None means the system itself (the extraction worker, email intake).
    Details whose value is None are left out rather than stored as null."""
    db.add(
        AuditEvent(
            actor_user_id=actor.id if actor is not None else None,
            tenant_id=tenant_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=_jsonable({name: value for name, value in details.items() if value is not None}),
        )
    )


def changes(before: dict[str, Any], after: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """{field: {"from": old, "to": new}} for the fields that actually changed."""
    return {
        name: {"from": before.get(name), "to": after.get(name)}
        for name in after
        if before.get(name) != after.get(name)
    }
