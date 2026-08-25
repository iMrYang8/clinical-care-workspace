#! /usr/bin/env bash

set -e
set -x

# Let the DB start
python app/backend_pre_start.py

# Create/harden the LOGIN NOSUPERUSER NOBYPASSRLS role before grants run.
python app/configure_db_roles.py

# Run migrations
alembic upgrade head

# Create initial data in DB
python app/initial_data.py
