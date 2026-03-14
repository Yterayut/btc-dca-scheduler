"""Bootstrap helpers for runtime environment and client setup."""

from __future__ import annotations

import os

from binance.client import Client
from dotenv import load_dotenv


def env_flag(name: str, default: bool = False) -> bool:
    val = os.getenv(name)
    if val is None:
        return default
    return str(val).strip().lower() in ("1", "true", "yes", "on")


def load_required_env_vars() -> dict[str, str]:
    load_dotenv()
    required_env_vars = {
        "DB_HOST": os.getenv("DB_HOST"),
        "DB_USER": os.getenv("DB_USER"),
        "DB_PASSWORD": os.getenv("DB_PASSWORD"),
        "DB_NAME": os.getenv("DB_NAME"),
        "BINANCE_API_KEY": os.getenv("BINANCE_API_KEY"),
        "BINANCE_API_SECRET": os.getenv("BINANCE_API_SECRET"),
    }
    missing_vars = [key for key, value in required_env_vars.items() if value is None]
    if missing_vars:
        raise ValueError(f"Missing environment variables: {', '.join(missing_vars)}")
    return required_env_vars


def create_binance_client(required_env_vars: dict[str, str], *, testnet: bool) -> Client:
    return Client(
        required_env_vars["BINANCE_API_KEY"],
        required_env_vars["BINANCE_API_SECRET"],
        testnet=testnet,
        requests_params={"timeout": 15},
        ping=False,
    )
