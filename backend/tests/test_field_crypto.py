import uuid

import pytest
from cryptography.exceptions import InvalidTag

from app.core.field_crypto import FORMAT_VERSION, NONCE_BYTES, FieldEncryptionCodec


def test_aes_gcm_envelope_uses_12_byte_nonce_aad_and_clinic_hkdf() -> None:
    codec = FieldEncryptionCodec(b"k" * 32)
    clinic = uuid.uuid4()
    other_clinic = uuid.uuid4()
    record = uuid.uuid4()
    envelope = codec.encrypt_text(
        clinic, "entry_version.content", record, "synthetic PHI"
    )

    assert envelope[:1] == FORMAT_VERSION
    assert len(envelope[1 : 1 + NONCE_BYTES]) == 12
    assert b"synthetic PHI" not in envelope
    assert (
        codec.decrypt_text(clinic, "entry_version.content", record, envelope)
        == "synthetic PHI"
    )
    with pytest.raises(InvalidTag):
        codec.decrypt_text(other_clinic, "entry_version.content", record, envelope)
    with pytest.raises(InvalidTag):
        codec.decrypt_text(clinic, "comment.body", record, envelope)
