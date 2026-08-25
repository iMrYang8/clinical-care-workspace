"""Add encrypted voice capture, immutable transcript review, and audio provenance.

Revision ID: ee8a2f6b9010
Revises: d7f4a2c9e610
"""

from collections.abc import Sequence

from alembic import op

revision: str = "ee8a2f6b9010"
down_revision: str | None = "d7f4a2c9e610"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = (
    "voice_sessions",
    "voice_devices",
    "audio_chunks",
    "audio_assets",
    "transcript_revisions",
    "transcript_segments",
    "clinical_facts",
)

IMMUTABLE_TABLES = (
    "audio_chunks",
    "audio_assets",
    "transcript_revisions",
    "transcript_segments",
)


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE voice_sessions (
          id UUID PRIMARY KEY,
          clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          patient_id UUID NOT NULL,
          capture_kind VARCHAR(20) NOT NULL
            CHECK (capture_kind IN ('patient','clinical')),
          state VARCHAR(30) NOT NULL DEFAULT 'created',
          synthetic_fixture BOOLEAN NOT NULL DEFAULT false,
          fixture_id VARCHAR(100),
          created_by_id UUID NOT NULL REFERENCES users(id),
          current_transcript_revision_id UUID,
          processing_job_id UUID,
          published_entry_id UUID,
          patient_summary_ciphertext BYTEA,
          warning_codes_json JSONB NOT NULL DEFAULT '[]'::jsonb,
          error_code VARCHAR(80),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_voice_session_clinic_id UNIQUE(clinic_id,id),
          CONSTRAINT fk_voice_session_patient_tenant
            FOREIGN KEY(clinic_id,patient_id)
            REFERENCES patients(clinic_id,id) ON DELETE CASCADE,
          CONSTRAINT fk_voice_session_job_tenant
            FOREIGN KEY(clinic_id,processing_job_id)
            REFERENCES jobs(clinic_id,id),
          CONSTRAINT fk_voice_session_published_entry_tenant
            FOREIGN KEY(clinic_id,published_entry_id)
            REFERENCES entries(clinic_id,id),
          CONSTRAINT ck_voice_fixture_pair CHECK (
            (synthetic_fixture = false AND fixture_id IS NULL)
            OR (synthetic_fixture = true AND fixture_id IS NOT NULL)
          ),
          CONSTRAINT ck_voice_session_state CHECK (
            state IN ('created','recording','finalizing','assembling',
                      'preprocessing','transcribing','redacting','extracting',
                      'ready','needs_review','published')
          )
        );
        CREATE INDEX ix_voice_session_patient_created
          ON voice_sessions(clinic_id,patient_id,created_at);
        CREATE INDEX ix_voice_session_state_updated
          ON voice_sessions(clinic_id,state,updated_at);

        CREATE TABLE voice_devices (
          id UUID PRIMARY KEY,
          clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          session_id UUID NOT NULL,
          client_device_id VARCHAR(120) NOT NULL,
          capture_role VARCHAR(30) NOT NULL
            CHECK (capture_role IN ('patient','staff','clinician')),
          joined_by_id UUID NOT NULL REFERENCES users(id),
          last_declared_chunk_index INTEGER,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_voice_device_clinic_id UNIQUE(clinic_id,id),
          CONSTRAINT uq_voice_device_session_id UNIQUE(clinic_id,session_id,id),
          CONSTRAINT uq_voice_device_client_id
            UNIQUE(clinic_id,session_id,client_device_id),
          CONSTRAINT fk_voice_device_session_tenant
            FOREIGN KEY(clinic_id,session_id)
            REFERENCES voice_sessions(clinic_id,id) ON DELETE CASCADE,
          CONSTRAINT ck_voice_device_last_index CHECK(
            last_declared_chunk_index IS NULL
            OR last_declared_chunk_index BETWEEN 0 AND 21600
          )
        );
        CREATE INDEX ix_voice_device_session
          ON voice_devices(clinic_id,session_id);

        CREATE TABLE audio_chunks (
          id UUID PRIMARY KEY,
          clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          session_id UUID NOT NULL,
          device_id UUID NOT NULL,
          chunk_index INTEGER NOT NULL,
          payload_ciphertext BYTEA NOT NULL,
          plaintext_sha256 VARCHAR(64) NOT NULL,
          byte_length INTEGER NOT NULL CHECK(byte_length > 0),
          media_type VARCHAR(100) NOT NULL,
          start_ms INTEGER CHECK(start_ms >= 0),
          end_ms INTEGER CHECK(end_ms >= 0),
          received_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_audio_chunk_clinic_id UNIQUE(clinic_id,id),
          CONSTRAINT uq_audio_chunk_index UNIQUE(clinic_id,device_id,chunk_index),
          CONSTRAINT fk_audio_chunk_session_tenant
            FOREIGN KEY(clinic_id,session_id)
            REFERENCES voice_sessions(clinic_id,id) ON DELETE CASCADE,
          CONSTRAINT fk_audio_chunk_device_tenant
            FOREIGN KEY(clinic_id,session_id,device_id)
            REFERENCES voice_devices(clinic_id,session_id,id) ON DELETE CASCADE,
          CONSTRAINT ck_audio_chunk_range CHECK (
            (start_ms IS NULL AND end_ms IS NULL)
            OR (start_ms IS NOT NULL AND end_ms IS NOT NULL AND end_ms > start_ms)
          ),
          CONSTRAINT ck_audio_chunk_index_bound CHECK (
            chunk_index BETWEEN 0 AND 21600
          )
        );
        CREATE INDEX ix_audio_chunk_session_device_index
          ON audio_chunks(clinic_id,session_id,device_id,chunk_index);

        CREATE TABLE audio_assets (
          id UUID PRIMARY KEY,
          clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          session_id UUID NOT NULL,
          payload_ciphertext BYTEA NOT NULL,
          plaintext_sha256 VARCHAR(64) NOT NULL,
          duration_ms INTEGER NOT NULL CHECK(duration_ms >= 0),
          media_type VARCHAR(100) NOT NULL DEFAULT 'audio/wav',
          sample_rate_hz INTEGER NOT NULL DEFAULT 16000 CHECK(sample_rate_hz > 0),
          channels INTEGER NOT NULL DEFAULT 1 CHECK(channels > 0),
          preprocessing_json JSONB NOT NULL DEFAULT '{}'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_audio_asset_clinic_id UNIQUE(clinic_id,id),
          CONSTRAINT uq_audio_asset_session UNIQUE(clinic_id,session_id),
          CONSTRAINT uq_audio_asset_session_id UNIQUE(clinic_id,session_id,id),
          CONSTRAINT fk_audio_asset_session_tenant
            FOREIGN KEY(clinic_id,session_id)
            REFERENCES voice_sessions(clinic_id,id) ON DELETE CASCADE
        );

        CREATE TABLE transcript_revisions (
          id UUID PRIMARY KEY,
          clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          session_id UUID NOT NULL,
          revision_no INTEGER NOT NULL CHECK(revision_no >= 1),
          previous_revision_id UUID,
          text_ciphertext BYTEA NOT NULL,
          text_sha256 VARCHAR(64) NOT NULL,
          summary_ciphertext BYTEA,
          provider VARCHAR(80) NOT NULL,
          model VARCHAR(160) NOT NULL,
          detected_language VARCHAR(80),
          status VARCHAR(30) NOT NULL DEFAULT 'ready',
          needs_review BOOLEAN NOT NULL DEFAULT false,
          stale BOOLEAN NOT NULL DEFAULT false,
          fallback BOOLEAN NOT NULL DEFAULT false,
          corrected_by_id UUID REFERENCES users(id),
          warning_codes_json JSONB NOT NULL DEFAULT '[]'::jsonb,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_transcript_revision_clinic_id UNIQUE(clinic_id,id),
          CONSTRAINT uq_transcript_revision_session_id UNIQUE(clinic_id,session_id,id),
          CONSTRAINT uq_transcript_revision_number
            UNIQUE(clinic_id,session_id,revision_no),
          CONSTRAINT fk_transcript_revision_session_tenant
            FOREIGN KEY(clinic_id,session_id)
            REFERENCES voice_sessions(clinic_id,id) ON DELETE CASCADE,
          CONSTRAINT fk_transcript_previous_revision_tenant
            FOREIGN KEY(clinic_id,session_id,previous_revision_id)
            REFERENCES transcript_revisions(clinic_id,session_id,id),
          CONSTRAINT ck_transcript_revision_status
            CHECK(status IN ('ready','needs_review'))
        );
        CREATE INDEX ix_transcript_revision_session_created
          ON transcript_revisions(clinic_id,session_id,created_at);

        ALTER TABLE voice_sessions ADD CONSTRAINT
          fk_voice_session_current_revision_tenant
          FOREIGN KEY(clinic_id,id,current_transcript_revision_id)
          REFERENCES transcript_revisions(clinic_id,session_id,id)
          DEFERRABLE INITIALLY DEFERRED;

        CREATE TABLE transcript_segments (
          id UUID PRIMARY KEY,
          clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          session_id UUID NOT NULL,
          revision_id UUID NOT NULL,
          ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
          text_ciphertext BYTEA NOT NULL,
          text_sha256 VARCHAR(64) NOT NULL,
          text_start INTEGER NOT NULL CHECK(text_start >= 0),
          text_end INTEGER NOT NULL CHECK(text_end > text_start),
          start_ms INTEGER NOT NULL CHECK(start_ms >= 0),
          end_ms INTEGER NOT NULL CHECK(end_ms > start_ms),
          speaker_id VARCHAR(80),
          detected_language VARCHAR(80),
          confidence DOUBLE PRECISION CHECK(confidence >= 0 AND confidence <= 1),
          confidence_source VARCHAR(80) NOT NULL,
          overlap_group_id VARCHAR(100),
          provider VARCHAR(80) NOT NULL,
          model VARCHAR(160) NOT NULL,
          CONSTRAINT uq_transcript_segment_clinic_id UNIQUE(clinic_id,id),
          CONSTRAINT uq_transcript_segment_revision_id UNIQUE(clinic_id,session_id,revision_id,id),
          CONSTRAINT uq_transcript_segment_ordinal UNIQUE(clinic_id,revision_id,ordinal),
          CONSTRAINT fk_transcript_segment_revision_tenant
            FOREIGN KEY(clinic_id,session_id,revision_id)
            REFERENCES transcript_revisions(clinic_id,session_id,id) ON DELETE CASCADE
        );
        CREATE INDEX ix_transcript_segment_revision_time
          ON transcript_segments(clinic_id,revision_id,start_ms);

        CREATE TABLE clinical_facts (
          id UUID PRIMARY KEY,
          clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          session_id UUID NOT NULL,
          revision_id UUID NOT NULL,
          segment_id UUID NOT NULL,
          ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
          fact_type VARCHAR(80) NOT NULL,
          value_ciphertext BYTEA NOT NULL,
          exact_quote_ciphertext BYTEA NOT NULL,
          quote_sha256 VARCHAR(64) NOT NULL,
          transcript_start INTEGER NOT NULL CHECK(transcript_start >= 0),
          transcript_end INTEGER NOT NULL CHECK(transcript_end > transcript_start),
          audio_asset_id UUID NOT NULL,
          audio_start_ms INTEGER NOT NULL CHECK(audio_start_ms >= 0),
          audio_end_ms INTEGER NOT NULL CHECK(audio_end_ms > audio_start_ms),
          status VARCHAR(30) NOT NULL DEFAULT 'proposed',
          patient_facing BOOLEAN NOT NULL DEFAULT false,
          stale BOOLEAN NOT NULL DEFAULT false,
          reviewed_by_id UUID REFERENCES users(id),
          reviewed_at TIMESTAMPTZ,
          CONSTRAINT uq_clinical_fact_clinic_id UNIQUE(clinic_id,id),
          CONSTRAINT uq_clinical_fact_ordinal UNIQUE(clinic_id,revision_id,ordinal),
          CONSTRAINT fk_clinical_fact_revision_tenant
            FOREIGN KEY(clinic_id,session_id,revision_id)
            REFERENCES transcript_revisions(clinic_id,session_id,id) ON DELETE CASCADE,
          CONSTRAINT fk_clinical_fact_segment_tenant
            FOREIGN KEY(clinic_id,session_id,revision_id,segment_id)
            REFERENCES transcript_segments(clinic_id,session_id,revision_id,id),
          CONSTRAINT fk_clinical_fact_audio_asset_tenant
            FOREIGN KEY(clinic_id,session_id,audio_asset_id)
            REFERENCES audio_assets(clinic_id,session_id,id),
          CONSTRAINT ck_clinical_fact_status
            CHECK(status IN ('proposed','accepted','rejected')),
          CONSTRAINT ck_clinical_fact_review_pair CHECK (
            (status = 'proposed' AND reviewed_by_id IS NULL AND reviewed_at IS NULL)
            OR (status IN ('accepted','rejected') AND reviewed_by_id IS NOT NULL
                AND reviewed_at IS NOT NULL)
          )
        );
        CREATE INDEX ix_clinical_fact_revision_status
          ON clinical_facts(clinic_id,revision_id,status);

        ALTER TABLE provenance_pointers
          ADD COLUMN clinical_fact_id UUID;
        ALTER TABLE provenance_pointers ADD CONSTRAINT
          fk_provenance_audio_asset_tenant
          FOREIGN KEY(clinic_id,audio_asset_id)
          REFERENCES audio_assets(clinic_id,id);
        ALTER TABLE provenance_pointers ADD CONSTRAINT
          fk_provenance_clinical_fact_tenant
          FOREIGN KEY(clinic_id,clinical_fact_id)
          REFERENCES clinical_facts(clinic_id,id);
        """
    )

    for table in TENANT_TABLES:
        op.execute(f'ALTER TABLE "{table}" ENABLE ROW LEVEL SECURITY')
        op.execute(
            f"""
            CREATE POLICY clinic_isolation ON "{table}"
            USING (
              clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid
            )
            WITH CHECK (
              clinic_id = NULLIF(current_setting('app.current_clinic_id', true), '')::uuid
            )
            """
        )

    op.execute(
        """
        CREATE FUNCTION nightingale_voice_append_only()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          RAISE EXCEPTION '% is append-only', TG_TABLE_NAME USING ERRCODE = '55000';
        END;
        $$;
        """
    )
    for table in IMMUTABLE_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER trg_{table}_append_only
              BEFORE UPDATE OR DELETE ON {table}
              FOR EACH ROW EXECUTE FUNCTION nightingale_voice_append_only()
            """
        )
    op.execute(
        """
        CREATE FUNCTION nightingale_clinical_fact_review_guard()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'clinical fact evidence is immutable' USING ERRCODE = '55000';
          END IF;
          IF OLD.clinic_id IS DISTINCT FROM NEW.clinic_id
             OR OLD.session_id IS DISTINCT FROM NEW.session_id
             OR OLD.revision_id IS DISTINCT FROM NEW.revision_id
             OR OLD.segment_id IS DISTINCT FROM NEW.segment_id
             OR OLD.ordinal IS DISTINCT FROM NEW.ordinal
             OR OLD.fact_type IS DISTINCT FROM NEW.fact_type
             OR OLD.value_ciphertext IS DISTINCT FROM NEW.value_ciphertext
             OR OLD.exact_quote_ciphertext IS DISTINCT FROM NEW.exact_quote_ciphertext
             OR OLD.quote_sha256 IS DISTINCT FROM NEW.quote_sha256
             OR OLD.transcript_start IS DISTINCT FROM NEW.transcript_start
             OR OLD.transcript_end IS DISTINCT FROM NEW.transcript_end
             OR OLD.audio_asset_id IS DISTINCT FROM NEW.audio_asset_id
             OR OLD.audio_start_ms IS DISTINCT FROM NEW.audio_start_ms
             OR OLD.audio_end_ms IS DISTINCT FROM NEW.audio_end_ms
             OR OLD.patient_facing IS DISTINCT FROM NEW.patient_facing
             OR OLD.stale IS DISTINCT FROM NEW.stale
             OR OLD.status <> 'proposed'
             OR NEW.status NOT IN ('accepted','rejected')
             OR NEW.reviewed_by_id IS NULL
             OR NEW.reviewed_at IS NULL THEN
            RAISE EXCEPTION 'clinical fact evidence is immutable' USING ERRCODE = '55000';
          END IF;
          RETURN NEW;
        END;
        $$;
        CREATE TRIGGER trg_clinical_fact_review_guard
          BEFORE UPDATE OR DELETE ON clinical_facts
          FOR EACH ROW EXECUTE FUNCTION nightingale_clinical_fact_review_guard();
        """
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_clinical_fact_review_guard ON clinical_facts"
    )
    op.execute("DROP FUNCTION IF EXISTS nightingale_clinical_fact_review_guard()")
    for table in IMMUTABLE_TABLES:
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_append_only ON {table}")
    # Older development snapshots briefly treated fact review status as
    # append-only. Keep downgrade resilient for those local databases.
    op.execute(
        "DROP TRIGGER IF EXISTS trg_clinical_facts_append_only ON clinical_facts"
    )
    op.execute("DROP FUNCTION IF EXISTS nightingale_voice_append_only()")
    op.execute(
        """
        ALTER TABLE provenance_pointers
          DROP CONSTRAINT IF EXISTS fk_provenance_clinical_fact_tenant;
        ALTER TABLE provenance_pointers
          DROP CONSTRAINT IF EXISTS fk_provenance_audio_asset_tenant;
        ALTER TABLE provenance_pointers
          DROP COLUMN IF EXISTS clinical_fact_id;
        ALTER TABLE voice_sessions
          DROP CONSTRAINT IF EXISTS fk_voice_session_current_revision_tenant;
        DROP TABLE IF EXISTS clinical_facts;
        DROP TABLE IF EXISTS transcript_segments;
        DROP TABLE IF EXISTS transcript_revisions;
        DROP TABLE IF EXISTS audio_assets;
        DROP TABLE IF EXISTS audio_chunks;
        DROP TABLE IF EXISTS voice_devices;
        DROP TABLE IF EXISTS voice_sessions;
        """
    )
