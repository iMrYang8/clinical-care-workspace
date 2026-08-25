import uuid
from collections.abc import Callable, Generator
from dataclasses import dataclass
from typing import Annotated, cast

import jwt
from fastapi import Cookie, Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jwt.exceptions import InvalidTokenError
from pydantic import ValidationError
from sqlmodel import Session

from app.core import security
from app.core.config import settings
from app.core.db import engine, set_rls_clinic
from app.models import ClinicMembership, Role, TokenPayload, User

reusable_oauth2 = OAuth2PasswordBearer(
    tokenUrl=f"{settings.API_V1_STR}/auth/login", auto_error=False
)


def get_db() -> Generator[Session]:
    with Session(engine) as session:
        yield session


SessionDep = Annotated[Session, Depends(get_db)]
BearerTokenDep = Annotated[str | None, Depends(reusable_oauth2)]
CookieTokenDep = Annotated[
    str | None, Cookie(default=None, alias=settings.AUTH_COOKIE_NAME)
]


@dataclass(frozen=True)
class RequestContext:
    user: User
    membership: ClinicMembership
    job_id: uuid.UUID | None = None

    @property
    def user_id(self) -> uuid.UUID:
        return self.user.id

    @property
    def clinic_id(self) -> uuid.UUID:
        return self.membership.clinic_id

    @property
    def role(self) -> Role:
        return cast(Role, self.membership.role)


def _resolve_request_context(session: Session, token: str) -> RequestContext:
    try:
        payload = jwt.decode(
            token, settings.SECRET_KEY, algorithms=[security.ALGORITHM]
        )
        token_data = TokenPayload(**payload)
        user_id = uuid.UUID(token_data.sub or "")
        membership_id = uuid.UUID(token_data.membership_id or "")
        token_clinic_id = uuid.UUID(token_data.clinic_id or "")
        job_id = uuid.UUID(token_data.job_id) if token_data.job_id else None
    except (InvalidTokenError, ValidationError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Could not validate credentials",
        )

    # Bootstrap RLS from a signed server-issued claim, then verify it against the
    # live membership row. A moved/revoked membership therefore invalidates the JWT.
    set_rls_clinic(session, token_clinic_id)
    user = session.get(User, user_id)
    membership = session.get(ClinicMembership, membership_id)
    if (
        user is None
        or membership is None
        or membership.user_id != user_id
        or membership.clinic_id != token_clinic_id
    ):
        raise HTTPException(status_code=404, detail="Membership not found")
    if not user.is_active or not membership.is_active:
        raise HTTPException(status_code=403, detail="Inactive membership")
    if membership.role not in {"patient", "staff", "clinician", "admin", "worker"}:
        raise HTTPException(status_code=403, detail="Invalid membership role")

    return RequestContext(user=user, membership=membership, job_id=job_id)


def _trusted_token(bearer: str | None, cookie: str | None) -> str:
    token = bearer or cookie
    if token is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


def get_request_context(
    session: SessionDep, bearer: BearerTokenDep, cookie: CookieTokenDep
) -> RequestContext:
    return _resolve_request_context(session, _trusted_token(bearer, cookie))


def get_detached_request_context(
    bearer: BearerTokenDep, cookie: CookieTokenDep
) -> RequestContext:
    """Resolve SSE auth in a bounded session released before streaming starts."""

    with Session(engine) as session:
        context = _resolve_request_context(session, _trusted_token(bearer, cookie))
        session.expunge(context.user)
        session.expunge(context.membership)
        return context


CurrentContext = Annotated[RequestContext, Depends(get_request_context)]
EventContext = Annotated[RequestContext, Depends(get_detached_request_context)]


def require_roles(*roles: Role) -> Callable[[RequestContext], RequestContext]:
    allowed = set(roles)

    def dependency(context: CurrentContext) -> RequestContext:
        if context.role not in allowed:
            raise HTTPException(status_code=403, detail="Role is not permitted")
        return context

    return dependency
