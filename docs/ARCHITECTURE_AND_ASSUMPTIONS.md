# Architecture, schemas and accounting assumptions (data schema v4, app v15)

Layers: `web/static` (browser UI) -> `web/server.py` (JSON API) -> `services/` (all rules) -> `database/`
(SQL storage, validation, backups, Excel import/export). `utils/` holds Decimal money, dates/timezone, settings,
logging. The UI never holds accounting rules; the services never know about HTTP.

The web app and the desktop app are the same server. The desktop launcher (`cafebooks/desktop.py`) runs it on
127.0.0.1 with a SQLite file and shows it in a native window; the web deployment points it at PostgreSQL.

## Storage (`database/sql_store.py`)
* One SQL table per schema table below, plus `row_seq` (insertion order), `cafebooks_meta` (data_version) and
  `cafebooks_backups` (backup zips, used when backups live in the database).
* Types: money `NUMERIC(14,2)` on PostgreSQL / exact decimal text on SQLite; dates `DATE`; `vendors.active` boolean;
  other columns text. In memory the services still receive the v4 frame convention (money as "123.45" strings etc.).
* `transaction(tables)` takes a database-wide write lock (PostgreSQL `pg_advisory_xact_lock`, SQLite
  `BEGIN IMMEDIATE`), re-reads the tables under the lock, lets the service mutate copies, then writes only the
  changed rows and bumps `data_version` in the same transaction. Any exception rolls everything back.
* Each process caches tables keyed by `data_version`, so several servers / serverless instances stay coherent.
* Business settings are rows of the `settings` table (JSON values) and are audited when changed.

## Tables (schema_version 4 in the settings table)
Money is exact to 2 dp and handled as Decimal; dates are ISO; text exported to Excel is always stored as literal
text (no formula injection). Every table has a primary key (sequential IDs, e.g. BIL-000123).
| Table | Columns |
|---|---|
| vendors | vendor_id, name, contact_person, phone, email, address, category, gstin, payment_terms_days, default_payment_method, notes, active, created_at, modified_at |
| bills | bill_id, vendor_id, invoice_number, invoice_date, due_date, category, description, subtotal, tax_amount, other_charges, invoice_total, attachment_path, notes, status(Active/Cancelled), created_at, modified_at |
| payments | payment_id, vendor_id, payment_date, amount, method, reference, notes, status(Active/Reversed), reversal_reason, reversed_at, created_at, modified_at |
| payment_allocations (append-only) | allocation_id, payment_id, bill_id, entry_type(ALLOCATE/REVERSE), amount(>0), effective_date, allocated_at, reversal_of, status |
| customer_sales | sales_id, business_date, shift, gross_sales, discounts, refunds, net_sales, cash/upi/card/other_collected, credit_sales, credit_collections, tips_collected, service_charges_collected, taxes_collected, expected_cash, actual_cash, cash_difference, notes, status(Active/Voided), created_at, modified_at |
| sales_adjustments | adjustment_id, sales_id, adjustment_type(REFUND/DISCOUNT), amount, adjustment_date, notes, created_at, reversal_of, status |
| tips_ledger | entry_id, sales_id, entry_type(tip/service_charge), collected, distributed, retained_or_payable, entry_date, distribution_date, notes, created_at, status |
| audit_log (append-only) | audit_id, timestamp, actor, request_id, entity_type, entity_id, action, details, before, after |
| settings | key, value, modified_at |

## Accounting treatment
* **Bill**: expense on the invoice date (accrual); total = subtotal + tax + other charges (checked).
* **Outstanding** = total - net allocations whose effective_date <= as-of date (ALLOCATE minus REVERSE).
  Status precedence: Cancelled > Paid > Overdue (due < as-of and balance > 0) > Partially Paid > Unpaid.
  Aging/outstanding reports take an explicit as-of date and exclude bills dated after it.
* **Payment**: cash movement, not an expense. Unallocated = amount - net allocations, shown separately.
  Allocation effective date = payment date (later allocations: allocation date; reversals: reversal date).
  A timeline check guarantees net allocations never exceed the bill total on any date.
* **Reversal / correction**: REVERSE rows are appended; nothing is flipped or deleted. A "correction" is reverse + new entry.
* **Sales**: net_sales = gross - discounts - refunds (as entered; excludes tips/service/tax if the POS gross does).
  Later refunds/discounts are separate adjustments. Expected cash defaults to cash collected; actual cash is
  blank until counted (no fabricated zero difference).
* **Cafe revenue** = net sales after adjustments, less tax if the POS gross includes it, plus tips / service
  charges / taxes only if Settings say they are revenue (defaults: all excluded - accountant decision).
* **Reconciliation per sales record**: cash+UPI+card+other = net [+ tax unless gross includes tax] + tips + service
  charges + credit collections - credit sales. Also checks ledger vs record for tips/service charges and the cash count.
* **Credit collections** are never revenue; credit sales are summary-only.
* **Idempotency**: create operations take a request_id; replays return the original record (audit-backed).

## Open decisions for the accountant
Whether service charges / taxes are revenue; whether POS gross includes tax; cash tips distribution timing;
opening cash balance and date (Settings) - cash-on-hand closing balances need these.
