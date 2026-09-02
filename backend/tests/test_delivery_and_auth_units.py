from __future__ import annotations

from email.message import EmailMessage
from typing import Any

import pytest

from app import crud
from app.core.config import settings
from app.models import User
from app.services import invitations

pytestmark = pytest.mark.unit


class _SMTPFixture:
    instances: list[_SMTPFixture] = []

    def __init__(self, host: str, port: int, *, timeout: int) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.started_tls = False
        self.login_credentials: tuple[str, str] | None = None
        self.messages: list[EmailMessage] = []
        self.__class__.instances.append(self)

    def __enter__(self) -> _SMTPFixture:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def starttls(self) -> None:
        self.started_tls = True

    def login(self, username: str, password: str) -> None:
        self.login_credentials = (username, password)

    def send_message(self, message: EmailMessage) -> None:
        self.messages.append(message)


def _configure_smtp(monkeypatch: pytest.MonkeyPatch, *, ssl: bool) -> None:
    _SMTPFixture.instances.clear()
    monkeypatch.setattr(settings, "SMTP_HOST", "smtp.fixture.invalid")
    monkeypatch.setattr(settings, "SMTP_PORT", 2525)
    monkeypatch.setattr(settings, "SMTP_SSL", ssl)
    monkeypatch.setattr(settings, "SMTP_TLS", not ssl)
    monkeypatch.setattr(settings, "SMTP_USER", "fixture-user")
    monkeypatch.setattr(settings, "SMTP_PASSWORD", "fixture-password")
    monkeypatch.setattr(settings, "EMAILS_FROM_NAME", "Nightingale Fixture")
    monkeypatch.setattr(settings, "EMAILS_FROM_EMAIL", "no-reply@example.test")
    monkeypatch.setattr(invitations.smtplib, "SMTP", _SMTPFixture)
    monkeypatch.setattr(invitations.smtplib, "SMTP_SSL", _SMTPFixture)


def test_membership_invitation_uses_fragment_only_smtp_delivery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_smtp(monkeypatch, ssl=False)
    invitations.deliver_membership_invitation(
        recipient="staff@example.test", token="TOKEN / with spaces"
    )

    smtp = _SMTPFixture.instances[-1]
    assert smtp.started_tls is True
    assert smtp.login_credentials == ("fixture-user", "fixture-password")
    assert smtp.timeout == 10
    message = smtp.messages[0]
    content = message.get_content()
    assert message["To"] == "staff@example.test"
    assert "#TOKEN%20%2F%20with%20spaces" in content
    assert "?TOKEN" not in content


def test_patient_invitation_uses_ssl_and_clinic_scoped_copy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _configure_smtp(monkeypatch, ssl=True)
    invitations.deliver_patient_portal_invitation(
        recipient="patient@example.test",
        token="PATIENT_TOKEN",
        clinic_name="Clinic Fixture",
    )

    smtp = _SMTPFixture.instances[-1]
    assert smtp.started_tls is False
    assert smtp.login_credentials == ("fixture-user", "fixture-password")
    message = smtp.messages[0]
    assert message["Subject"] == "Access your care information from Clinic Fixture"
    assert "/patient/accept-invitation#PATIENT_TOKEN" in message.get_content()


@pytest.mark.parametrize(
    "delivery",
    [
        lambda: invitations.deliver_membership_invitation(
            recipient="staff@example.test", token="TOKEN"
        ),
        lambda: invitations.deliver_patient_portal_invitation(
            recipient="patient@example.test", token="TOKEN", clinic_name="Clinic"
        ),
    ],
)
def test_invitation_delivery_fails_loudly_without_smtp(
    monkeypatch: pytest.MonkeyPatch, delivery: Any
) -> None:
    monkeypatch.setattr(settings, "SMTP_HOST", None)
    with pytest.raises(RuntimeError, match="delivery is not configured"):
        delivery()


class _Result:
    def __init__(self, user: User | None) -> None:
        self.user = user

    def first(self) -> User | None:
        return self.user


class _Session:
    def __init__(self, user: User | None) -> None:
        self.user = user
        self.added: list[User] = []
        self.commits = 0
        self.refreshed: list[User] = []

    def exec(self, _statement: object) -> _Result:
        return _Result(self.user)

    def add(self, user: User) -> None:
        self.added.append(user)

    def commit(self) -> None:
        self.commits += 1

    def refresh(self, user: User) -> None:
        self.refreshed.append(user)


def test_authenticate_equalizes_missing_and_ineligible_accounts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    checked: list[tuple[str, str]] = []
    monkeypatch.setattr(
        crud,
        "verify_password",
        lambda password, encoded: checked.append((password, encoded)) or (False, None),
    )

    assert (
        crud.authenticate(
            session=_Session(None), email=" Missing@Example.Test ", password="guess"
        )
        is None
    )
    service = User(
        email="worker@example.test",
        hashed_password="stored",
        account_kind="service",
    )
    assert (
        crud.authenticate(
            session=_Session(service),
            email="worker@example.test",
            password="guess",
        )
        is None
    )
    assert len(checked) == 2
    assert all(encoded == crud.DUMMY_HASH for _password, encoded in checked)


def test_authenticate_rejects_bad_password_and_rehashes_valid_password(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = User(
        email="staff@example.test",
        hashed_password="old-hash",
        account_kind="staff",
    )
    session = _Session(user)
    monkeypatch.setattr(crud, "verify_password", lambda *_args: (False, None))
    assert (
        crud.authenticate(session=session, email="STAFF@example.test", password="wrong")
        is None
    )
    assert session.commits == 0

    monkeypatch.setattr(crud, "verify_password", lambda *_args: (True, "upgraded-hash"))
    authenticated = crud.authenticate(
        session=session, email=" STAFF@example.test ", password="correct"
    )
    assert authenticated is user
    assert user.hashed_password == "upgraded-hash"
    assert session.added == [user]
    assert session.commits == 1
    assert session.refreshed == [user]
