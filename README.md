# CafeBooks v6 - lightweight desktop accounting for a small cafe

Python 3.11+ | CustomTkinter | Pandas | OpenPyXL | Matplotlib | pytest.
**Storage: Excel (.xlsx) workbooks** in a configurable data folder. Single user only.

## 1. Install and run (Windows)
```
py -3.11 -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py
```
First launch creates the `data/` folder with all workbooks. Re-running never erases data.
Try it with fake data first: `python demo_data.py`, then set `"data_dir": "demo_data"` in `config.json`
(or Settings) and restart. Demo data is synthetic and clearly separate.

## 2. Run the tests
```
python -m pytest -q
```
Tests use temporary folders and synthetic data only; they never touch your real workbooks.

## 3. Welcome and guided tutorial
On the first launch, CafeBooks opens a welcome note explaining what the app does and a key accounting limitation: cash movement is not profit. Choose **Start guided tour** for the in-app, step-by-step walkthrough, or **Maybe later** to open the app. The welcome note can be disabled with its checkbox; reopen it any time using **Welcome & Tutorial** in the sidebar. The tour has six steps and lets you jump directly to the related section.

## 4. Using the app
Sidebar: Quick Entry, Dashboard, Vendors, Purchase Bills, Supplier Payments, Daily Sales, Tips and Service Charges, Reports,
Data Management and Settings (Ctrl+1..9 switch screens). Quick Entry is the central launchpad for common entry workflows and shows current record counts; saves use the same validation and ledger logic as the dedicated tabs. Tables have search, click-to-sort headers, date-range and
choice filters. Saves run in the background; the status bar shows the data folder and last successful save.

The Dashboard uses color-accented KPI cards to highlight sales, supplier liabilities, cash movement and reconciliation. Tips and service charges remain in their dedicated ledger/report screens rather than the Dashboard.

Key rules the app enforces
* A bill's paid / outstanding / status are **calculated** from the payment ledger, never typed.
* Payments cannot be edited or deleted. **Reverse Payment** or **Reverse Allocation** adds reversing ledger rows;
  history is kept and every action is in `audit_log.xlsx` (who, when, before/after).
* A payment may stay (partly) **unallocated**; it never settles a bill. Use *Allocate Unallocated* later.
* Bills with payments cannot be cancelled or have amounts edited until the allocations are reversed.
* Duplicate supplier invoices (same vendor + normalised invoice number) are blocked. Duplicate sales date/shift
  and same-name vendors give a warning you can confirm.
* Tips and service charges are not cafe revenue by default; credit collections are never revenue; supplier
  payments are cash movements, not expenses (the *bill* is the expense). Policies are in Settings.
* Cash flow is not profit. Inventory cost, payroll, depreciation etc. are not tracked, so profit is not calculated.

## 5. Backups and recovery
* **Backup Now** (zip + SHA-256 manifest, verified on creation). Daily automatic backup (Settings). Automatic
  backups before migration, restore, external import and export. Retention = newest N (Settings).
* **Restore Backup** verifies the archive, shows a row-count preview (backup vs current) and asks to confirm; a safety
  backup of the current data is taken first. A backup with accounting errors is refused.
* **Interrupted writes:** every save is staged, verified, journaled (`transaction_journal.json`) and swapped in.
  If the app/PC dies mid-save, the next start automatically rolls back to the last consistent state (or confirms
  the save had completed) and tells you. Odd files are copied to `data/recovery_quarantine/` - nothing is deleted.
* **Excel open / locked:** if a workbook is open in Excel (`~$file.xlsx`) the save is refused with a clear
  message and nothing changes. Close Excel and retry. Do not edit live workbooks directly; use the workflow below.
* **Corrupted or missing workbook:** startup stops with the file name and changes nothing. Restore the newest backup
  from `backups/` (Settings > Restore Backup after fixing, or unzip the backup into `data/`).
* A second copy of the app on the same data folder is refused (lock file; stale locks are reclaimed).

## 6. Safe external Excel editing (Export / Edit / Re-import)
1. *Export for Edit* validates the data, backs up, and writes `staging/export_<time>/EDIT_THESE_FILES` plus an
   untouched base snapshot and manifest.
2. Edit only those copies (no formulas, do not delete rows). Live data is never changed by exporting or opening them.
3. *Import / Preview* shows additions/modifications/conflicts and validation errors. Rows changed both in the app and
   in the files are **conflicts** and need your explicit decision. Deleted rows, formulas, edits to the append-only
   audit/allocation ledgers and accounting-invariant violations are rejected.
4. Confirm -> backup -> atomic journaled commit. Re-applying the same import is a no-op.

## 7. Packaging an optional .exe
```
pip install pyinstaller
pyinstaller --noconfirm --windowed --name CafeBooks --collect-all customtkinter main.py
```
Copy `config.json` next to `dist\CafeBooks\CafeBooks.exe`; `data/`, `backups/`, `logs/` are created beside it.

## 8. Known limitations (read these)
* Excel is **not** a database: no ACID, no multi-user access, no encryption or access control. Protect the folder
  (disk encryption, OS permissions) and keep off-machine backups. The journal protects against crashes and failed
  swaps, not against disk failure.
* Each save rewrites the changed workbooks. Measured on a 10k-bill / 50k-allocation dataset: first load ~7 s,
  later reads ~0.2 s from cache, a payment save ~5 s (small tables: ~0.02 s). Saves run off the UI thread.
* Customer credit is summary-only (no customer ledger). Tax/GST rules are NOT invented: gross/net/tax conventions
  are settings your accountant must confirm.
* `actor` in the audit log is the configured operator name or OS user; there is no login.
* Import from POS files, bank reconciliation, multi-currency and payroll are not implemented.
* The GUI is smoke-tested under a virtual display (all screens, all reports, form flows), not exhaustively on Windows.

## Version 8 dashboard refresh

- Redesigned the dashboard to match a polished modern café analytics interface, using a soft neutral background, teal/blue/purple/amber accents, rounded panels, and clearer visual hierarchy.
- Added a welcome strip with the active reporting period and a set of four prominent KPI cards for net sales, gross sales, cash movement, and supplier outstanding.
- Replaced the collection-mix bars with a pictorial donut chart and matching color-coded legend for Cash, UPI, Card, and Other.
- Redesigned the money snapshot as horizontal comparison bars for net sales, supplier payments, and outstanding supplier balances.
- Grouped the remaining operational figures into compact cards below the charts so important information is easier to scan.
- Kept the date-range controls, data-integrity status, and the distinction between cash movement and profit.
- Preserved the v7 in-field data-type hints and whole-number-only money/day-count inputs; accounting calculations and storage remain Decimal-safe.


## Version 14 polish and demo validation

- Reworked shared action bars into a consistently spaced two-row grid so Settings actions, including Excel Snapshot, remain aligned and visible.
- Rebuilt the Integrity Check window to remain open while checks run and clearly show pass/error/warning results, including useful failure details.
- Corrected the included demo workbook to use the application's exact enum values and valid cross-table references.
- Balanced demo sales collections against net sales, tips, service charges, credit sales/collections, cash counts, and ledger entries. The supplied demo workbook passes dataset validation and reconciliation with zero errors and zero warnings.
- Demo records are synthetic and dated 1–3 October 2026. Importing replaces only tables represented by the workbook; back up real data first.
