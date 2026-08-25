#!/usr/bin/env sh

set -eu

# Password input is environment-only so it cannot appear in process arguments.
exec python -m app.provision_clinic_admin "$@"
