#!/bin/sh
set -eu

export MINIO_ROOT_USER="$(cat /run/secrets/minio-access-key)"
export MINIO_ROOT_PASSWORD="$(cat /run/secrets/minio-secret-key)"
exec /usr/bin/docker-entrypoint.sh "$@"
