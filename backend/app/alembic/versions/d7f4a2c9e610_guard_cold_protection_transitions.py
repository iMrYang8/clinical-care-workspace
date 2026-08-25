"""Guard cold payloads from later protection transitions.

Revision ID: d7f4a2c9e610
Revises: b5e7a9c2d140
"""

from collections.abc import Sequence

from alembic import op

revision: str = "d7f4a2c9e610"
down_revision: str | None = "b5e7a9c2d140"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        -- Freeze both sides of the invariant until the preflight and trigger
        -- installation commit. Without these locks, a legacy writer could
        -- create cold+protected state after the scan but before CREATE TRIGGER.
        LOCK TABLE entry_versions, highlights, conflict_cases, care_tasks
          IN SHARE ROW EXCLUSIVE MODE;

        DO $$
        BEGIN
          -- Earlier releases allowed a version to become protected after it
          -- was archived. Installing prospective guards over such rows would
          -- leave the database in a state the new invariant says is invalid.
          -- Stop the upgrade so an operator can rehydrate first; never hide a
          -- protected source behind a cold payload.
          IF EXISTS (
            SELECT 1
            FROM highlights AS h
            JOIN entry_versions AS v
              ON v.clinic_id = h.clinic_id
             AND v.id = h.source_entry_version_id
            WHERE v.storage_tier = 'cold'
              AND (h.critical OR h.unresolved OR h.pinned
                   OR h.clinician_confirmed)
          ) OR EXISTS (
            SELECT 1
            FROM conflict_cases AS c
            JOIN entry_versions AS v
              ON v.clinic_id = c.clinic_id
             AND v.entry_id IN (c.left_entry_id, c.right_entry_id)
            WHERE c.status = 'unresolved' AND v.storage_tier = 'cold'
          ) OR EXISTS (
            SELECT 1
            FROM care_tasks AS t
            JOIN entries AS e
              ON e.clinic_id = t.clinic_id AND e.patient_id = t.patient_id
            JOIN entry_versions AS v
              ON v.clinic_id = e.clinic_id AND v.entry_id = e.id
            WHERE t.status <> 'completed' AND v.storage_tier = 'cold'
          ) THEN
            RAISE EXCEPTION
              'rehydrate protected cold entry versions before applying d7f4a2c9e610'
              USING ERRCODE = '55000';
          END IF;
        END;
        $$;

        CREATE FUNCTION nightingale_guard_highlight_cold_source()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          source_tier VARCHAR(10);
        BEGIN
          IF TG_OP = 'UPDATE'
             AND NEW.source_entry_version_id IS NOT DISTINCT FROM
                 OLD.source_entry_version_id
             AND NOT (
               (NEW.critical AND NOT OLD.critical)
               OR (NEW.unresolved AND NOT OLD.unresolved)
               OR (NEW.pinned AND NOT OLD.pinned)
               OR (NEW.clinician_confirmed AND NOT OLD.clinician_confirmed)
             ) THEN
            RETURN NEW;
          END IF;

          SELECT storage_tier INTO source_tier
          FROM entry_versions
          WHERE clinic_id = NEW.clinic_id
            AND id = NEW.source_entry_version_id
          FOR KEY SHARE;

          -- Every new anchor must validate against active content. Existing
          -- anchors may remain cold only while they do not protect retention.
          IF source_tier = 'cold' THEN
            RAISE EXCEPTION 'highlight protection requires rehydrated source'
              USING ERRCODE = '55000';
          END IF;
          RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_highlight_cold_source
        BEFORE INSERT OR UPDATE OF critical, unresolved, pinned,
          clinician_confirmed, source_entry_version_id
        ON highlights
        FOR EACH ROW EXECUTE FUNCTION nightingale_guard_highlight_cold_source();

        CREATE FUNCTION nightingale_guard_conflict_cold_source()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          cold_exists BOOLEAN;
        BEGIN
          IF NEW.status = 'unresolved' THEN
            -- Entry row locks serialize both new and reopened conflicts with
            -- archive_version, which locks each source entry before rechecking.
            PERFORM id FROM entries
            WHERE clinic_id = NEW.clinic_id
              AND id IN (NEW.left_entry_id, NEW.right_entry_id)
            ORDER BY id
            FOR KEY SHARE;
            SELECT EXISTS (
              SELECT 1 FROM entry_versions
              WHERE clinic_id = NEW.clinic_id
                AND entry_id IN (NEW.left_entry_id, NEW.right_entry_id)
                AND storage_tier = 'cold'
            ) INTO cold_exists;
            IF cold_exists THEN
              RAISE EXCEPTION 'unresolved conflict requires rehydrated sources'
                USING ERRCODE = '55000';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_conflict_cold_source
        BEFORE INSERT OR UPDATE OF status, left_entry_id, right_entry_id
        ON conflict_cases
        FOR EACH ROW EXECUTE FUNCTION nightingale_guard_conflict_cold_source();

        CREATE FUNCTION nightingale_guard_task_cold_source()
        RETURNS trigger LANGUAGE plpgsql AS $$
        DECLARE
          cold_exists BOOLEAN;
        BEGIN
          IF NEW.status <> 'completed' THEN
            -- The patient lock matches archive_version's lock before its task
            -- scan and closes both INSERT and completed -> open races.
            PERFORM id FROM patients
            WHERE clinic_id = NEW.clinic_id AND id = NEW.patient_id
            FOR KEY SHARE;
            SELECT EXISTS (
              SELECT 1
              FROM entry_versions AS v
              JOIN entries AS e
                ON e.clinic_id = v.clinic_id AND e.id = v.entry_id
              WHERE e.clinic_id = NEW.clinic_id
                AND e.patient_id = NEW.patient_id
                AND v.storage_tier = 'cold'
            ) INTO cold_exists;
            IF cold_exists THEN
              RAISE EXCEPTION 'open care task requires rehydrated patient content'
                USING ERRCODE = '55000';
            END IF;
          END IF;
          RETURN NEW;
        END;
        $$;

        CREATE TRIGGER trg_task_cold_source
        BEFORE INSERT OR UPDATE OF status, patient_id
        ON care_tasks
        FOR EACH ROW EXECUTE FUNCTION nightingale_guard_task_cold_source();
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DROP TRIGGER IF EXISTS trg_task_cold_source ON care_tasks;
        DROP FUNCTION IF EXISTS nightingale_guard_task_cold_source();
        DROP TRIGGER IF EXISTS trg_conflict_cold_source ON conflict_cases;
        DROP FUNCTION IF EXISTS nightingale_guard_conflict_cold_source();
        DROP TRIGGER IF EXISTS trg_highlight_cold_source ON highlights;
        DROP FUNCTION IF EXISTS nightingale_guard_highlight_cold_source();
        """
    )
