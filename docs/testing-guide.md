# Testing Guide

Date: 2026-03-14

## Test Environment

ใช้ virtualenv ใน repo:

```bash
./venv/bin/python -m pytest -q
```

หมายเหตุ:

- environment นี้ไม่มี `python` บน PATH เสมอไป ให้ใช้ `./venv/bin/python`
- runtime import-safe ถูกปรับแล้ว แต่ tests หลายชุดยัง patch ผ่าน `main` และ `app`

## Default Safety Check

ก่อน merge refactor ทุกก้อน ให้รัน:

```bash
./venv/bin/python -m pytest -q
```

baseline ล่าสุด:

- `97 passed, 3 warnings`

## Test Matrix

### Runtime core

ใช้เมื่อแก้ execution paths, reserve logic, half-sell, guards, fee totals

```bash
./venv/bin/python -m pytest -q tests/test_purchase_on_exchange.py tests/test_s4_runtime_flow.py tests/test_guards.py tests/test_fee_totals.py
```

### Balance and holdings

ใช้เมื่อแก้ holdings cache หรือ balance snapshots

```bash
./venv/bin/python -m pytest -q tests/test_balance_service.py tests/test_notify_holdings.py
```

### CDC domain logic

ใช้เมื่อแก้ transition decisions หรือ orchestrator behavior

```bash
./venv/bin/python -m pytest -q tests/test_cdc_strategy.py
```

### S4 domain and observability

ใช้เมื่อแก้ S4 status, utilities, runtime data shaping, observability rules

```bash
./venv/bin/python -m pytest -q tests/test_s4_utils.py tests/test_s4_observability.py tests/test_s4_status_data.py tests/test_s4_strategy.py
```

### Web/status surface

ใช้เมื่อแก้ status/dashboard/API data assembly

```bash
./venv/bin/python -m pytest -q tests/test_s4_status_data.py tests/test_notify_holdings.py
```

### Line/Flex notifications

ใช้เมื่อแก้ notification formatting หรือ Flex routing

```bash
./venv/bin/python -m pytest -q tests/test_line_flex.py tests/test_notify_holdings.py
```

## Refactor Protocol

### For service extraction

1. รัน targeted suite ตามก้อนที่แก้
2. รัน full suite
3. ถ้าเปลี่ยน import boundary ให้รัน `py_compile`

```bash
./venv/bin/python -m py_compile main.py app.py services/*.py
```

### For web/API refactor

1. รัน `tests/test_s4_status_data.py`
2. รัน `tests/test_notify_holdings.py`
3. รัน full suite

### For exchange/runtime refactor

1. รัน `tests/test_purchase_on_exchange.py`
2. รัน `tests/test_guards.py`
3. รัน `tests/test_fee_totals.py`
4. รัน full suite

## Known Gaps

1. tests ยัง patch ผ่าน `main` หนักพอสมควร
2. ยังไม่มี dedicated suite สำหรับ reserve-buy matrix ที่ครบ
3. ยังไม่มี isolated tests สำหรับ route modules เพราะ web layer ยังไม่ถูกแยก
4. Bitkub runtime monitoring ยังต้องพึ่ง log evidence จากของจริงร่วมด้วย

## Suggested Additions

1. `tests/test_reserve_buy.py`
2. `tests/test_bitkub_fill_resolution.py`
3. `tests/test_strategy_routes.py`

## Warnings

warnings ปัจจุบันมาจาก dependency `websockets` ฝั่ง Binance client:

- `WebSocketClientProtocol is deprecated`
- `websockets.legacy is deprecated`

ยังไม่ใช่ blocker สำหรับ refactor โครงสร้างรอบนี้ แต่ควรติดไว้ใน technical debt backlog
