import uuid
from collections.abc import Generator
from dataclasses import dataclass
from typing import Annotated, cast

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError
from pydantic import ValidationError
from sqlalchemy import text
from sqlmodel import Session

from app.core import security
from app.core.config import settings
from app.core.db import engine
from app.models import ClinicMembership, Role, TokenPayload, User

reusable_oauth2 = OAuth2PasswordBearer(tokenUrl=f"{settings.API_V1_STR}/auth/login")


def get_db() -> Generator[Session]:
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_db)]
TokenDep = Annotated[str, Depends(reusable_oauth2)]


@dataclass(frozen=True)
class RequestContext:
    user: User
    membership: ClinicMembership

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id

    @property
    def clinic_id(self) -> uuid.UUID:
        return self.membership.clinic_id

    @property
    def role(self) -> Role:
        return cast(Role, self.membership.role)


def get_request_context(session: SessionDep, token: TokenDep) -> RequestContext:
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[security.ALGORITHM]
        )
        token_data = TokenPayload(**payload)
        user_id = uuid.UUID(token_data.sub or "")
        membership_id = uuid.UUID(token_data.membership_id or "")
    except (InvalidTokenError, ValidationError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Could not validate credentials",
        )

    user = session.get(User, user_id)
    membership = session.get(ClinicMembership, membership_id)
    if user is None or membership is None or membership.user_id != user_id:
        raise HTTPException(status_code=404, detail="Membership not found")
    if not user.is_active or not membership.is_active:
        raise HTTPException(status_code=403, detail="Inactive membership")
    if membership.role not in {"patient", "staff", "clinician", "admin", "worker"}:
        raise HTTPException(status_code=403, detail="Invalid membership role")

    # The migration policy uses this transaction-local setting. API queries still
    # include clinic_id explicitly, including when the DB owner bypasses RLS.
    if session.get_bind().dialect.name == "postgresql":
        session.connection().execute(
            text("SELECT set_config('app.current_clinic_id', :clinic_id, true)"),
            {"clinic_id": str(membership.clinic_id)},
        )
    return RequestContext(user=user, membership=membership)


CurrentContext = Annotated[RequestContext, Depends(get_request_context)]


def require_roles(*roles: Role):
    allowed = set(roles)

    def dependency(context: CurrentContext) -> RequestContext:
        if context.role not in allowed:
            raise HTTPException(status_code=403, detail="Role is not permitted")
        return context

    return dependency
