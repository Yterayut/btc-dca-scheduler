## Strategy Contract Schema Migration Plan

### Goals
- บันทึก config สำหรับแต่ละกลยุทธ์ (CDC, S4 ฯลฯ) พร้อมพารามิเตอร์แบบ versioned
- เก็บประวัติการหมุนพอร์ต (rotation) และคำสั่งซื้อขายแบบละเอียด เพื่อรองรับการวิเคราะห์และ compliance
- แยกสโตร์สถานะ positions / orders / fills / balance snapshot ให้สอดคล้องกับ execution จริงบน exchange
- รองรับ caching ของข้อมูลราคา/แท่ง (candles) และการตรวจสอบ latency/ความถูกต้อง

### ตารางใหม่

1. **strategy_config**
   - `id` PK
   - `bot_id` (nullable สำหรับระบบเดี่ยว)
   - `active_strategy` ENUM('cdc_dca_v1','s4_rotation',...) NOT NULL
   - `timeframe` VARCHAR(16) NOT NULL (เช่น `1d`, `4h`)
   - `ema_fast`, `ema_slow` INT
   - `weekly_usd` DECIMAL(18,2)
   - `weekday` TINYINT (0=Mon)
   - `fee_bps`, `slippage_bps` DECIMAL(9,4)
   - `okx_account_id` VARCHAR(64)
   - `min_notional` DECIMAL(18,4)
   - `dust_threshold` DECIMAL(18,8)
  - `cooldown_minutes` INT
  - `config_json` JSON (สำรองค่าพิเศษ)
  - `created_at`, `updated_at`
  - UNIQUE (`bot_id`)

2. **strategy_runtime_state**
   - `id` PK
   - `strategy_key` VARCHAR(64) (เช่น `cdc_dca_v1`)
   - `cursor_json` JSON (state object)
   - `last_request_id` VARCHAR(64)
   - `last_dedupe_key` VARCHAR(128)
   - `updated_at`
   - UNIQUE(`strategy_key`)

3. **rotation_history**
   - `id` PK
   - `occurred_at` DATETIME(6)
   - `from_asset` VARCHAR(16)
   - `to_asset` VARCHAR(16)
   - `side` ENUM('buy','sell','flip')
   - `amount_base` DECIMAL(36,18)
   - `amount_quote` DECIMAL(36,18)
   - `avg_price` DECIMAL(36,18)
   - `maker_taker` ENUM('maker','taker','mixed')
   - `fee` DECIMAL(18,8)
   - `fee_asset` VARCHAR(16)
   - `slippage_bps` DECIMAL(9,4)
   - `latency_ms` INT
   - `reason` ENUM('flip','dca','manual','failsafe')
   - `cdc_state_before` ENUM('up','down','disabled')
   - `cdc_state_after` ENUM('up','down','disabled')
   - `request_id` VARCHAR(64)
   - `dedupe_key` VARCHAR(128)
   - `notes` VARCHAR(255)
   - Indices: `idx_occurred_at`, `idx_request_id`, `idx_dedupe_key`

4. **orders**
   - `id` PK
   - `exchange` VARCHAR(16)
   - `symbol` VARCHAR(32)
   - `side` ENUM('buy','sell')
   - `order_type` ENUM('market','limit','ioc','fok')
   - `status` ENUM('pending','filled','partial','cancelled','failed')
   - `request_id` VARCHAR(64) UNIQUE
   - `dedupe_key` VARCHAR(128)
   - `client_order_id` VARCHAR(64)
   - `exchange_order_id` VARCHAR(64)
   - `price` DECIMAL(36,18)
   - `quantity` DECIMAL(36,18)
   - `quote_amount` DECIMAL(36,18)
   - `fee` DECIMAL(18,8)
   - `fee_asset` VARCHAR(16)
   - `placed_at` DATETIME(6)
   - `updated_at` DATETIME(6)
   - Index: `idx_exchange_symbol`, `idx_status`

5. **fills**
   - `id` PK
   - `order_id` FK → orders.id
   - `fill_id` VARCHAR(64)
   - `price` DECIMAL(36,18)
   - `quantity` DECIMAL(36,18)
   - `quote_amount` DECIMAL(36,18)
   - `fee` DECIMAL(18,8)
   - `fee_asset` VARCHAR(16)
   - `is_maker` TINYINT(1)
   - `filled_at` DATETIME(6)
   - Index: `idx_order_id`, `idx_filled_at`

6. **positions**
   - `id` PK
   - `exchange` VARCHAR(16)
   - `asset` VARCHAR(16)
   - `quantity` DECIMAL(36,18)
   - `average_cost` DECIMAL(36,18)
   - `realized_pnl` DECIMAL(36,18)
   - `unrealized_pnl` DECIMAL(36,18)
   - `last_updated_at` DATETIME(6)
   - UNIQUE(`exchange`,`asset`)

7. **balances_snapshots**
   - `id` PK
   - `exchange` VARCHAR(16)
   - `captured_at` DATETIME(6)
   - `balances_json` JSON
   - `usd_valuation` DECIMAL(36,18)
   - Index: `idx_exchange_time`

8. **candles_cache**
   - `id` PK
   - `exchange` VARCHAR(16)
   - `symbol` VARCHAR(32)
   - `interval` VARCHAR(8)
   - `open_time` DATETIME(6)
   - `close_time` DATETIME(6)
   - `open`, `high`, `low`, `close`, `volume` DECIMAL(36,18)
   - `source_checksum` CHAR(64)
   - `latency_ms` INT
   - `fetched_at` DATETIME(6)
   - UNIQUE(`exchange`,`symbol`,`interval`,`open_time`)

### คอลัมน์เพิ่มเติมต่อของเดิม
- `purchase_history`: เพิ่ม `strategy` ENUM, `request_id`, `dedupe_key`, `fee`, `fee_asset`
- `sell_history`: เพิ่ม `strategy`, `request_id`, `dedupe_key`, `fee`, `fee_asset`, `slippage_bps`
- `reserve_log`: เพิ่ม `request_id`, `dedupe_key`, `metadata` JSON
- `strategy_state`: migration ค่อยๆ ย้ายค่าที่กระจัดกระจายไปยัง `strategy_config` / `strategy_runtime_state`

### ขั้นตอน Migration (สูง)
1. เพิ่มตารางใหม่ทั้งหมดใน transaction-safe migration (MySQL InnoDB).
2. Seed `strategy_config` ด้วยค่าเดิมจาก `strategy_state`.
3. เขียน backfill script ย้าย history (purchase/sell) ให้ใส่ค่า default `strategy='cdc_dca_v1'`.
4. Update แอปให้ใช้ตารางใหม่แบบอ่าน-ก่อน (dual-write ถ้าจำเป็น).
5. เมื่อโค้ดใช้ข้อมูลใหม่ครบ ค่อยล้างคอลัมน์ legacy ที่ไม่ใช้ใน `strategy_state`.
6. จัดทำ rollback script เพื่อลดความเสี่ยง (DROP tables ใหม่พร้อมย้ายค่า config กลับ).

### ความเสี่ยง/ข้อควรระวัง
- ต้องใช้ transaction/lock ระวัง downtime; พิจารณา run ช่วง maintenance.
- ตรวจสอบ version ของ MySQL รองรับ JSON, generated columns.
- ทดสอบบน staging ด้วยข้อมูลสำรองก่อน production.
