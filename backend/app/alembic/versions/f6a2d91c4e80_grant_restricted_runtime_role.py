"""Grant least-privilege access to the non-owner API runtime role.

Revision ID: f6a2d91c4e80
Revises: e8b5c1d7a2f0
"""

from collections.abc import Sequence

from alembic import op

revision: str = "f6a2d91c4e80"
down_revision: str | None = "e8b5c1d7a2f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

RUNTIME_ROLE = "nightingale_app"


def upgrade() -> None:
    # Role lifecycle/password rotation belongs to the one-shot bootstrap process,
    # not migration history.  Failing here when the role is absent catches an
    # unsafe deployment order instead of silently leaving the API on an owner URL.
    op.execute(
        f"""
        ALTER ROLE {RUNTIME_ROLE} WITH
          LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT
          NOREPLICATION NOBYPASSRLS;
        ALTER ROLE {RUNTIME_ROLE} SET row_security = on;

        REVOKE CREATE ON SCHEMA public FROM {RUNTIME_ROLE};
        GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE};

        REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {RUNTIME_ROLE};
        GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public
          TO {RUNTIME_ROLE};
        REVOKE ALL PRIVILEGES ON TABLE alembic_version FROM {RUNTIME_ROLE};

        REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {RUNTIME_ROLE};
        GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public
          TO {RUNTIME_ROLE};
        """
    )
    op.execute(
        f"""
        DO $$
        BEGIN
          EXECUTE format(
            'GRANT CONNECT ON DATABASE %I TO {RUNTIME_ROLE}',
            current_database()
          );
          EXECUTE format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
            'GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {RUNTIME_ROLE}',
            current_user
          );
          EXECUTE format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
            'GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {RUNTIME_ROLE}',
            current_user
          );
        END;
        $$;
        """
    )


def downgrade() -> None:
    op.execute(
        f"""
        DO $$
        BEGIN
          EXECUTE format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
            'REVOKE SELECT, INSERT, UPDATE, DELETE ON TABLES FROM {RUNTIME_ROLE}',
            current_user
          );
          EXECUTE format(
            'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public '
            'REVOKE USAGE, SELECT, UPDATE ON SEQUENCES FROM {RUNTIME_ROLE}',
            current_user
          );
        END;
        $$;

        REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public FROM {RUNTIME_ROLE};
        REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM {RUNTIME_ROLE};
        REVOKE USAGE ON SCHEMA public FROM {RUNTIME_ROLE};
        ALTER ROLE {RUNTIME_ROLE} RESET row_security;
        """
    )
