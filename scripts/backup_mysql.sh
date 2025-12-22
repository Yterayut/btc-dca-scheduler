#!/usr/bin/env bash
set -euo pipefail

DEFAULTS_FILE="${MYSQL_DEFAULTS_FILE:-$HOME/.my.cnf}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/mysql}"
DB_NAME="${DB_NAME:-}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-30}"

if [[ -z "${DB_NAME}" ]]; then
  echo "ERROR: DB_NAME is not set (export DB_NAME or ensure it's in the EnvironmentFile)." >&2
  exit 2
fi

if [[ ! -f "${DEFAULTS_FILE}" ]]; then
  echo "ERROR: MySQL defaults file not found: ${DEFAULTS_FILE}" >&2
  echo "Create it (chmod 600) or set MYSQL_DEFAULTS_FILE=/path/to/file." >&2
  exit 2
fi

mkdir -p "${BACKUP_DIR}"

ts="$(date +%F_%H%M%S)"
out="${BACKUP_DIR}/${DB_NAME}_${ts}.sql.gz"

mysqldump \
  --defaults-extra-file="${DEFAULTS_FILE}" \
  --single-transaction \
  --quick \
  --routines \
  --events \
  --triggers \
  --no-tablespaces \
  --column-statistics=0 \
  "${DB_NAME}" | gzip -c > "${out}"

echo "OK: wrote ${out}"

if [[ "${KEEP_DAYS}" =~ ^[0-9]+$ ]] && [[ "${KEEP_DAYS}" -gt 0 ]]; then
  find "${BACKUP_DIR}" -type f -name "${DB_NAME}_*.sql.gz" -mtime +"${KEEP_DAYS}" -delete || true
fi
