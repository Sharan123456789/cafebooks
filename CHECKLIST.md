# Requirement checklist (spec section -> implementation -> tests)
Tests: `tests/test_core.py` (C), `tests/test_sales_tips.py` (S), `tests/test_reliability.py` (R). GUI: `tools/gui_*.py` (run under a display).

| # | Spec requirement | Implementation | Tests |
|---|---|---|---|
| 1 | First-run workbook creation, re-init never erases | `ExcelManager.initialize` | C: first_run_creates_all_workbooks, reinitialization_never_erases_data |
| 2 | Vendors: create, deactivate, no delete with history, similar names allowed | `AccountingService.add/update/deactivate/delete_vendor` | C: vendor_create_deactivate_and_delete_rules |
| 3 | Duplicate invoice detection (vendor + normalised no.) | `_find_invoice_duplicate`, `DuplicateError` | C: duplicate_invoice_detection_normalised |
| 4 | Invoice total reconciles with components | `add_bill`, validate_dataset | C: invoice_total_must_reconcile |
| 5 | Full / partial payments, multi-bill allocation, spec example (A 5000/B 2000 -> 5000/3000, vendor 8000) | `record_payment`, `_apply_allocations` | C: spec_example_partial_allocation, full_payment_and_status_precedence |
| 6 | Reject overpayment, over-allocation, zero/negative | same | C: rejects_overpayment_and_overallocation |
| 7 | Unallocated payments never settle bills; allocate later | `allocate_payment`, `unallocated_total` | C: unallocated_payment_and_later_allocation |
| 8 | Reversal/correction, append-only ledger, audit | `reverse_payment`, `reverse_allocation` | C: reversal_keeps_history_and_is_audited |
| 9 | Cancel blocked while payments allocated | `cancel_bill` | C: cancel_blocked_while_paid_then_allowed_after_reversal |
| 10 | Overdue status, historical aging by as-of date | `bill_balances`, `ReportingService.vendor_aging*` | C: historical_aging_uses_as_of_not_today, aging_buckets_and_future_bills_excluded |
| 11 | Sales / payment-method reconciliation | `ReconciliationService` | S: sales_reconcile_with_tips_included_in_collections, reconciliation_flags_difference |
| 12 | Tips / service charges kept separate, not revenue by default | `cafe_revenue`, tips ledger | S: tips_and_service_charges_do_not_inflate_revenue, tips_ledger_entries_separate_and_linked |
| 13 | Credit collections not new revenue | `cafe_revenue` | S: credit_collections_are_not_new_revenue |
| 14 | Decimal / paise precision | `utils/currency_utils.money` | C: paise_precision |
| 15 | Excel lock + corruption handling | `_check_writable`, `_commit` rollback, startup validation | R: excel_lock_file_blocks_write..., replace_permission_error_rolls_back..., corrupted_workbook_refuses_start..., missing_workbook..., unexpected_columns_rejected |
| 16 | Import validation + conflicting edits | `SyncManager` | R: export_edit_import_applies_validated_changes, import_conflict_requires_decision, import_rejects_bad_edits, export_refused_when_data_has_errors |
| 17 | Backup restoration, verification, retention | `BackupManager`, `ExcelManager.restore*` | R: backup_verify_restore_and_preview, tampered_backup_is_rejected, backup_retention_prunes_oldest |
| 18 | Interrupted-write recovery | journal + `recover_interrupted_write` | R: interrupted_write_is_rolled_back_on_restart, fully_committed_write_survives_missing_journal_cleanup |
| 19 | Audit-log consistency | `_audit` | R: audit_log_consistency; C: update_bill_whitelist_and_locking |
| 20 | Report totals match ledgers | `ReportingService` | S: reports_match_ledgers, cash_flow_opening_and_closing, supplier_payments_not_counted_as_expenses_twice |
| 21 | Formula injection safe | `excel_io` text cells, import formula scan | C: formula_text_is_stored_as_text; S: report_export_is_formula_safe; R: import_rejects_bad_edits |
| 22 | Retry / duplicate-write protection | `request_id` + audit | C: idempotent_retry_does_not_duplicate |
| 23 | Cache consistent with disk | `ExcelManager` cache | C: cache_matches_disk_after_operations |
| 24 | Integrity checker | `validate_dataset`, `integrity_check` | R: integrity_checker_finds_orphans_and_overallocation |
| 25 | Instance lock | `acquire_instance_lock` | R: instance_lock |
| 26 | v3 data migration | `migrations.py` | R: legacy_v3_data_is_migrated |
| 27 | Performance 10k bills / 50k allocations | cache, grouped sums | R: performance_10k_bills (prints timings) |
| 28 | Duplicate sales date/shift warn, multi-shift allowed | `_sales_duplicate` | S: duplicate_date_shift_warns... |
| 29 | Settings persist | `utils/config.py` | (manual / GUI) |
| 30 | GUI: all screens, reports, forms, background saves | `ui/` | tools/gui_smoke.py, tools/gui_forms.py |

Not implemented (listed honestly): POS file import, bank reconciliation, customer ledger, multi-user, encryption, per-shift cash opening float,
exhaustive Windows GUI testing, PyInstaller build verification.
