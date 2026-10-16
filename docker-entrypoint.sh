#!/bin/sh
set -eu

mkdir -p /app/data /app/backups
chown -R app:app /app/data /app/backups

exec gosu app "$@"
