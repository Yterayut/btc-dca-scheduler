#!/usr/bin/env python3
"""Utility to preview Flex message payloads in the terminal."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from notifications.line_flex import build_basic_bubble, make_flex_message


def load_environment(env_path: str | None) -> None:
    if env_path:
        load_dotenv(env_path)
    else:
        load_dotenv()


def sample_payload(kind: str) -> dict:
    if kind == "weekly_dca":
        bubble = build_basic_bubble(
            "Weekly DCA Buy",
            [
                ("Asset", "BTC | Binance"),
                ("Amount", "50.00 USDT"),
                ("Filled", "0.001234 BTC @ 40,520"),
                ("Schedule", "#3"),
            ],
            subtitle="CDC status: UP / Mode: LIVE",
            footer_note="Preview payload for Flex simulator",
        )
        return make_flex_message("Weekly DCA Buy preview", bubble)
    if kind == "s4_dca":
        bubble = build_basic_bubble(
            "S4 DCA Buy",
            [
                ("Asset", "XAUT | OKX"),
                ("Amount", "15.00 USDT"),
                ("Qty", "0.0035 XAUT @ 4,240"),
                ("Schedule", "#20"),
            ],
            subtitle="CDC status: DOWN / Mode: DRY RUN",
            footer_note="S4 overlay channel",
            theme="info",
        )
        return make_flex_message("S4 DCA Buy preview", bubble)
    raise ValueError(f"Unknown sample kind: {kind}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Render Flex message JSON for preview.")
    parser.add_argument(
        "--kind",
        default="weekly_dca",
        choices=["weekly_dca", "s4_dca"],
        help="Type of sample payload to render",
    )
    parser.add_argument(
        "--output",
        "-o",
        type=Path,
        help="Optional file to write JSON (pretty-printed)",
    )
    parser.add_argument("--env-file", help="Optional .env file to load before rendering")
    args = parser.parse_args()

    load_environment(args.env_file)

    payload = sample_payload(args.kind)
    if args.output:
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
        print(f"Saved preview to {args.output}")
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
