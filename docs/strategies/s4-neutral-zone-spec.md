# S4 Neutral Zone — Spec (Final)
Version: 1.0.0
Last Updated: 2026-01-28
Status: Ready for Implementation

> เป้าหมาย: ลด whipsaw + execution cost ด้วย Neutral Zone
> หลักการ: เริ่ม log-only → ใช้ data จริง → ค่อยเปิด skip flip
> Scope: เฉพาะ S4 rotation decision (ไม่แตะ CDC/weekly DCA/reserve/half-sell)

---

## Phase 0 — Spec & Definition (Lock Rules)

### 1) State Model (enum เดียว)
ใช้ state string เดียว (ไม่ใช้ boolean ซ้ำ)
- `neutral_zone` = ตลาดนิ่ง/แรงสูสีกัน (no edge)
- `weak_signal` = มี bias แต่ยังไม่ strong enough
- `btc_signal` = strong up bias
- `gold_signal` = strong down bias

### 2) Core Variables
- `ema_gap_pct = abs(EMA12 − EMA26) / EMA26 * 100`
- `slope_pct = (EMA12(t) − EMA12(t−N)) / EMA12(t−N) * 100`

### 3) Rule Set (deterministic)
- `neutral_zone` if `ema_gap_pct < low AND abs(slope_pct) <= deadband`
- `btc_signal` if `ema_gap_pct > high AND slope_pct > +deadband`
- `gold_signal` if `ema_gap_pct > high AND slope_pct < −deadband`
- else → `weak_signal`

### 4) CDC‑Confirm Behavior (pause + resume/reset)
- ระหว่าง `state in {neutral_zone, weak_signal}` → pause confirm (ไม่นับเพิ่ม ไม่ลบ)
- เมื่อหลุด neutral/weak:
  - ถ้า CDC signal เหมือนเดิม → resume counting ต่อทันที
  - ถ้า CDC signal เปลี่ยน → reset confirm = 0 แล้วเริ่มนับใหม่
- Flip เกิดได้หลัง confirm ครบเท่านั้น

### 5) S4 DCA Behavior (final)
- ถ้า `state = neutral_zone` → PAUSE DCA
- ถ้า `state = weak_signal` → DCA ตามปกติ
- ถ้า `state = btc_signal | gold_signal` → DCA ตามปกติ

### 6) Execution Order (ล็อกลำดับชัดเจน)
1. ดึง ratio + คำนวณ EMA + state
2. เช็ค cooldown
3. ถ้า state เป็น neutral/weak → HOLD
4. เช็ค CDC‑confirm (resume/reset ตามข้อบน)
5. Execute flip (ถ้าครบ confirm)

### 7) Major Move Definition (EOD‑only)
- CDC flip ต้องเกิดจาก daily closed candle
- วัดจาก EOD close → EOD close 5 วัน
- Movement ≥ 2% ในทิศเดียวกัน

---

## Phase 1 — Neutral Logic (Log‑Only)
**เป้าหมาย:** คำนวณ state + logging โดยไม่เปลี่ยน behavior

### งานหลัก
- คำนวณ `state` ตาม rule set
- เพิ่ม `state` ลง runtime snapshot (read‑only)
- เพิ่ม log ข้อมูลเชิงตัวเลข (gap/slope)

### Acceptance
- ไม่มี skip flip
- log แสดง state เดียวชัดเจน

---

## Phase 1.5 — Production Log Collection (Log‑Only)
**เป้าหมาย:** เก็บ data จริงจาก feed production

### (A) EOD Summary — วันละ 1 แถว
> ใช้ daily closed candle เท่านั้น
```json
{
  "date": "YYYY-MM-DD",
  "ratio_close": 0.00,
  "ema12": 0.00,
  "ema26": 0.00,
  "ema_gap_pct": 0.00,
  "slope_pct": 0.00,
  "state": "neutral_zone|weak_signal|btc_signal|gold_signal",
  "cdc_status": "up|down",
  "active_asset": "BTC|GOLD"
}
```

### (B) State Change Log — event-driven
- log เฉพาะเมื่อ state เปลี่ยน (ไม่สแปม)
```
STATE_CHANGE | ts=... | old=weak_signal | new=btc_signal | gap=... | slope=...
```

### ระยะเวลาแนะนำ
- 30 วัน → เห็น whipsaw ชัด
- 60 วัน → preset เริ่มนิ่ง
- 90 วัน → production‑grade

---

## Phase 0.5 — Data‑Driven Threshold Selection (หลังมี data)
**เป้าหมาย:** เลือก preset จากข้อมูลจริง (ไม่เดา)

### งานหลัก
- วิเคราะห์ย้อนหลัง 30–90 วัน
- วัด:
  - flip reduction %
  - missed major moves / เดือน (ตามนิยามที่ล็อก)
  - % วันอยู่ neutral/weak
- เลือก preset ที่ balance:
  - ลด flip ≥ 20–40%
  - missed major move ≤ 1 ครั้ง/เดือน

---

## Phase 2 — Soft Enable (Skip Flip Only)
**เป้าหมาย:** เปิด neutral/weak เพื่อ pause flip เท่านั้น

### Behavior
- `state=neutral_zone` → HOLD `reason=neutral_zone` + PAUSE DCA
- `state=weak_signal` → HOLD `reason=weak_signal`
- CDC‑confirm pause/resume/reset ตามสเปค
- S4 DCA buys ปกติ ยกเว้น neutral_zone

---

## Phase 3 — UI + Preset Control
**เป้าหมาย:** แสดง preset/params ชัด ๆ และปรับได้จาก `/s4`

### UI Features
- Dropdown: Conservative / Balanced / Aggressive
- แสดง preset ที่ active + param table
- Rule explanation
- แสดง state ปัจจุบัน (neutral/weak/btc/gold)

---

## Phase 4 — Tuning & Interaction Metrics
**เป้าหมาย:** ปรับค่า + ตรวจ interaction

### เพิ่ม metrics/log ที่ควรวัด
- `neutral_streak_days`
- `dca_while_neutral` (ควรเป็น 0 หลัง Phase 2)
- log sequence: `cooldown_active → neutral_zone_after_cooldown`

---

## Phase 5 — Optional Enhancements
- adaptive thresholds ตาม volatility
- overlay chart ใน UI
- A/B testing preset แบบไม่เปลี่ยน execution

---

## Preset Examples (Tentative — จะปรับใน Phase 0.5)

| Preset | gap_low | gap_high | slope_lookback | slope_deadband |
|--------|---------|----------|----------------|----------------|
| Conservative | 0.15% | 0.30% | 3 days | 0.02% |
| Balanced | 0.25% | 0.40% | 3 days | 0.03% |
| Aggressive | 0.35% | 0.50% | 3 days | 0.05% |

> ค่าเหล่านี้เป็นตัวอย่างเบื้องต้น จะปรับตาม backtest ใน Phase 0.5

---

## Example Scenarios

### Scenario A: Resume Counting
Day 1: BTC strong → confirm=1/2  
Day 2: neutral_zone → pause at 1/2  
Day 3: neutral_zone → still at 1/2  
Day 4: BTC strong → resume → confirm=2/2 → FLIP to BTC

### Scenario B: Reset When Signal Changes
Day 1: BTC strong → confirm=1/2  
Day 2: neutral_zone → pause at 1/2  
Day 3: GOLD strong → CDC เปลี่ยน → reset=0  
Day 4: GOLD strong → confirm=1/2  
Day 5: GOLD strong → confirm=2/2 → FLIP to GOLD

### Scenario C: DCA Pause in Neutral
Day 1-5: neutral_zone  
→ DCA paused (ไม่ซื้ออะไร)  
Day 6: btc_signal  
→ DCA resume (ซื้อ BTC)

---

## FAQ

**Q: Neutral zone จะทำให้พลาด big move ไหม?**  
A: ไม่ เพราะ confirm จะ pause ไม่ใช่ reset — พอหลุด neutral ถ้า signal เหมือนเดิมจะ flip ทันที

**Q: ทำไมไม่ reset confirm ทุกครั้งที่หลุด neutral?**  
A: เพราะถ้า reset → neutral กลายเป็น delay mechanism ไม่ใช่ whipsaw filter

**Q: ทำไม weak_signal ไม่ pause DCA?**  
A: เพราะ weak_signal ยังมี directional bias (แค่อ่อน) → DCA ตาม bias ยังมีเหตุผล

**Q: ถ้า neutral 7 วันแล้ว DCA pause ตลอด จะเสียเปรียบไหม?**  
A: ไม่ เพราะ neutral = no edge → ไม่ควรซื้ออยู่แล้ว (ถ้า neutral นาน → ระบบจะ hold cash มากขึ้น)

---

# Guardrails (ต้องคงไว้)
- Neutral/Weak อยู่ “ก่อน plan_s4_rotation”
- weak_signal = pause เท่านั้น (ไม่ใช่ dynamic allocation)
- ไม่กระทบ CDC / weekly DCA / reserve / half‑sell
- CDC‑confirm pause + resume/reset ตามสเปค
- ใช้ daily closed candle ใน EOD summary เสมอ
