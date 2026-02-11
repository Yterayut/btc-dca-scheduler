#!/usr/bin/env python3
"""
Rotate APP_ENCRYPTION_KEY and re-encrypt compliance_audit_log metadata.

Usage:
    python scripts/rotate_encryption_key.py --new-key <key>
    python scripts/rotate_encryption_key.py  # auto-generate key
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import MySQLdb
from cryptography.fernet import Fernet
from dotenv import load_dotenv

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

import security_utils  # noqa: E402


def _connect() -> MySQLdb.Connection:
    return MySQLdb.connect(
        host=os.getenv("DB_HOST"),
        user=os.getenv("DB_USER"),
        passwd=os.getenv("DB_PASSWORD"),
        db=os.getenv("DB_NAME"),
        charset="utf8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Rotate APP_ENCRYPTION_KEY for compliance metadata.")
    parser.add_argument("--new-key", dest="new_key", help="Provide a new APP_ENCRYPTION_KEY (Base64). Optional.")
    parser.add_argument("--dry-run", action="store_true", help="Decode entries and report but do not write changes.")
    args = parser.parse_args()

    load_dotenv()

    old_key = os.getenv("APP_ENCRYPTION_KEY")
    if not old_key:
        raise SystemExit("APP_ENCRYPTION_KEY must be set in environment to rotate keys.")

    new_key = args.new_key or Fernet.generate_key().decode()

    # Load rows
    conn = _connect()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, metadata_blob, metadata_encrypted FROM compliance_audit_log ORDER BY id ASC"
    )
    rows = cur.fetchall()

    # Prepare decrypt with old key
    os.environ["APP_ENCRYPTION_KEY"] = old_key
    security_utils.get_cipher.cache_clear()

    decoded: dict[int, tuple[dict[str, Any], bool]] = {}
    for row in rows:
        item_id, blob, encrypted_flag = row
        if not blob:
            decoded[item_id] = ({}, False)
            continue
        try:
            payload = security_utils.decrypt_metadata(blob, encrypted=bool(encrypted_flag))
            decoded[item_id] = (payload, True)
        except Exception as exc:
            raise SystemExit(f"Failed to decrypt record id={item_id}: {exc}") from exc

    if args.dry_run:
        print(f"[DRY RUN] Decoded {len(decoded)} records. New key would be: {new_key}")
        return

    # Re-encrypt with new key
    os.environ["APP_ENCRYPTION_KEY"] = new_key
    security_utils.get_cipher.cache_clear()

    for item_id, (payload, _) in decoded.items():
        token, encrypted = security_utils.encrypt_metadata(payload)
        cur.execute(
            """
            UPDATE compliance_audit_log
            SET metadata_blob=%s, metadata_encrypted=%s
            WHERE id=%s
            """,
            (token, 1 if encrypted else 0, item_id),
        )

    conn.commit()
    cur.close()
    conn.close()

    print("Rotation complete.")
    print("New APP_ENCRYPTION_KEY:")
    print(new_key)


if __name__ == "__main__":
    main()
