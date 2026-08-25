"""Clinic-scoped envelope codec for encrypted database fields."""

import base64
import hashlib
import json
import os
import uuid
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from app.core.config import settings

FORMAT_VERSION = b"\x01"
NONCE_BYTES = 12


def _master_key() -> bytes:
    configured = settings.FIELD_ENCRYPTION_MASTER_KEY
    if configured:
        try:
            key = bytes.fromhex(configured)
        except ValueError:
            padding = "=" * (-len(configured) % 4)
            key = base64.urlsafe_b64decode(configured + padding)
    else:
        key = hashlib.sha256(settings.SECRET_KEY.encode("utf-8")).digest()
    if len(key) != 32:
        raise ValueError("FIELD_ENCRYPTION_MASTER_KEY must decode to exactly 32 bytes")
    return key


class FieldEncryptionCodec:
    """AES-256-GCM with a distinct HKDF-derived key for every clinic."""

    def __init__(self, master_key: bytes | None = None) -> None:
        self.master_key = master_key or _master_key()
        if len(self.master_key) != 32:
            raise ValueError("AES-256 master key must be exactly 32 bytes")

    def _clinic_key(self, clinic_id: uuid.UUID) -> bytes:
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=clinic_id.bytes,
            info=b"nightingale/field-encryption/v1",
        ).derive(self.master_key)

    @staticmethod
    def aad(clinic_id: uuid.UUID, namespace: str, record_id: uuid.UUID) -> bytes:
        return f"nightingale:v1:{clinic_id}:{namespace}:{record_id}".encode()

    def encrypt(
        self,
        clinic_id: uuid.UUID,
        namespace: str,
        record_id: uuid.UUID,
        plaintext: bytes,
    ) -> bytes:
        nonce = os.urandom(NONCE_BYTES)
        encrypted = AESGCM(self._clinic_key(clinic_id)).encrypt(
            nonce, plaintext, self.aad(clinic_id, namespace, record_id)
        )
        return FORMAT_VERSION + nonce + encrypted

    def decrypt(
        self,
        clinic_id: uuid.UUID,
        namespace: str,
        record_id: uuid.UUID,
        envelope: bytes,
    ) -> bytes:
        if len(envelope) < 1 + NONCE_BYTES + 16 or envelope[:1] != FORMAT_VERSION:
            raise ValueError("Unsupported or truncated encrypted field")
        nonce = envelope[1 : 1 + NONCE_BYTES]
        ciphertext = envelope[1 + NONCE_BYTES :]
        return AESGCM(self._clinic_key(clinic_id)).decrypt(
            nonce, ciphertext, self.aad(clinic_id, namespace, record_id)
        )

    def encrypt_text(
        self, clinic_id: uuid.UUID, namespace: str, record_id: uuid.UUID, value: str
    ) -> bytes:
        return self.encrypt(clinic_id, namespace, record_id, value.encode("utf-8"))

    def decrypt_text(
        self, clinic_id: uuid.UUID, namespace: str, record_id: uuid.UUID, value: bytes
    ) -> str:
        return self.decrypt(clinic_id, namespace, record_id, value).decode("utf-8")

    def encrypt_json(
        self, clinic_id: uuid.UUID, namespace: str, record_id: uuid.UUID, value: Any
    ) -> bytes:
        payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
        return self.encrypt(clinic_id, namespace, record_id, payload)

    def decrypt_json(
        self, clinic_id: uuid.UUID, namespace: str, record_id: uuid.UUID, value: bytes
    ) -> Any:
        return json.loads(self.decrypt(clinic_id, namespace, record_id, value))


field_codec = FieldEncryptionCodec()
