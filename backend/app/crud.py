from sqlmodel import Session, select

from app.core.security import verify_password
from app.models import User

DUMMY_HASH = "$argon2id$v=19$m=65536,t=3,p=4$MjQyZWE1MzBjYjJlZTI0Yw$YTU4NGM5ZTZmYjE2NzZlZjY0ZWY3ZGRkY2U2OWFjNjk"


def authenticate(*, session: Session, email: str, password: str) -> User | None:
    user = session.exec(select(User).where(User.email == email)).first()
    if user is None:
        verify_password(password, DUMMY_HASH)
        return None
    verified, updated_hash = verify_password(password, user.hashed_password)
    if not verified:
        return None
    if updated_hash:
        user.hashed_password = updated_hash
        session.add(user)
        session.commit()
        session.refresh(user)
    return user
