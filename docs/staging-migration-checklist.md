# Staging Migration Rehearsal Checklist

## Preparation
- [ ] Provision staging DB snapshot cloned from production (same schema & anonymised data)
- [ ] Ensure application version with strategy orchestrator changes is deployed to staging (feature flagged off for S4)
- [ ] Backup staging database before migration (`mysqldump` with schema + data)
- [ ] Verify maintenance window and alert stakeholders
- [ ] Set Flex pilot env vars on staging (`LINE_USE_FLEX=1`, `LINE_FLEX_ALLOWLIST=weekly_dca`) and restart app worker

## Migration Execution
1. Apply migration script
   ```bash
   mysql -u $USER -p$PASS $DB_NAME < migrations/2025_03_15_strategy_tables.sql
   ```
   - [ ] Confirm all new tables exist (`strategy_config`, `strategy_runtime_state`, `rotation_history`, `orders`, `fills`, `positions`, `balances_snapshots`, `candles_cache`)
   - [ ] Validate new columns on existing tables (`purchase_history`, `sell_history`, `reserve_log`)
2. Seed baseline config
   - [ ] Insert row into `strategy_config` derived from current `strategy_state`
   - [ ] Create `strategy_runtime_state` row with empty cursor/dedupe values
3. Backfill histories (dry run scripts)
   - [ ] Populate `strategy_config`/runtime from legacy values
   - [ ] Update `purchase_history`/`sell_history` to set `strategy='cdc_dca_v1'` and fill request/dedupe keys with generated UUIDs
   - [ ] Log summary of affected rows for audit
4. Smoke-test orchestrator writes
   - [ ] Run scheduler in dry-run mode to trigger weekly DCA + transitions
   - [ ] Confirm new request_id/dedupe_key columns populate via application code
5. Verify analytics/API
   - [ ] Hit `/api/strategy_state`, `/api/wallet`, `/api/reserve_log` and ensure new fields do not break responses
   - [ ] Execute `/api/use_reserve_now` (dry-run) and confirm dedupe prevents duplicate entries when retried
6. Observe metrics/logs for 30 minutes; check for SQL errors or warnings

## Validation
- [ ] Compare row counts pre/post migration for key tables
- [ ] Run unit/integration test suite against staging environment
- [ ] Verify LINE notifications still fire (use test token)

## Rollback Plan
- If failures detected:
  1. Restore staging DB backup taken before migration
  2. Re-deploy previous application version
  3. Document root cause and fixes before next rehearsal

## Sign-off
- [ ] Capture migration report (commands, timings, validation results)
- [ ] Update To-Do Masterlist / documentation with lessons learned
