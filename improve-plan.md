# 🚀 BTC DCA System - Improvement Plan

**Created:** 2026-02-02
**Status:** Planning Phase
**Target:** Enhance system functionality, analytics, and security

---

## 📋 Table of Contents

1. [Overview](#overview)
2. [Priority 1: Essential Features](#priority-1-essential-features)
3. [Priority 2: Recommended Features](#priority-2-recommended-features)
4. [Priority 3: Nice to Have](#priority-3-nice-to-have)
5. [Security Hardening](#security-hardening)
6. [Implementation Timeline](#implementation-timeline)

---

## 🎯 Overview

### Current System Strengths
- ✅ Multi-exchange support (Binance, OKX)
- ✅ CDC & S4 strategies implemented
- ✅ Real-time web dashboard
- ✅ LINE notification system
- ✅ Liquidity guards (spread, depth, TWAP)
- ✅ Action deduplication
- ✅ Compliance logging

### Key Gaps Identified
- ❌ No performance analytics dashboard
- ❌ Limited risk management tools
- ❌ No email/Telegram notifications
- ❌ Missing tax reporting
- ❌ No backtest engine integration
- ❌ Weak authentication/authorization
- ❌ No automated reporting

---

## 🔥 Priority 1: Essential Features

### 1.1 Analytics & Reporting

#### [ ] Task 1.1.1: Performance Analytics Dashboard
**Importance:** 🔴 Critical
**Estimated Effort:** 2-3 days
**Files to modify:**
- `app.py` - Add new API endpoint
- Create `analytics.py` module
- Add frontend page `/analytics`

**Implementation:**
```python
# /api/portfolio_performance
Endpoint returns:
- total_invested_usdt
- current_btc_value_usdt
- realized_pnl / unrealized_pnl
- total_return_pct
- roi_annualized
- sharpe_ratio
- max_drawdown_pct
- win_rate
- avg_win_size / avg_loss_size
- time_period (from/to/days)

Functions needed:
- calculate_portfolio_metrics()
- calculate_sharpe_ratio()
- calculate_max_drawdown()
- calculate_win_loss_ratio()
```

**Acceptance Criteria:**
- [ ] API endpoint `/api/portfolio_performance` returns complete metrics
- [ ] Frontend dashboard displays charts (equity curve, drawdown)
- [ ] Metrics update in real-time via WebSocket
- [ ] Support date range filtering (7d, 30d, 90d, 1y, all)

---

#### [ ] Task 1.1.2: Monthly Summary Report
**Importance:** 🔴 Critical
**Estimated Effort:** 1 day
**Files to modify:**
- `notify.py` - Add monthly report function
- `main.py` - Schedule monthly job

**Implementation:**
```python
Functions needed:
- generate_monthly_summary(year, month) -> dict
- notify_monthly_report(summary) -> bool
- schedule_monthly_report() # APScheduler job, runs on 1st of month

Report includes:
- Total invested vs current value
- BTC gained
- Number of trades
- CDC status distribution (UP days vs DOWN days)
- Reserve usage count
- Half-sell count
- Best/worst day performance
```

**Acceptance Criteria:**
- [ ] Auto-sends on 1st of each month at 09:00 ICT
- [ ] LINE Flex message format
- [ ] Fallback to plain text if Flex fails
- [ ] Store report history in DB (table: monthly_reports)

---

#### [ ] Task 1.1.3: Tax Report Export
**Importance:** 🟡 High
**Estimated Effort:** 2 days
**Files to modify:**
- `app.py` - Add export endpoint
- Create `tax_report.py` module

**Implementation:**
```python
# /api/tax_report?year=2025&format=csv
Functions needed:
- generate_tax_report(year, format='csv') -> file
- calculate_capital_gains_tax()
- export_tax_csv() / export_tax_pdf()

Report columns:
- Date, Type (Buy/Sell), Symbol, Quantity, Price
- Cost Basis (FIFO), Proceeds, Gain/Loss
- Short-term vs Long-term classification
- Exchange, Fee
```

**Acceptance Criteria:**
- [ ] Export CSV format
- [ ] Export PDF format (optional)
- [ ] FIFO cost basis calculation
- [ ] Separate short-term (<1 year) and long-term gains
- [ ] Summary section with total gains/losses

---

### 1.2 Risk Management

#### [ ] Task 1.2.1: Portfolio Rebalancing Alert
**Importance:** 🔴 Critical
**Estimated Effort:** 1 day
**Files to modify:**
- `main.py` - Add rebalancing check
- `notify.py` - Add alert function

**Implementation:**
```python
def check_allocation_drift():
    """
    Target: CDC 65%, S4 35%
    Alert if drift > 10%
    """
    current = calculate_current_allocation()
    target = {'cdc': 0.65, 's4': 0.35}

    for strategy, target_pct in target.items():
        drift = abs(current[strategy] - target_pct)
        if drift > 0.10:
            notify_rebalancing_needed(strategy, current, target)

Schedule: Check every 6 hours
```

**Acceptance Criteria:**
- [ ] Alert triggers when drift > 10%
- [ ] Notification includes current vs target allocation
- [ ] Suggests rebalancing action
- [ ] Throttled alerts (max 1 per day)

---

#### [ ] Task 1.2.2: Volatility Monitor & Dynamic Position Sizing
**Importance:** 🟡 High
**Estimated Effort:** 1-2 days
**Files to modify:**
- `main.py` - Modify purchase logic
- Create `volatility.py` module

**Implementation:**
```python
def calculate_dynamic_dca_amount(base_amount=100):
    """
    Adjust order size based on 30-day volatility
    High volatility -> reduce size
    Low volatility -> increase size
    """
    btc_volatility = calculate_30d_volatility()

    if btc_volatility > 0.60:  # Very high
        multiplier = 0.5
    elif btc_volatility > 0.45:  # High
        multiplier = 0.75
    elif btc_volatility < 0.30:  # Low
        multiplier = 1.5
    elif btc_volatility < 0.20:  # Very low
        multiplier = 2.0
    else:
        multiplier = 1.0

    return base_amount * multiplier

def calculate_30d_volatility() -> float:
    """Returns annualized volatility (standard deviation of returns)"""
```

**Acceptance Criteria:**
- [ ] Volatility calculated from 30-day price history
- [ ] Order size adjusts automatically
- [ ] Configuration via env vars (VOLATILITY_ADJ_ENABLED, VOL_HIGH_THRESHOLD, etc.)
- [ ] Logs adjustment decisions

---

#### [ ] Task 1.2.3: BTC/Gold Correlation Monitor (S4 Strategy)
**Importance:** 🟡 High
**Estimated Effort:** 1 day
**Files to modify:**
- `strategies/s4_utils.py`
- `notify.py`

**Implementation:**
```python
def calculate_btc_gold_correlation(days=30) -> float:
    """
    Calculate rolling correlation between BTC and XAUT
    Alert if correlation > 0.8 (high correlation = low diversification)
    """
    btc_returns = get_daily_returns('BTCUSDT', days)
    gold_returns = get_daily_returns('XAUTUSDT', days)

    correlation = np.corrcoef(btc_returns, gold_returns)[0, 1]

    if abs(correlation) > 0.8:
        notify_high_correlation({
            'correlation': correlation,
            'period_days': days,
            'warning': 'S4 strategy may not provide diversification'
        })

    return correlation

Schedule: Check daily at 08:00 ICT
```

**Acceptance Criteria:**
- [ ] Daily correlation calculation
- [ ] Alert when |correlation| > 0.8
- [ ] Store correlation history in DB
- [ ] Display on S4 dashboard

---

### 1.3 Notification Improvements

#### [ ] Task 1.3.1: Email Notification Support
**Importance:** 🔴 Critical
**Estimated Effort:** 1 day
**Files to modify:**
- `notify.py` - Implement `send_email_notification()`

**Implementation:**
```python
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

def send_email_notification(subject: str, message: str, email: str = None) -> bool:
    """
    Send email via SMTP (Gmail, SendGrid, AWS SES)
    ENV vars needed:
    - SMTP_SERVER (default: smtp.gmail.com)
    - SMTP_PORT (default: 587)
    - SMTP_USER
    - SMTP_PASSWORD
    - ALERT_EMAIL (default recipient)
    """
    try:
        email = email or os.getenv('ALERT_EMAIL')
        smtp_server = os.getenv('SMTP_SERVER', 'smtp.gmail.com')
        smtp_port = int(os.getenv('SMTP_PORT', '587'))
        smtp_user = os.getenv('SMTP_USER')
        smtp_password = os.getenv('SMTP_PASSWORD')

        msg = MIMEMultipart()
        msg['From'] = smtp_user
        msg['To'] = email
        msg['Subject'] = f"[BTC DCA] {subject}"
        msg.attach(MIMEText(message, 'plain'))

        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_password)
            server.send_message(msg)

        logging.info(f"Email sent to {email}")
        return True
    except Exception as e:
        logging.error(f"Email error: {e}")
        return False
```

**Acceptance Criteria:**
- [ ] Support Gmail SMTP
- [ ] Support SendGrid API (optional)
- [ ] HTML email templates
- [ ] Test endpoint `/test_email_notify`
- [ ] Fallback to LINE if email fails

---

#### [ ] Task 1.3.2: Telegram Bot Support
**Importance:** 🟡 High
**Estimated Effort:** 1 day
**Files to modify:**
- `notify.py` - Add Telegram support

**Implementation:**
```python
import telegram

def send_telegram_notification(message: str, parse_mode='Markdown') -> bool:
    """
    Send via Telegram Bot API
    ENV vars needed:
    - TELEGRAM_BOT_TOKEN
    - TELEGRAM_CHAT_ID
    """
    try:
        bot_token = os.getenv('TELEGRAM_BOT_TOKEN')
        chat_id = os.getenv('TELEGRAM_CHAT_ID')

        bot = telegram.Bot(token=bot_token)
        bot.send_message(
            chat_id=chat_id,
            text=message,
            parse_mode=parse_mode
        )

        logging.info(f"Telegram message sent")
        return True
    except Exception as e:
        logging.error(f"Telegram error: {e}")
        return False
```

**Acceptance Criteria:**
- [ ] Create Telegram bot via @BotFather
- [ ] Support Markdown formatting
- [ ] Support inline buttons (optional)
- [ ] Test endpoint `/test_telegram_notify`

---

#### [ ] Task 1.3.3: Multi-Channel Alert System with Priority Levels
**Importance:** 🟡 High
**Estimated Effort:** 1 day
**Files to modify:**
- `notify.py` - Refactor notification system

**Implementation:**
```python
from enum import Enum

class AlertLevel(Enum):
    INFO = "info"         # LINE only
    WARNING = "warning"   # LINE + Email
    CRITICAL = "critical" # LINE + Email + Telegram

class NotificationChannel(Enum):
    LINE = "line"
    EMAIL = "email"
    TELEGRAM = "telegram"
    SMS = "sms"  # Future

def send_alert(
    message: str,
    level: AlertLevel,
    subject: str = None,
    channels: list[NotificationChannel] = None
):
    """
    Send multi-channel alert based on priority level
    """
    if channels is None:
        # Auto-select channels based on level
        if level == AlertLevel.INFO:
            channels = [NotificationChannel.LINE]
        elif level == AlertLevel.WARNING:
            channels = [NotificationChannel.LINE, NotificationChannel.EMAIL]
        elif level == AlertLevel.CRITICAL:
            channels = [
                NotificationChannel.LINE,
                NotificationChannel.EMAIL,
                NotificationChannel.TELEGRAM
            ]

    results = {}
    for channel in channels:
        if channel == NotificationChannel.LINE:
            results['line'] = send_line_message(message)
        elif channel == NotificationChannel.EMAIL:
            results['email'] = send_email_notification(subject or "Alert", message)
        elif channel == NotificationChannel.TELEGRAM:
            results['telegram'] = send_telegram_notification(message)

    return results

# Example usage:
send_alert(
    "CDC status flipped to DOWN",
    AlertLevel.WARNING,
    subject="CDC Transition Alert"
)
```

**Acceptance Criteria:**
- [ ] Support 3 alert levels
- [ ] Auto-select channels based on level
- [ ] Override channel selection per alert
- [ ] Log all notification attempts (success/failure)
- [ ] Retry logic for failed notifications

---

### 1.4 Operational Tools

#### [ ] Task 1.4.1: Database Health Check & Auto-Maintenance
**Importance:** 🔴 Critical
**Estimated Effort:** 1 day
**Files to modify:**
- Create `db_health.py` module
- `main.py` - Schedule daily health check

**Implementation:**
```python
def check_database_health() -> dict:
    """
    Daily health check (scheduled at 03:00 ICT)
    """
    issues = []
    metrics = {}

    with db_transaction() as (cursor, _):
        # 1. Check table sizes
        cursor.execute("""
            SELECT table_name,
                   ROUND(data_length / 1024 / 1024, 2) as size_mb,
                   table_rows
            FROM information_schema.tables
            WHERE table_schema = %s
            ORDER BY data_length DESC
        """, (os.getenv('DB_NAME'),))

        for table, size_mb, rows in cursor.fetchall():
            metrics[f"{table}_size_mb"] = size_mb
            metrics[f"{table}_rows"] = rows

            if size_mb > 1000:  # > 1GB
                issues.append(f"Large table: {table} ({size_mb}MB)")

        # 2. Check for old data (archive candidates)
        cursor.execute("""
            SELECT COUNT(*) FROM purchase_history
            WHERE purchase_time < DATE_SUB(NOW(), INTERVAL 2 YEAR)
        """)
        old_purchases = cursor.fetchone()[0]
        if old_purchases > 10000:
            issues.append(f"Old purchase records ready for archival: {old_purchases}")

        # 3. Check for orphaned records
        cursor.execute("""
            SELECT COUNT(*) FROM purchase_history
            WHERE schedule_id IS NOT NULL
            AND schedule_id NOT IN (SELECT id FROM schedules)
        """)
        orphaned = cursor.fetchone()[0]
        if orphaned > 0:
            issues.append(f"Orphaned purchase records: {orphaned}")

        # 4. Check index fragmentation (MySQL specific)
        cursor.execute("""
            SELECT table_name, index_name,
                   ROUND(stat_value * @@innodb_page_size / 1024 / 1024, 2) as size_mb
            FROM mysql.innodb_index_stats
            WHERE database_name = %s AND stat_name = 'size'
            ORDER BY stat_value DESC LIMIT 10
        """, (os.getenv('DB_NAME'),))

        # 5. Check slow queries (if enabled)
        # ...

    # Send alert if issues found
    if issues:
        notify_database_health_issues(issues, metrics)

    return {
        'issues': issues,
        'metrics': metrics,
        'checked_at': datetime.now().isoformat()
    }

# Schedule daily at 03:00
# Add to main.py run_loop_scheduler()
```

**Acceptance Criteria:**
- [ ] Runs daily at 03:00 ICT
- [ ] Checks table sizes, row counts
- [ ] Identifies archival candidates
- [ ] Detects orphaned records
- [ ] Sends alert if issues found
- [ ] Stores health check results in DB

---

#### [ ] Task 1.4.2: Configuration Version Control
**Importance:** 🟡 High
**Estimated Effort:** 1 day
**Files to modify:**
- Create `config_history` table
- `app.py` - Log all config changes

**Implementation:**
```sql
CREATE TABLE config_history (
    id INT PRIMARY KEY AUTO_INCREMENT,
    changed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    changed_by VARCHAR(64),
    config_key VARCHAR(128) NOT NULL,
    old_value TEXT,
    new_value TEXT,
    reason VARCHAR(255),
    ip_address VARCHAR(45),
    user_agent TEXT,
    INDEX idx_config_key (config_key),
    INDEX idx_changed_at (changed_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
```

```python
def log_config_change(
    key: str,
    old_value: any,
    new_value: any,
    reason: str = None,
    changed_by: str = None
):
    """Log every config change to audit trail"""
    with db_transaction() as (cursor, _):
        cursor.execute("""
            INSERT INTO config_history
            (config_key, old_value, new_value, reason, changed_by, ip_address, user_agent)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
        """, (
            key,
            json.dumps(old_value) if old_value else None,
            json.dumps(new_value) if new_value else None,
            reason,
            changed_by or 'system',
            request.remote_addr if request else None,
            request.headers.get('User-Agent') if request else None
        ))

# Integrate into existing update endpoints
@app.route('/api/strategy_update', methods=['POST'])
def api_strategy_update():
    # ... existing code ...

    # Log change
    log_config_change(
        key='strategy_state.sell_percent',
        old_value=old_sell_percent,
        new_value=new_sell_percent,
        reason=data.get('reason'),
        changed_by=session.get('username', 'admin')
    )
```

**Acceptance Criteria:**
- [ ] All config changes logged
- [ ] Stores old & new values (JSON)
- [ ] Captures who, when, why, from where
- [ ] Web UI to view config history (`/admin/config_history`)
- [ ] Filter by config_key, date range
- [ ] Rollback functionality (optional)

---

## ⚡ Priority 2: Recommended Features

### 2.1 Advanced Analytics

#### [ ] Task 2.1.1: Strategy Comparison Dashboard
**Importance:** 🟡 High
**Estimated Effort:** 2 days
**Files to modify:**
- Create `comparison.py` module
- Add frontend page `/comparison`

**Implementation:**
```python
# /api/strategy_comparison?period=30d
def compare_strategies(period_days=30):
    """
    Compare CDC vs S4 vs Buy-and-Hold
    """
    end_date = datetime.now()
    start_date = end_date - timedelta(days=period_days)

    # CDC Performance
    cdc_stats = calculate_strategy_performance('cdc_dca_v1', start_date, end_date)

    # S4 Performance
    s4_stats = calculate_strategy_performance('s4_multi_leg', start_date, end_date)

    # Buy-and-Hold Benchmark
    bah_stats = calculate_buy_and_hold_performance(start_date, end_date)

    return {
        'period': f"{period_days}d",
        'start_date': start_date.isoformat(),
        'end_date': end_date.isoformat(),
        'strategies': {
            'cdc_dca': cdc_stats,
            's4_multi_leg': s4_stats,
            'buy_and_hold': bah_stats
        }
    }
```

**Acceptance Criteria:**
- [ ] Compare multiple time periods (7d, 30d, 90d, 1y)
- [ ] Side-by-side comparison table
- [ ] Charts: equity curves, drawdown comparison
- [ ] Risk-adjusted returns (Sharpe, Sortino ratios)

---

#### [ ] Task 2.1.2: Backtest Engine Integration
**Importance:** 🟡 High
**Estimated Effort:** 3-4 days
**Files to modify:**
- Create `backtest/` module
- Enhance existing `scripts/backtest_cdc.py`

**Implementation:**
```python
# scripts/backtest_engine.py
def run_backtest(
    strategy_name: str,
    start_date: str,
    end_date: str,
    initial_capital: float = 10000,
    params: dict = None
) -> dict:
    """
    Backtest any strategy with historical data

    Strategies supported:
    - cdc_dca_v1
    - s4_multi_leg
    - custom (user-defined rules)

    Returns:
        - equity_curve: list of (date, value) tuples
        - total_return: float
        - sharpe_ratio: float
        - max_drawdown: float
        - trade_log: list of trades
        - metrics: dict
    """
    # Load historical data
    historical_data = load_historical_data(start_date, end_date)

    # Initialize strategy
    strategy = get_strategy_instance(strategy_name, params)

    # Run simulation
    portfolio = Portfolio(initial_capital)
    trade_log = []

    for date, data in historical_data.items():
        # Strategy decision
        decision = strategy.decide(date, data, portfolio)

        # Execute trades
        if decision.action == 'buy':
            trade = portfolio.buy(decision.amount, data['price'])
            trade_log.append(trade)
        elif decision.action == 'sell':
            trade = portfolio.sell(decision.quantity, data['price'])
            trade_log.append(trade)

    # Calculate metrics
    metrics = calculate_backtest_metrics(portfolio, trade_log)

    return {
        'equity_curve': portfolio.get_equity_curve(),
        'total_return': metrics['total_return'],
        'sharpe_ratio': metrics['sharpe_ratio'],
        'max_drawdown': metrics['max_drawdown'],
        'trade_log': trade_log,
        'metrics': metrics
    }

# Web UI integration
@app.route('/api/backtest', methods=['POST'])
def api_backtest():
    """Run backtest via API"""
    data = request.get_json()

    # Validate inputs
    if not all(k in data for k in ['strategy', 'start_date', 'end_date']):
        return jsonify({'error': 'missing_required_fields'}), 400

    # Run backtest (async recommended)
    results = run_backtest(
        strategy_name=data['strategy'],
        start_date=data['start_date'],
        end_date=data['end_date'],
        initial_capital=data.get('initial_capital', 10000),
        params=data.get('params', {})
    )

    return jsonify(results)
```

**Acceptance Criteria:**
- [ ] Support CDC & S4 strategies
- [ ] Historical data from Binance API or CSV import
- [ ] Configurable parameters (DCA amount, frequency, etc.)
- [ ] Output: equity curve, metrics, trade log
- [ ] Export results to CSV/JSON
- [ ] Web UI for backtesting (`/backtest`)

---

### 2.2 Trading Enhancements

#### [ ] Task 2.2.1: Trailing Stop Loss
**Importance:** 🟡 High
**Estimated Effort:** 2 days
**Files to modify:**
- `main.py` - Add trailing stop logic
- Create `stop_loss.py` module

**Implementation:**
```python
# Table: trailing_stops
CREATE TABLE trailing_stops (
    id INT PRIMARY KEY AUTO_INCREMENT,
    strategy_mode VARCHAR(32),
    activation_pct DECIMAL(5,2),  # e.g., 20.00 (activate when 20% profit)
    trail_pct DECIMAL(5,2),       # e.g., 10.00 (sell if drops 10% from peak)
    peak_price DECIMAL(18,2),
    last_check_at TIMESTAMP,
    status ENUM('inactive', 'active', 'triggered'),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

def check_trailing_stop(strategy_mode='cdc_dca_v1'):
    """
    Check trailing stop conditions
    Example: If BTC up 20% from entry, activate trail
             If drops 10% from peak, trigger sell
    """
    with db_transaction() as (cursor, _):
        cursor.execute("""
            SELECT * FROM trailing_stops
            WHERE strategy_mode = %s AND status = 'active'
            LIMIT 1
        """, (strategy_mode,))

        stop = cursor.fetchone()
        if not stop:
            return

        current_price = get_btc_price()
        peak_price = stop['peak_price']
        trail_pct = float(stop['trail_pct'])

        # Update peak if new high
        if current_price > peak_price:
            cursor.execute("""
                UPDATE trailing_stops
                SET peak_price = %s, last_check_at = NOW()
                WHERE id = %s
            """, (current_price, stop['id']))
            return

        # Check if dropped below trail threshold
        drop_pct = ((peak_price - current_price) / peak_price) * 100

        if drop_pct >= trail_pct:
            # Trigger stop loss sell
            logging.warning(f"Trailing stop triggered! Drop: {drop_pct:.2f}%")

            # Mark as triggered
            cursor.execute("""
                UPDATE trailing_stops
                SET status = 'triggered', last_check_at = NOW()
                WHERE id = %s
            """, (stop['id'],))

            # Execute sell (partial or full)
            result = execute_trailing_stop_sell(strategy_mode, reason='trailing_stop')

            # Notify
            send_alert(
                f"🚨 Trailing Stop Triggered!\nPrice dropped {drop_pct:.2f}% from peak ${peak_price:.2f}",
                AlertLevel.CRITICAL
            )

            return result

# Schedule check every 5 minutes
```

**Acceptance Criteria:**
- [ ] Configurable activation threshold (% profit)
- [ ] Configurable trail percentage
- [ ] Auto-updates peak price
- [ ] Triggers sell when trail exceeded
- [ ] Notification on trigger
- [ ] Disable/enable via web UI

---

#### [ ] Task 2.2.2: Smart Order Routing (SOR)
**Importance:** 🟢 Medium
**Estimated Effort:** 2 days
**Files to modify:**
- Create `smart_routing.py` module
- `main.py` - Integrate SOR

**Implementation:**
```python
def get_best_execution_price(symbol='BTCUSDT', side='buy'):
    """
    Compare prices across Binance and OKX
    Return exchange with best price (lowest for buy, highest for sell)

    Considers:
    - Price
    - Liquidity (spread, depth)
    - Fees
    - Execution speed
    """
    exchanges = ['binance', 'okx']
    quotes = {}

    for exchange in exchanges:
        try:
            adapter = get_adapter(exchange, testnet=USE_TESTNET, dry_run=is_dry_run())

            # Get top-of-book
            tob = adapter.get_top_of_book()

            # Get fees
            fee_pct = 0.001 if exchange == 'binance' else 0.0008  # Example

            # Calculate effective price including fees
            if side == 'buy':
                effective_price = tob['ask'] * (1 + fee_pct)
            else:
                effective_price = tob['bid'] * (1 - fee_pct)

            quotes[exchange] = {
                'price': tob['ask'] if side == 'buy' else tob['bid'],
                'effective_price': effective_price,
                'spread_pct': ((tob['ask'] - tob['bid']) / tob['bid']) * 100,
                'timestamp': tob['ts']
            }
        except Exception as e:
            logging.warning(f"Failed to get quote from {exchange}: {e}")

    # Select best exchange
    if side == 'buy':
        best = min(quotes.items(), key=lambda x: x[1]['effective_price'])
    else:
        best = max(quotes.items(), key=lambda x: x[1]['effective_price'])

    exchange, quote = best

    logging.info(f"Smart routing: Selected {exchange} for {side} @ ${quote['effective_price']:.2f}")

    return exchange, quote

# Usage in purchase_on_exchange:
def purchase_on_exchange_smart(amount: float, ...):
    """
    Smart version that auto-selects best exchange
    """
    exchange, quote = get_best_execution_price(side='buy')

    logging.info(f"Routing order to {exchange} (savings: ${savings:.2f})")

    return purchase_on_exchange(now, exchange, amount, schedule_id, context)
```

**Acceptance Criteria:**
- [ ] Compares Binance vs OKX prices
- [ ] Considers fees in price calculation
- [ ] Checks liquidity before routing
- [ ] Logs routing decisions
- [ ] Enable/disable via env var (SMART_ROUTING_ENABLED)

---

### 2.3 Security Enhancements

#### [ ] Task 2.3.1: Authentication System (Session-based)
**Importance:** 🔴 Critical
**Estimated Effort:** 3 days
**Files to modify:**
- `app.py` - Add authentication middleware
- Create `auth.py` module
- Add login page

**Implementation:**
```python
from functools import wraps
from flask import session, redirect, url_for, flash
import bcrypt

# Table: users
CREATE TABLE users (
    id INT PRIMARY KEY AUTO_INCREMENT,
    username VARCHAR(64) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    email VARCHAR(255),
    role ENUM('admin', 'viewer') DEFAULT 'viewer',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login_at TIMESTAMP NULL
);

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode(), password_hash.encode())

def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            flash('Please log in to access this page.', 'error')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return jsonify({'error': 'unauthorized'}), 401

        user_role = session.get('role')
        if user_role != 'admin':
            return jsonify({'error': 'forbidden'}), 403

        return f(*args, **kwargs)
    return decorated_function

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')

        with db_transaction() as (cursor, _):
            cursor.execute("""
                SELECT id, username, password_hash, role
                FROM users WHERE username = %s
            """, (username,))
            user = cursor.fetchone()

        if user and verify_password(password, user['password_hash']):
            session['user_id'] = user['id']
            session['username'] = user['username']
            session['role'] = user['role']

            # Update last login
            with db_transaction() as (cursor, _):
                cursor.execute("""
                    UPDATE users SET last_login_at = NOW() WHERE id = %s
                """, (user['id'],))

            flash('Login successful!', 'success')
            return redirect(url_for('index'))
        else:
            flash('Invalid username or password', 'error')

    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out successfully', 'info')
    return redirect(url_for('login'))

# Protect routes
@app.route('/')
@login_required
def index():
    # ... existing code

@app.route('/api/strategy_toggle', methods=['POST'])
@admin_required
def api_strategy_toggle():
    # ... existing code
```

**Acceptance Criteria:**
- [ ] User registration (admin only can create users)
- [ ] Login/logout functionality
- [ ] Session management
- [ ] Password hashing (bcrypt)
- [ ] Role-based access control (admin/viewer)
- [ ] Protect all sensitive routes
- [ ] Login page UI

---

#### [ ] Task 2.3.2: Rate Limiting
**Importance:** 🔴 Critical
**Estimated Effort:** 1 day
**Files to modify:**
- `app.py` - Add rate limiting

**Implementation:**
```python
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

limiter = Limiter(
    app=app,
    key_func=get_remote_address,
    default_limits=["200 per day", "50 per hour"],
    storage_uri="memory://"  # or "redis://localhost:6379"
)

# Apply to specific routes
@app.route('/api/strategy_toggle', methods=['POST'])
@limiter.limit("10 per minute")
@admin_required
def api_strategy_toggle():
    # ... existing code

@app.route('/api/use_reserve_now', methods=['POST'])
@limiter.limit("5 per hour")
@admin_required
def api_use_reserve_now():
    # ... existing code

@app.route('/login', methods=['POST'])
@limiter.limit("5 per minute")
def login():
    # ... existing code

# Custom limits for different user roles
@limiter.request_filter
def ip_whitelist():
    """Whitelist trusted IPs (no rate limit)"""
    whitelist = os.getenv('IP_WHITELIST', '').split(',')
    return request.remote_addr in whitelist
```

**Acceptance Criteria:**
- [ ] Global rate limits (200/day, 50/hour)
- [ ] Endpoint-specific limits
- [ ] IP whitelist support
- [ ] Redis backend for distributed rate limiting
- [ ] Rate limit headers in responses
- [ ] Custom error page for rate limit exceeded

---

#### [ ] Task 2.3.3: 2FA/OTP for Critical Actions
**Importance:** 🟡 High
**Estimated Effort:** 2 days
**Files to modify:**
- `app.py` - Add OTP verification
- Add QR code generation for setup

**Implementation:**
```python
import pyotp
import qrcode
from io import BytesIO
import base64

# Table: user_2fa
CREATE TABLE user_2fa (
    user_id INT PRIMARY KEY,
    secret VARCHAR(32) NOT NULL,
    enabled TINYINT(1) DEFAULT 0,
    backup_codes TEXT,  # JSON array of backup codes
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id)
);

@app.route('/admin/2fa/setup')
@login_required
def setup_2fa():
    """Generate QR code for 2FA setup"""
    user_id = session['user_id']

    # Generate secret
    secret = pyotp.random_base32()

    # Store in DB
    with db_transaction() as (cursor, _):
        cursor.execute("""
            INSERT INTO user_2fa (user_id, secret, enabled)
            VALUES (%s, %s, 0)
            ON DUPLICATE KEY UPDATE secret = %s
        """, (user_id, secret, secret))

    # Generate QR code
    totp = pyotp.TOTP(secret)
    uri = totp.provisioning_uri(
        name=session['username'],
        issuer_name='BTC DCA Bot'
    )

    qr = qrcode.make(uri)
    buffer = BytesIO()
    qr.save(buffer, format='PNG')
    qr_base64 = base64.b64encode(buffer.getvalue()).decode()

    return render_template('2fa_setup.html',
        qr_code=qr_base64,
        secret=secret
    )

@app.route('/admin/2fa/verify', methods=['POST'])
@login_required
def verify_2fa():
    """Verify OTP code and enable 2FA"""
    user_id = session['user_id']
    otp_code = request.form.get('otp')

    # Get secret from DB
    with db_transaction() as (cursor, _):
        cursor.execute("SELECT secret FROM user_2fa WHERE user_id = %s", (user_id,))
        row = cursor.fetchone()

    if not row:
        flash('2FA not set up', 'error')
        return redirect(url_for('setup_2fa'))

    # Verify OTP
    totp = pyotp.TOTP(row['secret'])
    if totp.verify(otp_code):
        # Enable 2FA
        with db_transaction() as (cursor, _):
            cursor.execute("""
                UPDATE user_2fa SET enabled = 1 WHERE user_id = %s
            """, (user_id,))

        session['2fa_verified'] = True
        flash('2FA enabled successfully!', 'success')
        return redirect(url_for('index'))
    else:
        flash('Invalid OTP code', 'error')
        return redirect(url_for('setup_2fa'))

def require_2fa(f):
    """Decorator to require 2FA verification"""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        user_id = session.get('user_id')
        if not user_id:
            return jsonify({'error': 'unauthorized'}), 401

        # Check if user has 2FA enabled
        with db_transaction() as (cursor, _):
            cursor.execute("""
                SELECT enabled FROM user_2fa WHERE user_id = %s
            """, (user_id,))
            row = cursor.fetchone()

        if row and row['enabled']:
            # Require OTP verification
            otp_code = request.json.get('otp') if request.is_json else request.form.get('otp')

            if not otp_code:
                return jsonify({'error': 'otp_required'}), 403

            # Verify OTP
            with db_transaction() as (cursor, _):
                cursor.execute("SELECT secret FROM user_2fa WHERE user_id = %s", (user_id,))
                secret = cursor.fetchone()['secret']

            totp = pyotp.TOTP(secret)
            if not totp.verify(otp_code):
                return jsonify({'error': 'invalid_otp'}), 403

        return f(*args, **kwargs)
    return decorated_function

# Protect critical endpoints
@app.route('/api/strategy_toggle', methods=['POST'])
@admin_required
@require_2fa
def api_strategy_toggle():
    # ... existing code
```

**Acceptance Criteria:**
- [ ] QR code generation for setup
- [ ] OTP verification using TOTP (Google Authenticator compatible)
- [ ] Backup codes generation
- [ ] Protect critical actions (toggle strategy, use reserve, etc.)
- [ ] Optional: SMS-based OTP as fallback

---

## 🎯 Priority 3: Nice to Have

### 3.1 Advanced Features

#### [ ] Task 3.1.1: TWAP/VWAP Order Execution
**Importance:** 🟢 Medium
**Estimated Effort:** 2 days
**Implementation:** Split large orders into smaller chunks over time

---

#### [ ] Task 3.1.2: Conditional Orders
**Importance:** 🟢 Medium
**Estimated Effort:** 3 days
**Implementation:** Buy/sell when specific conditions met (price, RSI, etc.)

---

#### [ ] Task 3.1.3: GraphQL API
**Importance:** 🟢 Low
**Estimated Effort:** 3 days
**Implementation:** Unified API endpoint for mobile apps

---

#### [ ] Task 3.1.4: Public Portfolio Sharing
**Importance:** 🟢 Low
**Estimated Effort:** 2 days
**Implementation:** Share performance (% only, no amounts)

---

#### [ ] Task 3.1.5: Webhook Integration (TradingView, etc.)
**Importance:** 🟢 Medium
**Estimated Effort:** 1-2 days
**Implementation:** Receive external signals

---

## 🔒 Security Hardening Checklist

### Critical (Do First)
- [ ] **Authentication System** (Task 2.3.1)
- [ ] **Rate Limiting** (Task 2.3.2)
- [ ] **Fix SECRET_KEY** - Make it mandatory, no default
- [ ] **Restrict CORS** - No wildcard in production
- [ ] **HTTPS Enforcement** - Redirect HTTP to HTTPS
- [ ] **Security Headers** - CSP, X-Frame-Options, etc.

### High Priority
- [ ] **2FA/OTP** (Task 2.3.3)
- [ ] **Input Validation** - Use marshmallow schemas
- [ ] **SQL Injection Review** - Audit all dynamic queries
- [ ] **API Key Rotation** - Implement key rotation schedule
- [ ] **Audit Logging** - Log all sensitive actions

### Medium Priority
- [ ] **IP Whitelist** - Restrict admin access
- [ ] **Database Connection Pooling** - Prevent exhaustion
- [ ] **Secrets Manager** - Move from .env to vault
- [ ] **Remove .git folder** - From production servers
- [ ] **File Upload Restrictions** - If any upload features exist

---

## 📅 Implementation Timeline

### Phase 1: Foundation (Week 1-2)
**Focus:** Security & Core Analytics

- [ ] Authentication system
- [ ] Rate limiting
- [ ] Performance analytics dashboard
- [ ] Email notifications
- [ ] Database health checks

### Phase 2: Risk Management (Week 3-4)
**Focus:** Risk Controls & Monitoring

- [ ] Portfolio rebalancing alerts
- [ ] Volatility monitor
- [ ] Correlation monitor
- [ ] Monthly summary reports
- [ ] Multi-channel alerts

### Phase 3: Advanced Features (Week 5-6)
**Focus:** Trading Enhancements

- [ ] Trailing stop loss
- [ ] Smart order routing
- [ ] Tax report export
- [ ] Strategy comparison
- [ ] Backtest engine

### Phase 4: Operational Excellence (Week 7-8)
**Focus:** DevOps & Maintenance

- [ ] Configuration version control
- [ ] 2FA implementation
- [ ] Audit log viewer
- [ ] Telegram bot
- [ ] Webhook integrations

---

## 📊 Success Metrics

After completing Priority 1 & 2 tasks:

### Functionality Improvements
- ✅ 95%+ uptime with health monitoring
- ✅ Complete portfolio analytics (Sharpe, drawdown, etc.)
- ✅ Multi-channel notifications (LINE, Email, Telegram)
- ✅ Automated monthly reports
- ✅ Risk alerts for portfolio drift

### Security Improvements
- ✅ 100% of sensitive endpoints protected by auth
- ✅ Rate limiting on all API endpoints
- ✅ 2FA enabled for admin actions
- ✅ All config changes audited
- ✅ No critical vulnerabilities (OWASP Top 10)

### Operational Improvements
- ✅ Daily database health checks
- ✅ Automated backups verified
- ✅ Config change history tracked
- ✅ Alert escalation working
- ✅ Tax reports generated

---

## 🔧 Development Guidelines

### Code Quality
- Write tests for new features (pytest)
- Document all new functions (docstrings)
- Follow PEP 8 style guide
- Use type hints where possible

### Database Changes
- Always create migration scripts
- Test migrations on dev/staging first
- Backup before schema changes
- Add indexes for new queries

### Deployment
- Use feature flags for new features
- Test in DRY_RUN mode first
- Deploy during low-traffic hours
- Monitor logs after deployment

### Documentation
- Update `SYSTEM_LOGIC.md` for logic changes
- Update `memory.md` for new features
- Create API docs for new endpoints
- Update `.env.example` for new env vars

---

## 📝 Notes

### Dependencies to Add
```bash
# Analytics & Reporting
pip install pandas numpy scipy matplotlib

# Notifications
pip install python-telegram-bot

# Security
pip install flask-limiter bcrypt pyotp qrcode[pil]

# Validation
pip install marshmallow

# Database
pip install DBUtils  # Connection pooling

# Backtest
pip install backtrader  # Optional backtest framework
```

### Environment Variables to Add
```bash
# Email
SMTP_SERVER=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your@email.com
SMTP_PASSWORD=your-app-password
ALERT_EMAIL=alerts@yourdomain.com

# Telegram
TELEGRAM_BOT_TOKEN=your-bot-token
TELEGRAM_CHAT_ID=your-chat-id

# Security
OTP_SECRET=your-otp-secret
IP_WHITELIST=192.168.1.100,10.0.0.5
WEBHOOK_SECRET=your-webhook-secret

# Features
SMART_ROUTING_ENABLED=1
VOLATILITY_ADJ_ENABLED=1
TRAILING_STOP_ENABLED=0
```

---

## 🎯 Quick Wins (Can Do in 1 Day Each)

1. **Email Notification** - Implement send_email_notification() (Task 1.3.1)
2. **Telegram Bot** - Add Telegram support (Task 1.3.2)
3. **Database Health Check** - Daily monitoring (Task 1.4.1)
4. **Config Version Control** - Audit trail (Task 1.4.2)
5. **Monthly Summary** - Auto-generated reports (Task 1.1.2)

Start with these quick wins to get immediate value! 🚀

---

**Last Updated:** 2026-02-02
**Next Review:** After Phase 1 completion
