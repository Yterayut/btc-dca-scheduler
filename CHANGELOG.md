# 📝 BTC DCA System - Changelog

---

## [2025-10-02] - Session: Analysis, Improvements & Fixes

### 🔍 **System Analysis Completed**

#### **Documentation Created:**
1. **SYSTEM_LOGIC.md** (23 KB)
   - Complete system flow documentation
   - Main Scheduler Loop explanation
   - CDC Strategy Logic with EMA 12/26
   - DCA Schedule Logic (gate_weekly_dca)
   - Multi-Exchange Logic (Binance + OKX)
   - Reserve Management (2 systems)
   - 3 Complete Flow Diagrams with real examples

2. **IMPROVEMENT_PLAN.md** (7.3 KB)
   - Critical Issues identified (P0)
   - Medium Issues (P1-P2)
   - 4-Phase Development Plan
   - Success Metrics (Before/After)
   - Resources & References

### 🐛 **Issues Found & Analysis**

#### **Critical Issues (P0):**
1. **Security Risks**
   - Hardcoded credentials in .env
   - SQL Injection risks in some queries
   - No secret rotation policy

2. **Error Handling**
   - Quantity format errors (Binance/OKX)
   - LINE API 429 rate limiting (no fallback)
   - Binance API timeout (10s insufficient)

3. **Data Integrity**
   - Reserve management not transactional
   - Race conditions in strategy_state updates
   - No SELECT FOR UPDATE locks

4. **Performance**
   - Log files: app.log (10MB), btc_purchase_log.log (6MB)
   - No log rotation configured
   - N+1 query problems
   - No connection pooling

#### **Code Quality Issues:**
- DRY violations (duplicate purchase logic)
- Magic numbers (EMA_FAST=12, EMA_SLOW=26, LIMIT=300)
- Inconsistent naming (execute_half_sell not actually half)
- No unit tests (0% coverage)

### ✅ **Fixes Applied**

#### **1. OKX Max USDT Configuration**
**Issue:** OKX Reserve Buy only purchased 100 USDT instead of full 11,464 USDT

**Root Cause:**
```python
# exchanges/okx.py
def place_market_buy_quote(self, usdt_amount):
    spend = min(usdt_amount, self.max_usdt)  # Capped at 100 USDT
```

**Fix Applied:**
```sql
UPDATE strategy_state SET okx_max_usdt = 12000 WHERE id=1;
```

**Result:**
- Before: 100 USDT per order
- After: 12,000 USDT per order (can buy full reserve)

**Impact:**
- Next CDC UP transition will buy full OKX reserve in one order
- Reduces number of reserve buy cycles from ~113 to 1

---

#### **2. Manual Reserve Buy Execution**
**Action:** Executed manual reserve buy to clear OKX reserve

**Command:**
```python
from main import execute_reserve_buy_exchange
execute_reserve_buy_exchange(now, 'okx')
execute_reserve_buy_exchange(now, 'binance')
```

**Results:**
- **OKX**: ✅ Bought 11,464.30 USDT → 0.09648817 BTC @ ฿118,815.60
  - Reserve: 11,464.37 → 0.07 USDT
  - Order ID: 2915082130300215296

- **Binance**: ⚠️ Skipped (0.91 USDT < 10 USDT minimum)
  - Reserve: 0.91 USDT (unchanged)

**LINE Notifications:** 2 messages sent successfully

---

#### **3. Web Server Crash & Recovery**

**Issue:** Dashboard inaccessible at http://10.200.0.11:5001
- Error: ERR_CONNECTION_REFUSED
- Port 5001 not listening

**Diagnosis:**
```bash
# Process not found
pgrep -f "python.*app.py"  # No results

# Port not listening
curl http://10.200.0.11:5001/health  # Connection refused

# web.pid had stale PID: 2674208 (process not running)
```

**Root Cause:**
- app.py process killed/crashed during previous code modification attempt
- Failed to restart properly

**Fix Applied:**
```bash
cd /home/oneclimate-dev/yterayut-project/DCA
pkill -9 -f "python.*app.py"
source venv/bin/activate
python app.py > /tmp/web_test.out 2>&1 &
echo $! > web.pid
```

**Verification:**
```bash
# New PID: 2677661
curl http://127.0.0.1:5001/health
# {"status":"healthy","database":"connected","scheduler":"Scheduler is running"}

# Port listening confirmed
ss -tlnp | grep 5001
# LISTEN 0.0.0.0:5001 (python,pid=2677661)
```

**Result:** ✅ Dashboard accessible again

---

### 📊 **Current System Status**

#### **Database State:**
```sql
-- strategy_state
exchange: okx
cdc_enabled: 1
last_cdc_status: up
reserve_usdt: 0.00 (legacy)
reserve_binance_usdt: 0.91
reserve_okx_usdt: 0.07
okx_max_usdt: 12000.00
sell_percent: 55
sell_percent_binance: 100
sell_percent_okx: 55
red_epoch_active: 0
```

#### **Active Schedules:**
- Schedule 17-19: All inactive (is_active=0)

#### **Recent Purchases:**
```
2025-10-02 11:16:57 | okx     | 11464.30 USDT | 0.09648817 BTC | ฿118,815.60
2025-10-02 07:00:45 | okx     |   100.00 USDT | 0.00084236 BTC | ฿118,713.80
2025-10-02 07:00:44 | binance |   229.09 USDT | 0.00193000 BTC | ฿118,700.00
```

#### **Running Processes:**
- **main.py** (Trading Engine): PID unknown, running
- **app.py** (Web Server): PID 2677661, healthy
  - Listening: 0.0.0.0:5001
  - Health: OK
  - Database: Connected
  - Scheduler: Running

---

### 🎯 **Key Learnings**

1. **OKX Max USDT Setting:**
   - Purpose: Risk management (prevent large slippage)
   - Default: 100 USDT per order
   - Current: 12,000 USDT (can buy full reserve)
   - Trade-off: Speed vs. Price impact

2. **Reserve Buy Logic:**
   - Triggered automatically on CDC UP transitions
   - Can be triggered manually via script
   - Respects max_usdt limits per exchange
   - Notifications sent via LINE

3. **Web Server Management:**
   - Process can crash silently
   - Always verify: `curl localhost:5001/health`
   - Proper restart requires venv activation
   - PID file (web.pid) can become stale

4. **Multi-Exchange Architecture:**
   - Per-exchange reserves work well
   - Adapter pattern allows easy extension
   - Different exchanges have different limits

---

### 📚 **Documentation Updates**

**New Files:**
1. `/home/oneclimate-dev/yterayut-project/DCA/SYSTEM_LOGIC.md`
   - Complete system architecture
   - Flow diagrams for all scenarios
   - Code examples and explanations

2. `/home/oneclimate-dev/yterayut-project/DCA/IMPROVEMENT_PLAN.md`
   - Prioritized improvement roadmap
   - 4-phase development plan
   - Success metrics defined

**Updated Files:**
1. `memory.md` - Original project memory (not updated this session)
2. `CHANGELOG.md` - This file (NEW)

---

### 🔄 **Next Steps (Recommended)**

#### **Immediate (P0):**
- [ ] Fix quantity format validation in `exchanges/base.py`
- [ ] Implement database transactions for reserve operations
- [ ] Add log rotation (5MB x 5 files)
- [ ] Increase API timeouts to 30s with exponential backoff

#### **Short-term (P1):**
- [ ] Add Telegram/Email fallback for LINE notifications
- [ ] Implement connection pooling (DBUtils)
- [ ] Add basic unit tests (target 60% coverage)
- [ ] Monitor OKX reserve buy success rate with new limit

#### **Long-term (P2):**
- [ ] Migrate to SQLAlchemy ORM
- [ ] Add Celery for async notifications
- [ ] Implement Prometheus metrics
- [ ] Create comprehensive test suite

---

### 🛡️ **Operational Notes**

**Current Production Settings:**
- DRY_RUN: 0 (LIVE trading)
- STRATEGY_DRY_RUN: 0 (LIVE strategy)
- USE_BINANCE_TESTNET: 0
- OKX_LIVE_ENABLED: 1
- CDC_ENABLED: 1 (Active)

**Risk Management:**
- OKX max per order: 12,000 USDT (monitor for slippage)
- Binance sell %: 100% (aggressive)
- OKX sell %: 55% (moderate)

**Monitoring:**
- Check logs daily: `tail -f btc_purchase_log.log`
- Verify web health: `curl localhost:5001/health`
- Monitor LINE notifications for errors

---

**Session Completed:** 2025-10-02 11:35 GMT+7  
**Analyst:** Claude Code  
**Status:** ✅ System operational, documentation complete, issues logged

