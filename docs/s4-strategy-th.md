# กลยุทธ์ S4

เอกสารนี้สรุป logic `S4` ที่ใช้งานจริงในโปรเจกต์ปัจจุบัน โดยอ้างอิงจาก `main.py:run_s4_tick()` และ helper ที่เกี่ยวข้อง

## 1. ภาพรวม

`S4` คือกลยุทธ์หมุนพอร์ตระหว่าง `BTC` กับ `GOLD/XAUT` ตามสัญญาณ CDC

- ถ้า `cdc_status = up` จะ bias ไปทาง `BTC`
- ถ้า `cdc_status = down` จะ bias ไปทาง `GOLD`
- flow หลักที่รันจริงอยู่ใน `main.py:run_s4_tick()`
- scheduler เรียกทุกประมาณ 300 วินาที

## 2. เงื่อนไขเริ่มทำงาน

ระบบจะไม่ทำอะไรถ้าเงื่อนไขใดเงื่อนไขหนึ่งไม่ผ่าน

- `FEATURE_S4_ENABLED` ต้องเปิด
- strategy `s4_multi_leg` ต้องมี `cdc_enabled = 1`

## 3. การดึงข้อมูลพื้นฐาน

เมื่อเริ่ม tick ระบบจะ:

- โหลด exchange adapter ตาม config
- ดึง balances ของ `USDT`, `BTC`, `XAUT`
- ดึงราคา `BTC` และ `XAUT`
- คำนวณ exposure ปัจจุบันของพอร์ตเป็น USD

ข้อมูล exposure จะถูกเก็บใน runtime metadata เพื่อใช้ตัดสินใจและแสดงผล

## 4. แหล่งสัญญาณหลัก

แหล่งสัญญาณหลักของ S4 คือ `okx_ratio` เมื่อ exchange เป็น `okx`

logic ของ `okx_ratio`:

- ดึง candle `BTC-USDT` และ `XAUT-USDT` จาก OKX
- ตัดแท่งที่ยังไม่ปิดออก
- คำนวณ ratio = `btc_close / gold_close`
- ส่ง ratio series เข้า `cdc_status_from_series(...)`
- ได้ผลลัพธ์เป็น `status`, `updated_at`, `ratio`, `btc_close`, `gold_close`

ถ้า `S4_HARDENING_ENABLED = 1`:

- `okx_ratio` จะเป็น PRIMARY 100%
- ถ้า `okx_ratio` หาย, invalid, parse เวลาไม่ได้, หรือ stale จะ `HOLD`
- จะไม่ fallback ไป `binance_cdc` เพื่อ flip

ถ้า `S4_HARDENING_ENABLED = 0`:

- ถ้า `okx_ratio` ใช้ได้ จะใช้มันก่อน
- ถ้าใช้ไม่ได้ จะ fallback ไป `binance_cdc`

## 5. กติกาเป้าหมายพอร์ต

target allocation ถูกคำนวณจาก config และ `cdc_status`

- ตอน `up` ใช้ `target_btc_pct_up` และ `target_gold_pct_up`
- ตอน `down` ใช้ `target_btc_pct_down` และ `target_gold_pct_down`
- ถ้าไม่ได้ตั้งค่า gold pct ระบบจะใช้ `1 - btc_pct`
- จากนั้น normalize ให้รวมกันเป็น 100%

ความหมายเชิงกลยุทธ์:

- `up` หมายถึงอยากถือ BTC มากขึ้น
- `down` หมายถึงอยากถือ GOLD มากขึ้น

## 6. Hardening Gates

ถ้าเปิด `S4_HARDENING_ENABLED` จะมี NO-GO gates ก่อนคำนวณ flip

### 6.1 Ratio freshness gate

จะ `HOLD` ทันทีถ้า:

- `okx_ratio` ไม่มีค่า
- `updated_at` parse ไม่ได้
- อายุข้อมูลเกิน `S4_RATIO_TTL_MINUTES` โดย default คือ 30 นาที

### 6.2 Signal history

ระบบจะเก็บ `signal_history` แบบรายวันตาม `asof_date`

- de-dupe ต่อวัน
- เก็บไว้ประมาณ 14 รายการล่าสุด
- ใช้สำหรับ confirmation logic

### 6.3 Confirmation gate

ใช้ `S4_CONFIRM_DAYS` โดย default คือ 2 วัน

กติกา:

- ต้องมี history ครบจำนวนวัน
- status ของทุกวันในช่วงนั้นต้องเหมือนกัน
- วันที่ต้องต่อกันจริงวันต่อวัน

ถ้ายังไม่ครบ confirmation จะ `HOLD` ด้วย reason `confirm_pending`

### 6.4 Cooldown gate

ใช้ `S4_COOLDOWN_DAYS` โดย default คือ 3 วัน

ถ้ายังไม่พ้นจาก `last_flip_at` จะ `HOLD` ด้วย reason `cooldown_active`

### 6.5 Max flips circuit breaker

ใช้ `S4_MAX_FLIPS_30D` โดย default คือ 2

- นับเฉพาะ flip ที่ `executed_ok = true`
- ถ้าครบเพดานใน 30 วัน จะเข้า `SAFE MODE` และ `HOLD`

## 7. HOLD behavior

เมื่อเข้า HOLD ระบบจะ:

- บันทึก `last_hold_reason`
- บันทึก `last_hold_detail`
- อัปเดต `last_action_result = HOLD`
- log `S4 HOLD | reason=...`
- ส่ง alert แบบ throttle ตาม `alert_interval_minutes`
- save metadata ทันที

## 8. การตัดสินใจ flip

หลังผ่าน gates แล้ว ระบบจะ:

- คำนวณ target allocation จาก `cdc_status`
- ดู `previous_confirmed` เทียบกับ `cdc_status` ปัจจุบัน
- ถ้าไม่เปลี่ยนสถานะ จะไม่มี rotation plan
- ถ้าเปลี่ยนสถานะ จะสร้าง `rotation_plan`

`rotation_plan` จะคำนวณ:

- total USD ของ BTC + GOLD
- target BTC USD ตามสัดส่วนเป้าหมาย
- ส่วนต่างที่ต้องย้าย
- ควรย้ายจาก asset ไหนไป asset ไหน
- วงเงินหมุนจริงไม่เกิน notional ที่มีอยู่จริง

## 9. โหมดการทำงานจริง

ปัจจุบัน S4 มี 2 โหมดหลัก

### 9.1 DCA-first / Shadow mode

ถ้า `S4_SWAP_EXEC_ENABLED = 0`

- จะไม่ swap จริง
- จะเก็บ `signal_target_asset` ตาม target signal ของรอบนั้น
- จะรักษา `active_asset` เป็น holding จริงถ้ามีอยู่แล้ว
- ถ้ายังไม่มี `active_asset` มาก่อน ระบบจะตั้งค่าเริ่มต้นให้ตรงกับ target signal
- จะบันทึก shadow plan และ heartbeat
- จะ track analytics/runtime mismatch

นี่คือโหมดที่ใช้สำหรับดู decision โดยยังไม่ execute full rotation จริง

### 9.2 Swap execution mode

ถ้า `S4_SWAP_EXEC_ENABLED = 1`

- ระบบจะพยายาม execute rotation จริง
- ถ้าไม่มี `rotation_plan` ก็จะไม่ส่ง order
- ถ้ามี plan จึงเข้า execution path

## 10. Execution logic

เมื่อเปิด execution จริง:

- เลือก `from_asset` และ `to_asset`
- คำนวณจำนวนที่ต้องขาย
- ถ้าเป็น OKX และเปิด `S4_EXEC_HARDENING_ENABLED = 1` จะใช้ hardened path

### 10.1 Spread guard

ก่อน execute จะเช็ค spread ของทั้งขา sell และ buy

- ถ้า spread ไม่ผ่าน threshold จะ `HOLD`
- reason คือ `s4_spread_guard`

### 10.2 Limit-first execution

ใน hardened path:

- ขา sell ใช้ limit sell ที่ ask
- ถ้าขายได้ไม่ครบและเปิด IOC fallback ค่อยใช้ IOC เพิ่ม
- ถ้าขายไม่ได้เลย จะ `HOLD` ด้วย `s4_sell_unfilled`

จากนั้น:

- เอา quote ที่ขายได้จริงไปคำนวณ buy qty
- ขา buy ใช้ limit buy ที่ bid
- ถ้า buy ไม่ได้เลย จะ `HOLD` ด้วย `s4_buy_unfilled`
- ถ้าทั้ง sell และ buy fill ได้จริง ระบบจะตั้ง `executed_ok = true`

### 10.3 Legacy execution

ถ้าไม่ได้ใช้ execution hardening:

- ขา sell ใช้ market sell
- ขา buy ใช้ market buy

## 11. Executed OK gating

ระบบถือว่า `executed_ok` เป็นแหล่งความจริงหลัก

จะอัปเดต `last_flip_at` และ `active_asset` เฉพาะเมื่อ:

- execute จริง
- ไม่ใช่ dry run
- และ `executed_ok = true`

จุดนี้ช่วยกันไม่ให้ cooldown ติดจาก order ที่ partial, unfilled, หรือ abort

## 12. Neutral Zone

Neutral Zone เป็น analytics/log layer ที่คำนวณจาก ratio EMA

ใช้ข้อมูล:

- `ema12`
- `ema26`
- `ema12_history`

metrics หลัก:

- `ema_gap_pct = |ema12 - ema26| / ema26 * 100`
- `slope_pct` จากการเปรียบเทียบ EMA12 ปัจจุบันกับอดีต

state ที่เป็นไปได้:

- `neutral_zone`
- `weak_signal`
- `btc_signal`
- `gold_signal`

default config:

- `ema_gap_low = 0.25`
- `ema_gap_high = 0.40`
- `slope_lookback_days = 3`
- `slope_deadband = 0.03`

runtime จะ log neutral state และเขียน EOD snapshot ลง `s4_neutral_zone_eod`

## 13. Shadow swap gate

ในโหมดที่ยังไม่ execute จริง ระบบจะประเมิน shadow decision เพิ่ม

ถ้าถือ `GOLD` แล้วจะกลับ `BTC`:

- `cdc_status` ต้องเป็น `up`
- ต้องผ่าน confirm ตาม `S4_SHADOW_BTC_CONFIRM_DAYS`
- ถ้า `S4_SHADOW_REQUIRE_NEUTRAL = 1` ต้องมี `neutral_state = btc_signal`
- `slope_pct` ต้องมากกว่าหรือเท่ากับ `S4_SHADOW_BTC_SLOPE_MIN`
- `gap_pct` ต้องไม่เกิน `S4_SHADOW_BTC_GAP_MAX`
- ต้องพ้น cooldown

ถ้าถือ `BTC` แล้วจะไป `GOLD`:

- `cdc_status` ต้องเป็น `down`
- ต้องผ่าน confirm ตาม `S4_SHADOW_XAU_CONFIRM_DAYS`
- `slope_pct` ต้องน้อยกว่าหรือเท่ากับ `S4_SHADOW_XAU_SLOPE_MAX`
- ต้องพ้น cooldown

default shadow config:

- `S4_SHADOW_BTC_CONFIRM_DAYS = 3`
- `S4_SHADOW_XAU_CONFIRM_DAYS = 5`
- `S4_SHADOW_BTC_SLOPE_MIN = 2.0`
- `S4_SHADOW_XAU_SLOPE_MAX = -0.5`
- `S4_SHADOW_BTC_GAP_MAX = 2.0`
- `S4_SHADOW_COOLDOWN_DAYS = 7`
- `S4_SHADOW_REQUIRE_NEUTRAL = true`

## 14. Analytics/Runtime mismatch

ระบบมี observability layer ที่เทียบ:

- `runtime cdc_status`
- กับ `EOD snapshot cdc_status` ในตาราง `s4_neutral_zone_eod`

ถ้าสองฝั่งไม่ตรงกัน:

- `analytics_runtime_mismatch = true`
- เก็บ `mismatch_streak_days`
- คำนวณ `mismatch_severity`
- อาจส่ง security alert

กติกา streak:

- นับตาม `EOD date` วันละครั้ง
- ไม่ได้นับตาม tick 5 นาที

กติกา severity:

- ถ้าไม่ mismatch => `match`
- ถ้า `eod_lag_days > 0`:
  - streak 1 => `info`
  - streak >= 2 => `warn`
- ถ้า `eod_lag_days = 0`:
  - streak >= 5 => `critical`
  - อื่น ๆ => `warn`

กติกา alert:

- `warn` ยิงซ้ำทุก 12 ชั่วโมงเฉพาะกรณี `eod_lag_days = 0`
- `critical` ยิงซ้ำทุก 3 ชั่วโมง
- ถ้า mismatch เกิดจาก `EOD lag` (`eod_lag_days > 0`) ระบบยังเก็บ severity ใน runtime/UI แต่จะไม่ยิง security alert ซ้ำ

ดังนั้น alert แบบ `S4 analytics/runtime mismatch` มักหมายถึง runtime วิ่งไปก่อน แต่ analytics EOD ยัง lag หรือยังไม่ align

## 15. ค่าตั้งต้นสำคัญ

ค่าตั้งต้นจากโค้ดที่มีผลกับ logic:

- `S4_RATIO_TTL_MINUTES = 30`
- `S4_CONFIRM_DAYS = 2`
- `S4_COOLDOWN_DAYS = 3`
- `S4_MAX_FLIPS_30D = 2`
- `S4_SHADOW_SWAP_LOG_ENABLED = true`
- `S4_SHADOW_BTC_CONFIRM_DAYS = 3`
- `S4_SHADOW_XAU_CONFIRM_DAYS = 5`
- `S4_SHADOW_BTC_SLOPE_MIN = 2.0`
- `S4_SHADOW_XAU_SLOPE_MAX = -0.5`
- `S4_SHADOW_BTC_GAP_MAX = 2.0`
- `S4_SHADOW_COOLDOWN_DAYS = 7`
- `S4_SHADOW_REQUIRE_NEUTRAL = true`

## 16. สรุปสั้น

logic S4 ตอนนี้สรุปได้ว่า:

- ใช้ `okx_ratio` เป็น signal หลักเมื่อเปิด hardening
- `up => BTC`, `down => GOLD`
- มี gates สำคัญคือ ratio freshness, confirmation, cooldown, max flips
- ถ้า `S4_SWAP_EXEC_ENABLED = 0` จะยังไม่หมุนจริง แต่จะ log shadow decision และ mismatch
- ถ้า `S4_SWAP_EXEC_ENABLED = 1` จะจึงเริ่ม execution จริง
- Neutral Zone ตอนนี้มีบทบาทหลักด้าน analytics และ shadow gating มากกว่าการบล็อก live flip โดยตรง
