#!/bin/sh
set -eu

export POSTGRES_PASSWORD="$(cat /run/secrets/annotation-db-password)"
exec /app/run.sh
