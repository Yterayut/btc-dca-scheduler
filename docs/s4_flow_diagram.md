# S4 Flow Diagram (Production Policy)

อัปเดตล่าสุด: 2025-12-18  
Scope: **S4 on OKX spot** (BTC-USDT ↔ XAUT-USDT)  

ไฟล์นี้เป็น decision tree จากการทำงานจริงของ `main.py:run_s4_tick()` หลังเปิด hardening flags

---

## Legend
- **HOLD** = ไม่ flip, ไม่ส่งคำสั่งซื้อขาย (แต่บันทึก runtime/metadata และส่ง alert แบบ throttle ได้)
- **FLIP** = หมุนพอร์ต BTC ↔ XAUT ตาม plan
- **GATE** = เงื่อนไข NO-GO ถ้าไม่ผ่านจะจบที่ HOLD ทันที

---

## High-level loop

```
main.py (scheduler loop)
  └─ every ~300s: run_s4_tick(now)
```

---

## Decision Tree

```
┌────────────────────────────────────────────────────────────┐
│ run_s4_tick(now)                                            │
└────────────────────────────────────────────────────────────┘
            │
            ▼
┌───────────────────────────────┐
│ FEATURE_S4_ENABLED == 1 ?      │
└───────────────────────────────┘
     │Yes                     │No
     ▼                        ▼
  continue                  return
     │
     ▼
┌───────────────────────────────┐
│ strategy_state.s4_multi_leg    │
│ cdc_enabled == 1 ?             │
└───────────────────────────────┘
     │Yes                     │No
     ▼                        ▼
  continue                  return
     │
     ▼
┌───────────────────────────────┐
│ Load OKX adapter + balances    │
│ Compute exposure (BTC/XAUT)    │
└───────────────────────────────┘
     │
     ▼
┌────────────────────────────────────────────────────────────┐
│ GATE: okx_ratio PRIMARY (when S4_HARDENING_ENABLED=1)       │
└────────────────────────────────────────────────────────────┘
     │
     ├─ ratio missing/invalid → HOLD (reason=ratio_missing)
     ├─ updated_at parse fail  → HOLD (reason=ratio_timestamp_invalid)
     ├─ stale > TTL minutes    → HOLD (reason=ratio_stale)
     └─ ok                     → continue (signal_source=okx_ratio)
     │
     ▼
┌───────────────────────────────┐
│ Persist signal snapshot +      │
│ update signal_history (1D)     │
└───────────────────────────────┘
     │
     ▼
┌────────────────────────────────────────────────────────────┐
│ GATE: cooldown (S4_COOLDOWN_DAYS)                            │
└────────────────────────────────────────────────────────────┘
     │
     ├─ now < last_flip + cooldown → HOLD (reason=cooldown_active)
     └─ ok                         → continue
     │
     ▼
┌────────────────────────────────────────────────────────────┐
│ GATE: max flips / 30d (S4_MAX_FLIPS_30D)                     │
│ count = successful cdc_flip (executed_ok=true)               │
└────────────────────────────────────────────────────────────┘
     │
     ├─ count >= max → HOLD + SAFE MODE alert (reason=max_flips_reached)
     └─ ok           → continue
     │
     ▼
┌────────────────────────────────────────────────────────────┐
│ GATE: 2-day confirmation (S4_CONFIRM_DAYS)                   │
│ requires consecutive daily closes + same status              │
└────────────────────────────────────────────────────────────┘
     │
     ├─ not confirmed → HOLD (reason=confirm_pending)
     └─ confirmed     → continue
     │
     ▼
┌───────────────────────────────┐
│ previous_status != curr ?      │
└───────────────────────────────┘
     │Yes                     │No
     ▼                        ▼
  compute plan               NOOP
  (plan_s4_rotation)         (update active_asset)
     │
     ▼
┌───────────────────────────────┐
│ plan exists ?                  │
└───────────────────────────────┘
     │Yes                     │No
     ▼                        ▼
  attempt FLIP               NOOP
     │
     ▼
┌────────────────────────────────────────────────────────────┐
│ Execution mode? (OKX + S4_EXEC_HARDENING_ENABLED)            │
└────────────────────────────────────────────────────────────┘
     │Yes                     │No
     ▼                        ▼
 limit-first path          legacy market path
     │
     ▼
┌────────────────────────────────────────────────────────────┐
│ GATE: spread guard (symbol-aware)                            │
│ - BTC-USDT <= S4_MAX_SPREAD_PCT_BTC                          │
│ - XAUT-USDT <= S4_MAX_SPREAD_PCT_XAUT                        │
└────────────────────────────────────────────────────────────┘
     │
     ├─ fail → HOLD (reason=s4_spread_guard)
     └─ ok   → continue
     │
     ▼
┌────────────────────────────────────────────────────────────┐
│ LIMIT-FIRST FLIP (timeout=S4_LIMIT_FIRST_SECONDS)            │
│ 1) SELL leg: limit sell @ ask                                │
│    - timeout → cancel + return partial fills                 │
│    - if 0 fill → HOLD (reason=s4_sell_unfilled)              │
│ 2) BUY  leg: limit buy @ bid                                 │
│    - timeout → cancel + return partial fills                 │
│    - if 0 fill → HOLD (reason=s4_buy_unfilled)               │
└────────────────────────────────────────────────────────────┘
     │
     ▼
┌────────────────────────────────────────────────────────────┐
│ Optional IOC fallback? (S4_IOC_FALLBACK_ENABLED=1)            │
│ - only if spread still <= threshold                           │
│ - else abort/HOLD                                              │
└────────────────────────────────────────────────────────────┘
     │
     ▼
┌───────────────────────────────┐
│ FLIP executed_ok == true ?     │
└───────────────────────────────┘
     │Yes                     │No
     ▼                        ▼
 record strategy_rotation_log   HOLD/NOOP (with reason)
 (executed_ok=true)             + alert (throttled)
 update last_flip_at (only when executed_ok=true)
     │
     ▼
┌───────────────────────────────┐
│ Notify LINE (rotation)         │
└───────────────────────────────┘
```

---

## Log keywords (for ops)

แนะนำใช้คำสั่ง:

```bash
egrep -n "S4 HOLD|S4 EXEC CHECK|OKX order" scheduler.out | tail -n 220
```

คีย์เวิร์ด:
- `S4 HOLD | reason=confirm_pending|ratio_stale|cooldown_active|max_flips_reached|s4_spread_guard|s4_sell_unfilled|s4_buy_unfilled`
- `S4 EXEC CHECK | ... spread ...`
- `OKX order placed|timeout|canceled|cancel on timeout failed`
