"""Helpers for loading schedule context used in notifications."""

from __future__ import annotations

import json
import logging


def fetch_schedule_context_with_connection(schedule_id: int, get_connection) -> dict:
    """Load schedule metadata (time/label) for notifications."""
    if not schedule_id:
        return {}

    conn = None
    cursor = None
    context: dict[str, str] = {}

    try:
        conn = get_connection()
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM schedules WHERE id = %s LIMIT 1", (schedule_id,))
        row = cursor.fetchone()
        if not row:
            return {}

        columns = [desc[0] for desc in cursor.description]
        row_dict = dict(zip(columns, row))

        time_value = row_dict.get("schedule_time")
        if hasattr(time_value, "strftime"):
            context["time"] = time_value.strftime("%H:%M")
        elif isinstance(time_value, str):
            cleaned = time_value.strip()
            if len(cleaned) >= 5 and cleaned[2] == ":":
                context["time"] = cleaned[:5]
            else:
                context["time"] = cleaned or None
        elif time_value is not None:
            context["time"] = str(time_value)

        label = None
        for key in (
            "slot_label",
            "label",
            "name",
            "title",
            "line_channel",
            "line_label",
            "line_topic",
            "channel_label",
            "display_name",
        ):
            value = row_dict.get(key)
            if value:
                label = str(value)
                break

        if not label:
            meta_value = row_dict.get("metadata") or row_dict.get("meta") or row_dict.get("extra") or row_dict.get("config_json")
            if meta_value:
                try:
                    if isinstance(meta_value, (bytes, bytearray)):
                        meta_value = meta_value.decode("utf-8")
                    meta_obj = json.loads(meta_value) if isinstance(meta_value, str) else meta_value
                    if isinstance(meta_obj, dict):
                        for key in (
                            "slot_label",
                            "label",
                            "name",
                            "title",
                            "line_channel",
                            "line_label",
                            "line_topic",
                            "channel_label",
                            "display_name",
                        ):
                            if meta_obj.get(key):
                                label = str(meta_obj[key])
                                break
                except Exception:
                    pass

        if label:
            context["label"] = label

    except Exception as exc:
        logging.debug(f"Schedule context lookup failed for id={schedule_id}: {exc}")
    finally:
        try:
            if cursor:
                cursor.close()
        except Exception:
            pass
        try:
            if conn:
                conn.close()
        except Exception:
            pass

    return context
