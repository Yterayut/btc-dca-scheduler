# Runtime Marker Checklist

Date: 2026-03-14

## Purpose

รายการนี้ใช้ตรวจว่าระบบมี log markers และ observable events พอสำหรับตาม runtime behavior ของ execution paths สำคัญ

## Priority Levels

- `P0`: ต้องมีเพื่อ debug เงินเข้าออกจริง
- `P1`: ควรมีเพื่ออธิบายเหตุผล skip/block
- `P2`: มีเพื่อช่วย audit และ tuning

## DCA Buy

### Required markers

- `P0` Weekly DCA executed
  - marker ปัจจุบัน:
    - `Weekly DCA notify sent (%s) schedule=%s order=%s amount=%.2f %s`
  - source:
    - [services/trading.py](/home/oneclimate-dev/yterayut-project/DCA/services/trading.py)

- `P1` DCA buy blocked by depth
  - marker ปัจจุบัน:
    - `DCA buy liquidity block (depth) ...`

- `P1` DCA buy blocked by twap
  - marker ปัจจุบัน:
    - `DCA buy liquidity block (twap) ...`

- `P1` DCA buy blocked by notional cap
  - marker ปัจจุบัน:
    - `DCA buy liquidity block (notional_cap) ...`

- `P1` OKX pure DCA bypass markers
  - markers ปัจจุบัน:
    - `Bypassing depth guard for okx_pure_dca ...`
    - `Bypassing twap guard for okx_pure_dca ...`
    - `Bypassing notional cap for okx_pure_dca ...`

## Bitkub Fill Resolution

### Required markers

- `P0` Fill confirmed from `order_info`
  - marker ปัจจุบัน:
    - `Bitkub fill confirmed source=order_info ...`

- `P0` Fill confirmed from `order_history`
  - marker ปัจจุบัน:
    - `Bitkub fill confirmed source=order_history ...`

- `P0` Fill inferred from balance delta
  - marker ปัจจุบัน:
    - `Bitkub fill inferred from balance delta ...`

- `P1` Non-numeric order id normalized to NULL in DB
  - marker ปัจจุบัน:
    - `Non-numeric order_id from %s adapter ...`

- `P1` Bitkub pre-balance snapshot failed
  - marker ปัจจุบัน:
    - `Bitkub pre-balance snapshot failed: ...`

- `P1` Bitkub order-info lookup failed
  - marker ปัจจุบัน:
    - `Bitkub order-info lookup failed (attempt=%s): ...`

- `P1` Bitkub order-history lookup failed
  - marker ปัจจุบัน:
    - `Bitkub order-history lookup failed (attempt=%s): ...`

### Current gap

- ยังไม่มี marker สรุปว่า path จบด้วย source ไหนแบบ normalized event schema เดียวกัน
- ควรมี event payload กลางเช่น `fill_resolution_source=order_info|order_history|balance_delta`

## Half Sell

### Required markers

- `P0` Half-sell execution success
  - ตอนนี้มี notify/compliance path แต่ยังไม่มี success log marker ที่ standardized เท่า DCA buy

- `P0` Half-sell execution error
  - marker ปัจจุบัน:
    - `Half-sell %s error: %s`

- `P1` Half-sell blocked by liquidity/depth/twap/notional
  - ตอนนี้ใช้ `notify_liquidity_blocked('half_sell', payload)` แล้ว
  - ควรเพิ่ม log marker ฝั่ง execution ให้ standardized มากขึ้น

- `P1` Half-sell skipped
  - ตอนนี้มี notification path แต่ marker log ยังไม่ explicit เท่าที่ควร

### Current gap

- ควรเพิ่ม explicit logs สำหรับ:
  - executed
  - skipped reason
  - blocked reason

## Reserve Buy

### Required markers

- `P0` Reserve buy executed
- `P0` Reserve buy exchange executed
- `P1` Reserve buy skipped no reserve
- `P1` Reserve buy blocked by depth/twap/notional/liquidity
- `P1` Reserve buy below min notional
- `P2` Reserve ledger updated after buy

### Current gap

- reserve-buy path ยังอยู่ใน [main.py](/home/oneclimate-dev/yterayut-project/DCA/main.py)
- marker มีอยู่บางส่วนผ่าน `notify_liquidity_blocked('reserve_buy', payload)` แต่ยังไม่มี checklist ที่ standardized เท่า DCA buy
- ควรเติม explicit success/skip markers ตอนย้าย path ไป service ใหม่

## Compliance and Fee Tracking

### Required markers

- `P1` Compliance log skipped for buy
- `P1` Compliance log skipped for half-sell
- `P1` Compliance log skipped for reserve buy
- `P1` Fee totals recorded per strategy/exchange/action

### Current gap

- fee totals ไม่มี dedicated runtime marker ที่ชัดเวลาบันทึกสำเร็จ
- อาจไม่จำเป็นต้อง log ทุกครั้งถ้า noisy แต่ควรมี optional debug marker

## Notification Delivery

### Required markers

- `P1` Flex send failed fallback to text
- `P1` Line push misconfigured
- `P2` Trade email disabled/misconfigured

### Existing examples

- `Flex send failed for liquidity block; falling back to text message`
- `Trade email not sent (disabled/misconfigured) ...`

## Operational Checklist Before/After Refactor

1. targeted tests ของ path ที่แก้ต้องผ่าน
2. full suite ต้องผ่าน
3. marker เดิมที่ถือเป็น `P0` ต้องยังอยู่ หรือมี replacement ที่ชัดกว่า
4. ถ้าเปลี่ยนข้อความ marker สำคัญ ต้องอัปเดตเอกสารนี้
5. ถ้า path ใหม่เกี่ยวกับเงินจริง ต้องเพิ่ม marker ระดับ `P0` อย่างน้อย 1 ตัว

## Immediate Follow-ups

1. เพิ่ม success/skip/block markers สำหรับ reserve-buy
2. เพิ่ม normalized marker สำหรับ Bitkub fill resolution source
3. เพิ่ม standardized half-sell executed/skipped markers
