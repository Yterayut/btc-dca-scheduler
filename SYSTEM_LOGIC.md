# 🔄 BTC DCA System - Logic การทำงานทั้งระบบ

**อัพเดท**: 2 ตุลาคม 2025  
**Version**: Production v3.0 (Multi-Exchange)

---

## 📋 สารบัญ

1. [ภาพรวมระบบ](#ภาพรวมระบบ)
2. [Main Scheduler Loop](#main-scheduler-loop)
3. [CDC Strategy Logic](#cdc-strategy-logic)
4. [DCA Schedule Logic](#dca-schedule-logic)
5. [Multi-Exchange Logic](#multi-exchange-logic)
6. [Reserve Management](#reserve-management)
7. [Complete Flow Diagram](#complete-flow-diagram)

---

## 🎯 ภาพรวมระบบ

### **2 Process หลัก:**

```
┌─────────────────────────────────────────────────────────┐
│  Process 1: main.py (Trading Engine)                    │
│  - Main Scheduler Loop (ทุก 10 วินาที)                 │
│  - CDC Transition Check (ทุก 60 วินาที)                │
│  - DCA Schedule Execution (ตามเวลาที่ตั้ง)              │
│  - Health Check Server (พอร์ต 8001)                     │
└─────────────────────────────────────────────────────────┘
                          ↕
┌─────────────────────────────────────────────────────────┐
│  Process 2: app.py (Web Dashboard)                      │
│  - Flask Web Server (พอร์ต 5001)                        │
│  - REST API Endpoints                                   │
│  - Real-time WebSocket (SocketIO)                       │
│  - Admin Controls                                       │
└─────────────────────────────────────────────────────────┘
```

---

## 🔁 Main Scheduler Loop

### **ขั้นตอนการทำงาน (ทุก 10 วินาที):**

```python
while True:
    # 1. โหลดเวลาปัจจุบัน
    now = datetime.now(timezone('Asia/Bangkok'))
    current_day = "monday"  # ตัวอย่าง
    current_time = "09:00"
    
    # 2. Refresh Config Cache (ทุก 5 นาที)
    if cache_expired:
        config_cache = load_active_schedules_from_db()
        cache_expiry = now + 5_minutes
    
    # 3. CDC Transition Check (ทุก 60 วินาที)
    if time_since_last_check >= 60:
        check_cdc_transition_and_act(now)
        last_transition_check = now
    
    # 4. Loop ทุก Schedule ที่ Active
    for schedule in config_cache:
        # ตรวจสอบว่าถึงเวลาซื้อหรือยัง
        if matches_schedule(schedule, current_day, current_time):
            # ผ่าน CDC Gate แล้วค่อยซื้อ
            await gate_weekly_dca(now, schedule_id, amount)
    
    # 5. Sleep 10 วินาที
    await asyncio.sleep(10)
```

### **ตัวอย่าง Timeline:**

```
00:00 → Check schedules + CDC check
00:10 → Check schedules
00:20 → Check schedules
...
01:00 → Check schedules + CDC check
01:10 → Check schedules
...
09:00 → ✅ Match schedule → Execute DCA
```

---

## 🎨 CDC Strategy Logic

### **CDC = Crypto Dollar Cost (กลยุทธ์ตามสัญญาณตลาด)**

### **วิธีคำนวณสัญญาณ:**

```python
def get_cdc_status_1d():
    # 1. ดึง 300 candles จาก Binance (1D timeframe)
    klines = binance.get_klines('BTCUSDT', '1d', limit=300)
    
    # 2. ใช้แค่ candle ที่ปิดแล้ว (ไม่รวม current)
    if last_candle_not_closed:
        klines = klines[:-1]
    
    # 3. คำนวณ EMA
    closes = [candle.close for candle in klines]
    xprice = EMA(closes, period=1)
    fast_ema = EMA(xprice, period=12)  # EMA 12
    slow_ema = EMA(xprice, period=26)  # EMA 26
    
    # 4. สร้างสัญญาณ
    bull = [fast > slow]  # เขียว (UP)
    bear = [fast < slow]  # แดง (DOWN)
    
    # 5. ตัดสินใจ
    if current_candle is bull:
        return "up"   # ✅ ตลาดขาขึ้น → ซื้อได้
    else:
        return "down" # ❌ ตลาดขาลง → ไม่ซื้อ
```

### **Flow CDC Transition:**

```
┌─────────────────────────────────────────────────────────┐
│  ทุก 60 วินาที: check_cdc_transition_and_act()          │
└─────────────────────────────────────────────────────────┘
                          ↓
        ┌─────────────────────────────────┐
        │  Load Current CDC Status        │
        │  curr = get_cdc_status_1d()     │
        │  prev = strategy_state.last_cdc │
        └─────────────────────────────────┘
                          ↓
        ┌─────────────────────────────────┐
        │  CDC เปลี่ยนหรือไม่?             │
        │  if prev != curr                │
        └─────────────────────────────────┘
               ↙                    ↘
        ┌──────────┐          ┌──────────┐
        │   ไม่     │          │   ใช่    │
        │ (Skip)    │          │ (Action) │
        └──────────┘          └──────────┘
                                    ↓
                    ┌──────────────────────────┐
                    │  curr = "down" (แดง)?    │
                    └──────────────────────────┘
                         ↙              ↘
            ┌─────────────────┐   ┌─────────────────┐
            │  ใช่ (UP → DOWN) │   │  ไม่ (DOWN → UP)│
            │  🔴 ขายตาม %    │   │  🟢 ซื้อกลับ     │
            └─────────────────┘   └─────────────────┘
                     ↓                      ↓
        execute_half_sell()      execute_reserve_buy()
        - ขาย BTC ตาม %          - ซื้อด้วย Reserve
        - เงินเข้า Reserve       - ลด Reserve
        - แจ้งเตือน LINE         - แจ้งเตือน LINE
```

### **การทำงานจริง:**

#### **📉 เมื่อตลาดเปลี่ยนเป็นสีแดง (UP → DOWN):**

```python
if curr == 'down':
    if red_epoch_active == 0:  # ป้องกันขายซ้ำ
        # 1. ขาย BTC ตาม %
        execute_half_sell(now)
        
        # ตัวอย่าง:
        # Binance: มี 0.01 BTC → ขาย 100% = 0.01 BTC
        # OKX: มี 0.1 BTC → ขาย 55% = 0.055 BTC
        
        # 2. เงินที่ได้เข้า Reserve อัตโนมัติ
        reserve_binance += 1,187 USDT
        reserve_okx += 6,534 USDT
        
        # 3. Mark เป็น red_epoch_active = 1
        strategy_state.red_epoch_active = 1
```

#### **📈 เมื่อตลาดเปลี่ยนเป็นสีเขียว (DOWN → UP):**

```python
elif curr == 'up':
    # 1. ซื้อด้วย Reserve (ถ้ามี)
    execute_reserve_buy_exchange(now, 'binance')
    execute_reserve_buy_exchange(now, 'okx')
    
    # ตัวอย่าง:
    # Binance Reserve: 229 USDT → ซื้อทั้งหมด
    # OKX Reserve: 11,464 USDT → ซื้อ (จำกัด max_usdt)
    
    # 2. Reset red_epoch
    strategy_state.red_epoch_active = 0
```

---

## 📅 DCA Schedule Logic

### **Flow การตรวจสอบ Schedule:**

```
┌─────────────────────────────────────────────────────────┐
│  Main Loop (ทุก 10 วินาที)                              │
└─────────────────────────────────────────────────────────┘
                          ↓
        ┌─────────────────────────────────┐
        │  For each active_schedule:      │
        │  - schedule_time: "09:00"       │
        │  - schedule_day: "monday"       │
        │  - amount: 100 USDT             │
        │  - exchange_mode: "okx"         │
        └─────────────────────────────────┘
                          ↓
        ┌─────────────────────────────────┐
        │  ตรงเวลาและวันหรือไม่?           │
        │  current_day == "monday" AND    │
        │  time_diff <= 15 seconds        │
        └─────────────────────────────────┘
               ↙                    ↘
        ┌──────────┐          ┌──────────┐
        │   ไม่     │          │   ใช่    │
        │ (Skip)    │          │ (Execute)│
        └──────────┘          └──────────┘
                                    ↓
                    ┌──────────────────────────┐
                    │  เคยรันแล้วหรือยัง?       │
                    │  Check last_run_times    │
                    └──────────────────────────┘
                         ↙              ↘
            ┌─────────────────┐   ┌─────────────────┐
            │  รันแล้ว (Skip) │   │  ยังไม่รัน       │
            └─────────────────┘   └─────────────────┘
                                         ↓
                            ┌──────────────────────────┐
                            │  ผ่าน CDC Gate ก่อน     │
                            │  gate_weekly_dca()       │
                            └──────────────────────────┘
```

### **Gate Weekly DCA (ประตูคัดกรอง):**

```python
async def gate_weekly_dca(now, schedule_id, amount, extra):
    mode = extra.get('exchange_mode')  # global/binance/okx/both
    
    # ตรวจสอบ CDC Status
    cdc_enabled = strategy_state.cdc_enabled
    cdc_status = get_cdc_status_1d().status  # "up" or "down"
    
    # ===== DECISION TREE =====
    
    if cdc_enabled == 0:
        # CDC ปิด → ซื้อเลยไม่ต้องดูสัญญาณ
        return purchase_btc(amount)
    
    if cdc_status == "up":
        # ✅ ตลาดเขียว → ซื้อได้
        if mode == "global":
            purchase_btc(amount)
        elif mode == "binance":
            purchase_on_exchange('binance', amount)
        elif mode == "okx":
            purchase_on_exchange('okx', amount)
        elif mode == "both":
            purchase_on_exchange('binance', binance_amount)
            purchase_on_exchange('okx', okx_amount)
    
    elif cdc_status == "down":
        # ❌ ตลาดแดง → ไม่ซื้อ แต่เก็บเงินใน Reserve
        if mode == "global":
            increment_reserve(amount)
        elif mode == "binance":
            increment_reserve_exchange('binance', amount)
        elif mode == "okx":
            increment_reserve_exchange('okx', amount)
        elif mode == "both":
            increment_reserve_exchange('binance', binance_amount)
            increment_reserve_exchange('okx', okx_amount)
        
        # แจ้งเตือนว่าข้ามการซื้อ
        notify_weekly_dca_skipped()
```

---

## 🏦 Multi-Exchange Logic

### **3 Exchange Modes:**

#### **1. Mode: "global" (Legacy)**
```python
# ใช้ Exchange ที่ตั้งไว้ใน strategy_state.exchange
exchange = strategy_state.exchange  # "binance" หรือ "okx"
purchase_btc(amount)  # ซื้อที่ exchange ที่เลือก
```

#### **2. Mode: "binance" หรือ "okx"**
```python
# ระบุ Exchange เฉพาะ
purchase_on_exchange('okx', amount=80)
```

#### **3. Mode: "both"**
```python
# ซื้อทั้ง 2 Exchanges พร้อมกัน
purchase_on_exchange('binance', binance_amount=10)
purchase_on_exchange('okx', okx_amount=80)
```

### **Adapter Pattern:**

```python
def purchase_on_exchange(exchange, amount):
    # 1. Get Adapter
    if exchange == 'binance':
        adapter = BinanceAdapter(testnet, dry_run)
    elif exchange == 'okx':
        adapter = OkxAdapter(testnet, dry_run, max_usdt)
    
    # 2. ตรวจสอบ Balance
    balance = adapter.get_balance('USDT')
    if balance.free < amount:
        raise ValueError("Insufficient balance")
    
    # 3. ส่งคำสั่งซื้อ
    result = adapter.place_market_buy_quote(amount)
    
    # 4. บันทึก DB
    save_to_purchase_history(
        exchange=exchange,
        usdt_amount=result.cummulative_quote_qty,
        btc_quantity=result.executed_qty,
        price=result.avg_price,
        order_id=result.order_id
    )
    
    # 5. แจ้งเตือน LINE
    notify_purchase_executed(result)
```

---

## 💰 Reserve Management

### **2 ระบบ Reserve:**

#### **1. Legacy Reserve (reserve_usdt)**
- ใช้สำหรับ mode: "global"
- ไม่แยก Exchange

#### **2. Per-Exchange Reserve**
- `reserve_binance_usdt`
- `reserve_okx_usdt`
- แยกตาม Exchange

### **Flow การเพิ่ม/ลด Reserve:**

```
┌─────────────────────────────────────────────────────────┐
│  เมื่อไหร่ Reserve จะเพิ่ม?                             │
└─────────────────────────────────────────────────────────┘
                          ↓
    ┌──────────────────────────────────────────┐
    │  1. Schedule DCA แต่ CDC = RED (ไม่ซื้อ) │
    │     → เก็บเงินใน Reserve แทน             │
    └──────────────────────────────────────────┘
                          ↓
    ┌──────────────────────────────────────────┐
    │  2. CDC Transition: UP → DOWN (ขาย BTC) │
    │     → เงินที่ขายได้เข้า Reserve          │
    └──────────────────────────────────────────┘
                          ↓
    ┌──────────────────────────────────────────┐
    │  3. Manual Reserve Transfer (Admin)      │
    │     → Admin โอนเงินเข้า Reserve เอง      │
    └──────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────┐
│  เมื่อไหร่ Reserve จะลด?                                │
└─────────────────────────────────────────────────────────┘
                          ↓
    ┌──────────────────────────────────────────┐
    │  1. CDC Transition: DOWN → UP (ซื้อกลับ)│
    │     → ใช้ Reserve ซื้อ BTC               │
    └──────────────────────────────────────────┘
                          ↓
    ┌──────────────────────────────────────────┐
    │  2. Manual "Use Reserve Now" (Admin)     │
    │     → Admin กดซื้อด้วย Reserve เอง       │
    └──────────────────────────────────────────┘
```

### **ตัวอย่างการทำงานจริง:**

```python
# สถานการณ์: Schedule DCA 100 USDT แต่ CDC = RED

# ก่อน
reserve_okx = 0 USDT

# ระบบทำอะไร?
if cdc_status == "down":
    # ไม่ซื้อ แต่เก็บเงิน
    increment_reserve_exchange('okx', 100)
    notify_weekly_dca_skipped()

# หลัง
reserve_okx = 100 USDT

# ถัดไป CDC เปลี่ยนเป็น UP
execute_reserve_buy_exchange(now, 'okx')
# → ซื้อด้วย 100 USDT
# → reserve_okx = 0
```

---

## 📊 Complete Flow Diagram

### **สถานการณ์ 1: DCA Schedule ตรงเวลา + CDC UP**

```
09:00 Monday
    ↓
Main Loop ตรวจสอบ
    ↓
Schedule Match: ✅
- schedule_time: "09:00"
- schedule_day: "monday"
- amount: 80 USDT
- exchange_mode: "okx"
    ↓
gate_weekly_dca()
    ↓
CDC Status Check
- cdc_enabled: 1
- cdc_status: "up" ✅
    ↓
Decision: ซื้อได้!
    ↓
purchase_on_exchange('okx', 80)
    ↓
Adapter: OkxAdapter
- Check Balance: 500 USDT ✅
- Max USDT Limit: 100 USDT
- Actual Spend: min(80, 100) = 80
    ↓
Place Order: Market Buy
- Symbol: BTC-USDT
- Quote: 80 USDT
    ↓
Order Filled
- Order ID: 2915082130300215296
- Got: 0.00067324 BTC
- Price: ฿118,815.60
- Spent: 80.00 USDT
    ↓
Save to DB
- purchase_history
- exchange: 'okx'
    ↓
LINE Notification ✅
"✅ Weekly DCA Buy (OKX)
Spend: 80.00 USDT
Got: 0.00067324 BTC
Price: ฿118,815.60"
```

### **สถานการณ์ 2: DCA Schedule ตรงเวลา + CDC DOWN**

```
09:00 Monday
    ↓
Main Loop ตรวจสอบ
    ↓
Schedule Match: ✅
- amount: 80 USDT
- exchange_mode: "okx"
    ↓
gate_weekly_dca()
    ↓
CDC Status Check
- cdc_status: "down" ❌
    ↓
Decision: ไม่ซื้อ! เก็บใน Reserve
    ↓
increment_reserve_exchange('okx', 80)
    ↓
UPDATE strategy_state
SET reserve_okx_usdt = reserve_okx_usdt + 80
    ↓
INSERT INTO reserve_log
- change_usdt: +80
- reason: 'weekly_skip_okx'
- note: 'Skipped weekly DCA on OKX due to CDC RED'
    ↓
LINE Notification ⚠️
"⚠️ Weekly DCA Skipped (OKX)
CDC Status: RED
Amount: 80 USDT
Added to Reserve: 80 USDT
Reserve After: 11,544.37 USDT"
```

### **สถานการณ์ 3: CDC Transition (DOWN → UP)**

```
07:00:00
    ↓
check_cdc_transition_and_act()
    ↓
Load Status
- prev: "down"
- curr: get_cdc_status_1d() → "up"
    ↓
Transition Detected! ✅
    ↓
LINE Notification
"🟢 CDC Action Zone Transition (1D)
down → up"
    ↓
Action: ซื้อด้วย Reserve
    ↓
execute_reserve_buy_exchange(now, 'binance')
    ↓
Check Reserve
- reserve_binance: 229.09 USDT ✅
- available_balance: 500 USDT ✅
- spend: min(229.09, 500) = 229.09
    ↓
Place Market Buy
- Order ID: 49404896979
- Got: 0.00193000 BTC
- Spent: 229.09 USDT
    ↓
Update Reserve
- reserve_binance: 229.09 - 229.09 = 0.91 USDT
    ↓
LINE Notification ✅
"✅ Reserve Buy Executed
Exchange: Binance
Spend: 229.09 USDT
Got: 0.00193000 BTC
Price: ฿118,700.00
Reserve Left: 0.91 USDT"
    ↓
execute_reserve_buy_exchange(now, 'okx')
    ↓
Check Reserve
- reserve_okx: 11,464.37 USDT ✅
- max_usdt: 12,000 USDT
- spend: min(11464.37, 12000) = 11,464.37
    ↓
Place Market Buy
- Order ID: 2914566318988599296
- Got: 0.09648817 BTC
- Spent: 11,464.30 USDT
    ↓
Update Reserve
- reserve_okx: 11,464.37 - 11,464.30 = 0.07 USDT
    ↓
LINE Notification ✅
"✅ Reserve Buy Executed
Exchange: OKX
Spend: 11,464.30 USDT
Got: 0.09648817 BTC
Reserve Left: 0.07 USDT"
    ↓
Update State
- red_epoch_active: 0
- last_cdc_status: "up"
```

---

## 🛡️ Guard & Security (v3.1)

- **Depth Guard**: ตรวจสอบ order book ภายใน ±`DEPTH_GUARD_BAND_PCT`% ต้องมี notional ≥ `DEPTH_GUARD_MIN_NOTIONAL_USDT` (ฟังก์ชัน `evaluate_depth_guard` ใน main.py)
- **TWAP Guard**: ราคา spot ต้องไม่เบี่ยงจาก TWAP (1 นาที) เกิน `TWAP_GUARD_MAX_DEVIATION_PCT`
- **Notional Cap**: จำกัด notional ต่อคำสั่งผ่าน `binance_max_usdt` / `okx_max_usdt` (ปรับได้บน Strategy UI)
- **Compliance Audit Log**: บันทึกคำสั่งซื้อ/ขายและสำรองใน `compliance_audit_log` พร้อม metadata เข้ารหัส (`security_utils.py`)
- **Security Alerts**: PnL หรือ notional เกิน threshold → LINE แจ้งเตือนผ่าน `notify_security_alert` และแสดงบน UI Compliance Log
- **UI Integration**: History tab เพิ่ม “Compliance Audit Log” + log filter `Compliance / Security`

---

## 🎯 สรุปหลักการทำงาน

### **Main Scheduler (main.py)**
- ✅ ทำงานทุก 10 วินาที
- ✅ ตรวจสอบ DCA Schedules
- ✅ ตรวจสอบ CDC Transition ทุก 60 วินาที
- ✅ Execute ตามเงื่อนไข

### **CDC Strategy**
- ✅ ใช้ EMA 12/26 บน 1D BTCUSDT
- ✅ UP = ซื้อได้ / DOWN = ไม่ซื้อ
- ✅ Transition ทำให้เกิด Auto Buy/Sell

### **Multi-Exchange**
- ✅ รองรับ Binance + OKX
- ✅ แยก Reserve ต่าง Exchange
- ✅ Adapter Pattern สำหรับ extensibility

### **Reserve System**
- ✅ เก็บเงินเมื่อ CDC RED
- ✅ ซื้อกลับเมื่อ CDC GREEN
- ✅ Admin สามารถจัดการเองได้

---

**🎉 ระบบทำงานอย่างอัตโนมัติและชาญฉลาด!**

