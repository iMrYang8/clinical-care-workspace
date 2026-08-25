"""add formal entry type and clinical occurrence time

Revision ID: f9127d3b4c50
Revises: ee8a2f6b9010
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f9127d3b4c50"
down_revision: str | None = "ee8a2f6b9010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "entries",
        sa.Column("entry_type", sa.String(length=60), nullable=True),
    )
    op.add_column(
        "entries",
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Preserve the clinical date of every legacy entry. Using migration time
    # here would rewrite the timeline whenever an existing installation
    # upgrades.
    op.execute("UPDATE entries SET occurred_at = created_at")

    # Human-authored entries are classified from the trusted, server-owned
    # section. Unknown combinations deliberately remain NULL and make the
    # migration fail below rather than being mislabeled as a system record.
    op.execute(
        """
        UPDATE entries
        SET entry_type = CASE section
            WHEN 'staff' THEN 'manual_staff_note'
            WHEN 'clinician' THEN 'manual_clinician_note'
            WHEN 'patient' THEN 'manual_patient_insight'
        END
        WHERE origin = 'human'
        """
    )

    # AI summaries are derived only from the immutable AIRun relationship and
    # its checked interaction type. A title or client-provided hint is never
    # trusted for this backfill.
    op.execute(
        """
        UPDATE entries AS entry
        SET entry_type = CASE run.interaction_type
            WHEN 'doctor_consult' THEN 'ai_doctor_consult_summary'
            WHEN 'care_note' THEN 'ai_nurse_consult_summary'
            WHEN 'patient_insight' THEN 'ai_patient_session_summary'
            WHEN 'voice_session' THEN 'ai_patient_session_summary'
        END
        FROM ai_runs AS run
        WHERE run.output_entry_id = entry.id
          AND run.clinic_id = entry.clinic_id
          AND entry.origin = 'ai'
        """
    )

    # Publishing a reviewed voice result is the strongest available
    # classification signal and therefore overrides any earlier AI mapping.
    op.execute(
        """
        UPDATE entries AS entry
        SET entry_type = 'voice_reviewed_result'
        FROM voice_sessions AS voice
        WHERE voice.published_entry_id = entry.id
          AND voice.clinic_id = entry.clinic_id
        """
    )

    # Worker-owned intermediate voice entries can be identified by their
    # tenant-safe source job. Other system rows retain the generic record type.
    op.execute(
        """
        UPDATE entries AS entry
        SET entry_type = 'voice_transcript_source'
        FROM jobs AS job
        WHERE entry.entry_type IS NULL
          AND entry.origin = 'system'
          AND entry.source_job_id = job.id
          AND entry.clinic_id = job.clinic_id
          AND entry.patient_id = job.patient_id
          AND job.kind IN ('voice_process', 'voice_reanalyze')
        """
    )
    op.execute(
        """
        UPDATE entries
        SET entry_type = 'system_record'
        WHERE entry_type IS NULL AND origin = 'system'
        """
    )

    # An unlinked AI row or an unsupported human section needs an explicit
    # operator review. Abort the upgrade instead of silently inventing its
    # provenance.
    op.execute(
        """
        DO $$
        DECLARE
            unresolved_count BIGINT;
        BEGIN
            SELECT count(*) INTO unresolved_count
            FROM entries
            WHERE entry_type IS NULL OR occurred_at IS NULL;

            IF unresolved_count > 0 THEN
                RAISE EXCEPTION
                    'entry metadata backfill requires review for % legacy row(s)',
                    unresolved_count;
            END IF;
        END
        $$
        """
    )

    op.alter_column("entries", "entry_type", nullable=False)
    op.alter_column("entries", "occurred_at", nullable=False)
    op.create_index("ix_entries_entry_type", "entries", ["entry_type"])
    op.create_index("ix_entries_occurred_at", "entries", ["occurred_at"])


def downgrade() -> None:
    op.drop_index("ix_entries_occurred_at", table_name="entries")
    op.drop_index("ix_entries_entry_type", table_name="entries")
    op.drop_column("entries", "occurred_at")
    op.drop_column("entries", "entry_type")
