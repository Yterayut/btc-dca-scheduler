import os
from collections.abc import Sequence

from .binance import BinanceAdapter
from .bitkub import BitkubAdapter
from .okx import OkxAdapter


def get_adapter(exchange: str | None = None, testnet: bool = False, dry_run: bool = False):
    ex = (exchange or os.getenv("EXCHANGE") or "binance").strip().lower()
    if ex == "okx":
        return OkxAdapter(testnet=testnet, dry_run=dry_run)
    if ex == "bitkub":
        return BitkubAdapter(testnet=testnet, dry_run=dry_run)
    return BinanceAdapter(testnet=testnet, dry_run=dry_run)


def get_adapters(
    exchanges: Sequence[str],
    *,
    testnet: bool = False,
    dry_run: bool = False,
    **adapter_kwargs,
) -> dict[str, object]:
    """
    Instantiate adapters for all requested exchanges.

    Unknown exchanges raise ValueError to surface configuration issues early.
    Additional kwargs are forwarded to each adapter.
    """
    adapters: dict[str, object] = {}
    for raw in exchanges or []:
        if raw is None:
            continue
        slug = str(raw).strip().lower()
        if not slug:
            continue
        if slug == "binance" and "binance" not in adapters:
            adapters["binance"] = BinanceAdapter(
                testnet=testnet, dry_run=dry_run, **adapter_kwargs
            )
        elif slug == "okx" and "okx" not in adapters:
            adapters["okx"] = OkxAdapter(testnet=testnet, dry_run=dry_run, **adapter_kwargs)
        elif slug == "bitkub" and "bitkub" not in adapters:
            adapters["bitkub"] = BitkubAdapter(testnet=testnet, dry_run=dry_run, **adapter_kwargs)
        else:
            if slug not in ("binance", "okx", "bitkub"):
                raise ValueError(f"Unsupported exchange '{raw}'")
    return adapters
