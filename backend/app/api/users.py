"""The operators' Users screen: who has a login, what they can open, and
changing that. All of it goes through app.users, the same code the command
line uses, and every change lands in the audit trail with the operator who
made it."""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Response
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import users as user_service
from app.api.auth import LOGIN_SUCCEEDED
from app.auth import require_operator
from app.db import get_db
from app.models import AuditEvent, Tenant, TenantMembership, User
from app.schemas.auth import LocationOut, PasswordReset, UserCreate, UserOut, UserUpdate, UserWithPassword

router = APIRouter(prefix="/users", tags=["users"], dependencies=[Depends(require_operator)])

# On the two responses that can carry a generated password.
NO_STORE = "no-store"


def _user_or_404(db: Session, user_id: uuid.UUID) -> User:
    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="user not found")
    return user


def _out(db: Session, users: list[User]) -> list[UserOut]:
    """Grouped queries for locations and last sign-in, not one per user."""
    ids = [u.id for u in users]
    locations: dict[uuid.UUID, list[LocationOut]] = {}
    last_sign_in = {}
    if ids:
        for user_id, tenant in db.execute(
            select(TenantMembership.user_id, Tenant)
            .join(Tenant, Tenant.id == TenantMembership.tenant_id)
            .where(TenantMembership.user_id.in_(ids))
            .order_by(Tenant.name)
        ).all():
            locations.setdefault(user_id, []).append(LocationOut(id=tenant.id, name=tenant.name, metro=tenant.metro))
        last_sign_in = dict(
            db.execute(
                select(AuditEvent.entity_id, func.max(AuditEvent.occurred_at))
                .where(AuditEvent.action == LOGIN_SUCCEEDED, AuditEvent.entity_id.in_(ids))
                .group_by(AuditEvent.entity_id)
            ).all()
        )
    return [
        UserOut(
            id=u.id,
            email=u.email,
            name=u.name,
            is_operator=u.is_operator,
            is_active=u.is_active,
            created_at=u.created_at,
            last_sign_in=last_sign_in.get(u.id),
            locations=locations.get(u.id, []),
        )
        for u in users
    ]


def _fail(exc: user_service.UserError) -> HTTPException:
    return HTTPException(status_code=409 if exc.conflict else 422, detail=str(exc))


@router.get("", response_model=list[UserOut])
def list_users(db: Session = Depends(get_db)) -> list[UserOut]:
    # Active first, then by name: the people an operator is most likely looking for.
    return _out(db, list(db.scalars(select(User).order_by(User.is_active.desc(), func.lower(User.name)))))


@router.post("", response_model=UserWithPassword, status_code=201)
def create_user(
    body: UserCreate,
    response: Response,
    db: Session = Depends(get_db),
    operator: User = Depends(require_operator),
) -> UserWithPassword:
    response.headers["Cache-Control"] = NO_STORE
    generated = user_service.generate_password() if body.password is None else None
    try:
        user = user_service.create_user(
            db,
            operator,
            email=body.email,
            name=body.name,
            password=body.password if body.password is not None else generated,
            is_operator=body.is_operator,
            tenant_ids=body.location_ids,
        )
    except user_service.UserError as exc:
        raise _fail(exc) from exc
    db.commit()
    db.refresh(user)
    return UserWithPassword(user=_out(db, [user])[0], generated_password=generated)


@router.patch("/{user_id}", response_model=UserOut)
def update_user(
    user_id: uuid.UUID, body: UserUpdate, db: Session = Depends(get_db), operator: User = Depends(require_operator)
) -> UserOut:
    user = _user_or_404(db, user_id)
    try:
        user_service.update_user(
            db, operator, user, name=body.name, is_operator=body.is_operator, is_active=body.is_active
        )
    except user_service.UserError as exc:
        raise _fail(exc) from exc
    db.commit()
    return _out(db, [user])[0]


@router.post("/{user_id}/password", response_model=UserWithPassword)
def reset_password(
    user_id: uuid.UUID,
    body: PasswordReset,
    response: Response,
    db: Session = Depends(get_db),
    operator: User = Depends(require_operator),
) -> UserWithPassword:
    response.headers["Cache-Control"] = NO_STORE
    user = _user_or_404(db, user_id)
    generated = user_service.generate_password() if body.password is None else None
    try:
        user_service.set_password(db, operator, user, body.password if body.password is not None else generated)
    except user_service.UserError as exc:
        raise _fail(exc) from exc
    db.commit()
    return UserWithPassword(user=_out(db, [user])[0], generated_password=generated)


@router.put("/{user_id}/locations/{tenant_id}", response_model=UserOut)
def grant_location(
    user_id: uuid.UUID, tenant_id: uuid.UUID, db: Session = Depends(get_db), operator: User = Depends(require_operator)
) -> UserOut:
    user = _user_or_404(db, user_id)
    try:
        user_service.grant_access(db, operator, user, tenant_id)
    except user_service.UserError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    return _out(db, [user])[0]


@router.delete("/{user_id}/locations/{tenant_id}", response_model=UserOut)
def revoke_location(
    user_id: uuid.UUID, tenant_id: uuid.UUID, db: Session = Depends(get_db), operator: User = Depends(require_operator)
) -> UserOut:
    user = _user_or_404(db, user_id)
    try:
        user_service.revoke_access(db, operator, user, tenant_id)
    except user_service.UserError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    return _out(db, [user])[0]
