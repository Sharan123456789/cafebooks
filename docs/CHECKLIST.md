# Requirement checklist (requirement -> implementation -> tests)
Tests: `tests/test_core.py` (C), `tests/test_sales_tips.py` (S), `tests/test_reliability.py` (R), `tests/test_web_api.py` (W).
Every storage-backed test runs on SQLite and on PostgreSQL.

| # | Requirement | Implementation | Tests |
|---|---|---|---|
| 1 | First run creates all tables; restarting never erases data | `SqlStore.initialize` | C: first_run_creates_all_tables, reinitialization_never_erases_data |
| 2 | Vendors: create, deactivate, no delete with history, similar names allowed | `AccountingService.add/update/deactivate/delete_vendor` | C: vendor_create_deactivate_and_delete_rules |
| 3 | Duplicate invoice detection (vendor + normalised no.) | `_find_invoice_duplicate`, `DuplicateError` | C: duplicate_invoice_detection_normalised; W: full_day_of_bookkeeping |
| 4 | Invoice total reconciles with components | `add_bill`, validate_dataset | C: invoice_total_must_reconcile |
| 5 | Full / partial payments, multi-bill allocation | `record_payment`, `_apply_allocations` | C: spec_example_partial_allocation, full_payment_and_status_precedence |
| 6 | Reject overpayment, over-allocation, zero/negative | same | C: rejects_overpayment_and_overallocation |
| 7 | Unallocated payments never settle bills; allocate later | `allocate_payment`, `unallocated_total` | C: unallocated_payment_and_later_allocation |
| 8 | Reversal/correction, append-only ledger, audit | `reverse_payment`, `reverse_allocation` | C: reversal_keeps_history_and_is_audited |
| 9 | Cancel blocked while payments allocated | `cancel_bill` | C: cancel_blocked_while_paid_then_allowed_after_reversal |
| 10 | Overdue status, historical aging by as-of date | `bill_balances`, `ReportingService.vendor_aging*` | C: historical_aging_uses_as_of_not_today, aging_buckets_and_future_bills_excluded |
| 11 | Sales / payment-method reconciliation | `ReconciliationService` | S: sales_reconcile_with_tips_included_in_collections, reconciliation_flags_difference |
| 12 | Tips / service charges kept separate, not revenue by default | `cafe_revenue`, tips ledger | S: tips_and_service_charges_do_not_inflate_revenue, tips_ledger_entries_separate_and_linked |
| 13 | Credit collections not new revenue | `cafe_revenue` | S: credit_collections_are_not_new_revenue |
| 14 | Decimal / paise precision | `utils/currency_utils.money` | C: paise_precision |
| 15 | All-or-nothing saves | `SqlStore.transaction` (one DB transaction) | R: failed_operation_writes_nothing, database_error_mid_commit_rolls_back_every_table, duplicate_primary_key_is_refused |
| 16 | Many users / servers at once | write lock + `data_version` cache | R: concurrent_users_never_get_duplicate_ids, two_servers_share_one_database |
| 17 | Import validation + conflicting edits (edit pack) | `SyncManager` (re-planned under the write lock) | R: export_edit_import_applies_validated_changes, edit_pack_zip_round_trip, import_conflict_requires_decision, import_rejects_bad_edits, unsafe_zip_is_rejected, export_refused_when_data_has_errors; W: excel_tools |
| 18 | Backup restore, verification, retention, backups in the database | `backup_manager.py`, `SqlStore.restore*` | R: backup_verify_restore_and_preview, backups_can_live_inside_the_database, tampered_backup_is_rejected, backup_retention_prunes_oldest, daily_backup_once_per_day, restore_of_earlier_excel_version_backup; W: backups_download_upload_and_restore, daily_backup_cron |
| 19 | Audit-log consistency, who did it | `_audit`, `utils/actor.py` | R: audit_log_consistency, web_actor_is_recorded; C: update_bill_whitelist_and_locking |
| 20 | Report totals match ledgers | `ReportingService` | S: reports_match_ledgers, cash_flow_opening_and_closing, supplier_payments_not_counted_as_expenses_twice; W: every_report_runs_and_exports |
| 21 | Formula injection safe | `excel_io` text cells, import formula scan | C: formula_text_is_stored_as_text; S: report_export_is_formula_safe; R: import_rejects_bad_edits |
| 22 | Retry / duplicate-write protection | `request_id` + audit | C: idempotent_retry_does_not_duplicate; W: full_day_of_bookkeeping |
| 23 | Cache consistent with the database | `SqlStore` cache keyed by data_version | C: cache_matches_database_after_operations |
| 24 | Integrity checker | `validate_dataset`, `integrity_check` | R: integrity_checker_finds_orphans_and_overallocation |
| 25 | Refuse foreign / unreachable databases | `_check_columns`, `StartupError` | R: foreign_tables_are_refused, unreachable_database_reports_startup_error |
| 26 | Earlier Excel data (incl. v3) imports | `DataService.import_legacy_folder`, `migrations.py` | R: legacy_v3_excel_folder_is_migrated_on_import |
| 27 | Data-file import validated before replacing tables | `DataService` | R: demo_workbook_imports_cleanly, data_file_with_broken_references_is_refused, snapshot_round_trips_through_data_import, reset_keeps_settings_and_a_backup |
| 28 | Performance 10k bills / 50k allocations | cache, grouped sums | R: performance_10k_bills (prints timings) |
| 29 | Duplicate sales date/shift warn, multi-shift allowed | `_sales_duplicate` | S: duplicate_date_shift_warns...; W: full_day_of_bookkeeping |
| 30 | Shared settings, validated and audited | `update_settings`, `reload_settings` | R: settings_are_shared_validated_and_audited; W: settings_are_validated_shared_and_audited |
| 31 | "Today" follows the business timezone | `date_utils.set_timezone` | R: today_follows_the_business_timezone |
| 32 | Vercel without a database refuses to run on temporary disk | `ServerSettings.from_env` | W: vercel_without_database_shows_setup_message |
| 33 | Web UI: every screen | `web/static/` | API covered by W; screens checked in a headless browser during development |

Not implemented (listed honestly): login and roles, POS file import, bank reconciliation, customer ledger, encryption
at rest (beyond the database host's), per-shift cash opening float, invoice file uploads (links only on the web),
PyInstaller build verification on a clean Windows machine.
