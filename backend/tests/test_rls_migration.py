import hashlib
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Connection, Engine, inspect, text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlmodel import Session, select

from app.configure_db_roles import configure_runtime_role
from app.core.db import (
    assert_restricted_runtime_connection,
    engine,
    set_rls_actor,
    set_rls_clinic,
)
from app.core.field_crypto import field_codec
from app.models import (
    CalibrationReport,
    ClinicInvitation,
    ClinicMembership,
    Comment,
    EntryVersion,
    EvaluationRun,
    Highlight,
    ImportanceFeatureStat,
    Patient,
    PatientAccessCredential,
    PatientPortalInvitation,
    PatientUserLink,
    PlatformAdministrator,
    ProvenancePointer,
    User,
    get_datetime_utc,
)
from app.seed import demo_id

TENANT_TABLES = {
    "clinic_memberships",
    "clinic_invitations",
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
    "jobs",
    "job_attempts",
    "redaction_runs",
    "ai_runs",
    "importance_feedback_events",
    "importance_feature_stats",
    "archive_blobs",
    "decay_runs",
    "retention_locks",
    "voice_sessions",
    "voice_devices",
    "audio_chunks",
    "audio_assets",
    "transcript_revisions",
    "transcript_segments",
    "clinical_facts",
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
}


def _scope_predicate_alternatives(
    table: str, columns: set[str]
) -> tuple[tuple[str, ...], ...]:
    """Derive the approved patient/non-patient fence from schema topology."""

    if "patient_id" in columns:
        alternatives: list[tuple[str, ...]] = [
            ("app_patient_context_allows(clinic_id, patient_id)",)
        ]
        if "user_id" in columns:
            # Patient identity links use an intentionally stricter actor +
            # linked-patient expression instead of the generic row helper.
            alternatives.append(("app.current_actor_id", "app.current_patient_id"))
        return tuple(alternatives)
    if table == "patients":
        return (("app_patient_context_allows(clinic_id, id)",),)
    if table == "provenance_pointers":
        return (("app_pointer_context_allows(clinic_id, id)",),)

    contextual_children = (
        ("notification_id", "app_notification_context_allows"),
        ("credential_id", "app_credential_context_allows"),
        ("publication_id", "app_publication_context_allows"),
        ("highlight_id", "app_highlight_context_allows"),
        ("comment_id", "app_comment_context_allows"),
        ("job_id", "app_job_context_allows"),
        ("session_id", "app_voice_session_context_allows"),
        ("source_entry_version_id", "app_version_context_allows"),
        ("entry_version_id", "app_version_context_allows"),
        ("source_entry_id", "app_entry_context_allows"),
        ("entry_id", "app_entry_context_allows"),
    )
    alternatives = tuple(
        (f"{helper}(clinic_id, {column})",)
        for column, helper in contextual_children
        if column in columns
    )
    if alternatives:
        # A child can carry more than one valid immutable parent pointer.  For
        # example, comments have both entry_id and entry_version_id.  Either
        # parent helper is a complete fail-closed patient fence, so do not make
        # the invariant depend on an arbitrary column-priority order.
        return alternatives
    return (("app_nonpatient_context_allows(clinic_id)",),)


def _scope_predicate_matches(table: str, columns: set[str], expression: str) -> bool:
    return any(
        all(marker in expression for marker in alternative)
        for alternative in _scope_predicate_alternatives(table, columns)
    )


@pytest.mark.unit
def test_scope_topology_rejects_clinic_only_policy_for_direct_patient_rows() -> None:
    assert not _scope_predicate_matches(
        "future_patient_rows",
        {"id", "clinic_id", "patient_id"},
        "app_context_allows(clinic_id)",
    )


@pytest.mark.unit
def test_scope_topology_rejects_split_using_bypass_for_direct_patient_rows() -> None:
    columns = {"id", "clinic_id", "patient_id"}
    using = "app_context_allows(clinic_id)"
    with_check = "app_patient_context_allows(clinic_id, patient_id)"
    # This combined expression demonstrates the exact mutation the old
    # invariant missed: a strong INSERT/UPDATE check could mask a leaking
    # SELECT/DELETE USING expression.
    assert _scope_predicate_matches(
        "future_patient_rows",
        columns,
        f"{using} {with_check}",
    )
    assert not _scope_predicate_matches(
        "future_patient_rows",
        columns,
        using,
    )
    assert _scope_predicate_matches(
        "future_patient_rows",
        columns,
        with_check,
    )


def _set_local_context(
    connection: Connection,
    *,
    clinic_id: uuid.UUID,
    actor_id: uuid.UUID,
    role: str,
    patient_id: uuid.UUID | None = None,
) -> None:
    """Bind the same transaction-local identity context as an API request."""

    values = {
        "app.current_clinic_id": str(clinic_id),
        "app.current_actor_id": str(actor_id),
        "app.current_actor_role": role,
        "app.current_patient_id": str(patient_id) if patient_id is not None else "",
        "app.current_invitation_token_hash": "",
    }
    for setting, value in values.items():
        connection.execute(
            text("SELECT set_config(:setting, :value, true)"),
            {"setting": setting, "value": value},
        )


def _make_anchored_comment(
    client: TestClient, headers: dict[str, str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    entry_response = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "staff",
            "title": "Constraint fixture",
            "content": "anchor",
        },
    )
    assert entry_response.status_code == 201, entry_response.text
    entry = entry_response.json()
    comment_response = client.post(
        f"/api/v1/entries/{entry['id']}/comments",
        headers=headers | {"If-Match": entry["version_id"]},
        json={
            "entry_version_id": entry["version_id"],
            "start_offset": 0,
            "end_offset": 6,
            "exact_quote": "anchor",
            "body": "tenant constraint",
        },
    )
    assert comment_response.status_code == 201, comment_response.text
    return entry, comment_response.json()


def test_migration_installs_rls_composite_constraints_and_immutability() -> None:
    with engine.connect() as connection:
        policies = {
            row.tablename
            for row in connection.execute(
                text(
                    """
                    SELECT tablename FROM pg_policies
                    WHERE schemaname = current_schema()
                      AND policyname = 'clinic_isolation'
                    """
                )
            )
        }
        triggers = {
            row.tgname
            for row in connection.execute(
                text(
                    """
                    SELECT tgname FROM pg_trigger
                    WHERE NOT tgisinternal
                      AND tgname IN (
                        'trg_entry_version_append_only',
                        'trg_provenance_append_only',
                        'trg_highlight_anchor_guard',
                        'trg_retention_lock_decay_subject',
                        'trg_highlight_cold_source',
                        'trg_conflict_cold_source',
                        'trg_task_cold_source',
                        'trg_audio_chunks_append_only',
                        'trg_audio_assets_append_only',
                        'trg_transcript_revisions_append_only',
                        'trg_transcript_segments_append_only',
                        'trg_clinical_fact_review_guard'
                      )
                    """
                )
            )
        }
    assert TENANT_TABLES <= policies
    assert triggers == {
        "trg_entry_version_append_only",
        "trg_provenance_append_only",
        "trg_highlight_anchor_guard",
        "trg_retention_lock_decay_subject",
        "trg_highlight_cold_source",
        "trg_conflict_cold_source",
        "trg_task_cold_source",
        "trg_audio_chunks_append_only",
        "trg_audio_assets_append_only",
        "trg_transcript_revisions_append_only",
        "trg_transcript_segments_append_only",
        "trg_clinical_fact_review_guard",
    }

    inspector = inspect(engine)
    membership_uniques = {
        item["name"] for item in inspector.get_unique_constraints("clinic_memberships")
    }
    assert "uq_membership_clinic_id" in membership_uniques
    expected_fks = {
        "entries": {"fk_entry_source_job_patient_tenant"},
        "entry_versions": {"fk_version_archive_blob_tenant"},
        "comments": {"fk_comment_parent_tenant", "fk_comment_assignment_tenant"},
        "comment_mentions": {"fk_comment_mention_tenant"},
        "care_tasks": {"fk_task_comment_tenant", "fk_task_assignee_tenant"},
        "provenance_pointers": {
            "fk_provenance_highlight_tenant",
            "fk_provenance_comment_tenant",
            "fk_provenance_audio_asset_tenant",
            "fk_provenance_clinical_fact_tenant",
        },
        "audio_chunks": {
            "fk_audio_chunk_session_tenant",
            "fk_audio_chunk_device_tenant",
        },
        "voice_sessions": {"fk_voice_session_current_revision_tenant"},
        "transcript_revisions": {
            "fk_transcript_revision_session_tenant",
            "fk_transcript_previous_revision_tenant",
        },
        "transcript_segments": {"fk_transcript_segment_revision_tenant"},
        "clinical_facts": {
            "fk_clinical_fact_revision_tenant",
            "fk_clinical_fact_segment_tenant",
            "fk_clinical_fact_audio_asset_tenant",
        },
    }
    for table, expected in expected_fks.items():
        actual = {item["name"] for item in inspector.get_foreign_keys(table)}
        assert expected <= actual
    job_uniques = {item["name"] for item in inspector.get_unique_constraints("jobs")}
    assert "uq_job_clinic_id_patient" in job_uniques


def test_every_tenant_table_has_forced_rls_required_policies_and_grants() -> None:
    """Schema additions cannot silently escape the tenant-security invariant.

    This intentionally discovers tables from the live schema rather than from
    a maintained allowlist.  Every clinic-scoped table needs the permissive
    clinic fence, a restrictive actor/patient fence for every DML command, and
    a least-privilege runtime grant that those policies actually govern.
    """

    with engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    """
                SELECT column_info.table_name
                FROM information_schema.columns AS column_info
                JOIN information_schema.tables AS table_info
                  ON table_info.table_schema = column_info.table_schema
                 AND table_info.table_name = column_info.table_name
                 AND table_info.table_type = 'BASE TABLE'
                WHERE column_info.table_schema = current_schema()
                  AND column_info.column_name = 'clinic_id'
                ORDER BY column_info.table_name
                """
                )
            )
            .scalars()
            .all()
        )
        columns_by_table: dict[str, set[str]] = {}
        for row in connection.execute(
            text(
                """
                SELECT table_name, column_name
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                ORDER BY table_name, ordinal_position
                """
            )
        ):
            if row.table_name in rows:
                columns_by_table.setdefault(row.table_name, set()).add(row.column_name)
        security = {
            row.relname: (row.relrowsecurity, row.relforcerowsecurity)
            for row in connection.execute(
                text(
                    """
                    SELECT cls.relname,
                           cls.relrowsecurity,
                           cls.relforcerowsecurity
                    FROM pg_class AS cls
                    JOIN pg_namespace AS ns ON ns.oid = cls.relnamespace
                    WHERE ns.nspname = current_schema()
                      AND cls.relkind = 'r'
                    """
                )
            )
        }
        policies: dict[str, list[Any]] = {}
        for row in connection.execute(
            text(
                """
                SELECT tablename, policyname, permissive, roles, cmd, qual, with_check
                FROM pg_policies
                WHERE schemaname = current_schema()
                ORDER BY tablename, policyname
                """
            )
        ):
            policies.setdefault(row.tablename, []).append(row)
        grants: dict[str, set[str]] = {}
        for row in connection.execute(
            text(
                """
                SELECT table_name, privilege_type
                FROM information_schema.role_table_grants
                WHERE table_schema = current_schema()
                  AND grantee = 'nightingale_app'
                ORDER BY table_name, privilege_type
                """
            )
        ):
            grants.setdefault(row.table_name, set()).add(row.privilege_type)
        unexpected_grants = connection.execute(
            text(
                """
                SELECT privilege.table_name,
                       privilege.grantee,
                       privilege.privilege_type
                FROM information_schema.table_privileges AS privilege
                JOIN pg_tables AS owned_table
                  ON owned_table.schemaname = privilege.table_schema
                 AND owned_table.tablename = privilege.table_name
                WHERE privilege.table_schema = current_schema()
                  AND privilege.grantee NOT IN (
                    owned_table.tableowner,
                    'nightingale_app'
                  )
                  AND EXISTS (
                    SELECT 1
                    FROM information_schema.columns AS tenant_column
                    WHERE tenant_column.table_schema = privilege.table_schema
                      AND tenant_column.table_name = privilege.table_name
                      AND tenant_column.column_name = 'clinic_id'
                  )
                ORDER BY privilege.table_name,
                         privilege.grantee,
                         privilege.privilege_type
                """
            )
        ).all()

    missing_security = {
        table: security.get(table)
        for table in rows
        if security.get(table) != (True, True)
    }
    assert missing_security == {}
    assert unexpected_grants == []

    policy_errors: dict[str, list[str]] = {}
    allowed_scope_markers = (
        "app_context_allows(",
        "app_patient_context_allows(",
        "app_patient_actor_context_allows(",
        "app_nonpatient_context_allows(",
        "app_clinic_invitation_context_allows(",
        "app_patient_membership_bootstrap_allows(",
        "app_invitation_membership_bootstrap_allows(",
        "app_entry_context_allows(",
        "app_version_context_allows(",
        "app_pointer_context_allows(",
        "app_highlight_context_allows(",
        "app_comment_context_allows(",
        "app_job_context_allows(",
        "app_notification_context_allows(",
        "app_credential_context_allows(",
        "app_publication_context_allows(",
        "app_voice_session_context_allows(",
        "app.current_actor_id",
        "app.current_actor_role",
        "app.current_patient_id",
        "app.current_invitation_token_hash",
    )
    explicit_policy_topologies = (
        {
            "patient_actor_event_read": "SELECT",
            "patient_actor_event_insert": "INSERT",
            "patient_actor_event_update": "UPDATE",
            "patient_actor_event_delete": "DELETE",
        },
        {
            "operational_settings_read": "SELECT",
            "operational_settings_insert": "INSERT",
            "operational_settings_update": "UPDATE",
            "operational_settings_delete": "DELETE",
        },
    )
    for table in rows:
        table_policies = policies.get(table, [])
        clinic_policy = next(
            (
                policy
                for policy in table_policies
                if policy.policyname == "clinic_isolation"
            ),
            None,
        )
        errors: list[str] = []
        if clinic_policy is None:
            errors.append("clinic_isolation missing")
        else:
            if (
                clinic_policy.permissive != "PERMISSIVE"
                or clinic_policy.roles != ["public"]
                or clinic_policy.cmd != "ALL"
            ):
                errors.append("clinic_isolation topology invalid")
            if "app.current_clinic_id" not in (clinic_policy.qual or ""):
                errors.append("clinic_isolation USING is not bound to clinic context")
            if "app.current_clinic_id" not in (clinic_policy.with_check or ""):
                errors.append(
                    "clinic_isolation WITH CHECK is not bound to clinic context"
                )

        restrictive = [
            policy for policy in table_policies if policy.permissive == "RESTRICTIVE"
        ]
        if any(policy.roles != ["public"] for policy in restrictive):
            errors.append("restrictive policy role topology invalid")

        all_scope = next(
            (
                policy
                for policy in restrictive
                if policy.policyname == "patient_scope" and policy.cmd == "ALL"
            ),
            None,
        )
        if all_scope is not None:
            if len(restrictive) != 1:
                errors.append("patient_scope must be the only restrictive policy")
            if not _scope_predicate_matches(
                table,
                columns_by_table.get(table, set()),
                all_scope.qual or "",
            ):
                errors.append("patient_scope USING does not match table FK topology")
            if not _scope_predicate_matches(
                table,
                columns_by_table.get(table, set()),
                all_scope.with_check or "",
            ):
                errors.append(
                    "patient_scope WITH CHECK does not match table FK topology"
                )
        else:
            command_topology = {policy.cmd for policy in restrictive}
            if command_topology != {"SELECT", "INSERT", "UPDATE", "DELETE"}:
                errors.append("required restrictive command topology missing")
            policy_names = {policy.policyname for policy in restrictive}
            recognized_topology = next(
                (
                    topology
                    for topology in explicit_policy_topologies
                    if set(topology) == policy_names
                ),
                None,
            )
            if recognized_topology is None:
                errors.append("unrecognized restrictive policy-name topology")
            elif any(
                policy.cmd != recognized_topology[policy.policyname]
                for policy in restrictive
            ):
                errors.append("restrictive policy name/command topology mismatch")

        for command in ("SELECT", "INSERT", "UPDATE", "DELETE"):
            command_policies = [
                policy for policy in restrictive if policy.cmd in {"ALL", command}
            ]
            if not command_policies:
                errors.append(f"restrictive {command} policy missing")
                continue
            required_fields = {
                "SELECT": ("qual",),
                "INSERT": ("with_check",),
                "UPDATE": ("qual", "with_check"),
                "DELETE": ("qual",),
            }[command]
            for field in required_fields:
                expressions = " ".join(
                    getattr(policy, field) or "" for policy in command_policies
                )
                lowered_expressions = expressions.lower()
                if not any(marker in expressions for marker in allowed_scope_markers):
                    errors.append(f"restrictive {command} {field} is not context-bound")
                if " or true" in lowered_expressions or "1 = 1" in lowered_expressions:
                    errors.append(f"restrictive {command} {field} is tautological")
        if errors:
            policy_errors[table] = errors
    assert policy_errors == {}

    allowed_privileges = {"SELECT", "INSERT", "UPDATE", "DELETE"}
    grant_errors: dict[str, object] = {}
    for table in rows:
        table_grants = grants.get(table, set())
        if not {"SELECT", "INSERT"} <= table_grants:
            grant_errors[table] = sorted(table_grants)
        elif not table_grants <= allowed_privileges:
            grant_errors[table] = sorted(table_grants - allowed_privileges)
        else:
            table_policies = policies.get(table, [])
            for privilege in table_grants:
                if not any(
                    policy.cmd in {"ALL", privilege} for policy in table_policies
                ):
                    grant_errors[table] = f"{privilege} grant has no policy"
                    break
    assert grant_errors == {}


def _valid_evaluation_run() -> EvaluationRun:
    return EvaluationRun(
        clinic_id=demo_id("clinic-primary"),
        provider="constraint-fixture",
        exact_model_id="constraint-model",
        task="clinical_fact_extraction",
        request_parameters_json={"schema": "constraint-v1"},
        dataset_manifest_sha256="a" * 64,
        code_commit="b" * 40,
        calibration_split="constraint-calibration",
        holdout_split="constraint-holdout",
        total_sample_count=160,
        calibration_sample_count=40,
        holdout_sample_count=120,
        sample_count=120,
        status="completed",
    )


def test_evaluation_run_sample_accounting_is_database_enforced(
    owner_session: Session,
) -> None:
    run = _valid_evaluation_run()
    run.total_sample_count = 159
    owner_session.add(run)

    with pytest.raises(IntegrityError, match="ck_evaluation_run_sample_accounting"):
        owner_session.commit()
    owner_session.rollback()


def test_calibration_report_sample_accounting_is_database_enforced(
    owner_session: Session,
) -> None:
    run = _valid_evaluation_run()
    owner_session.add(run)
    owner_session.flush()
    report = CalibrationReport(
        clinic_id=run.clinic_id,
        evaluation_run_id=run.id,
        provider=run.provider,
        exact_model_id=run.exact_model_id,
        task=run.task,
        request_parameters_sha256="c" * 64,
        dataset_manifest_sha256=run.dataset_manifest_sha256,
        code_commit=run.code_commit,
        total_sample_count=159,
        calibration_sample_count=40,
        holdout_sample_count=120,
        sample_count=120,
        consultation_count=20,
        confidence_band="high",
        accuracy_lower_bound=0.91,
        expires_at=get_datetime_utc() + timedelta(days=1),
    )
    owner_session.add(report)

    with pytest.raises(IntegrityError, match="ck_calibration_report_sample_accounting"):
        owner_session.commit()
    owner_session.rollback()


@pytest.mark.parametrize("weight", [-0.200001, 0.200001])
def test_importance_feature_weight_bound_is_database_enforced(
    owner_session: Session,
    weight: float,
) -> None:
    owner_session.add(
        ImportanceFeatureStat(
            clinic_id=demo_id("clinic-primary"),
            feature_key=f"entry_type:weight_{str(weight).replace('.', '_')}",
            weight=weight,
        )
    )

    with pytest.raises(IntegrityError, match="ck_importance_feature_weight_bound"):
        owner_session.commit()
    owner_session.rollback()


def test_runtime_login_is_restricted_non_owner_and_rls_is_enforced() -> None:
    primary_clinic = demo_id("clinic-primary")
    other_clinic = demo_id("clinic-other")
    primary_patient = demo_id("patient-primary")
    other_patient = demo_id("patient-other")

    with engine.begin() as connection:
        role = connection.execute(
            text(
                """
                SELECT current_user AS role_name, rolcanlogin, rolsuper,
                       rolcreatedb, rolcreaterole, rolreplication, rolbypassrls
                FROM pg_roles WHERE rolname = current_user
                """
            )
        ).one()
        owner = connection.execute(
            text(
                """
                SELECT tableowner FROM pg_tables
                WHERE schemaname = current_schema() AND tablename = 'patients'
                """
            )
        ).scalar_one()
        privileges = connection.execute(
            text(
                """
                SELECT
                  has_schema_privilege(current_user, current_schema(), 'CREATE')
                    AS can_create_schema_objects,
                  has_table_privilege(current_user, 'patients', 'SELECT')
                    AS can_select_patients,
                  has_table_privilege(current_user, 'patients', 'TRUNCATE')
                    AS can_truncate_patients,
                  has_table_privilege(current_user, 'alembic_version', 'UPDATE')
                    AS can_mutate_migration_history,
                  has_table_privilege(current_user, 'audit_events', 'UPDATE')
                    AS can_update_audit,
                  has_table_privilege(current_user, 'domain_events', 'DELETE')
                    AS can_delete_events,
                  has_table_privilege(current_user, 'provenance_pointers', 'UPDATE')
                    AS can_update_provenance,
                  has_table_privilege(current_user, 'archive_blobs', 'DELETE')
                    AS can_delete_archive
                """
            )
        ).one()

    assert role.role_name == "nightingale_app"
    assert role.rolcanlogin is True
    assert role.rolsuper is False
    assert role.rolcreatedb is False
    assert role.rolcreaterole is False
    assert role.rolreplication is False
    assert role.rolbypassrls is False
    assert owner != role.role_name
    assert privileges.can_create_schema_objects is False
    assert privileges.can_select_patients is True
    assert privileges.can_truncate_patients is False
    assert privileges.can_mutate_migration_history is False
    assert privileges.can_update_audit is False
    assert privileges.can_delete_events is False
    assert privileges.can_update_provenance is False
    assert privileges.can_delete_archive is False

    # SET LOCAL is intentionally pool-safe; the Session hook reapplies the
    # trusted value when application code commits and then refreshes/queries.
    with Session(engine) as session:
        set_rls_clinic(session, other_clinic)
        set_rls_actor(
            session,
            demo_id("user-other_staff"),
            role="staff",
        )
        other_visible = (
            session.execute(text("SELECT id FROM patients ORDER BY id")).scalars().all()
        )
        assert other_patient in other_visible
        assert primary_patient not in other_visible
        session.commit()
        after_commit = session.execute(text("SELECT id FROM patients")).scalars().all()
        assert other_patient in after_commit
        assert primary_patient not in after_commit

    with engine.connect() as connection:
        transaction = connection.begin()
        _set_local_context(
            connection,
            clinic_id=primary_clinic,
            actor_id=demo_id("user-staff"),
            role="staff",
        )
        visible_ids = set(
            connection.execute(text("SELECT id FROM patients")).scalars().all()
        )
        assert primary_patient in visible_ids
        assert other_patient not in visible_ids

        allowed_patient_id = uuid.uuid4()
        connection.execute(
            text(
                """
                INSERT INTO patients (
                  id, clinic_id, display_name_ciphertext, external_ref_hash
                ) VALUES (:id, :clinic_id, :ciphertext, :external_ref_hash)
                """
            ),
            {
                "id": allowed_patient_id,
                "clinic_id": primary_clinic,
                "ciphertext": b"synthetic-rls-test",
                "external_ref_hash": "a" * 64,
            },
        )
        assert (
            connection.execute(
                text("SELECT count(*) FROM patients WHERE id = :id"),
                {"id": allowed_patient_id},
            ).scalar_one()
            == 1
        )
        transaction.rollback()

    with pytest.raises(DBAPIError, match="row-level security policy"):
        with engine.begin() as connection:
            _set_local_context(
                connection,
                clinic_id=primary_clinic,
                actor_id=demo_id("user-staff"),
                role="staff",
            )
            connection.execute(
                text(
                    """
                    INSERT INTO patients (
                      id, clinic_id, display_name_ciphertext, external_ref_hash
                    ) VALUES (:id, :clinic_id, :ciphertext, :external_ref_hash)
                    """
                ),
                {
                    "id": uuid.uuid4(),
                    "clinic_id": other_clinic,
                    "ciphertext": b"cross-clinic-denied",
                    "external_ref_hash": "b" * 64,
                },
            )


def test_runtime_guard_rejects_owner_connection(owner_session: Session) -> None:
    with engine.connect() as runtime_connection:
        assert_restricted_runtime_connection(runtime_connection)
    with pytest.raises(RuntimeError, match="UNSAFE_DATABASE_RUNTIME_ROLE"):
        assert_restricted_runtime_connection(owner_session.connection())


def test_runtime_role_bootstrap_revokes_settable_memberships(
    owner_session: Session,
) -> None:
    role_name = f"nightingale_fixture_{uuid.uuid4().hex}"
    connection = owner_session.connection()
    create_role = connection.scalar(
        text("SELECT format('CREATE ROLE %I NOLOGIN', CAST(:role AS text))"),
        {"role": role_name},
    )
    grant_role = connection.scalar(
        text("SELECT format('GRANT %I TO nightingale_app', CAST(:role AS text))"),
        {"role": role_name},
    )
    assert isinstance(create_role, str) and isinstance(grant_role, str)
    connection.exec_driver_sql(create_role)
    connection.exec_driver_sql(grant_role)
    owner_session.commit()
    try:
        with engine.connect() as runtime_connection:
            with pytest.raises(RuntimeError, match="UNSAFE_DATABASE_RUNTIME_ROLE"):
                assert_restricted_runtime_connection(runtime_connection)
        configure_runtime_role()
        with engine.connect() as runtime_connection:
            assert_restricted_runtime_connection(runtime_connection)
    finally:
        with cast(Engine, owner_session.get_bind()).begin() as cleanup:
            drop_role = cleanup.scalar(
                text("SELECT format('DROP ROLE IF EXISTS %I', CAST(:role AS text))"),
                {"role": role_name},
            )
            assert isinstance(drop_role, str)
            cleanup.exec_driver_sql(drop_role)


def test_cross_clinic_composite_fk_and_append_only_triggers(
    client: TestClient, auth_headers: Callable[[str], dict[str, str]]
) -> None:
    entry, comment = _make_anchored_comment(client, auth_headers("staff"))

    with Session(engine) as session:
        set_rls_clinic(session, demo_id("clinic-primary"))
        set_rls_actor(session, demo_id("user-staff"), role="staff")
        stored_comment = session.get(Comment, uuid.UUID(comment["id"]))
        assert stored_comment is not None
        stored_comment.assigned_membership_id = demo_id("membership-other_staff")
        session.add(stored_comment)
        with pytest.raises(IntegrityError):
            session.commit()

    with Session(engine) as session:
        set_rls_clinic(session, demo_id("clinic-primary"))
        set_rls_actor(session, demo_id("user-staff"), role="staff")
        version = session.get(EntryVersion, uuid.UUID(entry["version_id"]))
        assert version is not None
        version.content_ciphertext = b"tampered-in-place"
        session.add(version)
        with pytest.raises(DBAPIError, match="immutable entry_version payload"):
            session.commit()

    with Session(engine) as session:
        set_rls_clinic(session, demo_id("clinic-primary"))
        set_rls_actor(session, demo_id("user-staff"), role="staff")
        version = session.get(EntryVersion, uuid.UUID(entry["version_id"]))
        assert version is not None
        version.title_ciphertext = b"tampered-title-in-place"
        session.add(version)
        with pytest.raises(DBAPIError, match="immutable entry_version payload"):
            session.commit()

    with Session(engine) as session:
        set_rls_clinic(session, demo_id("clinic-primary"))
        set_rls_actor(session, demo_id("user-staff"), role="staff")
        pointer = session.exec(
            select(ProvenancePointer).where(
                ProvenancePointer.comment_id == uuid.UUID(comment["id"])
            )
        ).one()
        pointer.review_required = True
        session.add(pointer)
        with pytest.raises(DBAPIError, match="append-only|permission denied"):
            session.commit()


def test_highlight_anchor_fields_are_database_immutable(
    client: TestClient, auth_headers: Callable[[str], dict[str, str]]
) -> None:
    headers = auth_headers("clinician")
    patient_id = client.get("/api/v1/patients", headers=headers).json()["data"][0]["id"]
    entry = client.post(
        "/api/v1/entries",
        headers=headers,
        json={
            "patient_id": patient_id,
            "section": "clinician",
            "title": "Highlight guard",
            "content": "anchored",
        },
    ).json()
    highlight = client.post(
        f"/api/v1/entries/{entry['id']}/highlights",
        headers=headers,
        json={
            "entry_version_id": entry["version_id"],
            "start_offset": 0,
            "end_offset": 8,
            "exact_quote": "anchored",
            "label": "Immutable anchor",
        },
    )
    assert highlight.status_code == 201, highlight.text

    with Session(engine) as session:
        set_rls_clinic(session, demo_id("clinic-primary"))
        set_rls_actor(session, demo_id("user-clinician"), role="clinician")
        stored = session.get(Highlight, uuid.UUID(highlight.json()["id"]))
        assert stored is not None
        stored.entry_id = uuid.uuid4()
        session.add(stored)
        with pytest.raises(DBAPIError, match="immutable highlight anchor"):
            session.commit()


def test_actor_and_patient_rls_fail_closed_without_route_filters(
    owner_session: Session,
) -> None:
    """An omitted WHERE clause still cannot cross the actor/patient boundary."""

    primary_clinic = demo_id("clinic-primary")
    primary_patient = demo_id("patient-primary")
    other_patient = demo_id("patient-other")
    unlinked_patient = uuid.uuid4()
    owner_session.add(
        Patient(
            id=unlinked_patient,
            clinic_id=primary_clinic,
            display_name_ciphertext=field_codec.encrypt_text(
                primary_clinic,
                "patient.display_name",
                unlinked_patient,
                "Unlinked RLS Fixture",
            ),
            external_ref_hash=hashlib.sha256(b"unlinked-rls-fixture").hexdigest(),
        )
    )
    owner_session.commit()

    with engine.begin() as connection:
        _set_local_context(
            connection,
            clinic_id=primary_clinic,
            actor_id=demo_id("user-staff"),
            role="staff",
        )
        visible = set(
            connection.execute(text("SELECT id FROM patients")).scalars().all()
        )
        assert {primary_patient, unlinked_patient} <= visible
        assert other_patient not in visible

    with engine.begin() as connection:
        _set_local_context(
            connection,
            clinic_id=primary_clinic,
            actor_id=demo_id("user-patient"),
            role="patient",
            patient_id=primary_patient,
        )
        assert set(
            connection.execute(text("SELECT id FROM patients")).scalars().all()
        ) == {primary_patient}
        # Identity tables expose only the patient's own bootstrap rows, never
        # a clinic/global directory that could be enumerated.
        assert set(
            connection.execute(text("SELECT id FROM users")).scalars().all()
        ) == {demo_id("user-patient")}
        assert set(
            connection.execute(text("SELECT id FROM clinic_memberships"))
            .scalars()
            .all()
        ) == {demo_id("membership-patient")}

    with engine.begin() as connection:
        _set_local_context(
            connection,
            clinic_id=primary_clinic,
            actor_id=uuid.uuid4(),
            role="staff",
        )
        assert connection.execute(text("SELECT id FROM patients")).all() == []
        assert connection.execute(text("SELECT id FROM users")).all() == []
        assert connection.execute(text("SELECT id FROM clinic_memberships")).all() == []


def test_patient_access_credentials_allow_shared_phone_hmac(
    owner_session: Session,
) -> None:
    """A household phone is a delivery address, not a unique patient identity."""

    clinic_id = demo_id("clinic-primary")
    second_patient_id = uuid.uuid4()
    owner_session.add(
        Patient(
            id=second_patient_id,
            clinic_id=clinic_id,
            display_name_ciphertext=field_codec.encrypt_text(
                clinic_id,
                "patient.display_name",
                second_patient_id,
                "Shared Phone Fixture",
            ),
            external_ref_hash=hashlib.sha256(b"shared-phone-fixture").hexdigest(),
        )
    )
    owner_session.flush()
    now = get_datetime_utc()
    invitations = [
        PatientPortalInvitation(
            clinic_id=clinic_id,
            patient_id=patient_id,
            email=None,
            token_hash=hashlib.sha256(f"shared-invite-{index}".encode()).hexdigest(),
            created_by_membership_id=demo_id("membership-staff"),
            expires_at=now + timedelta(days=7),
        )
        for index, patient_id in enumerate(
            (demo_id("patient-primary"), second_patient_id), start=1
        )
    ]
    owner_session.add_all(invitations)
    owner_session.flush()
    shared_phone_hmac = field_codec.blind_index(
        clinic_id, "patient_access.phone", "+6591234567"
    )
    credentials = [
        PatientAccessCredential(
            clinic_id=clinic_id,
            patient_id=invitation.patient_id,
            invitation_id=invitation.id,
            portal_id=f"NIGHTINGALE-SHARED{index}",
            phone_ciphertext=field_codec.encrypt_text(
                clinic_id,
                "patient_access.phone",
                credential_id,
                "+6591234567",
            ),
            phone_hmac=shared_phone_hmac,
            masked_phone="***4567",
            claim_code_hash=hashlib.sha256(
                f"shared-claim-{index}".encode()
            ).hexdigest(),
            claim_code_expires_at=now + timedelta(days=7),
            created_by_membership_id=demo_id("membership-staff"),
            id=credential_id,
        )
        for index, (invitation, credential_id) in enumerate(
            zip(invitations, (uuid.uuid4(), uuid.uuid4()), strict=True), start=1
        )
    ]
    owner_session.add_all(credentials)
    owner_session.commit()

    stored = owner_session.exec(
        select(PatientAccessCredential).where(
            PatientAccessCredential.clinic_id == clinic_id,
            PatientAccessCredential.phone_hmac == shared_phone_hmac,
        )
    ).all()
    assert {item.patient_id for item in stored} == {
        demo_id("patient-primary"),
        second_patient_id,
    }


def test_staff_invitation_bootstrap_requires_exact_secret_and_email(
    client: TestClient,
    owner_session: Session,
) -> None:
    clinic_id = demo_id("clinic-primary")
    email = "rls.invited.clinician@nightingale.example"
    token = f"{clinic_id}.{'staff-exact-secret-' * 2}"
    invitation = ClinicInvitation(
        clinic_id=clinic_id,
        email=email,
        invited_full_name="RLS Invited Clinician",
        role="clinician",
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        created_by_membership_id=demo_id("membership-admin"),
        expires_at=get_datetime_utc() + timedelta(days=1),
    )
    owner_session.add(invitation)
    owner_session.commit()

    base_body = {
        "email": email,
        "password": "exact-secret-passphrase",
        "full_name": "RLS Invited Clinician",
    }
    wrong_email = client.post(
        "/api/v1/auth/invitations/accept",
        headers={"Origin": "https://localhost"},
        json=base_body | {"email": "wrong.rls@example.com", "token": token},
    )
    assert wrong_email.status_code == 400
    wrong_secret = client.post(
        "/api/v1/auth/invitations/accept",
        headers={"Origin": "https://localhost"},
        json=base_body | {"token": f"{clinic_id}.{'wrong-secret-' * 3}"},
    )
    assert wrong_secret.status_code == 400

    accepted = client.post(
        "/api/v1/auth/invitations/accept",
        headers={"Origin": "https://localhost"},
        json=base_body | {"token": token},
    )
    assert accepted.status_code == 200, accepted.text
    owner_session.expire_all()
    user = owner_session.exec(select(User).where(User.email == email)).one()
    membership = owner_session.exec(
        select(ClinicMembership).where(
            ClinicMembership.clinic_id == clinic_id,
            ClinicMembership.user_id == user.id,
        )
    ).one()
    assert user.account_kind == "staff"
    assert membership.role == "clinician"


def test_legacy_patient_invitation_bootstrap_requires_exact_secret_and_email(
    client: TestClient,
    owner_session: Session,
) -> None:
    clinic_id = demo_id("clinic-primary")
    patient_id = uuid.uuid4()
    email = "legacy.rls.patient@nightingale.example"
    token = f"{clinic_id}.{'patient-exact-secret-' * 2}"
    patient = Patient(
        id=patient_id,
        clinic_id=clinic_id,
        display_name_ciphertext=field_codec.encrypt_text(
            clinic_id,
            "patient.display_name",
            patient_id,
            "Legacy Patient Fixture",
        ),
        external_ref_hash=hashlib.sha256(b"legacy-patient-fixture").hexdigest(),
    )
    invitation = PatientPortalInvitation(
        clinic_id=clinic_id,
        patient_id=patient_id,
        email=email,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        created_by_membership_id=demo_id("membership-staff"),
        expires_at=get_datetime_utc() + timedelta(days=1),
    )
    owner_session.add(patient)
    owner_session.flush()
    owner_session.add(invitation)
    owner_session.commit()

    wrong_email = client.post(
        "/api/v1/auth/patient-invitations/preview",
        json={"token": token, "email": "wrong.patient@example.com"},
    )
    assert wrong_email.status_code == 400
    wrong_secret = client.post(
        "/api/v1/auth/patient-invitations/preview",
        json={"token": f"{clinic_id}.{'wrong-secret-' * 3}", "email": email},
    )
    assert wrong_secret.status_code == 400

    preview = client.post(
        "/api/v1/auth/patient-invitations/preview",
        json={"token": token, "email": email},
    )
    assert preview.status_code == 200, preview.text
    accepted = client.post(
        "/api/v1/auth/patient-invitations/accept",
        json={
            "token": token,
            "email": email,
            "password": "legacy-patient-passphrase",
            "full_name": "Legacy Patient Fixture",
        },
    )
    assert accepted.status_code == 200, accepted.text

    owner_session.expire_all()
    user = owner_session.exec(select(User).where(User.email == email)).one()
    membership = owner_session.exec(
        select(ClinicMembership).where(
            ClinicMembership.clinic_id == clinic_id,
            ClinicMembership.user_id == user.id,
        )
    ).one()
    link = owner_session.exec(
        select(PatientUserLink).where(
            PatientUserLink.clinic_id == clinic_id,
            PatientUserLink.patient_id == patient_id,
            PatientUserLink.user_id == user.id,
        )
    ).one()
    assert user.account_kind == "patient"
    assert membership.role == "patient"
    assert link.patient_id == patient_id


def test_platform_oversight_tables_are_isolated_from_clinic_sessions() -> None:
    """Cross-clinic oversight rows are unreadable outside a platform session.

    These two tables are the only runtime-writable tables with no clinic_id, so
    the clinic-scoped installer skips them. They need their own actor fence, or
    any authenticated clinic session could enumerate platform operators.
    """

    platform_tables = ("platform_administrators", "platform_audit_events")

    with engine.connect() as connection:
        security = {
            row.relname: (row.relrowsecurity, row.relforcerowsecurity)
            for row in connection.execute(
                text(
                    """
                    SELECT class_info.relname,
                           class_info.relrowsecurity,
                           class_info.relforcerowsecurity
                    FROM pg_class AS class_info
                    JOIN pg_namespace AS namespace_info
                      ON namespace_info.oid = class_info.relnamespace
                    WHERE class_info.relkind = 'r'
                      AND namespace_info.nspname = current_schema()
                      AND class_info.relname = ANY(:tables)
                    """
                ),
                {"tables": list(platform_tables)},
            ).all()
        }
        policies = {
            (row.tablename, row.policyname)
            for row in connection.execute(
                text(
                    "SELECT tablename, policyname FROM pg_policies "
                    "WHERE tablename = ANY(:tables)"
                ),
                {"tables": list(platform_tables)},
            ).all()
        }

    assert security == {table: (True, True) for table in platform_tables}
    assert policies == {(table, "platform_actor_scope") for table in platform_tables}

    # The clinician GUC bound by the test fixture must see nothing.
    with Session(engine) as clinic_session:
        assert (
            clinic_session.exec(select(PlatformAdministrator)).all() == []
        )

    # A platform actor context sees the seeded operator through the same role.
    with Session(engine) as platform_session:
        set_rls_actor(
            platform_session,
            demo_id("user-platform-administrator"),
            role="platform_admin",
        )
        administrators = platform_session.exec(select(PlatformAdministrator)).all()
        assert [item.id for item in administrators] == [
            demo_id("platform-administrator")
        ]
