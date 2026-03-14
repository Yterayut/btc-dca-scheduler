import asyncio
from copy import deepcopy
from flask import Flask, render_template, request, redirect, g, flash, jsonify
from flask_socketio import SocketIO, emit
import json
import MySQLdb
import requests
from datetime import datetime, timedelta, timezone
import os
import time
from dotenv import load_dotenv
import logging
from logging.handlers import RotatingFileHandler
from apscheduler.schedulers.background import BackgroundScheduler
import functools
from contextlib import contextmanager
from math import isfinite
from pytz import timezone
from binance.client import Client
from notify import send_line_message_with_retry, notify_cdc_toggle
from exchanges.factory import get_adapter
from main import increment_reserve, increment_reserve_exchange
from compliance import fetch_events
from services.balance_service import fetch_balances
from services.bootstrap import create_binance_client, env_flag as shared_env_flag
from strategies.s4_observability import (
    derive_shadow_decision,
    mismatch_severity,
    next_unlock_from_gate_reason,
    normalize_reason_filter,
    parse_bool,
)
from decimal import Decimal
from pathlib import Path

try:
    # Optional import for price/balance
    from utils import get_btc_price, get_client
except Exception:
    get_btc_price = None
    get_client = None

# ตั้งค่า logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s',
    handlers=[
        RotatingFileHandler('app.log', maxBytes=5 * 1024 * 1024, backupCount=5),
        logging.StreamHandler()
    ]
)

# โหลด environment variables
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', 'your-secret-key')
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.jinja_env.auto_reload = True
# Allow narrowing CORS origin in production via env CORS_ORIGIN (comma-separated allowed origins or '*')
_cors_origins = os.getenv('CORS_ORIGIN', '*')
if _cors_origins and _cors_origins != '*':
    _cors_origins = [o.strip() for o in _cors_origins.split(',') if o.strip()]
socketio = SocketIO(app, cors_allowed_origins=_cors_origins or "*")

# Track app start for diagnostics
APP_START_TS = time.time()
REPO_ROOT = Path(__file__).resolve().parent

# Serve a tiny in-memory favicon to avoid 404 noise
try:
    from flask import send_file
    import base64, io

    _FAVICON_PNG_B64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR4nGNgYAAAAAMAASsJTYQAAAAASUVORK5CYII="
    )

    @app.route('/favicon.ico')
    @app.route('/static/favicon.ico')
    def _serve_favicon():
        try:
            data = base64.b64decode(_FAVICON_PNG_B64)
            return send_file(io.BytesIO(data), mimetype='image/png')
        except Exception:
            # As a safe fallback, return 204 No Content
            from flask import Response
            return Response(status=204)
except Exception:
    pass

# ====== Strategy Metadata Defaults ======
DEFAULT_STRATEGY_METADATA = {
    'cdc_dca_v1': {
        'display_name': 'CDC Reserve DCA',
        'short_name': 'CDC',
        'description': 'Classic color-based DCA with reserve deployment orchestrated through CDC signals.',
        'category': 'core',
        'status': 'active',
        'allocation': {
            'target_pct': 65.0,
            'capital_source': 'auto_total_active_amount',
            'buckets': [
                {'label': 'Weekly DCA', 'target_pct': 45.0},
                {'label': 'Reserve Deployments', 'target_pct': 20.0}
            ]
        },
        'log_filters': [
            {'id': 'cdc', 'label': 'CDC Actions'},
            {'id': 'reserve', 'label': 'Reserve Moves'},
            {'id': 'schedule', 'label': 'Schedule Engine'},
            {'id': 'compliance', 'label': 'Compliance / Security'}
        ],
        'help_overlay': {
            'title': 'CDC Strategy Overview',
            'bullets': [
                'Buys BTC automatically while CDC status is UP.',
                'On status flip to DOWN the strategy harvests profits and tops up reserves.',
                'Half-sell policy and exchange split can be tuned in the controls below.'
            ],
            'links': [
                {'label': 'CDC Playbook', 'href': 'https://docs.internal/strategies/cdc'},
                {'label': 'Reserve Guard Checklist', 'href': 'https://docs.internal/checklists/reserve-guard'}
            ]
        },
        'guards': [
            {'id': 'spread_guard', 'label': 'Spread Guard', 'status': 'active', 'description': 'Prevents half-sell/reserve buy when spread widens beyond guard rails.'},
            {'id': 'liquidity_guard', 'label': 'Liquidity Guard', 'status': 'active', 'description': 'Checks top-of-book spread and rejects thin markets.'},
            {'id': 'depth_guard', 'label': 'Depth Guard', 'status': 'active', 'description': 'Requires aggregated order book depth within ±1% band to exceed configured floor.'},
            {'id': 'twap_guard', 'label': 'TWAP Guard', 'status': 'active', 'description': 'Blocks orders when spot drifts beyond TWAP deviation threshold.'},
            {'id': 'notional_cap', 'label': 'Notional Cap', 'status': 'active', 'description': 'Hard limit per order notional via strategy_state.*_max_usdt.'}
        ]
    },
    's4_multi_leg': {
        'display_name': 'S4 Swing Overlay',
        'short_name': 'S4',
        'description': 'Four-signal swing allocator combining CDC, TWAP, depth and flow to stage opportunistic entries.',
        'category': 'overlay',
        'status': 'active',
        'allocation': {
            'target_pct': 35.0,
            'capital_source': 'manual',
            'capital_usdt': 10000.0,
            'buckets': [
                {'label': 'Spot Core', 'target_pct': 20.0},
                {'label': 'TWAP Adds', 'target_pct': 7.5},
                {'label': 'Hedge Offsets', 'target_pct': 7.5}
            ]
        },
        'config': {
            'target_btc_pct_up': 0.65,
            'target_btc_pct_down': 0.35,
            'rebalance_threshold_pct': 5.0,
            'min_flip_usd': 500.0,
            'max_flip_pct': 35.0,
            'cooldown_minutes': 90,
            'capital_usdt': 10000.0,
            'exchange': 'okx'
        },
        'log_filters': [
            {'id': 's4', 'label': 'S4 Signals'},
            {'id': 'twap', 'label': 'TWAP Windows'},
            {'id': 'hedge', 'label': 'Hedge Actions'}
        ],
        'help_overlay': {
            'title': 'S4 Quick Start',
            'bullets': [
                'S4 listens to CDC status plus proprietary flow signals.',
                'Deploys capital in four legs: base, add, hedge, unwind.',
                'Enable once depth/TWAP guards are calibrated and monitors lit green.'
            ],
            'links': [
                {'label': 'S4 Concept Note', 'href': 'https://docs.internal/strategies/s4'},
                {'label': 'Guard Calibration Sheet', 'href': 'https://docs.internal/guard-calibration'}
            ]
        },
        'guards': [
            {'id': 'depth_guard', 'label': 'Depth Guard', 'status': 'pending', 'description': 'Requires aggregated order book depth >= 5M USDT within 1% bands.'},
            {'id': 'twap_guard', 'label': 'TWAP Guard', 'status': 'pending', 'description': 'Ensures TWAP deviation stays within configured tolerance before staging orders.'},
            {'id': 'notional_cap', 'label': 'Notional Cap', 'status': 'planning', 'description': 'Hard caps per asset to avoid oversizing during volatile sessions.'}
        ]
    },
    'bitkub_dca_v1': {
        'display_name': 'Bitkub THB DCA',
        'short_name': 'BITKUB',
        'description': 'Direct BTC/THB DCA on Bitkub. Buys every schedule run without CDC gate.',
        'category': 'dca',
        'status': 'active',
        'allocation': {
            'target_pct': 0.0,
            'capital_source': 'thb_schedule_total',
            'buckets': [
                {'label': 'Scheduled THB DCA', 'target_pct': 100.0}
            ]
        },
        'log_filters': [
            {'id': 'bitkub', 'label': 'Bitkub Orders'},
            {'id': 'schedule', 'label': 'Schedule Engine'}
        ],
        'help_overlay': {
            'title': 'Bitkub DCA Overview',
            'bullets': [
                'Places market buy on BTC_THB at each active schedule time.',
                'No CDC dependency in this mode.',
                'Minimum order follows Bitkub market constraints.'
            ],
            'links': []
        },
        'guards': [
            {'id': 'min_quote_guard', 'label': 'Minimum Quote Guard', 'status': 'active', 'description': 'Rejects order if amount is below market min quote size.'},
            {'id': 'step_guard', 'label': 'Step/Tick Guard', 'status': 'active', 'description': 'Uses exchange filters for step size and price tick compatibility.'}
        ]
    }
}


def _merge_strategy_dict(base: dict, override: dict) -> dict:
    """Recursively merge strategy metadata dictionaries."""
    if not isinstance(base, dict):
        return {}
    if not isinstance(override, dict):
        return base
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_strategy_dict(merged[key], value)  # type: ignore[arg-type]
        else:
            merged[key] = value
    return merged


def _strategy_metadata_for(mode: str, override_raw: str | None) -> dict:
    """Return default metadata merged with DB overrides for a strategy mode."""
    base = deepcopy(DEFAULT_STRATEGY_METADATA.get(mode, {}))
    if override_raw:
        try:
            override = json.loads(override_raw)
            base = _merge_strategy_dict(base, override if isinstance(override, dict) else {})
        except json.JSONDecodeError as exc:
            logging.warning(f"Invalid metadata_json for strategy {mode}: {exc}")
    return base


def _dt_to_iso(value):
    """Convert datetime to ISO format string if possible."""
    if isinstance(value, datetime):
        return value.isoformat()
    return None


def _json_sanitize(value):
    """Recursively convert values so json.dumps works (datetime → iso, Decimal → float)."""
    if isinstance(value, dict):
        return {k: _json_sanitize(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_sanitize(v) for v in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    return value


def _safe_float(value) -> float:
    """Convert input to float with safe fallback."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _safe_int(value):
    """Convert input to int when possible."""
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None

# Ensure API requests never return HTML error pages
@app.errorhandler(404)
def handle_404(e):
    try:
        if request.path.startswith('/api/'):
            return jsonify({'ok': False, 'error': 'not_found', 'path': request.path}), 404
    except Exception:
        pass
    return e

@app.errorhandler(405)
def handle_405(e):
    try:
        if request.path.startswith('/api/'):
            return jsonify({'ok': False, 'error': 'method_not_allowed', 'path': request.path}), 405
    except Exception:
        pass
    return e

# ตรวจสอบ environment variables
required_env_vars = ['DB_HOST', 'DB_USER', 'DB_PASSWORD', 'DB_NAME', 'LINE_CHANNEL_ACCESS_TOKEN']
missing_vars = [var for var in required_env_vars if not os.getenv(var)]
if missing_vars:
    logging.error(f"Missing environment variables: {', '.join(missing_vars)}")
    raise ValueError(f"Missing environment variables: {', '.join(missing_vars)}")

# ตัวแปร global สำหรับติดตามสถานะ
last_scheduler_status = "Scheduler is running"
last_notify_time = None
NOTIFY_COOLDOWN = 300  # 5 นาที
migration_completed = False

# ====== CDC Action Zone Cache ======
_CDC_CACHE = {
    'data': None,     # last computed payload
    'expires': 0      # epoch seconds
}

def _ema(series, period: int):
    """Simple EMA implementation compatible enough for CDC.
    Returns list of EMA values with same length as input.
    """
    if not series:
        return []
    if period <= 1:
        return list(series)
    k = 2 / (period + 1)
    ema_vals = []
    prev = series[0]
    ema_vals.append(prev)
    for x in series[1:]:
        prev = (x * k) + (prev * (1 - k))
        ema_vals.append(prev)
    return ema_vals

def _last_true_index(flags):
    for i in range(len(flags) - 1, -1, -1):
        if flags[i]:
            return i
    return None

# ====== Custom Jinja2 filter ======
def floatformat(value, decimal_places=2):
    """Format ค่าทศนิยมเหมือน Django's floatformat"""
    try:
        return f"{float(value):.{decimal_places}f}"
    except (ValueError, TypeError) as e:
        logging.warning(f"floatformat error: value={value}, error={e}")
        return str(value)

app.jinja_env.filters['floatformat'] = floatformat

# ====== Database Connection Management ======
@contextmanager
def get_db_cursor():
    """Context manager สำหรับจัดการ database connection"""
    db = None
    cursor = None
    try:
        db = MySQLdb.connect(
            host=os.getenv('DB_HOST'),
            user=os.getenv('DB_USER'),
            passwd=os.getenv('DB_PASSWORD'),
            db=os.getenv('DB_NAME'),
            charset='utf8',
            autocommit=False
        )
        cursor = db.cursor()
        yield cursor, db
    except MySQLdb.Error as e:
        if db:
            db.rollback()
        logging.error(f"Database error: {e}")
        raise
    finally:
        if cursor:
            cursor.close()
        if db:
            db.close()

def get_db_connection():
    """Legacy function สำหรับ backward compatibility"""
    try:
        if 'db' not in g:
            g.db = MySQLdb.connect(
                host=os.getenv('DB_HOST'),
                user=os.getenv('DB_USER'),
                passwd=os.getenv('DB_PASSWORD'),
                db=os.getenv('DB_NAME'),
                charset='utf8'
            )
            logging.debug("New database connection established")
        else:
            g.db.ping(reconnect=True)
            logging.debug("Database connection reused")
        return g.db
    except MySQLdb.OperationalError as e:
        logging.error(f"Database connection error: {e}")
        raise


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _infer_s4_asset_from_purchase(price: float | None, fee_asset: str | None) -> str | None:
    asset_hint = (fee_asset or "").strip().upper()
    if asset_hint in ("BTC", "XAUT", "PAXG"):
        return "BTC" if asset_hint == "BTC" else "GOLD"
    price_f = _safe_float(price, 0.0)
    if price_f <= 0:
        return None
    threshold = _safe_float(os.getenv("S4_PNL_BTC_PRICE_THRESHOLD", "10000"), 10000.0)
    return "BTC" if price_f >= threshold else "GOLD"


def _infer_s4_asset_from_symbol(symbol: str | None) -> str | None:
    sym = (symbol or "").upper()
    if "XAUT" in sym or "PAXG" in sym:
        return "GOLD"
    if "BTC" in sym:
        return "BTC"
    return None


def _load_s4_fifo_open_lots(cursor, exchange: str, asset: str) -> list[dict]:
    lots: list[dict] = []
    cursor.execute(
        """
        SELECT purchase_time, btc_quantity, usdt_amount, btc_price, fee_buy_asset
        FROM purchase_history
        WHERE exchange = %s
        ORDER BY purchase_time ASC
        """,
        (exchange,),
    )
    purchases = cursor.fetchall()
    cursor.execute(
        """
        SELECT symbol, btc_quantity
        FROM sell_history
        WHERE exchange = %s
        ORDER BY sell_time ASC
        """,
        (exchange,),
    )
    sells = cursor.fetchall()

    for purchase_time, qty, notional, price, fee_asset in purchases:
        inferred = _infer_s4_asset_from_purchase(price, fee_asset)
        if inferred != asset:
            continue
        qty_f = _safe_float(qty, 0.0)
        if qty_f <= 0:
            continue
        notional_f = _safe_float(notional, 0.0)
        cost_per_unit = notional_f / qty_f if qty_f else 0.0
        lots.append({"qty": qty_f, "cost": cost_per_unit, "timestamp": purchase_time})

    for symbol, sell_qty in sells:
        inferred = _infer_s4_asset_from_symbol(symbol)
        if inferred != asset:
            continue
        remaining = _safe_float(sell_qty, 0.0)
        idx = 0
        while remaining > 0 and idx < len(lots):
            lot = lots[idx]
            available = _safe_float(lot.get("qty"), 0.0)
            if available <= 0:
                idx += 1
                continue
            consume = min(available, remaining)
            lot["qty"] = max(0.0, available - consume)
            remaining -= consume
            if lot["qty"] <= 1e-9:
                lot["qty"] = 0.0
            else:
                idx += 1
    return [lot for lot in lots if _safe_float(lot.get("qty"), 0.0) > 1e-9]


def _sum_lots_cost(lots: list[dict]) -> float:
    return sum(_safe_float(lot.get("qty")) * _safe_float(lot.get("cost")) for lot in lots)


def _s4_confirm_streak(history: list[dict], status: str | None) -> int:
    if not history:
        return 0
    target = (status or "").lower()
    if not target:
        return 0
    items = [h for h in history if isinstance(h, dict) and h.get("date")]
    items.sort(key=lambda x: x.get("date") or "")
    streak = 0
    last_date = None
    for entry in reversed(items):
        if str(entry.get("status") or "").lower() != target:
            break
        try:
            current_date = datetime.fromisoformat(str(entry.get("date"))).date()
        except Exception:
            break
        if last_date and (last_date - current_date).days != 1:
            break
        streak += 1
        last_date = current_date
    return streak


def _normalize_s4_runtime_aliases(runtime: dict | None) -> dict:
    if not isinstance(runtime, dict):
        return {}
    normalized = dict(runtime)
    holding_asset = normalized.get("holding_asset") or normalized.get("active_asset")
    if holding_asset:
        normalized["holding_asset"] = holding_asset
        normalized["active_asset"] = holding_asset
    target_asset = normalized.get("signal_target_asset") or holding_asset
    if target_asset:
        normalized["signal_target_asset"] = target_asset
    return normalized


def _build_s4_status_data() -> dict:
    data: dict = {
        "active_asset": "UNKNOWN",
        "holding_asset": "UNKNOWN",
        "cdc_status": "N/A",
        "signal_source": "N/A",
        "signal_time": "N/A",
        "exchange": "okx",
        "portfolio": {},
        "gates": {},
        "last_status": {},
        "last_error": {},
        "last_rotation": {},
        "shadow_swap": {"count_90d": 0, "last": {}, "recent": []},
        "signal_layers": {
            "eod": {},
            "runtime": {},
            "mismatch": False,
            "mismatch_streak_days": 0,
            "mismatch_severity": "match",
        },
        "why_not_flip": {},
    }
    with get_db_cursor() as (cursor, _):
        cursor.execute("SELECT * FROM strategy_state WHERE mode='s4_multi_leg' LIMIT 1")
        row = cursor.fetchone()
        if not row:
            data["error"] = "No S4 strategy state found."
            return data
        cols = [d[0] for d in cursor.description]
        record = dict(zip(cols, row))

        metadata_raw = record.get("metadata_json")
        metadata = {}
        if metadata_raw:
            try:
                metadata = json.loads(metadata_raw) if isinstance(metadata_raw, str) else metadata_raw
            except Exception:
                metadata = {}

        runtime = _normalize_s4_runtime_aliases(metadata.get("runtime") or {})
        config = metadata.get("config") or {}
        confirm_days = int(os.getenv("S4_CONFIRM_DAYS", "2") or 2)
        holding_asset = runtime.get("holding_asset") or runtime.get("active_asset") or "UNKNOWN"
        data["exchange"] = str(config.get("exchange") or "okx").lower()
        data["active_asset"] = holding_asset
        data["holding_asset"] = holding_asset
        data["signal_target_asset"] = runtime.get("signal_target_asset") or holding_asset
        data["cdc_status"] = str(runtime.get("last_cdc_status") or "N/A").upper()
        data["signal_source"] = runtime.get("signal_source") or "N/A"
        data["signal_time"] = runtime.get("last_signal_at") or "N/A"
        data["signal_layers"]["runtime"] = {
            "layer": "runtime_production",
            "runtime_ts_utc": runtime.get("last_signal_at") or "",
            "cdc_status_runtime": str(runtime.get("last_cdc_status") or "").lower(),
            "active_asset_runtime": holding_asset,
            "holding_asset_runtime": holding_asset,
            "signal_target_asset_runtime": runtime.get("signal_target_asset") or holding_asset,
            "signal_source_runtime": runtime.get("signal_source") or "",
            "mismatch_streak_event": str(runtime.get("mismatch_streak_event") or ""),
            "last_confirmed_status": str(runtime.get("last_confirmed_status") or "").lower(),
            "confirm_progress": {
                "streak": min(
                    _s4_confirm_streak(runtime.get("signal_history") or [], runtime.get("last_cdc_status")),
                    max(confirm_days, 0),
                ),
                "required_days": confirm_days,
            },
        }

        exp = runtime.get("exposure") if isinstance(runtime, dict) else {}
        total_usd = _safe_float(exp.get("total_usd"), 0.0) if isinstance(exp, dict) else 0.0
        btc_value = _safe_float((exp.get("btc") or {}).get("notional_usd"), 0.0) if isinstance(exp, dict) else 0.0
        gold_value = _safe_float((exp.get("gold") or {}).get("notional_usd"), 0.0) if isinstance(exp, dict) else 0.0
        btc_weight = _safe_float((exp.get("btc") or {}).get("weight"), 0.0) * 100 if isinstance(exp, dict) else 0.0
        gold_weight = _safe_float((exp.get("gold") or {}).get("weight"), 0.0) * 100 if isinstance(exp, dict) else 0.0

        exchange = data["exchange"]
        lots_btc = _load_s4_fifo_open_lots(cursor, exchange, "BTC")
        lots_gold = _load_s4_fifo_open_lots(cursor, exchange, "GOLD")
        cost_btc = _sum_lots_cost(lots_btc)
        cost_gold = _sum_lots_cost(lots_gold)
        cost_total = cost_btc + cost_gold
        pnl_total = total_usd - cost_total if cost_total > 0 else 0.0
        pnl_total_pct = (pnl_total / cost_total) * 100.0 if cost_total > 0 else 0.0

        def _pnl(value: float, cost: float) -> tuple[float, float]:
            if cost <= 0:
                return 0.0, 0.0
            pnl = value - cost
            pct = (pnl / cost) * 100.0
            return pnl, pct

        btc_pnl, btc_pnl_pct = _pnl(btc_value, cost_btc)
        gold_pnl, gold_pnl_pct = _pnl(gold_value, cost_gold)

        data["portfolio"] = {
            "total_usd": total_usd,
            "cost_total": cost_total,
            "pnl_total": pnl_total,
            "pnl_total_pct": pnl_total_pct,
            "btc": {
                "notional_usd": btc_value,
                "weight_pct": btc_weight,
                "cost": cost_btc,
                "pnl": btc_pnl,
                "pnl_pct": btc_pnl_pct,
            },
            "gold": {
                "notional_usd": gold_value,
                "weight_pct": gold_weight,
                "cost": cost_gold,
                "pnl": gold_pnl,
                "pnl_pct": gold_pnl_pct,
            },
        }

        data["gates"] = {
            "signal_history_len": len(runtime.get("signal_history") or []) if isinstance(runtime.get("signal_history"), list) else 0,
            "last_flip_at": runtime.get("last_flip_at") or "N/A",
            "flips_30d": runtime.get("flip_count_30d") or 0,
            "max_flips_30d": config.get("max_flips_30d") or os.getenv("S4_MAX_FLIPS_30D", "2"),
            "last_hold_reason": runtime.get("last_hold_reason") or "",
            "confirm_days": confirm_days,
            "confirm_streak": min(
                _s4_confirm_streak(runtime.get("signal_history") or [], runtime.get("last_cdc_status")),
                max(confirm_days, 0),
            ),
        }

        cursor.execute(
            """
            SELECT date, cdc_status, state, slope_pct, ema_gap_pct, eod_lag_days
            FROM s4_neutral_zone_eod
            ORDER BY date DESC
            LIMIT 1
            """
        )
        eod_row = cursor.fetchone()
        eod = {}
        if eod_row:
            eod_cols = [d[0] for d in cursor.description]
            eod = dict(zip(eod_cols, eod_row))
        eod_cdc = str((eod or {}).get("cdc_status") or "").lower()
        runtime_cdc = str(runtime.get("last_cdc_status") or "").lower()
        eod_lag_days = int(_safe_float((eod or {}).get("eod_lag_days"), 0.0))
        mismatch = bool(eod_cdc and runtime_cdc and eod_cdc != runtime_cdc)
        streak = int(_safe_float(runtime.get("mismatch_streak_days"), 0.0))
        severity = mismatch_severity(mismatch=mismatch, eod_lag_days=eod_lag_days, streak=streak)
        data["signal_layers"]["eod"] = {
            "layer": "eod_analytics",
            "asof_date": (eod or {}).get("date") or "",
            "snapshot_ts_utc": "",
            "cdc_status_eod": eod_cdc,
            "neutral_state_eod": str((eod or {}).get("state") or ""),
            "slope_pct_eod": _safe_float((eod or {}).get("slope_pct"), 0.0),
            "gap_pct_eod": _safe_float((eod or {}).get("ema_gap_pct"), 0.0),
            "eod_lag_days": eod_lag_days,
        }
        data["signal_layers"]["mismatch"] = mismatch
        data["signal_layers"]["mismatch_streak_days"] = streak
        data["signal_layers"]["mismatch_severity"] = severity
        fallback_event = "mismatch_detected" if mismatch else "match_state"
        data["signal_layers"]["mismatch_streak_event"] = str(runtime.get("mismatch_streak_event") or fallback_event)

        last_results = runtime.get("last_action_result")
        if isinstance(last_results, list) and last_results:
            res = last_results[0] if isinstance(last_results[0], dict) else {}
            data["last_status"] = {
                "status": res.get("status") or "N/A",
                "reason": res.get("reason") or "",
            }

        last_err = runtime.get("last_error")
        if isinstance(last_err, dict):
            data["last_error"] = {
                "at": last_err.get("at") or "",
                "reason": last_err.get("reason") or "",
                "detail": last_err.get("detail") or "",
            }

        cursor.execute(
            """
            SELECT executed_at, from_asset, to_asset, reason
            FROM strategy_rotation_log
            WHERE strategy_mode='s4_multi_leg'
              AND metadata_json LIKE '%"executed_ok": true%'
            ORDER BY executed_at DESC
            LIMIT 1
            """
        )
        rot_row = cursor.fetchone()
        if rot_row:
            rot_cols = [d[0] for d in cursor.description]
            data["last_rotation"] = dict(zip(rot_cols, rot_row))

        cursor.execute(
            """
            SELECT COUNT(*)
            FROM strategy_rotation_log
            WHERE strategy_mode='s4_multi_leg'
              AND reason IN ('shadow_swap_plan', 'shadow_swap_heartbeat')
              AND executed_at >= (UTC_TIMESTAMP() - INTERVAL 90 DAY)
            """
        )
        cnt_row = cursor.fetchone()
        data["shadow_swap"]["count_90d"] = int((cnt_row or [0])[0] or 0)

        cursor.execute(
            """
            SELECT executed_at, from_asset, to_asset, notional_usd, cdc_status, reason, metadata_json
            FROM strategy_rotation_log
            WHERE strategy_mode='s4_multi_leg'
              AND reason IN ('shadow_swap_plan', 'shadow_swap_heartbeat')
            ORDER BY executed_at DESC
            LIMIT 10
            """
        )
        recent_rows = cursor.fetchall() or []
        cols = [d[0] for d in cursor.description]
        recent: list[dict] = []
        for row in recent_rows:
            entry = dict(zip(cols, row))
            meta_raw = entry.get("metadata_json")
            if isinstance(meta_raw, str):
                try:
                    entry["metadata_json"] = json.loads(meta_raw)
                except Exception:
                    pass
            meta_obj = entry.get("metadata_json") if isinstance(entry.get("metadata_json"), dict) else {}
            gate_obj = meta_obj.get("gate") if isinstance(meta_obj.get("gate"), dict) else {}
            if gate_obj:
                gate_reason = str(gate_obj.get("reason") or entry.get("reason") or "")
                if not gate_obj.get("next_unlock_condition") or gate_obj.get("next_unlock_min_days") is None:
                    cond, min_days = next_unlock_from_gate_reason(
                        gate_reason,
                        btc_confirm_days=max(int(os.getenv("S4_SHADOW_BTC_CONFIRM_DAYS", "3") or 3), 0),
                        xau_confirm_days=max(int(os.getenv("S4_SHADOW_XAU_CONFIRM_DAYS", "5") or 5), 0),
                    )
                    gate_obj["next_unlock_condition"] = cond
                    gate_obj["next_unlock_min_days"] = min_days
                meta_obj["gate"] = gate_obj
                entry["metadata_json"] = meta_obj
            recent.append(entry)
        if recent:
            data["shadow_swap"]["last"] = recent[0]
            data["shadow_swap"]["recent"] = recent
            latest_heartbeat = next((r for r in recent if str(r.get("reason") or "") == "shadow_swap_heartbeat"), recent[0])
            hb_meta = latest_heartbeat.get("metadata_json") if isinstance(latest_heartbeat.get("metadata_json"), dict) else {}
            hb_gate = hb_meta.get("gate") if isinstance(hb_meta, dict) and isinstance(hb_meta.get("gate"), dict) else {}
            hb_reason = str(hb_gate.get("reason") or latest_heartbeat.get("reason") or "")
            unlock_cond = hb_gate.get("next_unlock_condition")
            unlock_days_raw = hb_gate.get("next_unlock_min_days")
            if not unlock_cond or unlock_days_raw is None:
                unlock_cond, unlock_days = next_unlock_from_gate_reason(
                    hb_reason,
                    btc_confirm_days=max(int(os.getenv("S4_SHADOW_BTC_CONFIRM_DAYS", "3") or 3), 0),
                    xau_confirm_days=max(int(os.getenv("S4_SHADOW_XAU_CONFIRM_DAYS", "5") or 5), 0),
                )
            else:
                unlock_days = int(_safe_float(unlock_days_raw, 0.0))
            data["why_not_flip"] = {
                "decision": hb_gate.get("decision") or "HOLD",
                "reason": hb_reason,
                "next_unlock_condition": unlock_cond,
                "next_unlock_min_days": unlock_days,
                "days_since_last_swap": int(_safe_float(hb_gate.get("days_since_last_swap"), 0.0)),
                "holding": hb_gate.get("holding") or latest_heartbeat.get("from_asset"),
                "target_asset": hb_gate.get("target_asset") or latest_heartbeat.get("to_asset"),
            }

    return data

@app.teardown_appcontext
def close_db_connection(exception):
    db = g.pop('db', None)
    if db is not None:
        db.close()
        logging.debug("Database connection closed")

# ====== Error Handler Decorator ======
def handle_db_errors(f):
    """Decorator สำหรับจัดการ database errors"""
    @functools.wraps(f)
    def wrapper(*args, **kwargs):
        try:
            return f(*args, **kwargs)
        except MySQLdb.IntegrityError as e:
            logging.error(f"Database integrity error in {f.__name__}: {e}")
            flash("Database constraint error. Please check your data.", 'error')
            return redirect('/')
        except MySQLdb.Error as e:
            logging.error(f"Database error in {f.__name__}: {e}")
            flash("Database error occurred.", 'error')
            return redirect('/')
        except Exception as e:
            logging.error(f"Unexpected error in {f.__name__}: {e}")
            flash(f"Unexpected error: {str(e)}", 'error')
            return redirect('/')
    return wrapper

# ====== Line Notify Functions ======
def send_line_notify(message):
    """ส่งข้อความผ่าน Line Notify"""
    try:
        url = 'https://notify-api.line.me/api/notify'
        token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
        headers = {'Authorization': f'Bearer {token}'}
        data = {'message': message}
        
        response = requests.post(url, headers=headers, data=data, timeout=10)
        success = response.status_code == 200
        
        if success:
            logging.info("Line Notify sent successfully")
        else:
            logging.error(f"Failed to send Line Notify: {response.status_code}")
            
        return success
    except requests.RequestException as e:
        logging.error(f"Line Notify request error: {e}")
        return False
    except Exception as e:
        logging.error(f"Line Notify unexpected error: {e}")
        return False

def check_scheduler_status():
    """ตรวจสอบสถานะ Scheduler และแจ้งเตือน"""
    global last_scheduler_status, last_notify_time
    
    try:
        health_check_port = os.getenv('HEALTH_CHECK_PORT', '8001')
        response = requests.get(f'http://localhost:{health_check_port}', timeout=5)
        current_status = response.text if response.status_code == 200 else 'Scheduler is not responding'
    except requests.RequestException:
        current_status = 'Scheduler is not responding'

    # บันทึกการเปลี่ยนสถานะ
    if current_status != last_scheduler_status:
        logging.info(f"Scheduler status changed: {last_scheduler_status} -> {current_status}")
        last_scheduler_status = current_status

    # แจ้งเตือนถ้า scheduler ไม่ตอบสนอง
    if current_status == 'Scheduler is not responding':
        current_time = datetime.now()
        if last_notify_time is None or (current_time - last_notify_time).total_seconds() >= NOTIFY_COOLDOWN:
            timestamp = current_time.strftime('%Y-%m-%d %H:%M:%S')
            message = f"⚠️ Scheduler Alert: Not responding at {timestamp}"
            if send_line_notify(message):
                last_notify_time = current_time

# ====== Background Tasks ======
def cleanup_old_logs():
    """ลบ log เก่า"""
    try:
        log_file = 'app.log'
        if os.path.exists(log_file):
            # Keep only last 10MB of logs
            max_size = 10 * 1024 * 1024  # 10MB
            if os.path.getsize(log_file) > max_size:
                with open(log_file, 'rb') as f:
                    f.seek(-max_size, 2)  # Seek to last 10MB
                    data = f.read()
                
                with open(log_file, 'wb') as f:
                    f.write(data)
                    
                logging.info("Log file trimmed to 10MB")
    except Exception as e:
        logging.error(f"Error cleaning up logs: {e}")

def update_cache_schedules():
    """อัปเดต cache ของ schedules"""
    try:
        # Simple cache clear for now
        logging.debug("Cache refresh triggered")
    except Exception as e:
        logging.error(f"Error updating schedule cache: {e}")

# ====== Data Migration ======
def migrate_data_if_needed():
    """ตรวจสอบและ migrate ข้อมูลถ้าจำเป็น"""
    global migration_completed
    
    if migration_completed:
        return True
    
    try:
        with get_db_cursor() as (cursor, db):
            # 1. ตรวจสอบและสร้างตาราง schedules
            cursor.execute("SHOW TABLES LIKE 'schedules'")
            if not cursor.fetchone():
                logging.info("Creating schedules table...")
                cursor.execute("""
                    CREATE TABLE schedules (
                        id INT AUTO_INCREMENT PRIMARY KEY,
                        schedule_time VARCHAR(5) NOT NULL,
                        schedule_day VARCHAR(255) NOT NULL,
                        purchase_amount DECIMAL(10,2) NOT NULL,
                        is_active TINYINT(1) DEFAULT 1,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
                    )
                """)
                db.commit()
                logging.info("Schedules table created")

            # 2. ตรวจสอบข้อมูลใน schedules
            cursor.execute("SELECT COUNT(*) FROM schedules")
            if cursor.fetchone()[0] == 0:
                logging.info("Migrating data...")
                
                # Migrate จาก config
                cursor.execute("SHOW TABLES LIKE 'config'")
                if cursor.fetchone():
                    cursor.execute("SELECT * FROM config WHERE id = 1")
                    config_data = cursor.fetchone()
                    if config_data:
                        _, purchase_amount, schedule_time, schedule_day = config_data
                        time_str = schedule_time.strftime('%H:%M') if hasattr(schedule_time, 'strftime') else str(schedule_time)
                        cursor.execute("""
                            INSERT INTO schedules (id, schedule_time, schedule_day, purchase_amount, is_active)
                            VALUES (%s, %s, %s, %s, %s)
                        """, (1, time_str, schedule_day, purchase_amount, 1))

                # เพิ่ม schedules ที่หายไป
                cursor.execute("SHOW TABLES LIKE 'purchase_history'")
                if cursor.fetchone():
                    cursor.execute("""
                        SELECT DISTINCT schedule_id, usdt_amount 
                        FROM purchase_history 
                        WHERE schedule_id IS NOT NULL 
                        AND schedule_id NOT IN (SELECT id FROM schedules WHERE 1=1)
                        ORDER BY schedule_id
                    """)
                    
                    missing_schedules = cursor.fetchall()
                    for schedule_id, amount in missing_schedules:
                        if schedule_id == 2:
                            cursor.execute("""
                                INSERT INTO schedules (id, schedule_time, schedule_day, purchase_amount, is_active)
                                VALUES (%s, %s, %s, %s, %s)
                            """, (2, '08:30', 'wednesday', amount, 1))
                        elif schedule_id == 3:
                            cursor.execute("""
                                INSERT INTO schedules (id, schedule_time, schedule_day, purchase_amount, is_active)
                                VALUES (%s, %s, %s, %s, %s)
                            """, (3, '07:00', 'monday', amount, 1))
                        else:
                            cursor.execute("""
                                INSERT INTO schedules (id, schedule_time, schedule_day, purchase_amount, is_active)
                                VALUES (%s, %s, %s, %s, %s)
                            """, (schedule_id, '12:00', 'monday', amount, 0))

                db.commit()
                logging.info("Data migration completed")

            # 3. ตรวจสอบ purchase_history table
            cursor.execute("SHOW TABLES LIKE 'purchase_history'")
            if not cursor.fetchone():
                logging.info("Creating purchase_history table...")
                cursor.execute("""
                    CREATE TABLE purchase_history (
                        id INT PRIMARY KEY AUTO_INCREMENT,
                        purchase_time DATETIME,
                        usdt_amount DECIMAL(10,2),
                        btc_quantity DECIMAL(18,8),
                        btc_price DECIMAL(18,2),
                        order_id BIGINT,
                        schedule_id INT,
                        INDEX idx_schedule_id (schedule_id),
                        INDEX idx_purchase_time (purchase_time)
                    )
                """)
                db.commit()

            # 3.1 ตรวจสอบ binance_trades table
            cursor.execute("SHOW TABLES LIKE 'binance_trades'")
            if not cursor.fetchone():
                logging.info("Creating binance_trades table...")
                cursor.execute(
                    """
                    CREATE TABLE binance_trades (
                        trade_id BIGINT PRIMARY KEY,
                        symbol VARCHAR(20) NOT NULL,
                        order_id BIGINT,
                        price DECIMAL(18,8),
                        qty DECIMAL(18,8),
                        quote_qty DECIMAL(18,8),
                        commission DECIMAL(18,8),
                        commission_asset VARCHAR(10),
                        is_buyer TINYINT(1),
                        is_maker TINYINT(1),
                        is_best_match TINYINT(1),
                        trade_time DATETIME,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_symbol_time (symbol, trade_time),
                        INDEX idx_order_id (order_id)
                    )
                    """
                )
                db.commit()
                logging.info("binance_trades table created")

            # 3.2 ตรวจสอบ strategy_state table
            cursor.execute("SHOW TABLES LIKE 'strategy_state'")
            if not cursor.fetchone():
                logging.info("Creating strategy_state table...")
                cursor.execute(
                    """
                    CREATE TABLE strategy_state (
                        id INT PRIMARY KEY AUTO_INCREMENT,
                        mode VARCHAR(32) NOT NULL,
                        last_cdc_status ENUM('up','down') NULL,
                        last_transition_at DATETIME NULL,
                        reserve_usdt DECIMAL(18,2) NOT NULL DEFAULT 0.00,
                        red_epoch_active TINYINT(1) NOT NULL DEFAULT 0,
                        last_half_sell_at DATETIME NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
                        UNIQUE KEY uq_strategy_mode (mode)
                    )
                    """
                )
                db.commit()
                logging.info("strategy_state table created")

                # Seed default row for CDC strategy
                cursor.execute(
                    """
                    INSERT IGNORE INTO strategy_state (mode, last_cdc_status, reserve_usdt, red_epoch_active)
                    VALUES ('cdc_dca_v1', NULL, 0.00, 0)
                    """
                )
                db.commit()

            # 3.3 ตรวจสอบ sell_history table
            cursor.execute("SHOW TABLES LIKE 'sell_history'")
            if not cursor.fetchone():
                logging.info("Creating sell_history table...")
                cursor.execute(
                    """
                    CREATE TABLE sell_history (
                        id INT PRIMARY KEY AUTO_INCREMENT,
                        sell_time DATETIME NOT NULL,
                        symbol VARCHAR(16) NOT NULL DEFAULT 'BTCUSDT',
                        btc_quantity DECIMAL(18,8) NOT NULL,
                        usdt_received DECIMAL(18,2) NOT NULL,
                        price DECIMAL(18,2) NOT NULL,
                        order_id BIGINT,
                        schedule_id INT NULL,
                        note VARCHAR(255) NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_sell_time (sell_time),
                        UNIQUE KEY uq_sell_order (order_id)
                    )
                    """
                )
                db.commit()
                logging.info("sell_history table created")

            # 3.4 ตรวจสอบคอลัมน์ cdc_enabled ใน strategy_state
            try:
                cursor.execute("""
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='cdc_enabled'
                """, (os.getenv('DB_NAME'),))
                has_col = cursor.fetchone()[0] > 0
                if not has_col:
                    logging.info("Adding column cdc_enabled to strategy_state...")
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN cdc_enabled TINYINT(1) NOT NULL DEFAULT 1 AFTER mode")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure cdc_enabled column: {e}")

            # 3.5 ตรวจสอบ reserve_log table
            cursor.execute("SHOW TABLES LIKE 'reserve_log'")
            if not cursor.fetchone():
                logging.info("Creating reserve_log table...")
                cursor.execute(
                    """
                    CREATE TABLE reserve_log (
                        id INT PRIMARY KEY AUTO_INCREMENT,
                        event_time DATETIME NOT NULL,
                        change_usdt DECIMAL(18,2) NOT NULL,
                        reserve_after DECIMAL(18,2) NOT NULL,
                        reason VARCHAR(32) NOT NULL,
                        note VARCHAR(255) NULL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        INDEX idx_event_time (event_time)
                    )
                    """
                )
                db.commit()
                logging.info("reserve_log table created")

            # 3.6 เพิ่มคอลัมน์ sell_percent ใน strategy_state ถ้ายังไม่มี
            try:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='sell_percent'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                has_col = cursor.fetchone()[0] > 0
                if not has_col:
                    logging.info("Adding column sell_percent (default 50) to strategy_state...")
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN sell_percent TINYINT NOT NULL DEFAULT 50 AFTER red_epoch_active")
                    db.commit()
                    cursor.execute("UPDATE strategy_state SET sell_percent = 50 WHERE mode='cdc_dca_v1'")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure sell_percent column: {e}")

            # 3.6.1 เพิ่มคอลัมน์ sell_percent แยกตาม exchange ถ้ายังไม่มี
            try:
                # sell_percent_binance
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='sell_percent_binance'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                if cursor.fetchone()[0] == 0:
                    logging.info("Adding column sell_percent_binance (default 50) to strategy_state...")
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN sell_percent_binance TINYINT NOT NULL DEFAULT 50 AFTER sell_percent")
                    db.commit()
                    try:
                        cursor.execute("UPDATE strategy_state SET sell_percent_binance = sell_percent WHERE mode='cdc_dca_v1'")
                        db.commit()
                    except Exception:
                        pass

                # sell_percent_okx
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='sell_percent_okx'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                if cursor.fetchone()[0] == 0:
                    logging.info("Adding column sell_percent_okx (default 50) to strategy_state...")
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN sell_percent_okx TINYINT NOT NULL DEFAULT 50 AFTER sell_percent_binance")
                    db.commit()
                    try:
                        cursor.execute("UPDATE strategy_state SET sell_percent_okx = sell_percent WHERE mode='cdc_dca_v1'")
                        db.commit()
                    except Exception:
                        pass
            except Exception as e:
                logging.warning(f"Could not ensure per-exchange sell_percent columns: {e}")

            # 3.7 เพิ่มคอลัมน์ sell_percent ใน sell_history เพื่อบันทึกสัดส่วนที่ขายจริง
            try:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='sell_history' AND COLUMN_NAME='sell_percent'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                has_col = cursor.fetchone()[0] > 0
                if not has_col:
                    logging.info("Adding column sell_percent to sell_history...")
                    cursor.execute("ALTER TABLE sell_history ADD COLUMN sell_percent TINYINT NULL AFTER order_id")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure sell_history.sell_percent: {e}")

            # 3.8 เพิ่มคอลัมน์ exchange ใน strategy_state ถ้ายังไม่มี
            try:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='exchange'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                has_col = cursor.fetchone()[0] > 0
                if not has_col:
                    logging.info("Adding column exchange (default 'binance') to strategy_state...")
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN exchange VARCHAR(16) NOT NULL DEFAULT 'binance' AFTER mode")
                    db.commit()
                    cursor.execute("UPDATE strategy_state SET exchange='binance' WHERE mode='cdc_dca_v1' AND (exchange IS NULL OR exchange='')")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure exchange column in strategy_state: {e}")

            # 3.9 เพิ่มคอลัมน์ exchange ใน purchase_history ถ้ายังไม่มี
            try:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='purchase_history' AND COLUMN_NAME='exchange'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                has_col = cursor.fetchone()[0] > 0
                if not has_col:
                    logging.info("Adding column exchange to purchase_history...")
                    cursor.execute("ALTER TABLE purchase_history ADD COLUMN exchange VARCHAR(16) NULL AFTER schedule_id")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure exchange column in purchase_history: {e}")

            # 3.10 เพิ่มคอลัมน์ exchange ใน sell_history ถ้ายังไม่มี
            try:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='sell_history' AND COLUMN_NAME='exchange'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                has_col = cursor.fetchone()[0] > 0
                if not has_col:
                    logging.info("Adding column exchange to sell_history...")
                    cursor.execute("ALTER TABLE sell_history ADD COLUMN exchange VARCHAR(16) NULL AFTER note")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure exchange column in sell_history: {e}")

            # 3.11 เพิ่มคอลัมน์ okx_max_usdt ใน strategy_state (default 10.00)
            try:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='okx_max_usdt'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                has_col = cursor.fetchone()[0] > 0
                if not has_col:
                    logging.info("Adding column okx_max_usdt (default 10.00) to strategy_state...")
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN okx_max_usdt DECIMAL(18,2) NOT NULL DEFAULT 10.00 AFTER sell_percent")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure okx_max_usdt column: {e}")

            # 3.11b เพิ่มคอลัมน์ binance_max_usdt ใน strategy_state (default 0.00)
            try:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='binance_max_usdt'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                has_col = cursor.fetchone()[0] > 0
                if not has_col:
                    logging.info("Adding column binance_max_usdt (default 0.00) to strategy_state...")
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN binance_max_usdt DECIMAL(18,2) NOT NULL DEFAULT 0.00 AFTER okx_max_usdt")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure binance_max_usdt column: {e}")

            # 3.12 สร้าง okx_trades table ถ้ายังไม่มี
            try:
                cursor.execute("SHOW TABLES LIKE 'okx_trades'")
                if not cursor.fetchone():
                    logging.info("Creating okx_trades table...")
                    cursor.execute(
                        """
                        CREATE TABLE okx_trades (
                            fill_id VARCHAR(64) PRIMARY KEY,
                            ord_id VARCHAR(64),
                            side VARCHAR(8),
                            price DECIMAL(18,8),
                            qty DECIMAL(18,8),
                            quote_qty DECIMAL(18,8),
                            fee DECIMAL(18,8),
                            fee_ccy VARCHAR(10),
                            trade_time DATETIME,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            INDEX idx_time (trade_time)
                        )
                        """
                    )
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not create okx_trades: {e}")

            # 3.12b สร้าง compliance_audit_log สำหรับ audit trail
            try:
                cursor.execute("SHOW TABLES LIKE 'compliance_audit_log'")
                if not cursor.fetchone():
                    logging.info("Creating compliance_audit_log table...")
                    cursor.execute(
                        """
                        CREATE TABLE compliance_audit_log (
                            id BIGINT AUTO_INCREMENT PRIMARY KEY,
                            event_time DATETIME NOT NULL,
                            event_type VARCHAR(32) NOT NULL,
                            exchange VARCHAR(16) NULL,
                            notional_usdt DECIMAL(18,2) NOT NULL DEFAULT 0.00,
                            btc_quantity DECIMAL(18,8) NOT NULL DEFAULT 0.00000000,
                            price_usdt DECIMAL(18,2) NOT NULL DEFAULT 0.00,
                            realized_pnl_usdt DECIMAL(18,2) NOT NULL DEFAULT 0.00,
                            metadata_blob MEDIUMTEXT NULL,
                            metadata_encrypted TINYINT(1) NOT NULL DEFAULT 0,
                            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            KEY idx_compliance_time (event_time),
                            KEY idx_compliance_type (event_type)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                        """
                    )
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure compliance_audit_log: {e}")

            # 3.12c สร้าง strategy_fee_totals สำหรับสะสมค่าธรรมเนียมตาม exchange/strategy
            try:
                cursor.execute("SHOW TABLES LIKE 'strategy_fee_totals'")
                if not cursor.fetchone():
                    logging.info("Creating strategy_fee_totals table...")
                    cursor.execute(
                        """
                        CREATE TABLE strategy_fee_totals (
                            exchange VARCHAR(16) NOT NULL,
                            strategy VARCHAR(64) NOT NULL,
                            fee_type ENUM('buy','sell') NOT NULL,
                            fee_asset VARCHAR(32) NOT NULL,
                            fee_usd DECIMAL(24,8) NOT NULL DEFAULT 0.00000000,
                            fee_asset_amount DECIMAL(24,8) NOT NULL DEFAULT 0.00000000,
                            last_updated DATETIME NOT NULL,
                            PRIMARY KEY (exchange, strategy, fee_type, fee_asset)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
                        """
                    )
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not create strategy_fee_totals: {e}")

            # 3.13 เพิ่มคอลัมน์ exchange_mode/binance_amount/okx_amount ใน schedules
            try:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='schedules' AND COLUMN_NAME='exchange_mode'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                has_col = cursor.fetchone()[0] > 0
                db_name = os.getenv('DB_NAME')
                if not has_col:
                    logging.info("Adding columns exchange_mode/binance_amount/okx_amount to schedules...")
                    cursor.execute("ALTER TABLE schedules ADD COLUMN exchange_mode ENUM('global','binance','okx','both','s4','pure_dca','okx_pure_dca','bitkub') NOT NULL DEFAULT 'global' AFTER purchase_amount")
                    db.commit()
                else:
                    try:
                        cursor.execute(
                            """
                            SELECT COLUMN_TYPE FROM INFORMATION_SCHEMA.COLUMNS
                            WHERE TABLE_SCHEMA=%s AND TABLE_NAME='schedules' AND COLUMN_NAME='exchange_mode'
                            """,
                            (db_name,)
                        )
                        col_type = cursor.fetchone()
                        if col_type and (
                            's4' not in (col_type[0] or '')
                            or 'pure_dca' not in (col_type[0] or '')
                            or 'bitkub' not in (col_type[0] or '')
                            or 'okx_pure_dca' not in (col_type[0] or '')
                        ):
                            logging.info("Extending schedules.exchange_mode enum to include 's4', 'pure_dca', 'okx_pure_dca' and 'bitkub'...")
                            cursor.execute("ALTER TABLE schedules MODIFY COLUMN exchange_mode ENUM('global','binance','okx','both','s4','pure_dca','okx_pure_dca','bitkub') NOT NULL DEFAULT 'global'")
                            db.commit()
                    except Exception as sub_exc:
                        logging.warning(f"Could not extend exchange_mode enum: {sub_exc}")
                # amounts
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='schedules' AND COLUMN_NAME='binance_amount'
                    """,
                    (os.getenv('DB_NAME'),)
                );
                if cursor.fetchone()[0] == 0:
                    cursor.execute("ALTER TABLE schedules ADD COLUMN binance_amount DECIMAL(10,2) NULL AFTER exchange_mode")
                    db.commit()
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='schedules' AND COLUMN_NAME='okx_amount'
                    """,
                    (os.getenv('DB_NAME'),)
                );
                if cursor.fetchone()[0] == 0:
                    cursor.execute("ALTER TABLE schedules ADD COLUMN okx_amount DECIMAL(10,2) NULL AFTER binance_amount")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure schedules exchange columns: {e}")

            # 3.14 เพิ่มสำรองแยก per-exchange ใน strategy_state
            try:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='reserve_binance_usdt'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                if cursor.fetchone()[0] == 0:
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN reserve_binance_usdt DECIMAL(18,2) NOT NULL DEFAULT 0.00 AFTER reserve_usdt")
                    db.commit()
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='reserve_okx_usdt'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                if cursor.fetchone()[0] == 0:
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN reserve_okx_usdt DECIMAL(18,2) NOT NULL DEFAULT 0.00 AFTER reserve_binance_usdt")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure per-exchange reserves: {e}")

            # 3.15 เพิ่ม half_sell_policy ใน strategy_state
            try:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='half_sell_policy'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                if cursor.fetchone()[0] == 0:
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN half_sell_policy ENUM('auto_proportional','binance_only','okx_only') NOT NULL DEFAULT 'auto_proportional' AFTER okx_max_usdt")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure half_sell_policy: {e}")

            # 3.16 เพิ่มคอลัมน์ last_run_at ใน strategy_state
            try:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='last_run_at'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                if cursor.fetchone()[0] == 0:
                    logging.info("Adding column last_run_at to strategy_state...")
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN last_run_at DATETIME NULL AFTER last_transition_at")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure last_run_at: {e}")

            # 3.17 เพิ่มคอลัมน์ allocation_target_pct และ allocation_actual_pct
            try:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='allocation_target_pct'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                if cursor.fetchone()[0] == 0:
                    logging.info("Adding allocation_target_pct to strategy_state...")
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN allocation_target_pct DECIMAL(5,2) NOT NULL DEFAULT 0.00 AFTER reserve_okx_usdt")
                    db.commit()
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='allocation_actual_pct'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                if cursor.fetchone()[0] == 0:
                    logging.info("Adding allocation_actual_pct to strategy_state...")
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN allocation_actual_pct DECIMAL(5,2) NOT NULL DEFAULT 0.00 AFTER allocation_target_pct")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure allocation columns: {e}")

            # 3.18 เพิ่มคอลัมน์ strategy_status และ metadata_json
            try:
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='strategy_status'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                if cursor.fetchone()[0] == 0:
                    logging.info("Adding strategy_status to strategy_state...")
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN strategy_status VARCHAR(32) NULL AFTER allocation_actual_pct")
                    db.commit()
                cursor.execute(
                    """
                    SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                    WHERE TABLE_SCHEMA=%s AND TABLE_NAME='strategy_state' AND COLUMN_NAME='metadata_json'
                    """,
                    (os.getenv('DB_NAME'),)
                )
                if cursor.fetchone()[0] == 0:
                    logging.info("Adding metadata_json to strategy_state...")
                    cursor.execute("ALTER TABLE strategy_state ADD COLUMN metadata_json TEXT NULL AFTER strategy_status")
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not ensure strategy metadata columns: {e}")

            # 3.19 ตั้งค่า metadata เริ่มต้นสำหรับแต่ละกลยุทธ์
            try:
                cursor.execute("SELECT metadata_json FROM strategy_state WHERE mode='cdc_dca_v1' LIMIT 1")
                row = cursor.fetchone()
                if row and (row[0] is None or not str(row[0]).strip()):
                    cursor.execute(
                        "UPDATE strategy_state SET metadata_json=%s, strategy_status=%s WHERE mode='cdc_dca_v1'",
                        (json.dumps(DEFAULT_STRATEGY_METADATA.get('cdc_dca_v1', {})), DEFAULT_STRATEGY_METADATA.get('cdc_dca_v1', {}).get('status', 'active'))
                    )
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not backfill CDC metadata: {e}")

            try:
                cursor.execute("SELECT COUNT(*) FROM strategy_state WHERE mode='s4_multi_leg'")
                exists = cursor.fetchone()[0] > 0
                if not exists:
                    logging.info("Seeding strategy_state row for s4_multi_leg...")
                    metadata_payload = json.dumps({
                        'config': DEFAULT_STRATEGY_METADATA.get('s4_multi_leg', {}).get('config', {}),
                        'runtime': {}
                    })
                    cursor.execute(
                        """
                        INSERT INTO strategy_state
                            (mode, last_cdc_status, last_transition_at, last_run_at, reserve_usdt, red_epoch_active,
                             last_half_sell_at, cdc_enabled, sell_percent, sell_percent_binance, sell_percent_okx,
                             exchange, okx_max_usdt, binance_max_usdt, reserve_binance_usdt, reserve_okx_usdt, half_sell_policy,
                             allocation_target_pct, allocation_actual_pct, strategy_status, metadata_json)
                        VALUES
                            (%s, NULL, NULL, NULL, 0.00, 0, NULL, 0, 0, 0, 0,
                             'binance', 10.00, 0.00, 0.00, 0.00, 'auto_proportional',
                             %s, %s, %s, %s)
                        """,
                        (
                            's4_multi_leg',
                            DEFAULT_STRATEGY_METADATA.get('s4_multi_leg', {}).get('allocation', {}).get('target_pct', 0.0),
                            0.0,
                            DEFAULT_STRATEGY_METADATA.get('s4_multi_leg', {}).get('status', 'planning'),
                            metadata_payload
                        )
                    )
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not seed S4 strategy row: {e}")

            try:
                cursor.execute("SELECT metadata_json FROM strategy_state WHERE mode='s4_multi_leg' LIMIT 1")
                row = cursor.fetchone()
                if row:
                    needs_update = False
                    metadata_blob = row[0]
                    if metadata_blob:
                        try:
                            meta = json.loads(metadata_blob)
                        except json.JSONDecodeError:
                            meta = {}
                            needs_update = True
                    else:
                        meta = {}
                        needs_update = True
                    if 'config' not in meta:
                        meta['config'] = DEFAULT_STRATEGY_METADATA.get('s4_multi_leg', {}).get('config', {})
                        needs_update = True
                    if 'runtime' not in meta:
                        meta['runtime'] = {}
                        needs_update = True
                    if needs_update:
                        cursor.execute(
                            "UPDATE strategy_state SET metadata_json=%s WHERE mode='s4_multi_leg'",
                            (json.dumps(meta),)
                        )
                db.commit()
            except Exception as e:
                logging.warning(f"Could not align S4 metadata config/runtime: {e}")

            try:
                cursor.execute(
                    "UPDATE strategy_state SET allocation_target_pct=%s WHERE mode='cdc_dca_v1' AND (allocation_target_pct IS NULL OR allocation_target_pct = 0)",
                    (DEFAULT_STRATEGY_METADATA.get('cdc_dca_v1', {}).get('allocation', {}).get('target_pct', 0.0),)
                )
                cursor.execute(
                    "UPDATE strategy_state SET allocation_target_pct=%s WHERE mode='s4_multi_leg' AND (allocation_target_pct IS NULL OR allocation_target_pct = 0)",
                    (DEFAULT_STRATEGY_METADATA.get('s4_multi_leg', {}).get('allocation', {}).get('target_pct', 0.0),)
                )
                db.commit()
            except Exception as e:
                logging.warning(f"Could not backfill allocation targets: {e}")

            # 3.20 สร้าง strategy_rotation_log เพื่อบันทึกการหมุนพอร์ต
            try:
                cursor.execute("SHOW TABLES LIKE 'strategy_rotation_log'")
                if not cursor.fetchone():
                    logging.info("Creating strategy_rotation_log table...")
                    cursor.execute(
                        """
                        CREATE TABLE strategy_rotation_log (
                            id INT PRIMARY KEY AUTO_INCREMENT,
                            executed_at DATETIME NOT NULL,
                            strategy_mode VARCHAR(32) NOT NULL,
                            from_asset VARCHAR(16) NOT NULL,
                            to_asset VARCHAR(16) NOT NULL,
                            notional_usd DECIMAL(18,2) NOT NULL,
                            cdc_status VARCHAR(16) NULL,
                            delta_pct DECIMAL(9,4) NULL,
                            reason VARCHAR(64) NULL,
                            metadata_json TEXT NULL,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                            INDEX idx_mode_time (strategy_mode, executed_at)
                        ) ENGINE=InnoDB DEFAULT CHARSET=utf8;
                        """
                    )
                    db.commit()
            except Exception as e:
                logging.warning(f"Could not create strategy_rotation_log: {e}")

            # 4. ตรวจสอบ Foreign Key
            cursor.execute("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE 
                WHERE TABLE_SCHEMA = %s 
                AND TABLE_NAME = 'purchase_history' 
                AND COLUMN_NAME = 'schedule_id'
                AND REFERENCED_TABLE_NAME = 'schedules'
            """, (os.getenv('DB_NAME'),))
            
            if cursor.fetchone()[0] == 0:
                try:
                    cursor.execute("""
                        ALTER TABLE purchase_history 
                        ADD CONSTRAINT fk_purchase_history_schedule_id 
                        FOREIGN KEY (schedule_id) REFERENCES schedules(id) 
                        ON DELETE SET NULL ON UPDATE CASCADE
                    """)
                    db.commit()
                    logging.info("Foreign key constraint added")
                except MySQLdb.Error as e:
                    logging.warning(f"Could not add foreign key: {e}")

            migration_completed = True
            return True

    except Exception as e:
        logging.error(f"Migration error: {e}")
        return False

# ====== Helper Functions ======
def get_total_active_amount():
    """คำนวณยอดรวม active schedules"""
    try:
        with get_db_cursor() as (cursor, _):
            try:
                # รวมยอดให้ถูกต้องตามโหมด: global/pure_dca/s4 → purchase_amount,
                # binance → binance_amount, okx → okx_amount, both → ผลรวมสองฝั่ง
                cursor.execute(
                    """
                    SELECT SUM(
                        CASE 
                            WHEN exchange_mode = 'both' THEN COALESCE(binance_amount,0) + COALESCE(okx_amount,0)
                            WHEN exchange_mode = 'binance' THEN COALESCE(binance_amount,0)
                            WHEN exchange_mode = 'okx' THEN COALESCE(okx_amount,0)
                            WHEN exchange_mode = 's4' THEN COALESCE(purchase_amount,0)
                            WHEN exchange_mode = 'pure_dca' THEN COALESCE(purchase_amount,0)
                            WHEN exchange_mode = 'okx_pure_dca' THEN COALESCE(purchase_amount,0)
                            WHEN exchange_mode = 'bitkub' THEN COALESCE(purchase_amount,0)
                            ELSE COALESCE(purchase_amount,0)
                        END
                    ) AS total
                    FROM schedules WHERE is_active = 1
                    """
                )
            except Exception:
                # สคีม่าเก่า: ไม่มีคอลัมน์ exchange_mode/binance_amount/okx_amount
                cursor.execute("SELECT SUM(purchase_amount) FROM schedules WHERE is_active = 1")
            result = cursor.fetchone()[0]
            return float(result) if result else 0.0
    except Exception as e:
        logging.error(f"Error getting total amount: {e}")
        return 0.0

def validate_schedule_data(amount, time_str, days):
    """ตรวจสอบความถูกต้องของข้อมูล schedule"""
    errors = []
    
    try:
        float_amount = float(amount)
        if float_amount <= 0:
            errors.append("Amount must be positive")
    except (ValueError, TypeError):
        errors.append("Invalid amount format")
    
    try:
        datetime.strptime(time_str, "%H:%M")
    except ValueError:
        errors.append("Invalid time format. Use HH:MM")
    
    if not days:
        errors.append("Please select at least one day")
    
    return errors

# ====== Routes ======
@app.route('/')
def index():
    """หน้าแสดงผลหลัก"""
    try:
        # Run migration check
        migrate_data_if_needed()
        
        with get_db_cursor() as (cursor, _):
            # ดึงข้อมูล schedules
            try:
                cursor.execute("""
                    SELECT id, schedule_time, schedule_day, purchase_amount, is_active, exchange_mode, binance_amount, okx_amount
                    FROM schedules 
                    ORDER BY is_active DESC, schedule_time
                """)
            except Exception:
                cursor.execute("""
                    SELECT id, schedule_time, schedule_day, purchase_amount, is_active 
                    FROM schedules 
                    ORDER BY is_active DESC, schedule_time
                """)
            schedules = cursor.fetchall()

            # สร้าง next run time map ตาม schedule
            next_run_map = {}
            try:
                tz = timezone('Asia/Bangkok')
            except Exception:
                tz = None

            now = datetime.now(tz) if tz else datetime.now()
            weekday_map = {
                'monday': 0, 'tuesday': 1, 'wednesday': 2,
                'thursday': 3, 'friday': 4, 'saturday': 5, 'sunday': 6
            }

            for s in schedules:
                # Support both legacy (5 cols) and extended schema (>=7 cols)
                if len(s) >= 7:
                    sid, time_str, day_str, _purchase_amount, is_active, _ex_mode, _bz_amt, _okx_amt = s[0], s[1], s[2], s[3], s[4], s[5], s[6], (s[7] if len(s) > 7 else None)
                else:
                    sid, time_str, day_str, _purchase_amount, is_active = s
                if not is_active:
                    next_run_map[sid] = '-'
                    continue
                try:
                    hh, mm = map(int, str(time_str).split(':'))
                    days = [d.strip().lower() for d in str(day_str).split(',') if d.strip()]
                    target_weekdays = [weekday_map.get(d) for d in days if d in weekday_map]
                    if not target_weekdays:
                        next_run_map[sid] = '-'
                        continue

                    # compute next occurrence >= now
                    # iterate next 14 days to find the next run
                    base_date = now.date()
                    for i in range(0, 14):
                        day = base_date + timedelta(days=i)
                        dt = datetime(day.year, day.month, day.day, hh, mm)
                        if tz:
                            dt = tz.localize(dt)
                        if dt.weekday() in target_weekdays and dt >= now:
                            next_run_map[sid] = dt.strftime('%Y-%m-%d %H:%M')
                            break
                    if sid not in next_run_map:
                        next_run_map[sid] = '-'
                except Exception:
                    next_run_map[sid] = '-'
            
            # คำนวณยอดรวม
            total_amount = get_total_active_amount()
            
            # ดึงประวัติการซื้อ
            cursor.execute("""
                SELECT ph.id, ph.purchase_time, ph.usdt_amount, ph.btc_quantity, 
                       ph.btc_price, ph.order_id, ph.schedule_id, s.schedule_time,
                       ph.fee_buy_usdt, ph.fee_buy_asset, COALESCE(ph.exchange, ''), ph.fee_buy_asset_amount
                FROM purchase_history ph
                LEFT JOIN schedules s ON ph.schedule_id = s.id
                ORDER BY ph.purchase_time DESC
                LIMIT 20
            """)
            history = cursor.fetchall()
            
            # เพิ่มข้อมูล stats สำหรับ template
            cursor.execute("SELECT COUNT(*) FROM schedules")
            total_schedules = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM schedules WHERE is_active = 1")
            active_schedules = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM purchase_history")
            total_purchases = cursor.fetchone()[0]
            
            # สร้าง stats object
            stats = {
                'total_schedules': total_schedules,
                'active_schedules': active_schedules,
                'total_purchases': total_purchases,
                'scheduler_status': last_scheduler_status
            }
            
        return render_template('index.html', 
                             schedules=schedules, 
                             history=history, 
                             total_amount=total_amount,
                             stats=stats,
                             next_run_map=next_run_map)
        
    except Exception as e:
        logging.error(f"Error in index route: {e}")
        flash("System error occurred", 'error')
        
        # สร้าง default stats สำหรับกรณี error
        default_stats = {
            'total_schedules': 0,
            'active_schedules': 0,
            'total_purchases': 0,
            'scheduler_status': 'Unknown'
        }
        
        return render_template('index.html', 
                             schedules=[], 
                             history=[], 
                             total_amount=0.0,
                             stats=default_stats)

@app.route('/check_duplicate_schedule', methods=['POST'])
def check_duplicate_schedule():
    """ตรวจสอบกำหนดการซ้ำ"""
    try:
        data = request.get_json()
        time_str = data['time']
        days = data['days']
        schedule_id = data.get('schedule_id')

        schedule_day = ",".join([d.lower() for d in days])
        
        with get_db_cursor() as (cursor, _):
            if schedule_id:
                cursor.execute("""
                    SELECT COUNT(*) FROM schedules 
                    WHERE schedule_time = %s AND schedule_day = %s AND id != %s
                """, (time_str, schedule_day, schedule_id))
            else:
                cursor.execute("""
                    SELECT COUNT(*) FROM schedules 
                    WHERE schedule_time = %s AND schedule_day = %s
                """, (time_str, schedule_day))
            
            count = cursor.fetchone()[0]
            
        return jsonify({'is_duplicate': count > 0})
        
    except Exception as e:
        logging.error(f"Error checking duplicate schedule: {e}")
        return jsonify({'is_duplicate': False, 'error': str(e)}), 500

@app.route('/get_total_amount')
def get_total_amount():
    """ดึงยอดรวม USDT"""
    try:
        total_amount = get_total_active_amount()
        return jsonify({'total_amount': total_amount})
    except Exception as e:
        logging.error(f"Error fetching total amount: {e}")
        return jsonify({'total_amount': 0.0, 'error': str(e)}), 500

@app.route('/check_schedule_usage/<int:schedule_id>')
def check_schedule_usage(schedule_id):
    """ตรวจสอบการใช้งาน schedule"""
    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute("""
                SELECT COUNT(*) as count, COALESCE(SUM(usdt_amount), 0) as total
                FROM purchase_history 
                WHERE schedule_id = %s
            """, (schedule_id,))
            
            result = cursor.fetchone()
            purchase_count = result[0]
            total_amount = float(result[1])
            
        return jsonify({
            'canDelete': purchase_count == 0,
            'purchaseCount': purchase_count,
            'totalAmount': total_amount
        })
        
    except Exception as e:
        logging.error(f"Error checking schedule usage: {e}")
        return jsonify({'canDelete': False, 'error': str(e)}), 500

@app.route('/api/analytics')
def api_analytics():
    """Return analytics data for Investment Summary and DCA Performance.

    Response schema:
    {
      total_invested: float,
      total_btc: float,
      avg_price: float,
      success_rate: float,
      series: {
        timestamps: [str],               # ISO strings ascending
        cumulative_usdt: [float],        # cumulative invested
        cumulative_btc: [float],         # cumulative BTC acquired
        price: [float]                   # purchase price at each point
      },
      count: int
    }
    """
    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute(
                """
                SELECT purchase_time, usdt_amount, btc_quantity, btc_price, fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount
                FROM purchase_history
                WHERE purchase_time IS NOT NULL
                ORDER BY purchase_time ASC
                """
            )
            rows = cursor.fetchall()

        fee_price_cache: dict[str, float] = {}

        def fee_amount_to_usdt(
            fee_usdt_value: float | None,
            asset: str | None,
            amount: float | None,
            ref_price: float | None,
        ) -> float:
            """Resolve fee into USDT using asset/amount fallback when direct USDT value is unavailable."""
            asset_code = (asset or '').strip().upper()
            amt = float(amount or 0.0)
            reference = float(ref_price or 0.0)
            converted = 0.0

            if asset_code and amt > 0.0:
                if asset_code == 'USDT':
                    converted = amt
                elif asset_code == 'BTC':
                    converted = amt * reference
                else:
                    cached = fee_price_cache.get(asset_code)
                    if cached is None:
                        cached = 0.0
                        try:
                            resp = requests.get(
                                'https://api.binance.com/api/v3/ticker/price',
                                params={'symbol': f'{asset_code}USDT'},
                                timeout=3,
                            )
                            if resp.status_code == 200:
                                payload_px = resp.json()
                                if isinstance(payload_px, dict):
                                    cached = float(payload_px.get('price') or 0.0)
                        except Exception:
                            cached = 0.0
                        fee_price_cache[asset_code] = cached
                    if cached > 0.0:
                        converted = amt * cached

            fee_usdt_val = float(fee_usdt_value or 0.0)
            if converted > 0.0:
                return converted
            return fee_usdt_val

        timestamps = []
        cumulative_usdt = []
        cumulative_btc = []
        prices = []

        total_usdt = 0.0
        total_btc = 0.0

        total_fees_buy = 0.0

        for row in rows:
            ts, usdt, btc, price, fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount = row
            # Coalesce None to 0 for robustness
            usdt = float(usdt or 0.0)
            btc = float(btc or 0.0)
            price = float(price or 0.0)
            fee_buy = fee_amount_to_usdt(fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount, price)

            total_usdt += usdt
            total_btc += btc
            total_fees_buy += max(fee_buy, 0.0)

            timestamps.append(str(ts))
            cumulative_usdt.append(total_usdt)
            cumulative_btc.append(total_btc)
            prices.append(price)

        # Weighted average price: total_usdt / total_btc
        avg_price = (total_usdt / total_btc) if total_btc > 0 else 0.0
        if not isfinite(avg_price):
            avg_price = 0.0

        # Success rate: we only record successful purchases in history
        success_rate = 100.0 if len(rows) > 0 else 0.0

        # Real-time price and unrealized PnL
        current_price = None
        portfolio_value = None
        pnl_abs = None
        pnl_pct = None

        try:
            if get_btc_price is not None:
                current_price = float(get_btc_price() or 0.0)
            else:
                current_price = 0.0
        except Exception:
            current_price = 0.0

        if current_price and total_btc > 0:
            portfolio_value = total_btc * current_price
            pnl_abs = portfolio_value - total_usdt
            pnl_pct = (pnl_abs / total_usdt * 100.0) if total_usdt > 0 else 0.0
        else:
            portfolio_value = 0.0
            pnl_abs = 0.0
            pnl_pct = 0.0

        payload = {
            'total_invested': round(total_usdt, 2),
            'total_buy_fees': round(total_fees_buy, 4),
            'total_btc': round(total_btc, 8),
            'avg_price': round(avg_price, 2),
            'success_rate': success_rate,
            'series': {
                'timestamps': timestamps,
                'cumulative_usdt': cumulative_usdt,
                'cumulative_btc': cumulative_btc,
                'price': prices,
            },
            'count': len(rows),
            'current_price': round(current_price, 2) if current_price is not None else 0.0,
            'portfolio_value': round(portfolio_value, 2) if portfolio_value is not None else 0.0,
            'pnl_abs': round(pnl_abs, 2) if pnl_abs is not None else 0.0,
            'pnl_pct': round(pnl_pct, 2) if pnl_pct is not None else 0.0,
        }

        try:
            with get_db_cursor() as (cursor, _):
                cursor.execute(
                    """
                    SELECT price, fee_sell_usdt, fee_sell_asset, fee_sell_asset_amount
                    FROM sell_history
                    """
                )
                sell_rows = cursor.fetchall()
            total_sell_fees = 0.0
            for price, fee_sell_usdt, fee_sell_asset, fee_sell_asset_amount in sell_rows:
                fee_s = fee_amount_to_usdt(fee_sell_usdt, fee_sell_asset, fee_sell_asset_amount, price)
                total_sell_fees += max(fee_s, 0.0)
            payload['total_sell_fees'] = round(total_sell_fees, 4)
        except Exception:
            payload['total_sell_fees'] = 0.0

        return jsonify(payload)

    except Exception as e:
        logging.error(f"Error building analytics: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/add_schedule', methods=['POST'])
@handle_db_errors
def add_schedule():
    """เพิ่มกำหนดการใหม่"""
    amount = request.form.get('amount')
    time_str = request.form['time']
    days = request.form.getlist('day')
    is_active = request.form.get('is_active', '0') == '1'
    ex_mode = request.form.get('exchange_mode', 'global').strip().lower()
    bz_amt = request.form.get('binance_amount')
    okx_amt = request.form.get('okx_amount')

    # Validate input
    errors = validate_schedule_data(amount, time_str, days)
    if errors:
        for error in errors:
            flash(error, 'error')
        return redirect('/')

    float_amount = float(amount) if amount is not None and amount != '' else 0.0
    # Validate exchange amounts
    if ex_mode not in ('global','binance','okx','both','s4','pure_dca','okx_pure_dca','bitkub'):
        flash('Invalid exchange mode', 'error'); return redirect('/')
    if ex_mode == 'binance':
        try:
            bz = float(bz_amt or 0)
            if bz <= 0: raise ValueError()
        except Exception:
            flash('Binance amount must be > 0', 'error'); return redirect('/')
    elif ex_mode == 'okx':
        try:
            ok = float(okx_amt or 0)
            if ok <= 0: raise ValueError()
        except Exception:
            flash('OKX amount must be > 0', 'error'); return redirect('/')
    elif ex_mode == 'both':
        try:
            bz = float(bz_amt or 0); ok = float(okx_amt or 0)
            if (bz + ok) <= 0: raise ValueError()
        except Exception:
            flash('Both-mode requires total amount > 0', 'error'); return redirect('/')
    elif ex_mode == 'bitkub':
        if float_amount < 10:
            flash('Bitkub amount must be >= 10 THB', 'error'); return redirect('/')
    schedule_day = ",".join([d.lower() for d in days])

    with get_db_cursor() as (cursor, db):
        # ตรวจสอบกำหนดการซ้ำ
        cursor.execute("""
            SELECT COUNT(*) FROM schedules 
            WHERE schedule_time = %s AND schedule_day = %s
        """, (time_str, schedule_day))
        
        if cursor.fetchone()[0] > 0:
            flash("A schedule with the same time and days already exists.", 'error')
            return redirect('/')

        try:
            cursor.execute("""
                INSERT INTO schedules (schedule_time, schedule_day, purchase_amount, is_active, exchange_mode, binance_amount, okx_amount) 
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (time_str, schedule_day, float_amount, is_active, ex_mode, (float(bz_amt) if bz_amt else None), (float(okx_amt) if okx_amt else None)))
        except Exception:
            # fallback to legacy columns
            cursor.execute("""
                INSERT INTO schedules (schedule_time, schedule_day, purchase_amount, is_active) 
                VALUES (%s, %s, %s, %s)
            """, (time_str, schedule_day, float_amount, is_active))
        
        schedule_id = cursor.lastrowid
        db.commit()
        
    logging.info(f"Schedule added: id={schedule_id}")
    flash("Schedule added successfully.", 'success')
    
    # Emit real-time updates
    socketio.emit('schedule_update', {
        'id': schedule_id,
        'schedule_time': time_str,
        'schedule_day': schedule_day,
        'purchase_amount': float_amount,
        'is_active': is_active,
        'exchange_mode': ex_mode,
        'binance_amount': float(bz_amt or 0) if ex_mode in ('binance','both') else None,
        'okx_amount': float(okx_amt or 0) if ex_mode in ('okx','both') else None
    })
    socketio.emit('total_amount_update', {'total_amount': get_total_active_amount()})
    
    return redirect('/')

@app.route('/edit_schedule/<int:schedule_id>', methods=['POST'])
@handle_db_errors
def edit_schedule(schedule_id):
    """แก้ไขกำหนดการ"""
    # Require ADMIN_TOKEN for edit (hardened)
    admin_env = os.getenv('ADMIN_TOKEN')
    admin_tok = request.form.get('admin_token')
    if not admin_env:
        flash('ADMIN_TOKEN is not configured on server', 'error')
        return redirect('/')
    if admin_tok != admin_env:
        flash('Forbidden: invalid ADMIN_TOKEN', 'error')
        return redirect('/')
    amount = request.form.get('amount')
    time_str = request.form['time']
    days = request.form.getlist('day')
    is_active = request.form.get('is_active', '0') == '1'
    ex_mode = request.form.get('exchange_mode', 'global').strip().lower()
    bz_amt = request.form.get('binance_amount')
    okx_amt = request.form.get('okx_amount')

    # Validate input
    errors = validate_schedule_data(amount, time_str, days)
    if errors:
        for error in errors:
            flash(error, 'error')
        return redirect('/')

    float_amount = float(amount) if amount is not None and amount != '' else 0.0
    # Validate exchange amounts
    if ex_mode not in ('global','binance','okx','both','s4','pure_dca','okx_pure_dca','bitkub'):
        flash('Invalid exchange mode', 'error'); return redirect('/')
    if ex_mode == 'binance':
        try:
            bz = float(bz_amt or 0)
            if bz <= 0: raise ValueError()
        except Exception:
            flash('Binance amount must be > 0', 'error'); return redirect('/')
    elif ex_mode == 'okx':
        try:
            ok = float(okx_amt or 0)
            if ok <= 0: raise ValueError()
        except Exception:
            flash('OKX amount must be > 0', 'error'); return redirect('/')
    elif ex_mode == 'both':
        try:
            bz = float(bz_amt or 0); ok = float(okx_amt or 0)
            if (bz + ok) <= 0: raise ValueError()
        except Exception:
            flash('Both-mode requires total amount > 0', 'error'); return redirect('/')
    elif ex_mode == 'bitkub':
        if float_amount < 10:
            flash('Bitkub amount must be >= 10 THB', 'error'); return redirect('/')
    schedule_day = ",".join([d.lower() for d in days])

    with get_db_cursor() as (cursor, db):
        # ตรวจสอบกำหนดการซ้ำ
        cursor.execute("""
            SELECT COUNT(*) FROM schedules 
            WHERE schedule_time = %s AND schedule_day = %s AND id != %s
        """, (time_str, schedule_day, schedule_id))
        
        if cursor.fetchone()[0] > 0:
            flash("A schedule with the same time and days already exists.", 'error')
            return redirect('/')

        try:
            cursor.execute("""
                UPDATE schedules 
                SET schedule_time = %s, schedule_day = %s, purchase_amount = %s, is_active = %s,
                    exchange_mode=%s, binance_amount=%s, okx_amount=%s
                WHERE id = %s
            """, (time_str, schedule_day, float_amount, is_active, ex_mode, (float(bz_amt) if bz_amt else None), (float(okx_amt) if okx_amt else None), schedule_id))
        except Exception:
            cursor.execute("""
                UPDATE schedules 
                SET schedule_time = %s, schedule_day = %s, purchase_amount = %s, is_active = %s 
                WHERE id = %s
            """, (time_str, schedule_day, float_amount, is_active, schedule_id))
        
        if cursor.rowcount == 0:
            flash("Schedule not found.", 'error')
            return redirect('/')
            
        db.commit()

    logging.info(f"Schedule updated: id={schedule_id}")
    flash("Schedule updated successfully.", 'success')
    
    # Emit real-time updates
    socketio.emit('schedule_update', {
        'id': schedule_id,
        'schedule_time': time_str,
        'schedule_day': schedule_day,
        'purchase_amount': float_amount,
        'is_active': is_active
    })
    socketio.emit('total_amount_update', {'total_amount': get_total_active_amount()})
    
    return redirect('/')

@app.route('/delete_schedule/<int:schedule_id>', methods=['POST'])
@handle_db_errors
def delete_schedule(schedule_id):
    """ลบกำหนดการ (Smart Delete)"""
    with get_db_cursor() as (cursor, db):
        # ตรวจสอบการใช้งาน
        cursor.execute("SELECT COUNT(*) FROM purchase_history WHERE schedule_id = %s", (schedule_id,))
        usage_count = cursor.fetchone()[0]
        
        if usage_count > 0:
            # Soft delete - deactivate
            cursor.execute("UPDATE schedules SET is_active = 0 WHERE id = %s", (schedule_id,))
            if cursor.rowcount > 0:
                flash(f"Schedule deactivated (has {usage_count} purchase records)", 'warning')
                logging.info(f"Schedule {schedule_id} deactivated")
            else:
                flash("Schedule not found", 'error')
                return redirect('/')
        else:
            # Hard delete - no purchase history
            cursor.execute("DELETE FROM schedules WHERE id = %s", (schedule_id,))
            if cursor.rowcount > 0:
                flash("Schedule deleted successfully", 'success')
                logging.info(f"Schedule {schedule_id} deleted")
            else:
                flash("Schedule not found", 'error')
                return redirect('/')
        
        db.commit()

    # Emit real-time updates
    socketio.emit('schedule_delete', {'id': schedule_id})
    socketio.emit('total_amount_update', {'total_amount': get_total_active_amount()})
    
    return redirect('/')

@app.route('/force_delete_schedule/<int:schedule_id>', methods=['POST'])
@handle_db_errors
def force_delete_schedule(schedule_id):
    """Force delete schedule (สำหรับ admin)"""
    with get_db_cursor() as (cursor, db):
        # ลบ purchase_history ก่อน
        cursor.execute("DELETE FROM purchase_history WHERE schedule_id = %s", (schedule_id,))
        deleted_purchases = cursor.rowcount
        
        # ลบ schedule
        cursor.execute("DELETE FROM schedules WHERE id = %s", (schedule_id,))
        if cursor.rowcount > 0:
            flash(f"Force deleted: schedule + {deleted_purchases} purchase records", 'warning')
            logging.warning(f"Force deleted schedule {schedule_id} with {deleted_purchases} purchases")
        else:
            flash("Schedule not found", 'error')
            return redirect('/')
            
        db.commit()

    # Emit real-time updates
    socketio.emit('schedule_delete', {'id': schedule_id})
    socketio.emit('total_amount_update', {'total_amount': get_total_active_amount()})
    
    return redirect('/')

@app.route('/scheduler_status')
def scheduler_status():
    """ตรวจสอบสถานะ Scheduler"""
    try:
        health_check_port = os.getenv('HEALTH_CHECK_PORT', '8001')
        response = requests.get(f"http://localhost:{health_check_port}", timeout=5)
        if response.status_code == 200:
            return {'status': response.text}
        return {'status': 'Scheduler is not responding'}
    except requests.RequestException:
        return {'status': 'Scheduler is not responding'}

@app.route('/test_line_notify')
def test_line_notify():
    """ทดสอบ Line Notify"""
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    message = f"🔔 Test Line Notify: {timestamp}"
    if send_line_notify(message):
        flash("Test Line Notify sent successfully.", 'success')
    else:
        flash("Failed to send test Line Notify.", 'error')
    return redirect('/')

# ====== Admin Routes ======
@app.route('/admin')
def admin_dashboard():
    """Admin dashboard"""
    try:
        with get_db_cursor() as (cursor, _):
            # System stats
            cursor.execute("SELECT COUNT(*) FROM schedules")
            total_schedules = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM schedules WHERE is_active = 1")
            active_schedules = cursor.fetchone()[0]
            
            cursor.execute("SELECT COUNT(*) FROM purchase_history")
            total_purchases = cursor.fetchone()[0]
            
            # Recent activity
            cursor.execute("""
                SELECT 'purchase' as type, purchase_time as timestamp, 
                       CONCAT('Purchase: ', usdt_amount, ' USDT') as description
                FROM purchase_history
                ORDER BY purchase_time DESC
                LIMIT 10
            """)
            recent_activity = cursor.fetchall()
            
            stats = {
                'total_schedules': total_schedules,
                'active_schedules': active_schedules,
                'total_purchases': total_purchases,
                'scheduler_status': last_scheduler_status,
                'recent_activity': [{
                    'type': row[0],
                    'timestamp': str(row[1]),
                    'description': row[2]
                } for row in recent_activity]
            }
            
        return render_template('admin.html', stats=stats)
        
    except Exception as e:
        logging.error(f"Error in admin dashboard: {e}")
        flash("Error loading admin dashboard", 'error')
        return redirect('/')

@app.route('/s4')
def s4_status():
    """Human-readable S4 status page."""
    try:
        data = _build_s4_status_data()
        return render_template('s4_status.html', data=data)
    except Exception as exc:
        logging.error(f"Error in /s4 route: {exc}")
        return render_template('s4_status.html', data={"error": str(exc)})


@app.route('/api/s4_shadow_swaps')
def api_s4_shadow_swaps():
    """Return shadow swap plans for S4 (read-only diagnostics)."""
    try:
        limit_raw = request.args.get('limit', '100')
        days_raw = request.args.get('days', '90')
        reason_raw = request.args.get('reason', 'all')
        decision_raw = str(request.args.get('decision', 'all') or 'all').strip().upper()
        include_mismatch = parse_bool(request.args.get('include_mismatch'), False)
        try:
            limit = max(1, min(int(limit_raw), 500))
        except (TypeError, ValueError):
            limit = 100
        try:
            days = max(1, min(int(days_raw), 3650))
        except (TypeError, ValueError):
            days = 90
        allowed_reasons = normalize_reason_filter(reason_raw)
        if decision_raw not in {'ALL', 'HOLD', 'SWAP_TO_BTC', 'SWAP_TO_XAU'}:
            decision_raw = 'ALL'

        with get_db_cursor() as (cursor, _):
            cursor.execute(
                """
                SELECT executed_at, from_asset, to_asset, notional_usd, cdc_status, reason, metadata_json
                FROM strategy_rotation_log
                WHERE strategy_mode='s4_multi_leg'
                  AND reason IN ('shadow_swap_plan', 'shadow_swap_heartbeat')
                  AND executed_at >= (UTC_TIMESTAMP() - INTERVAL %s DAY)
                ORDER BY executed_at DESC
                LIMIT %s
                """,
                (days, limit),
            )
            rows = cursor.fetchall() or []
            cols = [d[0] for d in cursor.description]

        items: list[dict] = []
        filtered_out_reason = 0
        filtered_out_decision = 0
        filtered_out_mismatch = 0
        for row in rows:
            entry = dict(zip(cols, row))
            meta_raw = entry.get('metadata_json')
            if isinstance(meta_raw, str):
                try:
                    entry['metadata_json'] = json.loads(meta_raw)
                except Exception:
                    pass
            if str(entry.get('reason') or '') not in allowed_reasons:
                filtered_out_reason += 1
                continue

            decision = derive_shadow_decision(entry).upper()
            if decision_raw != 'ALL' and decision != decision_raw:
                filtered_out_decision += 1
                continue

            meta = entry.get('metadata_json') if isinstance(entry.get('metadata_json'), dict) else {}
            mismatch = bool((meta or {}).get('analytics_runtime_mismatch'))
            if include_mismatch and not mismatch:
                filtered_out_mismatch += 1
                continue

            gate = meta.get('gate') if isinstance(meta.get('gate'), dict) else {}
            gate_reason = str(gate.get('reason') or entry.get('reason') or '')
            unlock_cond = gate.get('next_unlock_condition')
            unlock_days_raw = gate.get('next_unlock_min_days')
            if not unlock_cond or unlock_days_raw is None:
                unlock_cond, unlock_days = next_unlock_from_gate_reason(
                    gate_reason,
                    btc_confirm_days=max(int(os.getenv("S4_SHADOW_BTC_CONFIRM_DAYS", "3") or 3), 0),
                    xau_confirm_days=max(int(os.getenv("S4_SHADOW_XAU_CONFIRM_DAYS", "5") or 5), 0),
                )
            else:
                unlock_days = int(_safe_float(unlock_days_raw, 0.0))

            entry['decision'] = decision
            entry['analytics_runtime_mismatch'] = mismatch
            entry['mismatch_severity'] = str((meta or {}).get('mismatch_severity') or '')
            entry['mismatch_streak_days'] = int(_safe_float((meta or {}).get('mismatch_streak_days'), 0.0))
            entry['eod_asof_date'] = (meta or {}).get('eod_asof_date')
            entry['runtime_signal_ts'] = (meta or {}).get('runtime_signal_ts')
            entry['next_unlock_condition'] = unlock_cond
            entry['next_unlock_min_days'] = unlock_days
            items.append(entry)

        return jsonify({
            'window_days': days,
            'limit': limit,
            'reason': str(reason_raw or 'all'),
            'decision': decision_raw,
            'include_mismatch': include_mismatch,
            'count': len(items),
            'filtered_out_reason': filtered_out_reason,
            'filtered_out_decision': filtered_out_decision,
            'filtered_out_mismatch': filtered_out_mismatch,
            'items': items,
        })
    except Exception as exc:
        logging.error(f"Error in /api/s4_shadow_swaps route: {exc}")
        return jsonify({'error': str(exc)}), 500


@app.route('/api/s4_shadow_swaps_summary')
def api_s4_shadow_swaps_summary():
    """Return compact 30/60/90-day shadow diagnostics for operators."""
    try:
        windows = (30, 60, 90)
        summary: dict[str, dict] = {}
        with get_db_cursor() as (cursor, _):
            for days in windows:
                cursor.execute(
                    """
                    SELECT reason, metadata_json
                    FROM strategy_rotation_log
                    WHERE strategy_mode='s4_multi_leg'
                      AND reason IN ('shadow_swap_plan', 'shadow_swap_heartbeat')
                      AND executed_at >= (UTC_TIMESTAMP() - INTERVAL %s DAY)
                    ORDER BY executed_at DESC
                    """,
                    (days,),
                )
                rows = cursor.fetchall() or []
                reason_idx = 0
                meta_idx = 1
                mismatch_count = 0
                decision_counts = {'HOLD': 0, 'SWAP_TO_BTC': 0, 'SWAP_TO_XAU': 0}
                reason_counts = {'shadow_swap_heartbeat': 0, 'shadow_swap_plan': 0}
                for row in rows:
                    reason = str(row[reason_idx] or '')
                    meta_raw = row[meta_idx]
                    meta = {}
                    if isinstance(meta_raw, str):
                        try:
                            meta = json.loads(meta_raw)
                        except Exception:
                            meta = {}
                    elif isinstance(meta_raw, dict):
                        meta = meta_raw
                    entry = {'reason': reason, 'metadata_json': meta}
                    decision = derive_shadow_decision(entry).upper()
                    if decision in decision_counts:
                        decision_counts[decision] += 1
                    if reason in reason_counts:
                        reason_counts[reason] += 1
                    if bool(meta.get('analytics_runtime_mismatch')):
                        mismatch_count += 1
                summary[str(days)] = {
                    'count': len(rows),
                    'reason_counts': reason_counts,
                    'decision_counts': decision_counts,
                    'mismatch_count': mismatch_count,
                }
        return jsonify({'windows': list(windows), 'summary': summary})
    except Exception as exc:
        logging.error(f"Error in /api/s4_shadow_swaps_summary route: {exc}")
        return jsonify({'error': str(exc)}), 500

@app.route('/health')
def health_check():
    """Health check endpoint"""
    try:
        # Check database connection
        with get_db_cursor() as (cursor, _):
            cursor.execute("SELECT 1")
            cursor.fetchone()
        
        # Check scheduler status
        scheduler_ok = last_scheduler_status == "Scheduler is running"
        
        status = {
            'status': 'healthy' if scheduler_ok else 'degraded',
            'timestamp': datetime.now().isoformat(),
            'database': 'connected',
            'scheduler': last_scheduler_status,
            'cache_size': 0  # Simple placeholder
        }
        
        return jsonify(status), 200 if scheduler_ok else 503
        
    except Exception as e:
        logging.error(f"Health check failed: {e}")
        return jsonify({
            'status': 'unhealthy',
            'timestamp': datetime.now().isoformat(),
            'error': str(e)
        }), 503

# Read-only diagnostics for ops/CLI
@app.route('/api/health')
def api_health():
    """Extended health endpoint returning JSON only (read-only)."""
    now = datetime.now().isoformat()
    db_status = "unknown"
    db_error = None
    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute("SELECT 1")
            cursor.fetchone()
        db_status = "connected"
    except Exception as exc:
        db_status = "error"
        db_error = str(exc)

    # Scheduler pid / health
    scheduler_pid = None
    scheduler_alive = False
    pid_path = REPO_ROOT / "scheduler.pid"
    if pid_path.exists():
        try:
            scheduler_pid = int(pid_path.read_text().strip())
            try:
                os.kill(scheduler_pid, 0)
                scheduler_alive = True
            except OSError:
                scheduler_alive = False
        except Exception:
            scheduler_pid = None

    health_check_port = int(os.getenv('HEALTH_CHECK_PORT', '8001') or 8001)
    scheduler_health = last_scheduler_status
    try:
        r = requests.get(f"http://localhost:{health_check_port}", timeout=2)
        if r.status_code == 200 and r.text:
            scheduler_health = r.text
    except Exception:
        pass

    dry_run = str(os.getenv('STRATEGY_DRY_RUN') or os.getenv('DRY_RUN') or "0").strip().lower() in ("1","true","yes","on")
    use_testnet = str(os.getenv('USE_BINANCE_TESTNET') or os.getenv('BINANCE_TESTNET') or os.getenv('OKX_TESTNET') or "0").strip().lower() in ("1","true","yes","on")

    payload = {
        "ok": True,
        "timestamp": now,
        "status": "healthy" if scheduler_health == "Scheduler is running" else "degraded",
        "app": {
            "pid": os.getpid(),
            "uptime_seconds": int(time.time() - APP_START_TS),
        },
        "scheduler": {
            "pid": scheduler_pid,
            "alive": scheduler_alive,
            "health_port": health_check_port,
            "health_status": scheduler_health,
        },
        "database": {
            "status": db_status,
            "error": db_error,
        },
        "env": {
            "dry_run": dry_run,
            "use_testnet": use_testnet,
        },
    }
    code = 200 if payload["status"] == "healthy" and db_status == "connected" else 503
    return jsonify(payload), code

# ====== SocketIO Events ======
@socketio.on('request_latest')
def handle_request_latest():
    """ส่งข้อมูลประวัติล่าสุด"""
    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute("""
                SELECT ph.purchase_time, ph.usdt_amount, ph.btc_quantity, 
                       ph.btc_price, ph.order_id, ph.schedule_id, s.schedule_time
                FROM purchase_history ph
                LEFT JOIN schedules s ON ph.schedule_id = s.id
                ORDER BY ph.purchase_time DESC 
                LIMIT 10
            """)
            results = cursor.fetchall()

        data = [{
            "purchase_time": str(row[0]),
            "usdt_amount": float(row[1]) if row[1] else 0.0,
            "btc_quantity": float(row[2]) if row[2] else 0.0,
            "btc_price": float(row[3]) if row[3] else 0.0,
            "order_id": row[4],
            "schedule_id": row[5],
            "schedule_time": row[6]
        } for row in results]
        
        emit('latest_data', data)
        
    except Exception as e:
        logging.error(f"Error fetching latest data: {e}")
        emit('latest_data', {'error': 'Failed to fetch data'})

@socketio.on('connect')
def handle_connect():
    """การเชื่อมต่อ SocketIO"""
    logging.info("Client connected")
    handle_request_latest()

@socketio.on('disconnect')
def handle_disconnect():
    """การตัดการเชื่อมต่อ SocketIO"""
    logging.info("Client disconnected")

# ====== Error Handlers ======
@app.errorhandler(404)
def not_found_error(error):
    # สร้าง default stats สำหรับ error page
    default_stats = {
        'total_schedules': 0,
        'active_schedules': 0,
        'total_purchases': 0,
        'scheduler_status': 'Unknown'
    }
    return render_template('index.html', 
                         schedules=[], 
                         history=[], 
                         total_amount=0.0,
                         stats=default_stats), 404

@app.errorhandler(500)
def internal_error(error):
    logging.error(f"Internal server error: {error}")
    # สร้าง default stats สำหรับ error page
    default_stats = {
        'total_schedules': 0,
        'active_schedules': 0,
        'total_purchases': 0,
        'scheduler_status': 'Error'
    }
    return render_template('index.html', 
                         schedules=[], 
                         history=[], 
                         total_amount=0.0,
                         stats=default_stats), 500

# ====== Wallet API ======
@app.route('/api/wallet')
def api_wallet():
    """Return wallet snapshots for Binance/OKX/Bitkub plus totals."""

    def _safe_float(val, default=0.0):
        try:
            if val is None:
                return default
            return float(val)
        except (TypeError, ValueError):
            return default

    def _snapshot(exchange: str, reserve_value: float, testnet: bool, dry_run: bool) -> dict:
        quote_asset = 'THB' if exchange.lower() == 'bitkub' else 'USDT'
        snap = {
            'usdt_free': 0.0,
            'usdt_locked': 0.0,
            'quote_asset': quote_asset,
            'quote_free': 0.0,
            'quote_locked': 0.0,
            'btc_free': 0.0,
            'btc_locked': 0.0,
            'price': 0.0,
            'portfolio_value': 0.0,
            'reserve': _safe_float(reserve_value, 0.0),
            'error': None,
            'extra_assets': {},
        }
        try:
            adapter = get_adapter(exchange, testnet=testnet, dry_run=dry_run)
        except Exception as exc:
            snap['error'] = str(exc)
            logging.warning(f"wallet snapshot init {exchange}: {exc}")
            return snap

        asset_plan = [quote_asset, 'BTC']
        if exchange.lower() == 'okx':
            asset_plan.append('XAUT')
        if exchange.lower() == 'binance':
            asset_plan.append('PAXG')

        try:
            for asset in asset_plan:
                bal = adapter.get_balance(asset)
                free = _safe_float(bal.get('free'))
                locked = _safe_float(bal.get('locked'))
                if asset == quote_asset:
                    snap['quote_free'] = free
                    snap['quote_locked'] = locked
                if asset == 'USDT':
                    snap['usdt_free'] = free
                    snap['usdt_locked'] = locked
                elif asset in ('BTC',):
                    snap['btc_free'] = free
                    snap['btc_locked'] = locked
                elif asset == quote_asset:
                    continue
                else:
                    snap['extra_assets'][asset] = {'free': free, 'locked': locked}
        except Exception as exc:
            snap['error'] = str(exc)
            logging.warning(f"wallet snapshot balance {exchange}: {exc}")

        try:
            price = float(adapter.get_price() or 0.0)
        except Exception as exc:
            price = 0.0
            if snap['error'] is None:
                snap['error'] = str(exc)
            logging.warning(f"wallet snapshot price {exchange}: {exc}")
        snap['price'] = price
        snap['portfolio_value'] = snap['quote_free'] + snap['btc_free'] * price
        return snap

    payload = {
        'exchange': 'binance',
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'binance': {},
        'okx': {},
        'bitkub': {},
        'totals': {
            'usdt_free': 0.0,
            'thb_free': 0.0,
            'btc_free': 0.0,
            'portfolio_value': 0.0,
            'reserve': 0.0,
        }
    }

    try:
        snapshot_row = {}
        try:
            with get_db_cursor() as (cursor, _):
                cursor.execute("SELECT * FROM strategy_state WHERE mode='cdc_dca_v1' LIMIT 1")
                row = cursor.fetchone()
                if row:
                    cols = [col[0] for col in cursor.description]
                    snapshot_row = dict(zip(cols, row))
        except Exception as db_exc:
            logging.warning(f"wallet strategy_state load: {db_exc}")

        current_exchange = str(snapshot_row.get('exchange') or 'binance').lower()
        payload['exchange'] = current_exchange

        reserve_binance = _safe_float(snapshot_row.get('reserve_binance_usdt'))
        reserve_okx = _safe_float(snapshot_row.get('reserve_okx_usdt'))
        total_reserve = _safe_float(snapshot_row.get('reserve_usdt'), reserve_binance + reserve_okx)

        testnet = _env_flag('USE_BINANCE_TESTNET') or _env_flag('BINANCE_TESTNET') or _env_flag('OKX_TESTNET')
        dry_run = _env_flag('STRATEGY_DRY_RUN') or _env_flag('DRY_RUN')

        binance_snapshot = _snapshot('binance', reserve_binance, testnet, dry_run)
        okx_snapshot = _snapshot('okx', reserve_okx, testnet, dry_run)
        bitkub_snapshot = _snapshot('bitkub', 0.0, testnet, dry_run)

        payload['binance'] = binance_snapshot
        payload['okx'] = okx_snapshot
        payload['bitkub'] = bitkub_snapshot

        payload['totals']['usdt_free'] = binance_snapshot['usdt_free'] + okx_snapshot['usdt_free']
        payload['totals']['thb_free'] = bitkub_snapshot.get('quote_free', 0.0)
        payload['totals']['btc_free'] = binance_snapshot['btc_free'] + okx_snapshot['btc_free'] + bitkub_snapshot['btc_free']
        payload['totals']['portfolio_value'] = binance_snapshot['portfolio_value'] + okx_snapshot['portfolio_value']
        payload['totals']['reserve'] = total_reserve if total_reserve else (reserve_binance + reserve_okx)

        return jsonify(payload)
    except Exception as e:
        logging.error(f"Error fetching wallet: {e}")
        return jsonify(payload), 200

# ====== CDC Action Zone (1D, BTCUSDT) ======
import time

def _env_flag(name: str, default: bool = False) -> bool:
    return shared_env_flag(name, default)

def _get_binance_client():
    try:
        if get_client is not None:
            c = get_client()
            if c is not None:
                return c
    except Exception:
        pass
    # Fallback to env keys
    try:
        api_key = os.getenv('BINANCE_API_KEY')
        api_secret = os.getenv('BINANCE_API_SECRET')
        if api_key and api_secret:
            testnet = _env_flag('USE_BINANCE_TESTNET', False) or _env_flag('BINANCE_TESTNET', False)
            return create_binance_client(
                {
                    'BINANCE_API_KEY': api_key,
                    'BINANCE_API_SECRET': api_secret,
                },
                testnet=testnet,
            )
    except Exception:
        return None
    return None

@app.route('/api/cdc_action_zone')
def api_cdc_action_zone():
    """Return CDC Action Zone status using bullish/bearish logic on 1D BTCUSDT.
    Caches result for 60 seconds.
    """
    try:
        now = time.time()
        if _CDC_CACHE['data'] is not None and now < _CDC_CACHE['expires']:
            return jsonify(_CDC_CACHE['data'])

        client = _get_binance_client()
        if client is None:
            payload = {'status': 'down', 'symbol': 'BTCUSDT', 'timeframe': '1d', 'error': 'binance client unavailable'}
            _CDC_CACHE.update({'data': payload, 'expires': now + 60})
            return jsonify(payload)

        klines = client.get_klines(symbol='BTCUSDT', interval=Client.KLINE_INTERVAL_1DAY, limit=300)
        closes = [float(k[4]) for k in klines]
        if len(closes) < 50:
            payload = {'status': 'down', 'symbol': 'BTCUSDT', 'timeframe': '1d', 'error': 'insufficient data'}
            _CDC_CACHE.update({'data': payload, 'expires': now + 60})
            return jsonify(payload)

        xprice = _ema(closes, 1)
        fast = _ema(xprice, 12)
        slow = _ema(xprice, 26)

        n = len(closes)
        bull = [fast[i] > slow[i] for i in range(n)]
        bear = [fast[i] < slow[i] for i in range(n)]
        green = [bull[i] and (xprice[i] > fast[i]) for i in range(n)]
        red = [bear[i] and (xprice[i] < fast[i]) for i in range(n)]
        buycond = [False] * n
        sellcond = [False] * n
        for i in range(1, n):
            buycond[i] = green[i] and (not green[i-1])
            sellcond[i] = red[i] and (not red[i-1])

        last_buy = _last_true_index(buycond)
        last_sell = _last_true_index(sellcond)
        cur = n - 1
        inf = float('inf')
        bars_since_buy = (cur - last_buy) if last_buy is not None else inf
        bars_since_sell = (cur - last_sell) if last_sell is not None else inf

        if bars_since_buy == inf and bars_since_sell == inf:
            # fallback: use bull/bear of last bar
            is_bullish = bull[-1]
        else:
            is_bullish = bars_since_buy < bars_since_sell

        status = 'up' if is_bullish else 'down'

        payload = {
            'status': status,
            'symbol': 'BTCUSDT',
            'timeframe': '1d',
            'updated_at': datetime.utcnow().isoformat() + 'Z'
        }
        _CDC_CACHE.update({'data': payload, 'expires': now + 60})
        return jsonify(payload)
    except Exception as e:
        logging.error(f"CDC endpoint error: {e}")
        payload = {'status': 'down', 'symbol': 'BTCUSDT', 'timeframe': '1d', 'error': str(e)}
        # cache errors shortly to avoid hammering
        _CDC_CACHE.update({'data': payload, 'expires': time.time() + 30})
    return jsonify(payload), 200


@app.route('/api/strategies')
def api_strategies():
    """Return consolidated strategy metadata for UI strategy selector."""
    columns = [
        'mode', 'cdc_enabled', 'last_cdc_status', 'last_transition_at', 'last_run_at',
        'reserve_usdt', 'reserve_binance_usdt', 'reserve_okx_usdt',
        'allocation_target_pct', 'allocation_actual_pct', 'strategy_status', 'metadata_json',
        'half_sell_policy', 'sell_percent', 'sell_percent_binance', 'sell_percent_okx'
    ]
    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute(
                """
                SELECT mode, cdc_enabled, last_cdc_status, last_transition_at, last_run_at,
                       reserve_usdt, reserve_binance_usdt, reserve_okx_usdt,
                       allocation_target_pct, allocation_actual_pct, strategy_status, metadata_json,
                       half_sell_policy, sell_percent, sell_percent_binance, sell_percent_okx
                FROM strategy_state
                ORDER BY mode
                """
            )
            rows = cursor.fetchall()
    except Exception as exc:
        logging.error(f"strategies endpoint error: {exc}")
        return jsonify({'strategies': [], 'error': str(exc)}), 200

    if not rows:
        return jsonify({'strategies': [], 'active_strategy': None, 'capital_pool_usdt': 0.0, 'derived': {}})

    feature_s4_enabled = _env_flag('FEATURE_S4_ENABLED', False)
    if not feature_s4_enabled:
        rows = [row for row in rows if row[0] != 's4_multi_leg']
        if not rows:
            return jsonify({'strategies': [], 'active_strategy': None, 'capital_pool_usdt': 0.0, 'derived': {}})

    weekly_active_total = get_total_active_amount()
    strategy_buffers = []
    total_hint = 0.0

    for fetched in rows:
        row = dict(zip(columns, fetched))
        raw_metadata = {}
        raw_blob = row.get('metadata_json')
        if raw_blob:
            try:
                raw_metadata = json.loads(raw_blob)
            except json.JSONDecodeError as exc:
                logging.warning(f"Strategy {row['mode']} metadata parse error: {exc}")
                raw_metadata = {}

        metadata = _strategy_metadata_for(row['mode'], raw_blob)
        allocation_cfg = metadata.get('allocation') or {}

        target_pct_raw = row.get('allocation_target_pct') or allocation_cfg.get('target_pct') or 0.0
        try:
            target_pct = float(target_pct_raw or 0.0)
        except (TypeError, ValueError):
            target_pct = 0.0

        capital_source = allocation_cfg.get('capital_source') or 'manual'
        capital_hint = None

        metrics_meta = metadata.get('metrics') or {}
        metrics_cap = metrics_meta.get('capital_usdt')
        if isinstance(metrics_cap, (int, float)):
            capital_hint = float(metrics_cap)

        config_meta = metadata.get('config') or {}
        config_cap = config_meta.get('capital_usdt')
        if capital_hint is None and isinstance(config_cap, (int, float)):
            capital_hint = float(config_cap)

        alloc_cap = allocation_cfg.get('capital_usdt')
        if capital_hint is None and isinstance(alloc_cap, (int, float)):
            capital_hint = float(alloc_cap)

        if capital_hint is None:
            if capital_source == 'auto_total_active_amount':
                capital_hint = weekly_active_total
            elif capital_source == 'reserve_total':
                capital_hint = _safe_float(row.get('reserve_usdt'))

        if capital_hint is None:
            capital_hint = _safe_float(row.get('reserve_usdt'))

        capital_hint = max(float(capital_hint), 0.0)
        total_hint += capital_hint

        strategy_buffers.append({
            'row': row,
            'metadata': metadata,
            'target_pct': target_pct,
            'capital_hint': capital_hint
        })

    strategies = []
    for entry in strategy_buffers:
        row = entry['row']
        metadata = entry['metadata']
        actual_pct_raw = row.get('allocation_actual_pct') or 0.0
        try:
            actual_pct = float(actual_pct_raw)
        except (TypeError, ValueError):
            actual_pct = 0.0

        if actual_pct <= 0:
            if total_hint > 0:
                actual_pct = round((entry['capital_hint'] / total_hint) * 100.0, 2)
            else:
                actual_pct = round(entry['target_pct'], 2)
        else:
            actual_pct = round(actual_pct, 2)

        target_pct_display = round(entry['target_pct'], 2) if entry['target_pct'] else 0.0
        reserves = {
            'total': _safe_float(row.get('reserve_usdt')),
            'binance': _safe_float(row.get('reserve_binance_usdt')),
            'okx': _safe_float(row.get('reserve_okx_usdt')),
        }
        strategy_status = row.get('strategy_status') or metadata.get('status') or ('active' if row.get('cdc_enabled') else 'inactive')

        runtime_data = {}
        if isinstance(raw_metadata, dict):
            runtime_data = _normalize_s4_runtime_aliases(raw_metadata.get('runtime') or {})

        strategies.append({
            'id': row['mode'],
            'display_name': metadata.get('display_name', row['mode']),
            'short_name': metadata.get('short_name', (row['mode'] or '').upper()),
            'category': metadata.get('category', 'core'),
            'status': strategy_status,
            'enabled': bool(row.get('cdc_enabled')),
            'last_status': row.get('last_cdc_status'),
            'last_transition_at': _dt_to_iso(row.get('last_transition_at')),
            'last_run_at': _dt_to_iso(row.get('last_run_at')),
            'reserves': reserves,
            'allocation': {
                'target_pct': target_pct_display,
                'actual_pct': actual_pct,
                'capital_hint_usdt': round(entry['capital_hint'], 2)
            },
            'guards': metadata.get('guards', []),
            'log_filters': metadata.get('log_filters', []),
            'help_overlay': metadata.get('help_overlay'),
            'metadata': metadata,
             'runtime': runtime_data,
            'parameters': {
                'half_sell_policy': row.get('half_sell_policy'),
                'sell_percent': _safe_int(row.get('sell_percent')),
                'sell_percent_binance': _safe_int(row.get('sell_percent_binance')),
                'sell_percent_okx': _safe_int(row.get('sell_percent_okx'))
            }
        })

    # Append virtual Bitkub strategy card sourced from schedules + purchase history.
    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute(
                """
                SELECT COUNT(*), COALESCE(SUM(purchase_amount), 0)
                FROM schedules
                WHERE is_active = 1 AND exchange_mode = 'bitkub'
                """
            )
            sched_row = cursor.fetchone() or (0, 0)
            active_count = int(sched_row[0] or 0)
            active_total_thb = float(sched_row[1] or 0.0)

            cursor.execute(
                """
                SELECT purchase_time, usdt_amount, btc_quantity, btc_price, order_id,
                       fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount
                FROM purchase_history
                WHERE COALESCE(exchange, '') = 'bitkub'
                ORDER BY purchase_time DESC
                LIMIT 1
                """
            )
            last_buy = cursor.fetchone()
    except Exception as exc:
        logging.warning(f"bitkub strategy summary load failed: {exc}")
        active_count = 0
        active_total_thb = 0.0
        last_buy = None

    api_configured = bool(os.getenv('BITKUB_API_KEY')) and bool(os.getenv('BITKUB_API_SECRET'))
    market_filters = {}
    last_error = None
    if api_configured:
        try:
            ad = get_adapter('bitkub', testnet=False, dry_run=True)
            market_filters = ad.get_filters() or {}
        except Exception as exc:
            last_error = str(exc)
            logging.warning(f"bitkub market filter fetch failed: {exc}")

    bitkub_metadata = _strategy_metadata_for('bitkub_dca_v1', None)
    last_run_at = _dt_to_iso(last_buy[0]) if last_buy and last_buy[0] else None
    last_order = None
    if last_buy:
        quote_spent = float(last_buy[1] or 0.0)
        filled_btc = float(last_buy[2] or 0.0)
        avg_price = float(last_buy[3] or 0.0)
        fee_asset = (last_buy[6] or 'THB')
        fee_amount = float(last_buy[7] if last_buy[7] is not None else (last_buy[5] or 0.0))
        last_order = {
            'time': _dt_to_iso(last_buy[0]),
            'quote_amount': quote_spent,
            'quote_asset': 'THB',
            'filled_btc': filled_btc,
            'avg_price': avg_price,
            'order_id': str(last_buy[4]) if last_buy[4] is not None else None,
            'fee_asset': fee_asset,
            'fee_amount': fee_amount,
        }

    if not any(s.get('id') == 'bitkub_dca_v1' for s in strategies):
        strategies.append({
        'id': 'bitkub_dca_v1',
        'display_name': bitkub_metadata.get('display_name', 'Bitkub THB DCA'),
        'short_name': bitkub_metadata.get('short_name', 'BITKUB'),
        'category': bitkub_metadata.get('category', 'dca'),
        'status': 'active' if active_count > 0 else 'idle',
        'enabled': active_count > 0,
        'last_status': 'pure_dca',
        'last_transition_at': None,
        'last_run_at': last_run_at,
        'reserves': {'total': 0.0, 'binance': 0.0, 'okx': 0.0},
        'allocation': {
            'target_pct': 0.0,
            'actual_pct': 0.0,
            'capital_hint_usdt': 0.0
        },
        'guards': bitkub_metadata.get('guards', []),
        'log_filters': bitkub_metadata.get('log_filters', []),
        'help_overlay': bitkub_metadata.get('help_overlay'),
        'metadata': bitkub_metadata,
        'runtime': {
            'symbol': 'BTC_THB',
            'quote_asset': 'THB',
            'api_configured': api_configured,
            'active_schedules': active_count,
            'active_total_thb': round(active_total_thb, 2),
            'market_filters': {
                'min_notional': _safe_float(market_filters.get('minNotional')),
                'tick_size': _safe_float(market_filters.get('tickSize')),
                'step_size': _safe_float(market_filters.get('stepSize')),
                'min_qty': _safe_float(market_filters.get('minQty')),
            },
            'last_order': last_order,
            'last_error': last_error,
        },
        'parameters': {}
        })

    active_strategy = next((s['id'] for s in strategies if s['enabled']), None)
    response = {
        'strategies': strategies,
        'active_strategy': active_strategy,
        'capital_pool_usdt': round(total_hint, 2),
        'derived': {
            'weekly_active_total_usdt': round(weekly_active_total, 2)
        },
        'generated_at': datetime.utcnow().isoformat() + 'Z'
    }
    return jsonify(response)


@app.route('/api/strategy_holdings')
def api_strategy_holdings():
    """Return cached holdings snapshot for key exchanges/assets."""
    refresh_flag = str(request.args.get('refresh', '')).strip().lower() in ('1', 'true', 'yes', 'force')
    exchanges = set()
    assets = {'BTC', 'USDT'}
    s4_gold_asset = None
    bitkub_enabled = False

    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute("SELECT exchange FROM strategy_state WHERE mode='cdc_dca_v1' LIMIT 1")
            row = cursor.fetchone()
            if row:
                cdc_exchange = str(row[0] or '').strip().lower()
                if cdc_exchange:
                    exchanges.add(cdc_exchange)
    except Exception as exc:
        logging.debug(f"strategy_holdings: cdc exchange lookup failed: {exc}")

    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute("SELECT metadata_json FROM strategy_state WHERE mode='s4_multi_leg' LIMIT 1")
            row = cursor.fetchone()
            if row and row[0]:
                try:
                    metadata = json.loads(row[0])
                except json.JSONDecodeError:
                    metadata = {}
                config = metadata.get('config') or {}
                exch = str(config.get('exchange') or 'okx').strip().lower()
                if exch in ('binance', 'okx'):
                    exchanges.add(exch)
                    s4_gold_asset = 'PAXG' if exch == 'binance' else 'XAUT'
    except Exception as exc:
        logging.debug(f"strategy_holdings: s4 metadata lookup failed: {exc}")

    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute("SELECT COUNT(*) FROM schedules WHERE is_active=1 AND exchange_mode='bitkub'")
            bitkub_enabled = int((cursor.fetchone() or [0])[0] or 0) > 0
    except Exception as exc:
        logging.debug(f"strategy_holdings: bitkub schedule lookup failed: {exc}")

    if bitkub_enabled:
        exchanges.add('bitkub')
        assets.add('THB')

    if not exchanges:
        exchanges.add('binance')
    if s4_gold_asset:
        assets.add(s4_gold_asset)

    try:
        snapshot = fetch_balances(sorted(exchanges), sorted(assets), force_refresh=refresh_flag)
    except ValueError as exc:
        return jsonify({'ok': False, 'error': str(exc)}), 400
    except Exception as exc:
        logging.error(f"strategy_holdings error: {exc}")
        return jsonify({'ok': False, 'error': 'fetch_failed'}), 500

    holdings_meta = None
    if isinstance(snapshot, dict):
        holdings_meta = snapshot.pop('_meta', None)

    response = {
        'ok': True,
        'timestamp': datetime.utcnow().isoformat() + 'Z',
        'exchanges': sorted(exchanges),
        'assets': sorted(assets),
        'holdings': snapshot if isinstance(snapshot, dict) else {},
        'meta': holdings_meta,
        'force_refresh': refresh_flag,
    }
    if s4_gold_asset:
        response['s4_gold_asset'] = s4_gold_asset
    return jsonify(response)


@app.route('/api/fee_totals')
def api_fee_totals():
    """Return cumulative fee totals per exchange/strategy and summarized by exchange."""
    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute(
                """
                SELECT exchange, strategy, fee_type, fee_asset, fee_usd, fee_asset_amount, last_updated
                FROM strategy_fee_totals
                ORDER BY exchange, strategy, fee_type, fee_asset
                """
            )
            rows = cursor.fetchall()
    except Exception as exc:
        logging.error(f"fee_totals error: {exc}")
        return jsonify({'ok': False, 'error': str(exc)}), 500

    totals = []
    summary: dict[str, dict[str, dict]] = {}
    for row in rows:
        exchange, strategy, fee_type, fee_asset, fee_usd, fee_asset_amount, last_updated = row
        record = {
            'exchange': exchange,
            'strategy': strategy,
            'fee_type': fee_type,
            'fee_asset': fee_asset,
            'fee_usd': float(fee_usd or 0.0),
            'fee_asset_amount': float(fee_asset_amount or 0.0),
            'last_updated': last_updated.isoformat() if last_updated else None,
        }
        totals.append(record)

        ex_summary = summary.setdefault(exchange, {'buy': {'fee_usd': 0.0, 'fee_asset': {}}, 'sell': {'fee_usd': 0.0, 'fee_asset': {}}})
        bucket = ex_summary.get(fee_type)
        if bucket is None:
            bucket = {'fee_usd': 0.0, 'fee_asset': {}}
            ex_summary[fee_type] = bucket
        bucket['fee_usd'] += float(fee_usd or 0.0)
        asset_map = bucket.setdefault('fee_asset', {})
        asset_key = fee_asset or 'UNKNOWN'
        asset_map[asset_key] = asset_map.get(asset_key, 0.0) + float(fee_asset_amount or 0.0)

    response = {
        'ok': True,
        'generated_at': datetime.utcnow().isoformat() + 'Z',
        'totals': totals,
        'summary': summary,
    }
    return jsonify(response)

@app.route('/api/strategy_state')
def api_strategy_state():
    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute("SELECT cdc_enabled, last_cdc_status, reserve_usdt, last_transition_at, sell_percent, exchange, okx_max_usdt, binance_max_usdt, reserve_binance_usdt, reserve_okx_usdt, half_sell_policy, sell_percent_binance, sell_percent_okx FROM strategy_state WHERE mode='cdc_dca_v1' LIMIT 1")
            row = cursor.fetchone()
        if not row:
            return jsonify({'cdc_enabled': False, 'last_cdc_status': None, 'reserve_usdt': 0.0, 'reserve_binance_usdt': 0.0, 'reserve_okx_usdt': 0.0, 'last_transition_at': None, 'sell_percent': 50, 'exchange': 'binance', 'okx_max_usdt': float(os.getenv('OKX_MAX_USDT') or 10.0), 'binance_max_usdt': float(os.getenv('BINANCE_MAX_USDT') or 0.0), 'half_sell_policy': 'auto_proportional', 'testnet': _env_flag('USE_BINANCE_TESTNET') or _env_flag('BINANCE_TESTNET'), 'dry_run': _env_flag('STRATEGY_DRY_RUN') or _env_flag('DRY_RUN')})
        # Build response with backward compatibility
        exchange = (row[5] or 'binance')
        sell_percent_global = int(row[4] or 50)
        sp_bz = int(row[11] if row[11] is not None else sell_percent_global)
        sp_okx = int(row[12] if row[12] is not None else sell_percent_global)
        current_sp = sp_okx if str(exchange).lower() == 'okx' else sp_bz
        okx_db_val = row[6]
        bz_db_val = row[7]
        if okx_db_val is None:
            env_okx = os.getenv('OKX_MAX_USDT')
            try:
                okx_max = float(env_okx) if env_okx not in (None, '') else 10.0
            except (TypeError, ValueError):
                okx_max = 10.0
        else:
            try:
                okx_max = float(okx_db_val)
            except (TypeError, ValueError):
                okx_max = 10.0
        if bz_db_val is None:
            env_bz = os.getenv('BINANCE_MAX_USDT')
            try:
                binance_max = float(env_bz) if env_bz not in (None, '') else 0.0
            except (TypeError, ValueError):
                binance_max = 0.0
        else:
            try:
                binance_max = float(bz_db_val)
            except (TypeError, ValueError):
                binance_max = 0.0

        return jsonify({
            'cdc_enabled': bool(row[0]),
            'last_cdc_status': row[1],
            'reserve_usdt': float(row[2] or 0),
            'last_transition_at': str(row[3]) if row[3] else None,
            'sell_percent': current_sp,  # alias for UI
            'sell_percent_binance': sp_bz,
            'sell_percent_okx': sp_okx,
            'exchange': exchange,
            'okx_max_usdt': okx_max,
            'binance_max_usdt': binance_max,
            'reserve_binance_usdt': float(row[8] or 0),
            'reserve_okx_usdt': float(row[9] or 0),
            'half_sell_policy': row[10] or 'auto_proportional',
            'testnet': _env_flag('USE_BINANCE_TESTNET') or _env_flag('BINANCE_TESTNET'),
            'dry_run': _env_flag('STRATEGY_DRY_RUN') or _env_flag('DRY_RUN')
        })
    except Exception as e:
        logging.error(f"strategy_state error: {e}")
        return jsonify({'cdc_enabled': False, 'reserve_usdt': 0.0, 'exchange': 'binance', 'error': str(e), 'testnet': _env_flag('USE_BINANCE_TESTNET') or _env_flag('BINANCE_TESTNET'), 'dry_run': _env_flag('STRATEGY_DRY_RUN') or _env_flag('DRY_RUN')}), 200

@app.route('/api/strategy_update', methods=['POST'])
@app.route('/api/strategy_update/', methods=['POST'])
def api_strategy_update():
    """Update CDC strategy settings.
    Supports per-exchange sell percent by passing { sell_percent, exchange }.
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        sell_percent = data.get('sell_percent')
        sell_exchange = str(data.get('exchange') or '').strip().lower()
        half_sell_policy = data.get('half_sell_policy')
        updates = []
        params = []
        if sell_percent is not None:
            try:
                sell_percent = int(sell_percent)
            except Exception:
                return jsonify({'ok': False, 'error': 'sell_percent must be integer'}), 400
            if sell_percent < 0 or sell_percent > 100:
                return jsonify({'ok': False, 'error': 'sell_percent must be between 0 and 100'}), 400
            if sell_exchange in ('binance','okx'):
                col = 'sell_percent_okx' if sell_exchange == 'okx' else 'sell_percent_binance'
                updates.append(f"{col}=%s"); params.append(sell_percent)
            else:
                # legacy/global fallback
                updates.append('sell_percent=%s'); params.append(sell_percent)
        if half_sell_policy is not None:
            if str(half_sell_policy) not in ('auto_proportional','binance_only','okx_only'):
                return jsonify({'ok': False, 'error': 'invalid half_sell_policy'}), 400
            updates.append('half_sell_policy=%s'); params.append(half_sell_policy)
        if not updates:
            return jsonify({'ok': False, 'error': 'no fields to update'}), 400
        with get_db_cursor() as (cursor, db):
            cursor.execute(f"UPDATE strategy_state SET {', '.join(updates)} WHERE mode='cdc_dca_v1'", tuple(params))
            db.commit()
        return jsonify({'ok': True, 'sell_percent': sell_percent, 'exchange': sell_exchange or None, 'half_sell_policy': half_sell_policy})
    except Exception as e:
        logging.error(f"strategy_update error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/strategy_toggle', methods=['POST'])
@app.route('/api/strategy_toggle/', methods=['POST'])
def api_strategy_toggle():
    try:
        data = request.get_json(force=True, silent=True) or {}
        enabled = bool(data.get('enabled'))
        mode = str(data.get('mode') or 'cdc_dca_v1').strip().lower()
        allowed_modes = {'cdc_dca_v1', 's4_multi_leg'}
        if mode not in allowed_modes:
            return jsonify({'ok': False, 'error': f'invalid_mode:{mode}'}), 400
        with get_db_cursor() as (cursor, db):
            cursor.execute("UPDATE strategy_state SET cdc_enabled = %s WHERE mode=%s", (1 if enabled else 0, mode))
            if cursor.rowcount == 0:
                return jsonify({'ok': False, 'error': f'mode_not_found:{mode}'}), 404
            db.commit()
        # Notify via LINE
        if mode == 'cdc_dca_v1':
            try:
                notify_cdc_toggle(enabled, {
                    'testnet': _env_flag('USE_BINANCE_TESTNET') or _env_flag('BINANCE_TESTNET'),
                    'dry_run': _env_flag('STRATEGY_DRY_RUN') or _env_flag('DRY_RUN'),
                })
            except Exception as e:
                logging.warning(f"CDC toggle notify failed: {e}")
        return jsonify({'ok': True, 'mode': mode, 'enabled': enabled})
    except Exception as e:
        logging.error(f"strategy_toggle error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/okx_config', methods=['POST'])
def api_okx_config():
    """Update OKX config (e.g., okx_max_usdt). Admin-only."""
    try:
        admin_token = os.getenv('ADMIN_TOKEN')
        data = request.get_json(force=True, silent=True) or {}
        token = data.get('token')
        if not admin_token:
            return jsonify({'ok': False, 'error': 'admin_token_not_configured'}), 400
        if token != admin_token:
            return jsonify({'ok': False, 'error': 'invalid_admin_token'}), 403
        raw_value = data.get('okx_max_usdt')
        try:
            max_usdt = float(raw_value)
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'error': 'invalid okx_max_usdt'}), 400
        if max_usdt < 0:
            return jsonify({'ok': False, 'error': 'invalid okx_max_usdt'}), 400
        with get_db_cursor() as (cursor, db):
            cursor.execute("UPDATE strategy_state SET okx_max_usdt=%s WHERE mode='cdc_dca_v1'", (max_usdt,))
            db.commit()
        return jsonify({'ok': True, 'okx_max_usdt': max_usdt})
    except Exception as e:
        logging.error(f"okx_config error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/binance_config', methods=['POST'])
def api_binance_config():
    """Update Binance config (e.g., binance_max_usdt). Admin-only."""
    try:
        admin_token = os.getenv('ADMIN_TOKEN')
        data = request.get_json(force=True, silent=True) or {}
        token = data.get('token')
        if not admin_token:
            return jsonify({'ok': False, 'error': 'admin_token_not_configured'}), 400
        if token != admin_token:
            return jsonify({'ok': False, 'error': 'invalid_admin_token'}), 403
        max_usdt = data.get('binance_max_usdt')
        try:
            max_usdt = float(max_usdt)
            if max_usdt < 0:
                raise ValueError('binance_max_usdt must be >= 0')
        except Exception:
            return jsonify({'ok': False, 'error': 'invalid binance_max_usdt'}), 400
        with get_db_cursor() as (cursor, db):
            cursor.execute("UPDATE strategy_state SET binance_max_usdt=%s WHERE mode='cdc_dca_v1'", (max_usdt,))
            db.commit()
        return jsonify({'ok': True, 'binance_max_usdt': max_usdt})
    except Exception as e:
        logging.error(f"binance_config error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/s4_config', methods=['POST'])
def api_s4_config():
    """Update S4 strategy configuration (admin token required)."""
    try:
        admin_token = os.getenv('ADMIN_TOKEN')
        data = request.get_json(force=True, silent=True) or {}
        token = data.get('token')
        if not admin_token:
            return jsonify({'ok': False, 'error': 'admin_token_not_configured'}), 400
        if token != admin_token:
            return jsonify({'ok': False, 'error': 'invalid_admin_token'}), 403

        updates: dict[str, float | int | None] = {}

        def _maybe_ratio(field: str, *, min_val: float = 0.0, max_val: float = 100.0, allow_null: bool = False):
            if field not in data:
                return
            raw = data[field]
            if allow_null and (raw is None or (isinstance(raw, str) and raw.strip() == '')):
                updates[field] = None
                return
            try:
                val = float(raw)
            except Exception:
                raise ValueError(f"{field} must be numeric")
            if val < min_val or val > max_val:
                raise ValueError(f"{field} must be between {min_val} and {max_val}")
            updates[field] = round(val / 100.0, 6)

        def _maybe_percent(field: str, *, min_val: float = 0.0, max_val: float = 100.0):
            if field not in data:
                return
            try:
                val = float(data[field])
            except Exception:
                raise ValueError(f"{field} must be numeric")
            if val < min_val or val > max_val:
                raise ValueError(f"{field} must be between {min_val} and {max_val}")
            updates[field] = round(val, 6)

        def _maybe_positive(field: str, *, allow_zero: bool = False):
            if field not in data:
                return
            try:
                val = float(data[field])
            except Exception:
                raise ValueError(f"{field} must be numeric")
            if allow_zero:
                if val < 0:
                    raise ValueError(f"{field} must be >= 0")
            else:
                if val <= 0:
                    raise ValueError(f"{field} must be > 0")
            updates[field] = round(val, 6)

        def _maybe_int(field: str, *, allow_zero: bool = True):
            if field not in data:
                return
            try:
                val = int(float(data[field]))
            except Exception:
                raise ValueError(f"{field} must be integer")
            if not allow_zero and val <= 0:
                raise ValueError(f"{field} must be > 0")
            if allow_zero and val < 0:
                raise ValueError(f"{field} must be >= 0")
            updates[field] = val

        try:
            _maybe_ratio('target_btc_pct_up', min_val=0.0, max_val=100.0)
            _maybe_ratio('target_btc_pct_down', min_val=0.0, max_val=100.0)
            _maybe_ratio('target_gold_pct_up', min_val=0.0, max_val=100.0, allow_null=True)
            _maybe_ratio('target_gold_pct_down', min_val=0.0, max_val=100.0, allow_null=True)
            _maybe_percent('rebalance_threshold_pct', min_val=0.1, max_val=50.0)
            _maybe_percent('max_flip_pct', min_val=0.1, max_val=100.0)
            _maybe_positive('min_flip_usd')
            _maybe_positive('capital_usdt', allow_zero=True)
            _maybe_int('cooldown_minutes', allow_zero=True)
            if 'exchange' in data:
                exch = str(data.get('exchange') or '').strip().lower()
                if exch not in ('binance', 'okx'):
                    raise ValueError("exchange must be 'binance' or 'okx'")
                updates['exchange'] = exch
        except ValueError as exc:
            return jsonify({'ok': False, 'error': str(exc)}), 400

        if not updates:
            return jsonify({'ok': False, 'error': 'no fields to update'}), 400

        # Fetch existing metadata (separate connection)
        with get_db_cursor() as (cursor, db):
            cursor.execute("SELECT metadata_json FROM strategy_state WHERE mode='s4_multi_leg' LIMIT 1")
            row = cursor.fetchone()

        if row and row[0]:
            try:
                metadata = json.loads(row[0])
            except json.JSONDecodeError:
                metadata = {}
        else:
            metadata = deepcopy(DEFAULT_STRATEGY_METADATA.get('s4_multi_leg', {}))

        config = metadata.get('config') or {}
        for key, value in updates.items():
            if value is None:
                config.pop(key, None)
            else:
                config[key] = value
        metadata['config'] = config

        runtime = metadata.get('runtime') or {}
        runtime.pop('exposure', None)
        metadata['runtime'] = runtime

        metadata_json = json.dumps(_json_sanitize(metadata))

        # Apply update using new cursor to avoid reuse issues
        with get_db_cursor() as (cursor, db):
            cursor.execute(
                "UPDATE strategy_state SET metadata_json=%s, updated_at=NOW() WHERE mode='s4_multi_leg'",
                (metadata_json,)
            )
            db.commit()

        return jsonify({'ok': True, 'config': metadata.get('config', {})})
    except Exception as e:
        logging.exception(f"s4_config error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500


@app.route('/api/okx_trades_sync', methods=['POST'])
def api_okx_trades_sync():
    """Sync recent OKX spot fills (BTC-USDT) into okx_trades table."""
    try:
        from exchanges.okx import OkxAdapter
        ad = OkxAdapter()
        fills = ad.get_fills_history(limit=100)
        inserted = 0
        rows = []
        for f in fills:
            fill_id = str(f.get('billId') or f.get('tradeId') or f.get('ordId'))
            ord_id = str(f.get('ordId') or '')
            side = str(f.get('side') or '').upper()
            px = float(f.get('fillPx') or f.get('px') or 0)
            sz = float(f.get('fillSz') or f.get('sz') or 0)
            q = px * sz
            fee = float(f.get('fee') or 0)
            fee_ccy = str(f.get('feeCcy') or '')
            ts = f.get('fillTime') or f.get('ts')
            from datetime import datetime
            try:
                t = datetime.fromtimestamp(int(ts)/1000.0)
            except Exception:
                t = datetime.utcnow()
            rows.append((fill_id, ord_id, side, px, sz, q, fee, fee_ccy, t))
        if rows:
            with get_db_cursor() as (cursor, db):
                cursor.executemany(
                    """
                    INSERT IGNORE INTO okx_trades (fill_id, ord_id, side, price, qty, quote_qty, fee, fee_ccy, trade_time)
                    VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    rows
                )
                inserted = cursor.rowcount
                db.commit()
        return jsonify({'ok': True, 'synced': inserted})
    except Exception as e:
        logging.error(f"okx_trades_sync error: {e}")
        return jsonify({'ok': False, 'error': str(e), 'synced': 0}), 500

@app.route('/api/okx_trades')
def api_okx_trades():
    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute(
                """
                SELECT trade_time, side, price, qty, quote_qty, fee, fee_ccy, ord_id, fill_id
                FROM okx_trades ORDER BY trade_time DESC LIMIT 100
                """
            )
            rows = cursor.fetchall()
        data = [{
            'trade_time': str(r[0]), 'side': r[1], 'price': float(r[2] or 0), 'qty': float(r[3] or 0),
            'quote_qty': float(r[4] or 0), 'fee': float(r[5] or 0), 'fee_ccy': r[6], 'ord_id': r[7], 'fill_id': r[8]
        } for r in rows]
        return jsonify({'count': len(data), 'trades': data})
    except Exception as e:
        logging.error(f"okx_trades error: {e}")
        return jsonify({'count': 0, 'error': str(e)}), 500

@app.route('/api/okx_trades_analytics')
def api_okx_trades_analytics():
    """Compute PnL/avg price from okx_trades using moving-average method (similar to Binance)."""
    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute(
                """
                SELECT trade_time, side, price, qty, quote_qty, fee, fee_ccy
                FROM okx_trades
                ORDER BY trade_time ASC
                """
            )
            rows = cursor.fetchall()

        position_btc = 0.0
        cost_usdt = 0.0
        total_buy_usdt = 0.0
        total_sell_usdt = 0.0
        realized_pnl = 0.0

        for r in rows:
            trade_dt, side, price, qty, quote_qty, fee, fee_ccy = r
            price = float(price or 0.0)
            qty = float(qty or 0.0)
            quote_qty = float(quote_qty or (price * qty))
            fee = float(fee or 0.0)
            fee_ccy = (fee_ccy or '').upper()

            fee_usdt = fee if fee_ccy == 'USDT' else 0.0
            fee_btc = fee if fee_ccy == 'BTC' else 0.0
            fee_other_usdt = 0.0  # not converted for simplicity

            if (side or '').upper() == 'BUY':
                adj_qty = max(qty - fee_btc, 0.0)
                adj_cost = quote_qty + fee_usdt + fee_other_usdt
                total_buy_usdt += adj_cost
                new_position = position_btc + adj_qty
                if new_position <= 0:
                    position_btc = 0.0
                    cost_usdt = 0.0
                else:
                    cost_usdt += adj_cost
                    position_btc = new_position
            else:  # SELL
                proceeds = max(quote_qty - fee_usdt - fee_other_usdt, 0.0)
                total_sell_usdt += proceeds
                if position_btc <= 0:
                    realized_pnl += proceeds
                    continue
                avg_cost = cost_usdt / position_btc if position_btc > 0 else 0.0
                qty_to_close = min(qty, position_btc)
                realized_pnl += proceeds - (avg_cost * qty_to_close)
                cost_usdt -= avg_cost * qty_to_close
                position_btc -= qty_to_close
                if fee_btc > 0 and position_btc > 0:
                    extra_close = min(fee_btc, position_btc)
                    realized_pnl += 0.0 - (avg_cost * extra_close)
                    cost_usdt -= avg_cost * extra_close
                    position_btc -= extra_close

        # Current price from adapter (or utils)
        current_price = 0.0
        try:
            if get_btc_price is not None:
                current_price = float(get_btc_price() or 0.0)
        except Exception:
            current_price = 0.0
        portfolio_value = position_btc * current_price
        unrealized_pnl = portfolio_value - cost_usdt
        avg_price = (cost_usdt / position_btc) if position_btc > 0 else 0.0

        return jsonify({
            'total_buys_usdt': round(total_buy_usdt, 2),
            'total_sells_usdt': round(total_sell_usdt, 2),
            'position_btc': round(position_btc, 8),
            'avg_price': round(avg_price, 2),
            'realized_pnl': round(realized_pnl, 2),
            'unrealized_pnl': round(unrealized_pnl, 2),
            'current_price': round(current_price, 2),
            'portfolio_value': round(portfolio_value, 2),
            'count': len(rows),
        })
    except Exception as e:
        logging.error(f"okx_trades_analytics error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/strategy_exchange', methods=['POST'])
def api_strategy_exchange():
    """Update global exchange for trading (binance|okx). Admin-only."""
    try:
        admin_token = os.getenv('ADMIN_TOKEN')
        data = request.get_json(force=True, silent=True) or {}
        token = data.get('token')
        exchange = str(data.get('exchange') or '').strip().lower()
        if not admin_token:
            return jsonify({'ok': False, 'error': 'admin_token_not_configured'}), 400
        if token != admin_token:
            return jsonify({'ok': False, 'error': 'invalid_admin_token'}), 403
        if exchange not in ('binance', 'okx'):
            return jsonify({'ok': False, 'error': 'invalid_exchange'}), 400

        with get_db_cursor() as (cursor, db):
            cursor.execute("UPDATE strategy_state SET exchange=%s WHERE mode='cdc_dca_v1'", (exchange,))
            db.commit()

        try:
            from notify import notify_exchange_changed
            flags = {
                'testnet': _env_flag('USE_BINANCE_TESTNET') or _env_flag('BINANCE_TESTNET') or _env_flag('OKX_TESTNET'),
                'dry_run': _env_flag('STRATEGY_DRY_RUN') or _env_flag('DRY_RUN')
            }
            notify_exchange_changed(exchange, flags)
        except Exception as ne:
            logging.warning(f"exchange change notify failed: {ne}")

        return jsonify({'ok': True, 'exchange': exchange})
    except Exception as e:
        logging.error(f"strategy_exchange error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/reserve_transfer', methods=['POST'])
def api_reserve_transfer():
    """Move available USDT into strategy reserve (per exchange or global). Admin-only."""
    try:
        admin_token = os.getenv('ADMIN_TOKEN')
        data = request.get_json(force=True, silent=True) or {}
        token = data.get('token')
        if not admin_token or token != admin_token:
            return jsonify({'ok': False, 'error': 'forbidden'}), 403

        exchange = str(data.get('exchange') or '').strip().lower() or 'global'
        amount_raw = data.get('amount')
        try:
            amount = float(amount_raw)
        except (TypeError, ValueError):
            return jsonify({'ok': False, 'error': 'invalid_amount'}), 400
        if amount <= 0:
            return jsonify({'ok': False, 'error': 'amount_must_be_positive'}), 400

        note = str(data.get('note') or '').strip() or None
        testnet = _env_flag('USE_BINANCE_TESTNET') or _env_flag('BINANCE_TESTNET') or _env_flag('OKX_TESTNET')
        dry_run = _env_flag('STRATEGY_DRY_RUN') or _env_flag('DRY_RUN')

        new_value = 0.0
        if exchange == 'global':
            new_value = increment_reserve(amount, reason='manual_deposit', note=note or 'Manual reserve deposit (global)')
        elif exchange in ('binance', 'okx'):
            try:
                adapter = get_adapter(exchange, testnet=testnet, dry_run=dry_run)
                balance = adapter.get_balance('USDT')
                free_bal = float(balance.get('free') or 0.0)
                if not dry_run and not testnet and amount > free_bal:
                    return jsonify({'ok': False, 'error': f'insufficient_{exchange}_balance', 'free': free_bal}), 400
            except Exception as exc:
                logging.warning(f"reserve_transfer balance check failed ({exchange}): {exc}")
            reason = f'manual_deposit_{exchange}'
            default_note = f'Manual reserve deposit from {exchange.upper()} balance'
            new_value = increment_reserve_exchange(exchange, amount, reason=reason, note=note or default_note)
        else:
            return jsonify({'ok': False, 'error': 'invalid_exchange'}), 400

        reserves = {'binance': 0.0, 'okx': 0.0, 'total': 0.0}
        try:
            with get_db_cursor() as (cursor, _):
                cursor.execute("SELECT reserve_binance_usdt, reserve_okx_usdt, reserve_usdt FROM strategy_state WHERE mode='cdc_dca_v1' LIMIT 1")
                row = cursor.fetchone()
                if row:
                    reserves['binance'] = float(row[0] or 0.0)
                    reserves['okx'] = float(row[1] or 0.0)
                    reserves['total'] = float(row[2] or 0.0)
        except Exception as exc:
            logging.warning(f"reserve_transfer state fetch failed: {exc}")

        return jsonify({'ok': True, 'exchange': exchange, 'reserve': new_value, 'reserves': reserves})
    except Exception as e:
        logging.error(f"reserve_transfer error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500

@app.route('/api/reserve_log')
def api_reserve_log():
    try:
        limit = int(request.args.get('limit') or 50)
        with get_db_cursor() as (cursor, _):
            cursor.execute("SELECT event_time, change_usdt, reserve_after, reason, note FROM reserve_log ORDER BY event_time DESC LIMIT %s", (limit,))
            rows = cursor.fetchall()
        data = [{
            'event_time': str(r[0]),
            'change_usdt': float(r[1]),
            'reserve_after': float(r[2]),
            'reason': r[3],
            'note': r[4]
        } for r in rows]
        return jsonify({'items': data})
    except Exception as e:
        logging.error(f"reserve_log error: {e}")
        return jsonify({'items': [], 'error': str(e)}), 200

def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None

@app.route('/api/compliance_events')
def api_compliance_events():
    try:
        limit = int(request.args.get('limit') or 200)
        start = _parse_iso_datetime(request.args.get('start'))
        end = _parse_iso_datetime(request.args.get('end'))
        events = fetch_events(limit=limit, start=start, end=end)
        return jsonify({'events': events, 'generated_at': datetime.utcnow().isoformat() + 'Z'})
    except Exception as e:
        logging.error(f"compliance_events error: {e}")
        return jsonify({'events': [], 'error': str(e)}), 200

@app.route('/api/compliance_export')
def api_compliance_export():
    """Export compliance audit log as CSV."""
    try:
        limit = int(request.args.get('limit') or 500)
        start = _parse_iso_datetime(request.args.get('start'))
        end = _parse_iso_datetime(request.args.get('end'))
        events = fetch_events(limit=limit, start=start, end=end)
        import csv
        import io
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['event_time','event_type','exchange','notional_usdt','btc_quantity','price_usdt','realized_pnl_usdt','metadata'])
        for e in events:
            writer.writerow([
                e.get('event_time'),
                e.get('event_type'),
                e.get('exchange'),
                e.get('notional_usdt'),
                e.get('btc_quantity'),
                e.get('price_usdt'),
                e.get('realized_pnl_usdt'),
                json.dumps(e.get('metadata', {}), ensure_ascii=False),
            ])
        data = output.getvalue().encode('utf-8')
        from flask import Response
        import datetime as _dt
        fname = f"compliance_audit_{_dt.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.csv"
        return Response(data, mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename={fname}'})
    except Exception as e:
        logging.error(f"compliance_export error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/sell_history')
def api_sell_history():
    try:
        limit = int(request.args.get('limit') or 50)
        with get_db_cursor() as (cursor, _):
            cursor.execute(
                """
                SELECT sell_time, symbol, btc_quantity, usdt_received, price, order_id, sell_percent,
                       fee_sell_usdt, fee_sell_asset, fee_sell_asset_amount
                FROM sell_history ORDER BY sell_time DESC LIMIT %s
                """,
                (limit,)
            )
            rows = cursor.fetchall()
        data = [{
            'sell_time': str(r[0]),
            'symbol': r[1],
            'btc_quantity': float(r[2]),
            'usdt_received': float(r[3]),
            'price': float(r[4]),
            'order_id': r[5],
            'sell_percent': (int(r[6]) if r[6] is not None else None),
            'fee_sell_usdt': float(r[7]) if r[7] is not None else None,
            'fee_sell_asset': r[8],
            'fee_sell_asset_amount': float(r[9]) if r[9] is not None else None,
        } for r in rows]
        return jsonify({'items': data})
    except Exception as e:
        logging.error(f"sell_history api error: {e}")
        return jsonify({'items': [], 'error': str(e)}), 200

@app.route('/api/purchase_history_export')
def api_purchase_history_export():
    """Export purchase_history as CSV. Optional query: exchange=binance|okx|bitkub|all (default all)."""
    try:
        exch = (request.args.get('exchange') or 'all').strip().lower()
        q = (
            "SELECT purchase_time, COALESCE(exchange,''), usdt_amount, "
            "CASE WHEN COALESCE(exchange,'')='bitkub' THEN 'THB' ELSE 'USDT' END AS quote_asset, "
            "usdt_amount AS quote_amount, btc_quantity, btc_price, order_id, schedule_id, "
            "fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount "
            "FROM purchase_history"
        )
        params = []
        if exch in ('binance','okx','bitkub'):
            q += " WHERE exchange = %s"
            params.append(exch)
        q += " ORDER BY purchase_time DESC"
        with get_db_cursor() as (cursor, _):
            cursor.execute(q, tuple(params))
            rows = cursor.fetchall()

        import io, csv
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['time','exchange','quote_asset','quote_amount','usdt_amount','btc_quantity','btc_price','order_id','schedule_id','fee_buy_usdt','fee_buy_asset','fee_buy_asset_amount'])
        for r in rows:
            writer.writerow([
                str(r[0]) if r[0] else '',
                r[1] or '',
                r[3] or 'USDT',
                float(r[4] or 0.0),
                float(r[2] or 0.0),
                float(r[5] or 0.0),
                float(r[6] or 0.0),
                r[7] or '',
                r[8] or '',
                float(r[9] or 0.0),
                r[10] or '',
                float(r[11] or 0.0),
            ])
        csv_data = output.getvalue().encode('utf-8')
        from flask import Response
        import datetime as _dt
        fname = f"purchase_history_{exch}_{_dt.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.csv"
        return Response(csv_data, mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename={fname}'})
    except Exception as e:
        logging.error(f"purchase_history_export error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/bitkub_strategy_status')
def api_bitkub_strategy_status():
    """Return Bitkub strategy metadata/runtime for dashboard widgets."""
    response = {
        'ok': True,
        'api_configured': bool(os.getenv('BITKUB_API_KEY')) and bool(os.getenv('BITKUB_API_SECRET')),
        'symbol': 'BTC_THB',
        'quote_asset': 'THB',
        'active_schedules': 0,
        'active_total_thb': 0.0,
        'market_filters': {'min_notional': 0.0, 'tick_size': 0.0, 'step_size': 0.0, 'min_qty': 0.0},
        'last_order': None,
        'last_error': None,
        'generated_at': datetime.utcnow().isoformat() + 'Z',
    }
    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute(
                "SELECT COUNT(*), COALESCE(SUM(purchase_amount), 0) FROM schedules WHERE is_active=1 AND exchange_mode='bitkub'"
            )
            row = cursor.fetchone() or (0, 0)
            response['active_schedules'] = int(row[0] or 0)
            response['active_total_thb'] = float(row[1] or 0.0)
            cursor.execute(
                """
                SELECT purchase_time, usdt_amount, btc_quantity, btc_price, order_id,
                       fee_buy_usdt, fee_buy_asset, fee_buy_asset_amount
                FROM purchase_history
                WHERE COALESCE(exchange, '')='bitkub'
                ORDER BY purchase_time DESC
                LIMIT 1
                """
            )
            last_buy = cursor.fetchone()
            if last_buy:
                response['last_order'] = {
                    'time': _dt_to_iso(last_buy[0]),
                    'quote_amount': float(last_buy[1] or 0.0),
                    'quote_asset': 'THB',
                    'filled_btc': float(last_buy[2] or 0.0),
                    'avg_price': float(last_buy[3] or 0.0),
                    'order_id': str(last_buy[4]) if last_buy[4] is not None else None,
                    'fee_asset': last_buy[6] or 'THB',
                    'fee_amount': float(last_buy[7] if last_buy[7] is not None else (last_buy[5] or 0.0)),
                }

        if response['api_configured']:
            try:
                ad = get_adapter('bitkub', testnet=False, dry_run=True)
                filters = ad.get_filters() or {}
                response['market_filters'] = {
                    'min_notional': _safe_float(filters.get('minNotional')),
                    'tick_size': _safe_float(filters.get('tickSize')),
                    'step_size': _safe_float(filters.get('stepSize')),
                    'min_qty': _safe_float(filters.get('minQty')),
                }
            except Exception as exc:
                response['last_error'] = str(exc)
    except Exception as exc:
        logging.error(f"bitkub_strategy_status error: {exc}")
        response['ok'] = False
        response['last_error'] = str(exc)
    return jsonify(response)

@app.route('/api/sell_history_export')
def api_sell_history_export():
    """Export sell_history as CSV. Optional query: exchange=binance|okx|all (default all)."""
    try:
        exch = (request.args.get('exchange') or 'all').strip().lower()
        q = (
            "SELECT sell_time, symbol, COALESCE(exchange,''), btc_quantity, usdt_received, price, order_id, sell_percent, "
            "fee_sell_usdt, fee_sell_asset, fee_sell_asset_amount "
            "FROM sell_history"
        )
        params = []
        if exch in ('binance','okx'):
            q += " WHERE exchange = %s"
            params.append(exch)
        q += " ORDER BY sell_time DESC"
        with get_db_cursor() as (cursor, _):
            cursor.execute(q, tuple(params))
            rows = cursor.fetchall()

        import io, csv
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['time','symbol','exchange','quantity','usdt_received','price','order_id','sell_percent','fee_sell_usdt','fee_sell_asset','fee_sell_asset_amount'])
        for r in rows:
            writer.writerow([
                str(r[0]) if r[0] else '',
                r[1] or '',
                r[2] or '',
                float(r[3] or 0.0),
                float(r[4] or 0.0),
                float(r[5] or 0.0),
                r[6] or '',
                r[7] if r[7] is not None else '',
                float(r[8] or 0.0),
                r[9] or '',
                float(r[10] or 0.0),
            ])
        data = output.getvalue().encode('utf-8')
        from flask import Response
        import datetime as _dt
        fname = f"sell_history_{exch}_{_dt.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.csv"
        return Response(data, mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename={fname}'})
    except Exception as e:
        logging.error(f"sell_history_export error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/binance_trades_export')
def api_binance_trades_export():
    """Export last N (default 100) binance_trades rows to CSV."""
    try:
        limit = int(request.args.get('limit') or 100)
        with get_db_cursor() as (cursor, _):
            cursor.execute(
                """
                SELECT trade_time, is_buyer, price, qty, quote_qty, commission, commission_asset, order_id, trade_id
                FROM binance_trades
                WHERE symbol='BTCUSDT'
                ORDER BY trade_time DESC
                LIMIT %s
                """,
                (limit,)
            )
            rows = cursor.fetchall()
        import io, csv
        out = io.StringIO(); w = csv.writer(out)
        w.writerow(['time','side','price','qty','quote_qty','commission','commission_asset','order_id','trade_id'])
        for r in rows:
            w.writerow([
                str(r[0]), ('BUY' if r[1] else 'SELL'), float(r[2] or 0), float(r[3] or 0), float(r[4] or 0), float(r[5] or 0), r[6] or '', r[7] or '', r[8] or ''
            ])
        data = out.getvalue().encode('utf-8')
        from flask import Response
        import datetime as _dt
        fname = f"binance_trades_{_dt.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.csv"
        return Response(data, mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename={fname}'})
    except Exception as e:
        logging.error(f"binance_trades_export error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/okx_trades_export')
def api_okx_trades_export():
    """Export last N (default 100) okx_trades rows to CSV."""
    try:
        limit = int(request.args.get('limit') or 100)
        with get_db_cursor() as (cursor, _):
            cursor.execute(
                """
                SELECT trade_time, side, price, qty, quote_qty, fee, fee_ccy, ord_id, fill_id
                FROM okx_trades
                ORDER BY trade_time DESC
                LIMIT %s
                """,
                (limit,)
            )
            rows = cursor.fetchall()
        import io, csv
        out = io.StringIO(); w = csv.writer(out)
        w.writerow(['time','side','price','qty','quote_qty','fee','fee_ccy','order_id','fill_id'])
        for r in rows:
            w.writerow([
                str(r[0]), r[1] or '', float(r[2] or 0), float(r[3] or 0), float(r[4] or 0), float(r[5] or 0), r[6] or '', r[7] or '', r[8] or ''
            ])
        data = out.getvalue().encode('utf-8')
        from flask import Response
        import datetime as _dt
        fname = f"okx_trades_{_dt.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}.csv"
        return Response(data, mimetype='text/csv', headers={'Content-Disposition': f'attachment; filename={fname}'})
    except Exception as e:
        logging.error(f"okx_trades_export error: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/use_reserve_now', methods=['POST'])
def api_use_reserve_now():
    """Admin-only: Trigger reserve buy via orchestrator to maintain dedupe semantics."""
    try:
        admin_token = os.getenv('ADMIN_TOKEN')
        data = request.get_json(force=True, silent=True) or {}
        token = data.get('token')
        target_exchange = str(data.get('exchange') or '').lower()
        if not admin_token or token != admin_token:
            return jsonify({'ok': False, 'error': 'forbidden'}), 403

        now = datetime.now(timezone.utc)
        from strategies.base import (
            StrategyAction,
            StrategyActionType,
            StrategyDecision,
            ActionStatus,
            make_request_id,
            dedupe_key_for,
        )
        from main import handle_reserve_buy_action, strategy_orchestrator

        # Build a reserve-buy action, optionally scoped to exchange
        payload = {'mode': 'global'}
        if target_exchange in ('binance', 'okx'):
            payload = {'mode': 'exchange', 'exchange': target_exchange}

        action = StrategyAction(
            action_type=StrategyActionType.RESERVE_BUY,
            request_id=make_request_id('reserve-now'),
            dedupe_key=dedupe_key_for('reserve_now', payload.get('mode'), payload.get('exchange')),
            payload=payload,
        )

        decision = StrategyDecision(issued_at=now, actions=(action,))

        async def _execute():
            handlers = {
                StrategyActionType.RESERVE_BUY: (lambda act: handle_reserve_buy_action(now, act)),
            }
            results = await strategy_orchestrator.execute(decision, handlers)
            return results

        results = asyncio.run(_execute())
        if not results:
            return jsonify({'ok': False, 'error': 'no_action_executed'}), 500
        result = results[0]
        payload = result.data or {}
        if result.status is ActionStatus.SUCCESS:
            return jsonify({'ok': True, 'result': payload})
        return jsonify({
            'ok': False,
            'error': payload.get('payload', {}).get('error', result.detail or 'failed'),
            'result': payload,
        }), 500
    except Exception as e:
        logging.error(f"use_reserve_now error: {e}")
        return jsonify({'ok': False, 'error': str(e)}), 500

# ====== Binance Trades Sync & Analytics ======
@app.route('/api/sync_trades', methods=['POST', 'GET'])
@app.route('/api/sync_trades/', methods=['POST', 'GET'])
def api_sync_trades():
    """Sync BTCUSDT trades from Binance into binance_trades table.
    Incremental by trade_id.
    """
    try:
        if get_client is None:
            return jsonify({'synced': 0, 'message': 'Binance client unavailable'}), 200

        client = get_client()

        # find max trade_id
        with get_db_cursor() as (cursor, db):
            cursor.execute("SELECT COALESCE(MAX(trade_id), 0) FROM binance_trades WHERE symbol = 'BTCUSDT'")
            last_id = int(cursor.fetchone()[0] or 0)

        symbol = 'BTCUSDT'
        limit = 1000
        total_inserted = 0
        next_from_id = last_id + 1 if last_id > 0 else None

        while True:
            params = {'symbol': symbol, 'limit': limit}
            if next_from_id:
                params['fromId'] = next_from_id
            trades = client.get_my_trades(**params)
            if not trades:
                break

            rows = []
            for t in trades:
                rows.append((
                    int(t['id']),
                    symbol,
                    int(t.get('orderId') or 0),
                    float(t['price'] or 0),
                    float(t['qty'] or 0),
                    float(t.get('quoteQty') or (float(t['price'] or 0) * float(t['qty'] or 0))),
                    float(t.get('commission') or 0),
                    str(t.get('commissionAsset') or ''),
                    1 if t.get('isBuyer') else 0,
                    1 if t.get('isMaker') else 0,
                    1 if t.get('isBestMatch') else 0,
                    datetime.fromtimestamp(int(t['time'])/1000.0)
                ))

            with get_db_cursor() as (cursor, db):
                cursor.executemany(
                    """
                    INSERT IGNORE INTO binance_trades
                    (trade_id, symbol, order_id, price, qty, quote_qty, commission, commission_asset,
                     is_buyer, is_maker, is_best_match, trade_time)
                    VALUES
                    (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    rows
                )
                total_inserted += cursor.rowcount
                db.commit()

            # prepare next page
            if len(trades) < limit:
                break
            next_from_id = int(trades[-1]['id']) + 1

        return jsonify({'synced': total_inserted})
    except Exception as e:
        logging.error(f"Sync trades error: {e}")
        return jsonify({'synced': 0, 'error': str(e)}), 500


@app.route('/api/binance_trades')
def api_binance_trades():
    """Return recent trades from binance_trades table (BTCUSDT)."""
    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute(
                """
                SELECT trade_time, is_buyer, price, qty, quote_qty, commission, commission_asset, order_id, trade_id
                FROM binance_trades
                WHERE symbol = 'BTCUSDT'
                ORDER BY trade_time DESC
                LIMIT 100
                """
            )
            rows = cursor.fetchall()

        data = [{
            'trade_time': str(r[0]),
            'side': 'BUY' if r[1] else 'SELL',
            'price': float(r[2] or 0),
            'qty': float(r[3] or 0),
            'quote_qty': float(r[4] or 0),
            'commission': float(r[5] or 0),
            'commission_asset': r[6],
            'order_id': r[7],
            'trade_id': r[8],
        } for r in rows]

        return jsonify({'count': len(data), 'trades': data})
    except Exception as e:
        logging.error(f"Fetch trades error: {e}")
        return jsonify({'count': 0, 'error': str(e)}), 500


@app.route('/api/binance_trades_analytics')
def api_binance_trades_analytics():
    """Compute PnL/avg price from binance_trades using moving-average method."""
    try:
        with get_db_cursor() as (cursor, _):
            cursor.execute(
                """
                SELECT trade_time, is_buyer, price, qty, quote_qty, commission, commission_asset
                FROM binance_trades
                WHERE symbol = 'BTCUSDT'
                ORDER BY trade_time ASC
                """
            )
            rows = cursor.fetchall()

        # Helper: convert non-USDT fee to USDT using trade-time price
        price_cache = {}

        def get_usdt_price_for_asset_at(asset: str, trade_dt, fallback_price_for_btc: float) -> float:
            asset = (asset or '').upper()
            if asset in ('', 'USDT'):
                return 1.0
            if asset == 'BTC':
                # For BTC fee inside BTCUSDT trades, use trade price
                return float(fallback_price_for_btc or 0.0)
            # For other assets like BNB, fetch approximate 1m close at trade time
            key = (asset, int(trade_dt.timestamp() // 60))
            if key in price_cache:
                return price_cache[key]
            symbol = f"{asset}USDT"
            usdt_px = 0.0
            try:
                if get_client is not None:
                    client = get_client()
                    # fetch 1 minute kline covering the trade time
                    start_ms = int((trade_dt.timestamp() - 60) * 1000)
                    end_ms = int((trade_dt.timestamp() + 60) * 1000)
                    kl = client.get_klines(symbol=symbol, interval='1m', startTime=start_ms, endTime=end_ms, limit=1)
                    if kl:
                        # use close price
                        usdt_px = float(kl[0][4])
                if not usdt_px and get_btc_price is not None:
                    # last resort: use current price (less accurate)
                    ticker_client = get_client() if get_client is not None else None
                    if ticker_client:
                        t = ticker_client.get_symbol_ticker(symbol=symbol)
                        usdt_px = float(t['price'])
            except Exception:
                usdt_px = 0.0
            if usdt_px <= 0.0:
                usdt_px = 0.0
            price_cache[key] = usdt_px
            return usdt_px

        position_btc = 0.0
        cost_usdt = 0.0
        total_buy_usdt = 0.0
        total_sell_usdt = 0.0
        realized_pnl = 0.0

        for r in rows:
            trade_time_dt, is_buyer, price, qty, quote_qty, commission, commission_asset = r
            price = float(price or 0.0)
            qty = float(qty or 0.0)
            quote_qty = float(quote_qty or (price * qty))
            commission = float(commission or 0.0)
            commission_asset = (commission_asset or '').upper()

            # Convert fees to components
            fee_usdt = commission if commission_asset == 'USDT' else 0.0
            fee_btc = commission if commission_asset == 'BTC' else 0.0
            fee_other_usdt = 0.0
            if commission > 0.0 and commission_asset not in ('', 'USDT', 'BTC'):
                px = get_usdt_price_for_asset_at(commission_asset, trade_time_dt, price)
                fee_other_usdt = commission * float(px or 0.0)

            if is_buyer:  # BUY
                # Net BTC received after BTC fee
                adj_qty = max(qty - fee_btc, 0.0)
                # Add USDT-equivalent of non-USDT fees (e.g., BNB)
                adj_cost = quote_qty + fee_usdt + fee_other_usdt
                total_buy_usdt += adj_cost
                # update moving average
                new_position = position_btc + adj_qty
                if new_position <= 0:
                    position_btc = 0.0
                    cost_usdt = 0.0
                else:
                    cost_usdt = cost_usdt + adj_cost
                    position_btc = new_position
            else:  # SELL
                # Proceeds after USDT/other-asset fees (BNB converted to USDT)
                proceeds = max(quote_qty - fee_usdt - fee_other_usdt, 0.0)
                total_sell_usdt += proceeds

                if position_btc <= 0:
                    realized_pnl += proceeds
                    continue

                avg_cost = cost_usdt / position_btc if position_btc > 0 else 0.0
                qty_to_close = min(qty, position_btc)
                realized_pnl += proceeds - (avg_cost * qty_to_close)
                cost_usdt -= avg_cost * qty_to_close
                position_btc -= qty_to_close

                # If fee charged in BTC on a sell, deduct extra BTC with zero proceeds
                if fee_btc > 0 and position_btc > 0:
                    extra_close = min(fee_btc, position_btc)
                    realized_pnl += 0.0 - (avg_cost * extra_close)
                    cost_usdt -= avg_cost * extra_close
                    position_btc -= extra_close

        # current price
        try:
            current_price = float(get_btc_price() or 0.0) if get_btc_price is not None else 0.0
        except Exception:
            current_price = 0.0

        portfolio_value = position_btc * current_price
        unrealized_pnl = portfolio_value - cost_usdt
        avg_price = (cost_usdt / position_btc) if position_btc > 0 else 0.0

        payload = {
            'total_buys_usdt': round(total_buy_usdt, 2),
            'total_sells_usdt': round(total_sell_usdt, 2),
            'position_btc': round(position_btc, 8),
            'avg_price': round(avg_price, 2),
            'realized_pnl': round(realized_pnl, 2),
            'unrealized_pnl': round(unrealized_pnl, 2),
            'current_price': round(current_price, 2),
            'portfolio_value': round(portfolio_value, 2),
            'count': len(rows),
        }

        return jsonify(payload)
    except Exception as e:
        logging.error(f"Trades analytics error: {e}")
        return jsonify({'error': str(e)}), 500

# ====== Backfill Trades with Progress ======
from threading import Thread
import uuid

_BACKFILL_TASKS = {}

@app.route('/api/sync_trades_range', methods=['POST'])
def api_sync_trades_range():
    """Start a background backfill of BTCUSDT trades between start and end (UTC).
    Body JSON: { start: ISO8601, end: ISO8601 }
    Returns: { task_id }
    """
    try:
        if get_client is None:
            return jsonify({'error': 'Binance client unavailable'}), 400

        data = request.get_json(force=True, silent=True) or {}
        start_str = data.get('start') or request.args.get('start')
        end_str = data.get('end') or request.args.get('end')
        if not start_str or not end_str:
            return jsonify({'error': 'start and end required'}), 400

        start_dt = datetime.fromisoformat(start_str.replace('Z', '+00:00'))
        end_dt = datetime.fromisoformat(end_str.replace('Z', '+00:00'))
        if end_dt <= start_dt:
            return jsonify({'error': 'end must be after start'}), 400

        task_id = str(uuid.uuid4())
        _BACKFILL_TASKS[task_id] = {
            'status': 'running', 'synced': 0,
            'start': start_dt.isoformat(), 'end': end_dt.isoformat(),
            'progress': 0
        }

        def worker(task_id_local: str):
            try:
                client = get_client()
                symbol = 'BTCUSDT'
                start_ms = int(start_dt.timestamp() * 1000)
                end_ms = int(end_dt.timestamp() * 1000)
                limit = 1000
                from_id = None
                total_inserted = 0
                last_time = start_ms

                while True:
                    params = {'symbol': symbol, 'limit': limit, 'startTime': start_ms, 'endTime': end_ms}
                    if from_id:
                        params['fromId'] = from_id
                    trades = client.get_my_trades(**params)
                    if not trades:
                        break

                    rows = []
                    for t in trades:
                        tr_time = int(t['time'])
                        last_time = max(last_time, tr_time)
                        rows.append((
                            int(t['id']), symbol, int(t.get('orderId') or 0),
                            float(t['price'] or 0), float(t['qty'] or 0),
                            float(t.get('quoteQty') or (float(t['price'] or 0) * float(t['qty'] or 0))),
                            float(t.get('commission') or 0), str(t.get('commissionAsset') or ''),
                            1 if t.get('isBuyer') else 0, 1 if t.get('isMaker') else 0, 1 if t.get('isBestMatch') else 0,
                            datetime.fromtimestamp(tr_time/1000.0)
                        ))

                    with get_db_cursor() as (cursor, db):
                        cursor.executemany(
                            """
                            INSERT IGNORE INTO binance_trades
                            (trade_id, symbol, order_id, price, qty, quote_qty, commission, commission_asset,
                             is_buyer, is_maker, is_best_match, trade_time)
                            VALUES
                            (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            """,
                            rows
                        )
                        total_inserted += cursor.rowcount
                        db.commit()

                    if len(trades) < limit:
                        break
                    from_id = int(trades[-1]['id']) + 1
                    # Update progress by time coverage
                    covered = max(0, last_time - start_ms)
                    total = max(1, end_ms - start_ms)
                    _BACKFILL_TASKS[task_id_local]['progress'] = min(99, int(covered * 100 / total))
                    _BACKFILL_TASKS[task_id_local]['synced'] = total_inserted

                _BACKFILL_TASKS[task_id_local]['synced'] = total_inserted
                _BACKFILL_TASKS[task_id_local]['progress'] = 100
                _BACKFILL_TASKS[task_id_local]['status'] = 'done'
            except Exception as e:
                _BACKFILL_TASKS[task_id_local]['status'] = 'error'
                _BACKFILL_TASKS[task_id_local]['error'] = str(e)

        Thread(target=worker, args=(task_id,), daemon=True).start()
        return jsonify({'task_id': task_id})
    except Exception as e:
        logging.error(f"Backfill start error: {e}")
        return jsonify({'error': str(e)}), 500


@app.route('/api/sync_trades_progress')
def api_sync_trades_progress():
    task_id = request.args.get('task_id')
    if not task_id or task_id not in _BACKFILL_TASKS:
        return jsonify({'error': 'invalid task_id'}), 400
    return jsonify(_BACKFILL_TASKS[task_id])


# ====== Reconcile purchase_history with binance_trades ======
@app.route('/api/reconcile_trades', methods=['POST'])
def api_reconcile_trades():
    """Attach schedule_id to binance_trades by matching purchase_history via order_id/time.
    1) Ensure column schedule_id exists in binance_trades
    2) Match by order_id
    3) Fallback: match by near time (±5m) and similar amount when is_buyer=1
    """
    try:
        updated = 0
        with get_db_cursor() as (cursor, db):
            # 1) Ensure column exists
            cursor.execute("""
                SELECT COUNT(*) FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'binance_trades' AND COLUMN_NAME = 'schedule_id'
            """, (os.getenv('DB_NAME'),))
            if cursor.fetchone()[0] == 0:
                cursor.execute("ALTER TABLE binance_trades ADD COLUMN schedule_id INT NULL, ADD INDEX idx_sched (schedule_id)")
                db.commit()

            # 2) Match by order_id
            cursor.execute(
                """
                UPDATE binance_trades bt
                JOIN purchase_history ph ON ph.order_id = bt.order_id
                SET bt.schedule_id = ph.schedule_id
                WHERE bt.symbol = 'BTCUSDT' AND bt.schedule_id IS NULL AND ph.schedule_id IS NOT NULL
                """
            )
            updated += cursor.rowcount
            db.commit()

            # 3) Fallback time-based for buys only where order_id=0 or mismatch
            cursor.execute(
                """
                SELECT bt.trade_id, bt.trade_time, bt.quote_qty
                FROM binance_trades bt
                WHERE bt.symbol='BTCUSDT' AND bt.is_buyer=1 AND (bt.schedule_id IS NULL)
                ORDER BY bt.trade_time ASC
                LIMIT 2000
                """
            )
            to_check = cursor.fetchall()

            for trade_id, tr_time, q_qty in to_check:
                # find nearest purchase within ±5 minutes and similar amount (±5% or ±1 USDT)
                cursor.execute(
                    """
                    SELECT id, schedule_id FROM purchase_history
                    WHERE purchase_time BETWEEN %s AND %s
                    ORDER BY ABS(TIMESTAMPDIFF(SECOND, purchase_time, %s)) ASC
                    LIMIT 1
                    """,
                    (tr_time - timedelta(minutes=5), tr_time + timedelta(minutes=5), tr_time)
                )
                row = cursor.fetchone()
                if not row:
                    continue
                ph_id, sched_id = row
                if not sched_id:
                    continue
                # Optional: amount match check
                cursor.execute("SELECT usdt_amount FROM purchase_history WHERE id=%s", (ph_id,))
                amt_row = cursor.fetchone()
                if not amt_row:
                    continue
                usdt_amt = float(amt_row[0] or 0.0)
                tol = max(1.0, usdt_amt * 0.05)
                if abs(float(q_qty or 0.0) - usdt_amt) > tol:
                    continue
                cursor.execute("UPDATE binance_trades SET schedule_id=%s WHERE trade_id=%s AND schedule_id IS NULL", (sched_id, trade_id))
                updated += cursor.rowcount
            db.commit()

        return jsonify({'updated': updated})
    except Exception as e:
        logging.error(f"Reconcile error: {e}")
        return jsonify({'updated': 0, 'error': str(e)}), 500

# Final catch-all for unknown API routes to always return JSON instead of HTML
@app.route('/api/<path:subpath>')
def api_not_found(subpath):
    return jsonify({'ok': False, 'error': 'not_found', 'path': f'/api/{subpath}'}), 404

# ====== Application Startup ======
if __name__ == '__main__':
    # Single-instance guard to avoid multiple servers on the same port
    # This prevents flapping between different app versions.
    try:
        import fcntl  # type: ignore
        _lock_fh = open('web.lock', 'w')
        fcntl.flock(_lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _lock_fh.write(str(os.getpid()))
        _lock_fh.flush()
    except Exception:
        # If we cannot acquire the lock, exit silently to avoid duplicate servers
        print('Another instance appears to be running. Exiting.')
        raise SystemExit(0)
    scheduler = None  # Initialize scheduler variable
    
    try:
        logging.info("🚀 Starting BTC DCA Dashboard...")
        
        # ตรวจสอบและ migrate ข้อมูล
        if migrate_data_if_needed():
            logging.info("✅ Data migration check completed")
        else:
            logging.error("❌ Data migration failed")
            exit(1)
            
        # เริ่ม BackgroundScheduler
        scheduler = BackgroundScheduler()
        
        # Status check every 30 seconds
        scheduler.add_job(
            func=check_scheduler_status,
            trigger='interval',
            seconds=30,
            id='scheduler_status_check',
            name='Check Scheduler Status',
            replace_existing=True
        )
        
        # Log cleanup every hour
        scheduler.add_job(
            func=cleanup_old_logs,
            trigger='interval',
            hours=1,
            id='log_cleanup',
            name='Cleanup Old Logs',
            replace_existing=True
        )
        
        # Cache refresh every 5 minutes
        scheduler.add_job(
            func=update_cache_schedules,
            trigger='interval',
            minutes=5,
            id='cache_refresh',
            name='Refresh Cache',
            replace_existing=True
        )
        
        scheduler.start()
        logging.info("📋 Background scheduler started with 3 jobs")

        # แสดงข้อมูลระบบ
        logging.info(f"🏠 Server will run on http://0.0.0.0:5001")
        logging.info(f"📊 Admin panel available at http://0.0.0.0:5001/admin")
        logging.info(f"🔍 Health check at http://0.0.0.0:5001/health")

        # รัน Flask app
        socketio.run(
            app, 
            host='0.0.0.0', 
            port=5001, 
            debug=False,
            use_reloader=False,
            log_output=True
        )
        
    except KeyboardInterrupt:
        logging.info("🛑 Application stopped by user")
    except Exception as e:
        logging.error(f"💥 Failed to start application: {e}")
        raise
    finally:
        if scheduler and scheduler.running:
            scheduler.shutdown()
            logging.info("📋 Background scheduler shutdown")
        logging.info("👋 BTC DCA Dashboard stopped")
