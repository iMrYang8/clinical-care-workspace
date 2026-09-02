"""Bind actor/patient context, protect identities, and expose safe projections.

Revision ID: e3b8c54d9e12
Revises: e2a7b43c8d01
"""

from collections.abc import Sequence

from alembic import op

revision: str = "e3b8c54d9e12"
down_revision: str | None = "e2a7b43c8d01"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "nightingale_app"

NEW_TENANT_TABLES = (
    "patient_access_credentials",
    "patient_otp_challenges",
    "clinic_operational_settings",
    "notification_outbox",
    "notification_attempts",
    "notification_receipts",
    "patient_publication_acknowledgements",
    "publication_correction_outreaches",
    "patient_portal_events",
    "provider_circuit_states",
    "importance_candidate_exposures",
    "highlight_support_reviews",
    "provisional_safety_alerts",
)

DIRECT_PATIENT_TABLES: dict[str, str] = {
    "patients": "id",
    "patient_identifiers": "patient_id",
    "patient_portal_invitations": "patient_id",
    "patient_visits": "patient_id",
    "patient_access_credentials": "patient_id",
    "entries": "patient_id",
    "care_tasks": "patient_id",
    "highlights": "patient_id",
    "conflict_cases": "patient_id",
    "patient_glance_snapshots": "patient_id",
    "jobs": "patient_id",
    "ai_runs": "patient_id",
    "importance_impressions": "patient_id",
    "importance_candidate_exposures": "patient_id",
    "clinical_fact_assertions": "patient_id",
    "patient_sharing_requests": "patient_id",
    "patient_publications": "patient_id",
    "voice_sessions": "patient_id",
    "notification_outbox": "patient_id",
    "patient_publication_acknowledgements": "patient_id",
    "publication_correction_outreaches": "patient_id",
    "patient_portal_events": "patient_id",
    "highlight_support_reviews": "patient_id",
    "provisional_safety_alerts": "patient_id",
}

ENTRY_CHILD_TABLES: dict[str, str] = {
    "entry_versions": "entry_id",
    "entry_relations": "source_entry_id",
    "comments": "entry_id",
}

VERSION_CHILD_TABLES: dict[str, str] = {
    "redaction_runs": "source_entry_version_id",
    "archive_blobs": "entry_version_id",
}

HIGHLIGHT_CHILD_TABLES: dict[str, str] = {
    "importance_feedback_events": "highlight_id",
    "decision_assessments": "highlight_id",
}

JOB_CHILD_TABLES: dict[str, str] = {"job_attempts": "job_id"}

SESSION_CHILD_TABLES: dict[str, str] = {
    "voice_devices": "session_id",
    "audio_chunks": "session_id",
    "audio_assets": "session_id",
    "transcript_revisions": "session_id",
    "transcript_segments": "session_id",
    "clinical_facts": "session_id",
}

NON_PATIENT_TABLES = (
    "clinic_ai_settings",
    "importance_feature_stats",
    "evaluation_runs",
    "calibration_reports",
    "calibration_buckets",
    "redaction_evaluation_runs",
    "decay_runs",
    "retention_locks",
    "provider_circuit_states",
)

PATIENT_ACTOR_EVENT_TABLES = ("audit_events", "domain_events")


def _new_tenant_policy(table: str) -> None:
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


def _restrictive_policy(table: str, expression: str) -> None:
    op.execute(
        f"""
        CREATE POLICY patient_scope ON "{table}" AS RESTRICTIVE
        USING ({expression})
        WITH CHECK ({expression})
        """
    )


def upgrade() -> None:
    # These helpers centralize GUC parsing and keep every policy fail-closed on
    # malformed or missing context.  A patient login bootstrap has no actor yet,
    # but it must already carry the signed clinic and linked patient context.
    op.execute(
        """
        CREATE FUNCTION app_context_allows(p_clinic_id uuid)
        RETURNS boolean
        LANGUAGE sql STABLE SECURITY INVOKER
        SET search_path = pg_catalog, public
        AS $$
          SELECT CASE
            WHEN NULLIF(current_setting('app.current_clinic_id', true), '') IS NULL
              OR NULLIF(current_setting('app.current_actor_role', true), '') IS NULL
            THEN false
            WHEN current_setting('app.current_clinic_id', true)
                   !~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
            THEN false
            WHEN current_setting('app.current_actor_role', true) NOT IN
                   ('patient','staff','clinician','admin','worker','platform_admin')
            THEN false
            WHEN p_clinic_id <> current_setting('app.current_clinic_id', true)::uuid
            THEN false
            WHEN current_setting('app.current_actor_role', true) = 'patient'
            THEN current_setting('app.current_patient_id', true)
                   ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                 AND (
                   NULLIF(current_setting('app.current_actor_id', true), '') IS NULL
                   OR current_setting('app.current_actor_id', true)
                        ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
                 )
            ELSE NULLIF(current_setting('app.current_actor_id', true), '') IS NOT NULL
                 AND current_setting('app.current_actor_id', true)
                       ~* '^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$'
          END
        $$;

        CREATE FUNCTION app_nonpatient_context_allows(p_clinic_id uuid)
        RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT CASE
            WHEN NOT app_context_allows(p_clinic_id) THEN false
            WHEN current_setting('app.current_actor_role', true) = 'platform_admin'
              THEN EXISTS (
                SELECT 1
                FROM public.users AS identity
                JOIN public.platform_administrators AS administrator
                  ON administrator.user_id = identity.id
                 AND administrator.is_active
                WHERE identity.id = NULLIF(
                        current_setting('app.current_actor_id', true), ''
                      )::uuid
                  AND identity.is_active
              )
            WHEN current_setting('app.current_actor_role', true) IN
                   ('staff','clinician','admin','worker')
              THEN EXISTS (
                SELECT 1
                FROM public.users AS identity
                JOIN public.clinic_memberships AS membership
                  ON membership.user_id = identity.id
                 AND membership.clinic_id = p_clinic_id
                 AND membership.is_active
                WHERE identity.id = NULLIF(
                        current_setting('app.current_actor_id', true), ''
                      )::uuid
                  AND identity.is_active
                  AND (
                    (
                      current_setting('app.current_actor_role', true) = 'worker'
                      AND identity.account_kind = 'service'
                    )
                    OR (
                      current_setting('app.current_actor_role', true) IN
                        ('staff','clinician','admin')
                      AND identity.account_kind = 'staff'
                    )
                  )
                  AND membership.role = current_setting(
                        'app.current_actor_role', true
                      )
              )
            ELSE false
          END
        $$;

        CREATE FUNCTION app_patient_context_allows(
          p_clinic_id uuid, p_patient_id uuid
        ) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT CASE
            WHEN NOT app_context_allows(p_clinic_id) THEN false
            WHEN current_setting('app.current_actor_role', true) <> 'patient'
              THEN app_nonpatient_context_allows(p_clinic_id)
            WHEN p_patient_id <> NULLIF(
                   current_setting('app.current_patient_id', true), ''
                 )::uuid
              THEN false
            -- Exact-secret lookup helpers bind clinic/patient first so legacy
            -- invitation previews can resolve before an identity exists.
            WHEN NULLIF(current_setting('app.current_actor_id', true), '') IS NULL
              THEN true
            ELSE EXISTS (
              SELECT 1
              FROM public.users AS identity
              JOIN public.clinic_memberships AS membership
                ON membership.user_id = identity.id
               AND membership.clinic_id = p_clinic_id
               AND membership.is_active
               AND membership.role = 'patient'
              JOIN public.patient_user_links AS link
                ON link.user_id = identity.id
               AND link.clinic_id = p_clinic_id
               AND link.patient_id = p_patient_id
              WHERE identity.id = NULLIF(
                      current_setting('app.current_actor_id', true), ''
                    )::uuid
                AND identity.is_active
                AND identity.account_kind = 'patient'
            )
          END
        $$;

        CREATE FUNCTION app_patient_actor_context_allows(p_clinic_id uuid)
        RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT CASE
            WHEN NOT app_context_allows(p_clinic_id) THEN false
            WHEN current_setting('app.current_actor_role', true) <> 'patient'
              THEN false
            ELSE app_patient_context_allows(
              p_clinic_id,
              current_setting('app.current_patient_id', true)::uuid
            )
          END
        $$;

        CREATE FUNCTION app_patient_membership_bootstrap_allows(
          p_clinic_id uuid, p_user_id uuid, p_role text
        ) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT
            app_context_allows(p_clinic_id)
            AND current_setting('app.current_actor_role', true) = 'patient'
            AND p_role = 'patient'
            AND p_user_id::text = current_setting('app.current_actor_id', true)
            AND EXISTS (
              SELECT 1
              FROM public.users AS identity
              WHERE identity.id = p_user_id
                AND identity.is_active
                AND identity.account_kind = 'patient'
            )
        $$;
        """
    )

    # Boolean, SECURITY DEFINER relationship checks expose no clinical payload
    # and prevent child tables from becoming a route-filter escape hatch.
    op.execute(
        """
        CREATE FUNCTION app_entry_context_allows(p_clinic_id uuid, p_entry_id uuid)
        RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT CASE
            WHEN app_nonpatient_context_allows(p_clinic_id) THEN true
            ELSE EXISTS (
              SELECT 1 FROM public.entries AS entry
              WHERE entry.clinic_id = p_clinic_id
                AND entry.id = p_entry_id
                AND app_patient_context_allows(entry.clinic_id, entry.patient_id)
            )
          END
        $$;

        CREATE FUNCTION app_version_context_allows(
          p_clinic_id uuid, p_version_id uuid
        ) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT CASE
            WHEN app_nonpatient_context_allows(p_clinic_id) THEN true
            ELSE EXISTS (
              SELECT 1
              FROM public.entry_versions AS version
              JOIN public.entries AS entry
                ON entry.clinic_id = version.clinic_id
               AND entry.id = version.entry_id
              WHERE version.clinic_id = p_clinic_id
                AND version.id = p_version_id
                AND app_patient_context_allows(entry.clinic_id, entry.patient_id)
            )
          END
        $$;

        CREATE FUNCTION app_highlight_context_allows(
          p_clinic_id uuid, p_highlight_id uuid
        ) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT CASE
            WHEN app_nonpatient_context_allows(p_clinic_id) THEN true
            ELSE EXISTS (
              SELECT 1 FROM public.highlights AS highlight
              WHERE highlight.clinic_id = p_clinic_id
                AND highlight.id = p_highlight_id
                AND app_patient_context_allows(
                      highlight.clinic_id, highlight.patient_id
                    )
            )
          END
        $$;

        CREATE FUNCTION app_job_context_allows(p_clinic_id uuid, p_job_id uuid)
        RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT CASE
            WHEN app_nonpatient_context_allows(p_clinic_id) THEN true
            ELSE EXISTS (
              SELECT 1 FROM public.jobs AS job
              WHERE job.clinic_id = p_clinic_id
                AND job.id = p_job_id
                AND app_patient_context_allows(job.clinic_id, job.patient_id)
            )
          END
        $$;

        CREATE FUNCTION app_voice_session_context_allows(
          p_clinic_id uuid, p_session_id uuid
        ) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT CASE
            WHEN app_nonpatient_context_allows(p_clinic_id) THEN true
            ELSE EXISTS (
              SELECT 1 FROM public.voice_sessions AS voice_session
              WHERE voice_session.clinic_id = p_clinic_id
                AND voice_session.id = p_session_id
                AND app_patient_context_allows(
                      voice_session.clinic_id, voice_session.patient_id
                    )
            )
          END
        $$;

        CREATE FUNCTION app_notification_context_allows(
          p_clinic_id uuid, p_notification_id uuid
        ) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT CASE
            WHEN app_nonpatient_context_allows(p_clinic_id) THEN true
            ELSE EXISTS (
              SELECT 1 FROM public.notification_outbox AS notification
              WHERE notification.clinic_id = p_clinic_id
                AND notification.id = p_notification_id
                AND notification.patient_id IS NOT NULL
                AND app_patient_context_allows(
                      notification.clinic_id, notification.patient_id
                    )
            )
          END
        $$;

        CREATE FUNCTION app_publication_context_allows(
          p_clinic_id uuid, p_publication_id uuid
        ) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT CASE
            WHEN app_nonpatient_context_allows(p_clinic_id) THEN true
            ELSE EXISTS (
              SELECT 1 FROM public.patient_publications AS publication
              WHERE publication.clinic_id = p_clinic_id
                AND publication.id = p_publication_id
                AND app_patient_context_allows(
                      publication.clinic_id, publication.patient_id
                    )
            )
          END
        $$;

        CREATE FUNCTION app_credential_context_allows(
          p_clinic_id uuid, p_credential_id uuid
        ) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT CASE
            WHEN app_nonpatient_context_allows(p_clinic_id) THEN true
            ELSE EXISTS (
              SELECT 1 FROM public.patient_access_credentials AS credential
              WHERE credential.clinic_id = p_clinic_id
                AND credential.id = p_credential_id
                AND app_patient_context_allows(
                      credential.clinic_id, credential.patient_id
                    )
            )
          END
        $$;

        CREATE FUNCTION app_comment_context_allows(
          p_clinic_id uuid, p_comment_id uuid
        ) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT CASE
            WHEN app_nonpatient_context_allows(p_clinic_id) THEN true
            ELSE EXISTS (
              SELECT 1 FROM public.comments AS comment
              WHERE comment.clinic_id = p_clinic_id
                AND comment.id = p_comment_id
                AND app_entry_context_allows(comment.clinic_id, comment.entry_id)
            )
          END
        $$;

        CREATE FUNCTION app_pointer_context_allows(
          p_clinic_id uuid, p_pointer_id uuid
        ) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT CASE
            WHEN app_nonpatient_context_allows(p_clinic_id) THEN true
            ELSE EXISTS (
              SELECT 1 FROM public.provenance_pointers AS pointer
              WHERE pointer.clinic_id = p_clinic_id
                AND pointer.id = p_pointer_id
                AND (
                  (pointer.highlight_id IS NOT NULL AND app_highlight_context_allows(
                    pointer.clinic_id, pointer.highlight_id
                  ))
                  OR (pointer.comment_id IS NOT NULL AND app_comment_context_allows(
                    pointer.clinic_id, pointer.comment_id
                  ))
                  OR (pointer.clinical_fact_id IS NOT NULL AND EXISTS (
                    SELECT 1 FROM public.clinical_facts AS fact
                    WHERE fact.clinic_id = pointer.clinic_id
                      AND fact.id = pointer.clinical_fact_id
                      AND app_voice_session_context_allows(
                        fact.clinic_id, fact.session_id
                      )
                  ))
                )
            )
          END
        $$;
        """
    )

    # Exact-secret bootstrap lookups return identifiers only.  The API binds
    # those values into transaction-local GUCs before any ordinary ORM query.
    op.execute(
        """
        CREATE FUNCTION app_clinic_invitation_context_allows(
          p_clinic_id uuid, p_invitation_id uuid
        ) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT
            p_clinic_id::text = NULLIF(
              current_setting('app.current_clinic_id', true), ''
            )
            AND current_setting('app.current_invitation_token_hash', true)
                  ~ '^[0-9a-f]{64}$'
            AND EXISTS (
              SELECT 1
              FROM public.clinic_invitations AS invitation
              WHERE invitation.clinic_id = p_clinic_id
                AND invitation.id = p_invitation_id
                AND invitation.token_hash = current_setting(
                      'app.current_invitation_token_hash', true
                    )
                AND invitation.accepted_at IS NULL
                AND invitation.revoked_at IS NULL
                AND invitation.expires_at > now()
                AND (
                  EXISTS (
                    SELECT 1
                    FROM public.clinic_memberships AS creator
                    JOIN public.users AS creator_identity
                      ON creator_identity.id = creator.user_id
                     AND creator_identity.is_active
                    WHERE creator.clinic_id = invitation.clinic_id
                      AND creator.id = invitation.created_by_membership_id
                      AND creator.is_active
                      AND creator.role = 'admin'
                  )
                  OR EXISTS (
                    SELECT 1
                    FROM public.platform_administrators AS creator
                    JOIN public.users AS creator_identity
                      ON creator_identity.id = creator.user_id
                     AND creator_identity.is_active
                    WHERE creator.id = invitation.created_by_platform_admin_id
                      AND creator.is_active
                  )
                )
            )
        $$;

        CREATE FUNCTION app_invitation_membership_bootstrap_allows(
          p_clinic_id uuid, p_user_id uuid, p_role text
        ) RETURNS boolean
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT
            p_user_id::text = NULLIF(
              current_setting('app.current_actor_id', true), ''
            )
            AND p_role = NULLIF(
              current_setting('app.current_actor_role', true), ''
            )
            AND p_role IN ('staff','clinician','admin')
            AND EXISTS (
              SELECT 1
              FROM public.users AS identity
              JOIN public.clinic_invitations AS invitation
                ON invitation.clinic_id = p_clinic_id
               AND lower(invitation.email) = lower(identity.email)
              WHERE identity.id = p_user_id
                AND identity.is_active
                AND identity.account_kind = 'staff'
                AND invitation.role = p_role
                AND app_clinic_invitation_context_allows(
                      invitation.clinic_id, invitation.id
                    )
            )
        $$;

        CREATE FUNCTION app_lookup_clinic_user(
          p_clinic_code text, p_email text
        ) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT identity.id
          FROM public.users AS identity
          JOIN public.clinic_memberships AS membership
            ON membership.user_id = identity.id
           AND membership.is_active
          JOIN public.clinics AS clinic
            ON clinic.id = membership.clinic_id
          WHERE clinic.code = upper(btrim(p_clinic_code))
            AND lower(identity.email) = lower(btrim(p_email))
            AND identity.account_kind IN ('staff','patient','service')
            AND identity.is_active
          ORDER BY identity.id
          LIMIT 1
        $$;

        CREATE FUNCTION app_lookup_clinic_worker(p_clinic_id uuid)
        RETURNS TABLE(user_id uuid, membership_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT identity.id, membership.id
          FROM public.users AS identity
          JOIN public.clinic_memberships AS membership
            ON membership.user_id = identity.id
           AND membership.clinic_id = p_clinic_id
           AND membership.is_active
           AND membership.role = 'worker'
          WHERE identity.is_active
            AND identity.account_kind = 'service'
          ORDER BY membership.created_at, membership.id
          LIMIT 1
        $$;

        CREATE FUNCTION app_lookup_clinic_invitation(
          p_clinic_id uuid, p_token_hash text, p_email text
        ) RETURNS TABLE(
          invitation_id uuid, role text, existing_user_id uuid
        )
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT invitation.id,
                 invitation.role::text,
                 existing_identity.id
          FROM public.clinic_invitations AS invitation
          LEFT JOIN public.users AS existing_identity
            ON lower(existing_identity.email) = lower(btrim(p_email))
           AND existing_identity.is_active
          WHERE invitation.clinic_id = p_clinic_id
            AND invitation.token_hash = p_token_hash
            AND lower(invitation.email) = lower(btrim(p_email))
            AND invitation.accepted_at IS NULL
            AND invitation.revoked_at IS NULL
            AND invitation.expires_at > now()
            AND (
              EXISTS (
                SELECT 1
                FROM public.clinic_memberships AS creator
                JOIN public.users AS creator_identity
                  ON creator_identity.id = creator.user_id
                 AND creator_identity.is_active
                WHERE creator.clinic_id = invitation.clinic_id
                  AND creator.id = invitation.created_by_membership_id
                  AND creator.is_active
                  AND creator.role = 'admin'
              )
              OR EXISTS (
                SELECT 1
                FROM public.platform_administrators AS creator
                JOIN public.users AS creator_identity
                  ON creator_identity.id = creator.user_id
                 AND creator_identity.is_active
                WHERE creator.id = invitation.created_by_platform_admin_id
                  AND creator.is_active
              )
            )
          LIMIT 1
        $$;

        CREATE FUNCTION app_consume_clinic_invitation(
          p_clinic_id uuid,
          p_token_hash text,
          p_email text,
          p_user_id uuid,
          p_membership_id uuid
        ) RETURNS uuid
        LANGUAGE plpgsql VOLATILE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
        DECLARE
          accepted_role text;
        BEGIN
          IF NULLIF(current_setting('app.current_clinic_id', true), '')
               IS DISTINCT FROM p_clinic_id::text
             OR NULLIF(current_setting('app.current_actor_id', true), '')
               IS DISTINCT FROM p_user_id::text
          THEN
            RETURN NULL;
          END IF;

          SELECT invitation.role::text
          INTO accepted_role
          FROM public.clinic_invitations AS invitation
          WHERE invitation.clinic_id = p_clinic_id
            AND invitation.token_hash = p_token_hash
            AND lower(invitation.email) = lower(btrim(p_email))
            AND invitation.accepted_at IS NULL
            AND invitation.revoked_at IS NULL
            AND invitation.expires_at > now()
            AND invitation.role IN ('staff','clinician','admin')
            AND (
              EXISTS (
                SELECT 1
                FROM public.clinic_memberships AS creator
                JOIN public.users AS creator_identity
                  ON creator_identity.id = creator.user_id
                 AND creator_identity.is_active
                WHERE creator.clinic_id = invitation.clinic_id
                  AND creator.id = invitation.created_by_membership_id
                  AND creator.is_active
                  AND creator.role = 'admin'
              )
              OR EXISTS (
                SELECT 1
                FROM public.platform_administrators AS creator
                JOIN public.users AS creator_identity
                  ON creator_identity.id = creator.user_id
                 AND creator_identity.is_active
                WHERE creator.id = invitation.created_by_platform_admin_id
                  AND creator.is_active
              )
            )
          FOR UPDATE;

          IF accepted_role IS NULL
             OR accepted_role IS DISTINCT FROM NULLIF(
                  current_setting('app.current_actor_role', true), ''
                )
             OR NOT EXISTS (
               SELECT 1 FROM public.users AS identity
               WHERE identity.id = p_user_id
                 AND identity.is_active
                 AND identity.account_kind = 'staff'
                 AND lower(identity.email) = lower(btrim(p_email))
             )
             OR EXISTS (
               SELECT 1 FROM public.clinic_memberships AS membership
               WHERE membership.clinic_id = p_clinic_id
                 AND membership.user_id = p_user_id
             )
          THEN
            RETURN NULL;
          END IF;

          INSERT INTO public.clinic_memberships (
            id, clinic_id, user_id, role, is_active, created_at
          ) VALUES (
            p_membership_id, p_clinic_id, p_user_id, accepted_role, true, now()
          );

          UPDATE public.clinic_invitations AS invitation
          SET accepted_at = now()
          WHERE invitation.clinic_id = p_clinic_id
            AND invitation.token_hash = p_token_hash
            AND lower(invitation.email) = lower(btrim(p_email));

          RETURN p_membership_id;
        END
        $$;

        CREATE FUNCTION app_lookup_platform_user(p_email text)
        RETURNS TABLE(user_id uuid, administrator_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT identity.id, administrator.id
          FROM public.users AS identity
          JOIN public.platform_administrators AS administrator
            ON administrator.user_id = identity.id
           AND administrator.is_active
          WHERE lower(identity.email) = lower(btrim(p_email))
            AND identity.is_active
          ORDER BY administrator.id
          LIMIT 1
        $$;

        CREATE FUNCTION app_lookup_invited_user(
          p_clinic_id uuid,
          p_token_hash text,
          p_email text,
          p_invitation_kind text
        ) RETURNS uuid
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT identity.id
          FROM public.users AS identity
          WHERE lower(identity.email) = lower(btrim(p_email))
            AND identity.is_active
            AND (
              (
                p_invitation_kind = 'clinic'
                AND EXISTS (
                  SELECT 1 FROM public.clinic_invitations AS invitation
                  WHERE invitation.clinic_id = p_clinic_id
                    AND invitation.token_hash = p_token_hash
                    AND lower(invitation.email) = lower(btrim(p_email))
                    AND invitation.accepted_at IS NULL
                    AND invitation.revoked_at IS NULL
                    AND invitation.expires_at > now()
                )
              )
              OR (
                p_invitation_kind = 'patient'
                AND EXISTS (
                  SELECT 1 FROM public.patient_portal_invitations AS invitation
                  WHERE invitation.clinic_id = p_clinic_id
                    AND invitation.token_hash = p_token_hash
                    AND lower(invitation.email) = lower(btrim(p_email))
                    AND invitation.accepted_at IS NULL
                    AND invitation.revoked_at IS NULL
                    AND invitation.expires_at > now()
                )
              )
            )
          ORDER BY identity.id
          LIMIT 1
        $$;

        CREATE FUNCTION app_lookup_patient_enrollment(p_token_hash text)
        RETURNS TABLE(
          clinic_id uuid, patient_id uuid, invitation_id uuid, credential_id uuid
        )
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT invitation.clinic_id,
                 invitation.patient_id,
                 invitation.id,
                 credential.id
          FROM public.patient_portal_invitations AS invitation
          LEFT JOIN public.patient_access_credentials AS credential
            ON credential.clinic_id = invitation.clinic_id
           AND credential.invitation_id = invitation.id
          WHERE invitation.token_hash = p_token_hash
            AND invitation.accepted_at IS NULL
            AND invitation.revoked_at IS NULL
            AND invitation.expires_at > now()
            AND (
              credential.id IS NULL
              OR (credential.is_active AND credential.revoked_at IS NULL)
            )
            AND EXISTS (
              SELECT 1
              FROM public.clinic_memberships AS creator
              JOIN public.users AS creator_identity
                ON creator_identity.id = creator.user_id
               AND creator_identity.is_active
              WHERE creator.clinic_id = invitation.clinic_id
                AND creator.id = invitation.created_by_membership_id
                AND creator.is_active
                AND creator.role IN ('staff','clinician')
            )
          LIMIT 1
        $$;

        CREATE FUNCTION app_lookup_patient_portal(p_portal_id text)
        RETURNS TABLE(clinic_id uuid, patient_id uuid, credential_id uuid)
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT credential.clinic_id, credential.patient_id, credential.id
          FROM public.patient_access_credentials AS credential
          WHERE credential.portal_id = p_portal_id
            AND credential.is_active
            AND credential.revoked_at IS NULL
          LIMIT 1
        $$;

        CREATE FUNCTION app_lookup_patient_challenge(p_token_hash text)
        RETURNS TABLE(
          clinic_id uuid, patient_id uuid, credential_id uuid, challenge_id uuid
        )
        LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path = pg_catalog, public
        AS $$
          SELECT challenge.clinic_id,
                 credential.patient_id,
                 credential.id,
                 challenge.id
          FROM public.patient_otp_challenges AS challenge
          JOIN public.patient_access_credentials AS credential
            ON credential.clinic_id = challenge.clinic_id
           AND credential.id = challenge.credential_id
          WHERE challenge.challenge_token_hash = p_token_hash
            AND challenge.consumed_at IS NULL
            AND challenge.revoked_at IS NULL
            AND challenge.expires_at > now()
            AND challenge.attempts_remaining > 0
            AND credential.is_active
            AND credential.revoked_at IS NULL
          LIMIT 1
        $$;
        """
    )

    for function in (
        "app_context_allows(uuid)",
        "app_patient_context_allows(uuid,uuid)",
        "app_patient_actor_context_allows(uuid)",
        "app_patient_membership_bootstrap_allows(uuid,uuid,text)",
        "app_nonpatient_context_allows(uuid)",
        "app_entry_context_allows(uuid,uuid)",
        "app_version_context_allows(uuid,uuid)",
        "app_highlight_context_allows(uuid,uuid)",
        "app_job_context_allows(uuid,uuid)",
        "app_voice_session_context_allows(uuid,uuid)",
        "app_notification_context_allows(uuid,uuid)",
        "app_publication_context_allows(uuid,uuid)",
        "app_credential_context_allows(uuid,uuid)",
        "app_comment_context_allows(uuid,uuid)",
        "app_pointer_context_allows(uuid,uuid)",
        "app_clinic_invitation_context_allows(uuid,uuid)",
        "app_invitation_membership_bootstrap_allows(uuid,uuid,text)",
        "app_lookup_clinic_user(text,text)",
        "app_lookup_clinic_worker(uuid)",
        "app_lookup_clinic_invitation(uuid,text,text)",
        "app_consume_clinic_invitation(uuid,text,text,uuid,uuid)",
        "app_lookup_platform_user(text)",
        "app_lookup_invited_user(uuid,text,text,text)",
        "app_lookup_patient_enrollment(text)",
        "app_lookup_patient_portal(text)",
        "app_lookup_patient_challenge(text)",
    ):
        op.execute(f"REVOKE ALL ON FUNCTION {function} FROM PUBLIC")
        op.execute(f"GRANT EXECUTE ON FUNCTION {function} TO {RUNTIME_ROLE}")

    # Global user rows are no longer enumerable.  Exact login lookup happens
    # through the functions above; ordinary reads require the bound actor,
    # clinic membership, or active platform administrator actor.
    op.execute(
        """
        ALTER TABLE users ENABLE ROW LEVEL SECURITY;
        ALTER TABLE users FORCE ROW LEVEL SECURITY;
        CREATE POLICY user_identity_scope ON users
        USING (
          id::text = NULLIF(current_setting('app.current_actor_id', true), '')
          OR EXISTS (
            SELECT 1
            FROM clinic_memberships AS membership
            WHERE membership.user_id = users.id
              AND membership.is_active
              AND app_nonpatient_context_allows(membership.clinic_id)
          )
          OR EXISTS (
            SELECT 1
            FROM platform_administrators AS administrator
            WHERE administrator.user_id::text = NULLIF(
                    current_setting('app.current_actor_id', true), ''
                  )
              AND administrator.is_active
              AND current_setting('app.current_actor_role', true) = 'platform_admin'
          )
        )
        WITH CHECK (
          id::text = NULLIF(current_setting('app.current_actor_id', true), '')
          OR EXISTS (
            SELECT 1
            FROM platform_administrators AS administrator
            WHERE administrator.user_id::text = NULLIF(
                    current_setting('app.current_actor_id', true), ''
                  )
              AND administrator.is_active
              AND current_setting('app.current_actor_role', true) = 'platform_admin'
          )
        );
        """
    )

    for table in NEW_TENANT_TABLES:
        _new_tenant_policy(table)

    # Membership lookup is the only self-bootstrap: the exact email helper
    # returns a user id, then that actor may read only its own active row before
    # the application knows which role to bind.
    op.execute(
        """
        CREATE POLICY patient_scope ON clinic_memberships AS RESTRICTIVE
        USING (
          (
            clinic_id::text = NULLIF(
              current_setting('app.current_clinic_id', true), ''
            )
            AND user_id::text = NULLIF(
              current_setting('app.current_actor_id', true), ''
            )
            AND is_active
          )
          OR app_nonpatient_context_allows(clinic_id)
        )
        WITH CHECK (
          app_nonpatient_context_allows(clinic_id)
          OR app_patient_membership_bootstrap_allows(
               clinic_id, user_id, role
             )
          OR app_invitation_membership_bootstrap_allows(
               clinic_id, user_id, role
             )
        );
        """
    )

    # Legacy staff invitation acceptance still begins from a signed token and
    # clinic UUID.  Patient identities cannot enumerate the staff invitation
    # table after authentication.
    op.execute(
        """
        CREATE POLICY patient_scope ON clinic_invitations AS RESTRICTIVE
        USING (
          app_nonpatient_context_allows(clinic_id)
          OR app_clinic_invitation_context_allows(clinic_id, id)
        )
        WITH CHECK (app_nonpatient_context_allows(clinic_id));
        """
    )

    op.execute(
        """
        CREATE POLICY patient_scope ON patient_user_links AS RESTRICTIVE
        USING (
          app_nonpatient_context_allows(clinic_id)
          OR (
            current_setting('app.current_actor_role', true) = 'patient'
            AND clinic_id::text = NULLIF(
              current_setting('app.current_clinic_id', true), ''
            )
            AND user_id::text = NULLIF(
              current_setting('app.current_actor_id', true), ''
            )
            AND EXISTS (
              SELECT 1
              FROM users AS identity
              JOIN clinic_memberships AS membership
                ON membership.user_id = identity.id
               AND membership.clinic_id = patient_user_links.clinic_id
               AND membership.role = 'patient'
               AND membership.is_active
              WHERE identity.id = patient_user_links.user_id
                AND identity.is_active
                AND identity.account_kind = 'patient'
            )
            AND (
              NULLIF(current_setting('app.current_patient_id', true), '') IS NULL
              OR patient_id::text = current_setting('app.current_patient_id', true)
            )
          )
        )
        WITH CHECK (
          app_nonpatient_context_allows(clinic_id)
          OR (
            app_context_allows(clinic_id)
            AND current_setting('app.current_actor_role', true) = 'patient'
            AND user_id::text = current_setting('app.current_actor_id', true)
            AND patient_id::text = current_setting('app.current_patient_id', true)
            AND EXISTS (
              SELECT 1 FROM users AS identity
              WHERE identity.id = patient_user_links.user_id
                AND identity.is_active
                AND identity.account_kind = 'patient'
            )
          )
        );
        """
    )

    # Patients may inspect the clinic policy that governs their own voice and
    # portal request, but only an authenticated non-patient actor may mutate it.
    op.execute(
        """
        CREATE POLICY operational_settings_read
          ON clinic_operational_settings AS RESTRICTIVE FOR SELECT
        USING (
          app_nonpatient_context_allows(clinic_id)
          OR app_patient_actor_context_allows(clinic_id)
        );
        CREATE POLICY operational_settings_insert
          ON clinic_operational_settings AS RESTRICTIVE FOR INSERT
        WITH CHECK (app_nonpatient_context_allows(clinic_id));
        CREATE POLICY operational_settings_update
          ON clinic_operational_settings AS RESTRICTIVE FOR UPDATE
        USING (app_nonpatient_context_allows(clinic_id))
        WITH CHECK (app_nonpatient_context_allows(clinic_id));
        CREATE POLICY operational_settings_delete
          ON clinic_operational_settings AS RESTRICTIVE FOR DELETE
        USING (app_nonpatient_context_allows(clinic_id));
        """
    )

    # Patient-origin portal actions can append their own immutable audit/event
    # rows.  They cannot forge another actor, inspect another actor's events, or
    # update/delete history.
    for table in PATIENT_ACTOR_EVENT_TABLES:
        op.execute(
            f"""
            CREATE POLICY patient_actor_event_read
              ON "{table}" AS RESTRICTIVE FOR SELECT
            USING (
              app_nonpatient_context_allows(clinic_id)
              OR (
                app_patient_actor_context_allows(clinic_id)
                AND actor_id::text = current_setting('app.current_actor_id', true)
              )
            );
            CREATE POLICY patient_actor_event_insert
              ON "{table}" AS RESTRICTIVE FOR INSERT
            WITH CHECK (
              app_nonpatient_context_allows(clinic_id)
              OR (
                app_patient_actor_context_allows(clinic_id)
                AND actor_id::text = current_setting('app.current_actor_id', true)
              )
            );
            CREATE POLICY patient_actor_event_update
              ON "{table}" AS RESTRICTIVE FOR UPDATE
            USING (app_nonpatient_context_allows(clinic_id))
            WITH CHECK (app_nonpatient_context_allows(clinic_id));
            CREATE POLICY patient_actor_event_delete
              ON "{table}" AS RESTRICTIVE FOR DELETE
            USING (app_nonpatient_context_allows(clinic_id));
            """
        )

    for table, patient_column in DIRECT_PATIENT_TABLES.items():
        _restrictive_policy(
            table, f"app_patient_context_allows(clinic_id, {patient_column})"
        )
    for table, entry_column in ENTRY_CHILD_TABLES.items():
        _restrictive_policy(
            table, f"app_entry_context_allows(clinic_id, {entry_column})"
        )
    for table, version_column in VERSION_CHILD_TABLES.items():
        _restrictive_policy(
            table, f"app_version_context_allows(clinic_id, {version_column})"
        )
    for table, highlight_column in HIGHLIGHT_CHILD_TABLES.items():
        _restrictive_policy(
            table,
            f"app_highlight_context_allows(clinic_id, {highlight_column})",
        )
    for table, job_column in JOB_CHILD_TABLES.items():
        _restrictive_policy(table, f"app_job_context_allows(clinic_id, {job_column})")
    for table, session_column in SESSION_CHILD_TABLES.items():
        _restrictive_policy(
            table,
            f"app_voice_session_context_allows(clinic_id, {session_column})",
        )

    _restrictive_policy(
        "comment_mentions", "app_comment_context_allows(clinic_id, comment_id)"
    )
    _restrictive_policy(
        "provenance_pointers", "app_pointer_context_allows(clinic_id, id)"
    )
    _restrictive_policy(
        "patient_publication_items",
        "app_publication_context_allows(clinic_id, publication_id)",
    )
    _restrictive_policy(
        "patient_otp_challenges",
        "app_credential_context_allows(clinic_id, credential_id)",
    )
    for table in ("notification_attempts", "notification_receipts"):
        _restrictive_policy(
            table,
            "app_notification_context_allows(clinic_id, notification_id)",
        )
    for table in NON_PATIENT_TABLES:
        _restrictive_policy(table, "app_nonpatient_context_allows(clinic_id)")

    op.execute(
        """
        CREATE VIEW clinic_user_directory
        WITH (security_barrier = true, security_invoker = true)
        AS
        SELECT membership.clinic_id,
               membership.id AS membership_id,
               identity.id AS user_id,
               identity.email,
               identity.full_name,
               identity.account_kind,
               membership.role,
               membership.is_active
        FROM clinic_memberships AS membership
        JOIN users AS identity ON identity.id = membership.user_id
        WHERE membership.clinic_id = NULLIF(
                current_setting('app.current_clinic_id', true), ''
              )::uuid;

        CREATE VIEW patient_identity_projection
        WITH (security_barrier = true, security_invoker = true)
        AS
        SELECT patient.clinic_id,
               patient.id AS patient_id,
               identifier.identifier_type,
               identifier.masked_suffix,
               patient.status
        FROM patients AS patient
        LEFT JOIN patient_identifiers AS identifier
          ON identifier.clinic_id = patient.clinic_id
         AND identifier.patient_id = patient.id
        WHERE app_patient_context_allows(patient.clinic_id, patient.id);

        REVOKE ALL ON clinic_user_directory FROM PUBLIC;
        REVOKE ALL ON patient_identity_projection FROM PUBLIC;
        GRANT SELECT ON clinic_user_directory TO nightingale_app;
        GRANT SELECT ON patient_identity_projection TO nightingale_app;
        """
    )

    op.execute(
        f"""
        GRANT SELECT, INSERT, UPDATE, DELETE ON
          {", ".join(NEW_TENANT_TABLES)}
        TO {RUNTIME_ROLE};
        """
    )


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS patient_identity_projection")
    op.execute("DROP VIEW IF EXISTS clinic_user_directory")

    for table in PATIENT_ACTOR_EVENT_TABLES:
        for policy in (
            "patient_actor_event_delete",
            "patient_actor_event_update",
            "patient_actor_event_insert",
            "patient_actor_event_read",
        ):
            op.execute(f'DROP POLICY IF EXISTS {policy} ON "{table}"')
    for policy in (
        "operational_settings_delete",
        "operational_settings_update",
        "operational_settings_insert",
        "operational_settings_read",
    ):
        op.execute(f"DROP POLICY IF EXISTS {policy} ON clinic_operational_settings")

    for table in (
        *NON_PATIENT_TABLES,
        "notification_receipts",
        "notification_attempts",
        "patient_otp_challenges",
        "patient_publication_items",
        "provenance_pointers",
        "comment_mentions",
        *SESSION_CHILD_TABLES,
        *JOB_CHILD_TABLES,
        *HIGHLIGHT_CHILD_TABLES,
        *VERSION_CHILD_TABLES,
        *ENTRY_CHILD_TABLES,
        *DIRECT_PATIENT_TABLES,
        "patient_user_links",
        "clinic_invitations",
        "clinic_memberships",
    ):
        op.execute(f'DROP POLICY IF EXISTS patient_scope ON "{table}"')

    for table in reversed(NEW_TENANT_TABLES):
        op.execute(f'DROP POLICY IF EXISTS clinic_isolation ON "{table}"')
        op.execute(f'ALTER TABLE "{table}" DISABLE ROW LEVEL SECURITY')

    op.execute(
        """
        DROP POLICY IF EXISTS user_identity_scope ON users;
        ALTER TABLE users NO FORCE ROW LEVEL SECURITY;
        ALTER TABLE users DISABLE ROW LEVEL SECURITY;
        """
    )

    for function in (
        "app_lookup_patient_challenge(text)",
        "app_lookup_patient_portal(text)",
        "app_lookup_patient_enrollment(text)",
        "app_lookup_platform_user(text)",
        "app_lookup_invited_user(uuid,text,text,text)",
        "app_consume_clinic_invitation(uuid,text,text,uuid,uuid)",
        "app_lookup_clinic_user(text,text)",
        "app_lookup_clinic_invitation(uuid,text,text)",
        "app_lookup_clinic_worker(uuid)",
        "app_pointer_context_allows(uuid,uuid)",
        "app_comment_context_allows(uuid,uuid)",
        "app_credential_context_allows(uuid,uuid)",
        "app_publication_context_allows(uuid,uuid)",
        "app_notification_context_allows(uuid,uuid)",
        "app_voice_session_context_allows(uuid,uuid)",
        "app_job_context_allows(uuid,uuid)",
        "app_highlight_context_allows(uuid,uuid)",
        "app_version_context_allows(uuid,uuid)",
        "app_entry_context_allows(uuid,uuid)",
        "app_invitation_membership_bootstrap_allows(uuid,uuid,text)",
        "app_clinic_invitation_context_allows(uuid,uuid)",
        "app_patient_membership_bootstrap_allows(uuid,uuid,text)",
        "app_patient_actor_context_allows(uuid)",
        "app_patient_context_allows(uuid,uuid)",
        "app_nonpatient_context_allows(uuid)",
        "app_context_allows(uuid)",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function}")
