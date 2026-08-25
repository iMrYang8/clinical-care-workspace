"""Nightingale clinic-scoped core domain and RLS policies.

Revision ID: c7b13d0a9e21
Revises: fe56fa70289e
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c7b13d0a9e21"
down_revision: str | None = "fe56fa70289e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TENANT_TABLES = (
    "clinic_memberships",
    "patients",
    "patient_user_links",
    "entries",
    "entry_versions",
    "entry_relations",
    "comments",
    "comment_mentions",
    "care_tasks",
    "highlights",
    "provenance_pointers",
    "conflict_cases",
    "audit_events",
    "patient_glance_snapshots",
    "domain_events",
)


def upgrade() -> None:
    op.execute('DROP TABLE IF EXISTS "item" CASCADE')
    op.execute('DROP TABLE IF EXISTS "user" CASCADE')
    op.execute(
        """
        CREATE TABLE clinics (
          id UUID PRIMARY KEY, slug VARCHAR(80) NOT NULL UNIQUE,
          name VARCHAR(255) NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_clinics_slug ON clinics(slug);

        CREATE TABLE users (
          id UUID PRIMARY KEY, email VARCHAR(255) NOT NULL UNIQUE,
          full_name VARCHAR(255), hashed_password VARCHAR NOT NULL,
          is_active BOOLEAN NOT NULL DEFAULT true,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_users_email ON users(email);

        CREATE TABLE clinic_memberships (
          id UUID PRIMARY KEY, clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          role VARCHAR(20) NOT NULL CHECK (role IN ('patient','staff','clinician','admin','worker')),
          is_active BOOLEAN NOT NULL DEFAULT true, created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_membership_clinic_user UNIQUE(clinic_id,user_id)
        );
        CREATE INDEX ix_membership_user_active ON clinic_memberships(user_id,is_active);
        CREATE INDEX ix_clinic_memberships_clinic_id ON clinic_memberships(clinic_id);

        CREATE TABLE patients (
          id UUID PRIMARY KEY, clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          display_name_ciphertext BYTEA NOT NULL, external_ref_hash VARCHAR(64) NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_patient_clinic_id UNIQUE(clinic_id,id)
        );
        CREATE INDEX ix_patient_clinic_created ON patients(clinic_id,created_at);
        CREATE INDEX ix_patients_external_ref_hash ON patients(external_ref_hash);

        CREATE TABLE patient_user_links (
          id UUID PRIMARY KEY, clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          patient_id UUID NOT NULL, user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT fk_patient_link_tenant FOREIGN KEY(clinic_id,patient_id)
            REFERENCES patients(clinic_id,id) ON DELETE CASCADE,
          CONSTRAINT uq_patient_user_link UNIQUE(clinic_id,patient_id,user_id)
        );

        CREATE TABLE entries (
          id UUID PRIMARY KEY, clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          patient_id UUID NOT NULL, section VARCHAR(20) NOT NULL
            CHECK (section IN ('patient','staff','clinician','system')),
          origin VARCHAR(20) NOT NULL DEFAULT 'human'
            CHECK (origin IN ('human','ai','system')),
          patient_facing BOOLEAN NOT NULL DEFAULT false, current_version_id UUID,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_entry_clinic_id UNIQUE(clinic_id,id),
          CONSTRAINT fk_entry_patient_tenant FOREIGN KEY(clinic_id,patient_id)
            REFERENCES patients(clinic_id,id) ON DELETE CASCADE
        );
        CREATE INDEX ix_entry_patient_section ON entries(clinic_id,patient_id,section);

        CREATE TABLE entry_versions (
          id UUID PRIMARY KEY, clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          entry_id UUID NOT NULL, version_no INTEGER NOT NULL CHECK(version_no >= 1),
          title_ciphertext BYTEA, content_ciphertext BYTEA, content_sha256 VARCHAR(64) NOT NULL,
          author_id UUID NOT NULL REFERENCES users(id), reverted_from_version_id UUID,
          storage_tier VARCHAR(10) NOT NULL DEFAULT 'hot', archive_blob_id UUID,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_entry_version_clinic_id UNIQUE(clinic_id,id),
          CONSTRAINT uq_entry_version_number UNIQUE(entry_id,version_no),
          CONSTRAINT fk_version_entry_tenant FOREIGN KEY(clinic_id,entry_id)
            REFERENCES entries(clinic_id,id) ON DELETE CASCADE,
          CONSTRAINT fk_version_reverted_from FOREIGN KEY(reverted_from_version_id)
            REFERENCES entry_versions(id)
        );
        CREATE INDEX ix_entry_version_entry_created ON entry_versions(entry_id,created_at);
        ALTER TABLE entries ADD CONSTRAINT fk_entry_current_version_tenant
          FOREIGN KEY(clinic_id,current_version_id) REFERENCES entry_versions(clinic_id,id)
          DEFERRABLE INITIALLY DEFERRED;

        CREATE TABLE entry_relations (
          id UUID PRIMARY KEY, clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          source_entry_id UUID NOT NULL, target_entry_id UUID NOT NULL,
          relation_type VARCHAR(40) NOT NULL, created_by_id UUID NOT NULL REFERENCES users(id),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT fk_relation_source FOREIGN KEY(clinic_id,source_entry_id)
            REFERENCES entries(clinic_id,id) ON DELETE CASCADE,
          CONSTRAINT fk_relation_target FOREIGN KEY(clinic_id,target_entry_id)
            REFERENCES entries(clinic_id,id) ON DELETE CASCADE,
          CONSTRAINT uq_entry_relation UNIQUE(clinic_id,source_entry_id,target_entry_id,relation_type)
        );

        CREATE TABLE comments (
          id UUID PRIMARY KEY, clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          entry_id UUID NOT NULL, entry_version_id UUID NOT NULL, parent_id UUID,
          author_id UUID NOT NULL REFERENCES users(id), body_ciphertext BYTEA NOT NULL,
          start_offset INTEGER, end_offset INTEGER, exact_quote_ciphertext BYTEA,
          prefix_ciphertext BYTEA, suffix_ciphertext BYTEA, quote_sha256 VARCHAR(64),
          anchor_state VARCHAR(20) NOT NULL DEFAULT 'resolved', review_required BOOLEAN NOT NULL DEFAULT false,
          assigned_membership_id UUID REFERENCES clinic_memberships(id), resolved_at TIMESTAMPTZ,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_comment_clinic_id UNIQUE(clinic_id,id),
          CONSTRAINT fk_comment_entry FOREIGN KEY(clinic_id,entry_id)
            REFERENCES entries(clinic_id,id) ON DELETE CASCADE,
          CONSTRAINT fk_comment_version FOREIGN KEY(clinic_id,entry_version_id)
            REFERENCES entry_versions(clinic_id,id),
          CONSTRAINT fk_comment_parent FOREIGN KEY(parent_id) REFERENCES comments(id)
        );
        CREATE INDEX ix_comment_entry_status ON comments(entry_id,resolved_at);

        CREATE TABLE comment_mentions (
          id UUID PRIMARY KEY, clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          comment_id UUID NOT NULL REFERENCES comments(id) ON DELETE CASCADE,
          mentioned_user_id UUID NOT NULL REFERENCES users(id), created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_comment_mention UNIQUE(clinic_id,comment_id,mentioned_user_id)
        );

        CREATE TABLE care_tasks (
          id UUID PRIMARY KEY, clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          patient_id UUID NOT NULL, comment_id UUID REFERENCES comments(id),
          assignee_membership_id UUID NOT NULL REFERENCES clinic_memberships(id),
          status VARCHAR(20) NOT NULL DEFAULT 'open', title_ciphertext BYTEA NOT NULL,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT fk_task_patient FOREIGN KEY(clinic_id,patient_id)
            REFERENCES patients(clinic_id,id) ON DELETE CASCADE
        );

        CREATE TABLE highlights (
          id UUID PRIMARY KEY, clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          patient_id UUID NOT NULL, entry_id UUID NOT NULL, source_entry_version_id UUID NOT NULL,
          label_ciphertext BYTEA NOT NULL, status VARCHAR(20) NOT NULL DEFAULT 'pending',
          pinned BOOLEAN NOT NULL DEFAULT false, critical BOOLEAN NOT NULL DEFAULT false,
          patient_facing BOOLEAN NOT NULL DEFAULT false, anchor_state VARCHAR(20) NOT NULL DEFAULT 'resolved',
          review_required BOOLEAN NOT NULL DEFAULT false, created_by_id UUID NOT NULL REFERENCES users(id),
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_highlight_clinic_id UNIQUE(clinic_id,id),
          CONSTRAINT fk_highlight_patient FOREIGN KEY(clinic_id,patient_id)
            REFERENCES patients(clinic_id,id) ON DELETE CASCADE,
          CONSTRAINT fk_highlight_entry FOREIGN KEY(clinic_id,entry_id)
            REFERENCES entries(clinic_id,id) ON DELETE CASCADE,
          CONSTRAINT fk_highlight_version FOREIGN KEY(clinic_id,source_entry_version_id)
            REFERENCES entry_versions(clinic_id,id)
        );
        CREATE INDEX ix_highlight_patient_status ON highlights(patient_id,status,pinned);

        CREATE TABLE provenance_pointers (
          id UUID PRIMARY KEY, clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          highlight_id UUID REFERENCES highlights(id) ON DELETE CASCADE,
          comment_id UUID REFERENCES comments(id) ON DELETE CASCADE, entry_version_id UUID NOT NULL,
          start_offset INTEGER NOT NULL, end_offset INTEGER NOT NULL,
          exact_quote_ciphertext BYTEA NOT NULL, prefix_ciphertext BYTEA NOT NULL,
          suffix_ciphertext BYTEA NOT NULL, quote_sha256 VARCHAR(64) NOT NULL,
          anchor_state VARCHAR(20) NOT NULL DEFAULT 'resolved', review_required BOOLEAN NOT NULL DEFAULT false,
          audio_asset_id UUID, audio_start_ms INTEGER, audio_end_ms INTEGER,
          created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_provenance_clinic_id UNIQUE(clinic_id,id),
          CONSTRAINT fk_provenance_version FOREIGN KEY(clinic_id,entry_version_id)
            REFERENCES entry_versions(clinic_id,id)
        );
        CREATE INDEX ix_provenance_version_span ON provenance_pointers(entry_version_id,start_offset);

        CREATE TABLE conflict_cases (
          id UUID PRIMARY KEY, clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          patient_id UUID NOT NULL, left_entry_id UUID NOT NULL, right_entry_id UUID NOT NULL,
          status VARCHAR(20) NOT NULL DEFAULT 'unresolved', created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT fk_conflict_patient FOREIGN KEY(clinic_id,patient_id)
            REFERENCES patients(clinic_id,id) ON DELETE CASCADE,
          CONSTRAINT fk_conflict_left FOREIGN KEY(clinic_id,left_entry_id)
            REFERENCES entries(clinic_id,id),
          CONSTRAINT fk_conflict_right FOREIGN KEY(clinic_id,right_entry_id)
            REFERENCES entries(clinic_id,id)
        );

        CREATE TABLE audit_events (
          id UUID PRIMARY KEY, clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          actor_id UUID NOT NULL REFERENCES users(id), action VARCHAR(80) NOT NULL,
          resource_type VARCHAR(40) NOT NULL, resource_id UUID NOT NULL,
          metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_audit_clinic_created ON audit_events(clinic_id,created_at);
        CREATE INDEX ix_audit_resource ON audit_events(resource_type,resource_id);

        CREATE TABLE patient_glance_snapshots (
          id UUID PRIMARY KEY, clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          patient_id UUID NOT NULL, payload_ciphertext BYTEA NOT NULL,
          source_event_sequence BIGINT, generated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          CONSTRAINT uq_glance_patient UNIQUE(clinic_id,patient_id),
          CONSTRAINT fk_glance_patient FOREIGN KEY(clinic_id,patient_id)
            REFERENCES patients(clinic_id,id) ON DELETE CASCADE
        );

        CREATE TABLE domain_events (
          sequence_no BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
          id UUID NOT NULL UNIQUE, clinic_id UUID NOT NULL REFERENCES clinics(id) ON DELETE CASCADE,
          event_type VARCHAR(100) NOT NULL, aggregate_type VARCHAR(40) NOT NULL,
          aggregate_id UUID NOT NULL, actor_id UUID NOT NULL REFERENCES users(id),
          payload_json JSONB NOT NULL DEFAULT '{}'::jsonb, created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE INDEX ix_domain_event_clinic_sequence ON domain_events(clinic_id,sequence_no);
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


def downgrade() -> None:
    for table in reversed(TENANT_TABLES):
        op.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    op.execute("DROP TABLE IF EXISTS users CASCADE")
    op.execute("DROP TABLE IF EXISTS clinics CASCADE")
    op.execute(
        """
        CREATE TABLE "user" (
          id UUID PRIMARY KEY, email VARCHAR(255) NOT NULL UNIQUE,
          is_active BOOLEAN NOT NULL DEFAULT true, is_superuser BOOLEAN NOT NULL DEFAULT false,
          full_name VARCHAR(255), hashed_password VARCHAR NOT NULL, created_at TIMESTAMPTZ
        );
        CREATE TABLE item (
          id UUID PRIMARY KEY, title VARCHAR(255) NOT NULL, description VARCHAR(255),
          owner_id UUID NOT NULL REFERENCES "user"(id) ON DELETE CASCADE, created_at TIMESTAMPTZ
        );
        """
    )
