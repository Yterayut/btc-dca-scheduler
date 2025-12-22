#!/usr/bin/env bash
set -euo pipefail

DEFAULTS_FILE="${MYSQL_DEFAULTS_FILE:-$HOME/.my.cnf}"
BACKUP_DIR="${BACKUP_DIR:-$HOME/backups/mysql}"
DB_NAME="${DB_NAME:-btc_dca}"
DRILL_DB_NAME="${DRILL_DB_NAME:-btc_dca_test}"
BACKUP_FILE="${BACKUP_FILE:-}"

if [[ ! -f "${DEFAULTS_FILE}" ]]; then
  echo "ERROR: MySQL defaults file not found: ${DEFAULTS_FILE}" >&2
  exit 2
fi

if [[ -z "${BACKUP_FILE}" ]]; then
  BACKUP_FILE="$(ls -1t "${BACKUP_DIR}/${DB_NAME}"_*.sql.gz 2>/dev/null | head -n 1 || true)"
fi

if [[ -z "${BACKUP_FILE}" || ! -f "${BACKUP_FILE}" ]]; then
  echo "ERROR: Backup file not found. Set BACKUP_FILE=/path/to/file.sql.gz" >&2
  exit 2
fi

mysql_cmd=(mysql --defaults-extra-file="${DEFAULTS_FILE}")

echo "Using backup: ${BACKUP_FILE}"

cleanup() {
  "${mysql_cmd[@]}" -e "DROP DATABASE IF EXISTS ${DRILL_DB_NAME};" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "Creating drill database: ${DRILL_DB_NAME}"
"${mysql_cmd[@]}" -e "CREATE DATABASE ${DRILL_DB_NAME};"

echo "Restoring..."
zcat "${BACKUP_FILE}" | "${mysql_cmd[@]}" "${DRILL_DB_NAME}"

echo "--- REAL DB ---"
real_count=$("${mysql_cmd[@]}" -D "${DB_NAME}" -N -e "SELECT COUNT(*) FROM purchase_history;")
echo "purchase_history: ${real_count}"

echo "--- TEST DB ---"
test_count=$("${mysql_cmd[@]}" -D "${DRILL_DB_NAME}" -N -e "SELECT COUNT(*) FROM purchase_history;")
echo "purchase_history: ${test_count}"

if [[ "${real_count}" != "${test_count}" ]]; then
  echo "FAIL: counts do not match." >&2
  exit 1
fi

echo "PASS: restore drill completed successfully."
