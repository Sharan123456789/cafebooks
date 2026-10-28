# CafeBooks

Everyday accounting for a cafe or bakery: daily sales and cash counts, vendors, purchase bills,
supplier payments, tips and service charges, reports, backups and an activity log.

**One codebase, two ways to run it:**

| | Web app (shared) | Desktop app (standalone) |
|---|---|---|
| Who uses it | Everyone with the link, on any phone, tablet or PC | One Windows PC, works offline |
| Database | PostgreSQL (e.g. Neon on Vercel) — everyone shares the same books | SQLite file on that PC |
| Backups | Stored inside the database; download copies from Settings | Folder next to the data |
| Start | Deploy to Vercel, or `python -m cafebooks serve` | `python desktop.py`, or the installed `CafeBooks.exe` |

Both run the same server (`cafebooks/web/server.py`), the same screens (`cafebooks/web/static/`) and the
same accounting rules (`cafebooks/services/`), so a feature added once works in both.

> **There is no login yet.** Anyone who has the web address can view and change the books.
> Keep the address private until login is added, or turn on Vercel Deployment Protection.
> Every change is recorded in the Activity Log with the name the person typed in the top bar.

---

## 1. Run it on your computer (from source)

Requires Python 3.11 or newer.

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt

python -m cafebooks serve         # web app at http://127.0.0.1:8000  (SQLite in ./data)
python desktop.py                 # or: the desktop app in its own window (needs requirements-desktop.txt)
```

To let other devices on the shop Wi-Fi use it: `python -m cafebooks serve --host 0.0.0.0`, then open
`http://<this-pc's-IP>:8000` on the other device.

To try it with sample data: **Settings → Backups & data → Download demo workbook**, then
**Import data file…** with that workbook. Use **Reset all data** afterwards to start clean.

## 2. Deploy to Vercel (shared web app)

1. Push this repository to GitHub.
2. In Vercel: **Add New → Project**, import the repository. No build settings are needed — Vercel
   detects FastAPI from `requirements.txt` and the `app` in `app.py`.
3. In the project, open **Storage → Create Database → Neon (Postgres)** and connect it to the project.
   This sets `DATABASE_URL`. (Any PostgreSQL works — Supabase, Railway, your own server — just set
   `DATABASE_URL` yourself. Use a database dedicated to CafeBooks.)
4. Optional but recommended: add an environment variable `CRON_SECRET` with a long random value.
5. **Deploy**, then open the site. Tables are created automatically on first use.

What happens on Vercel:

* All business data and all backups live in PostgreSQL. Vercel's disk is temporary, so CafeBooks
  refuses to start there without `DATABASE_URL` and shows setup instructions instead.
* A daily backup runs at 00:00 IST via Vercel Cron (`vercel.json`). Change the time there.
  Also download a backup now and then (Settings → Backups & data) and keep it off the server.
* Uploads and downloads (backups, Excel packs) are limited to about 4.5 MB per request by Vercel.
  That is years of records for a small shop; for more, run on your own server instead.
* "Today" follows the timezone in Settings (default Asia/Kolkata), not the server clock (UTC).

Other hosts: anything that runs Python can serve `app:app` (e.g. `uvicorn app:app --host 0.0.0.0`)
with `DATABASE_URL` set.

## 3. Desktop app (standalone, Windows)

```bash
pip install -r requirements-desktop.txt
python desktop.py                                  # run it
pyinstaller --noconfirm packaging/CafeBooks.spec   # build dist/CafeBooks/CafeBooks.exe
```

Then compile `packaging/CafeBooks.iss` with Inno Setup to get `installer_output/CafeBooks_Setup.exe`.
The installed app keeps its data in `%LOCALAPPDATA%\CafeBooks` (database, backups, logs), so
reinstalling or updating never touches it. If it is already running, starting it again opens the
running copy in the browser instead of starting a second one. It only listens on this computer (127.0.0.1).

## 4. Bringing in data from the earlier Excel version

Earlier versions kept data in a folder of workbooks (`vendors.xlsx`, `bills.xlsx`, …). Import it once:

```bash
python -m cafebooks import-excel "path\to\old\data"           # local SQLite
set DATABASE_URL=postgresql://...  &&  python -m cafebooks import-excel "path\to\old\data"   # straight into the web database
```

The data is validated (and migrated from v3 if needed) before anything changes, and a safety backup is
taken first. Backup zips made by the earlier version can also be restored directly from
**Settings → Backups & data → Restore from a file…**.

## 5. Accounting rules the app enforces

* A bill's paid / outstanding / status are **calculated** from the payment ledger, never typed.
* Payments cannot be edited or deleted. **Reverse payment** or **Reverse allocation** adds reversing
  entries; history is kept and every action is in the Activity Log (who, when, before/after).
* A payment may stay (partly) **unallocated**; it never settles a bill until allocated.
* Bills with payments cannot be cancelled or have amounts changed until the allocations are reversed.
* Duplicate supplier invoices (same vendor + normalised invoice number) are blocked. A second sales
  record for the same date/shift, or a vendor with the same name, asks for confirmation.
* Tips, service charges and taxes are not revenue by default; credit collections are never revenue;
  supplier payments are cash movements, not expenses (the bill is the expense). Policies are in Settings.
* **Cash flow is not profit.** Stock cost, wages and depreciation are not tracked, so profit is not calculated.
* Money is exact to the paisa (Decimal); amounts with more than 2 decimals are rejected, not rounded.

## 6. Safety and recovery

* **Saves are all-or-nothing.** Each save is one database transaction; if anything fails, nothing changes.
* **Many people at once.** Every save takes a database-wide write lock, re-reads the latest data and
  re-checks the rules, so two cashiers can never create the same invoice twice or over-pay a bill.
  Pages show a notice when someone else has saved changes.
* **Retries are safe.** Forms send a request ID; a double-clicked or retried save does not duplicate records.
* **Backups** are verified zips (one workbook per table + SHA-256 manifest). Restore shows a preview,
  refuses backups with accounting errors, and takes a safety backup first.
* **Excel editing:** *Download edit pack*, edit in Excel, *Upload edited pack*. Changes are diffed
  against the original export; rows changed both in the app and in the file are shown as conflicts
  for you to decide. Deleting rows, formulas and edits to the append-only ledgers are refused.

## 7. Configuration

Business settings (name, categories, revenue policy, timezone, …) are edited in the app and stored in
the database, so they are the same for everyone. Server settings are environment variables
(see `.env.example`; a local `.env` file is read automatically):

| Variable | Purpose |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string (also `POSTGRES_URL` / `CAFEBOOKS_DATABASE_URL`). Unset = SQLite in the data folder. |
| `CAFEBOOKS_DATA_DIR` | Folder for the SQLite file, backups and logs (default `./data`, or `%LOCALAPPDATA%\CafeBooks` for the packaged app). |
| `CAFEBOOKS_BACKUP_STORE` | `files` or `database` (default: `database` with PostgreSQL, `files` with SQLite). |
| `CRON_SECRET` | If set, only Vercel Cron can trigger the daily backup endpoint. |

## 8. Project layout

```
app.py                 Vercel / ASGI entry point (exposes `app`)
desktop.py             desktop app entry point (also used for the .exe)
cafebooks/
  web/server.py        JSON API (/api/...) + serves the UI
  web/static/          the user interface (HTML, CSS, JavaScript; no build step)
  services/            accounting, reconciliation, reporting, imports — all business rules
  database/            SQL storage, schemas, validation, backups, Excel import/export
  utils/               money, dates/timezone, settings, logging
  desktop.py           desktop launcher (local server + window)
tests/                 pytest suite (runs against SQLite and PostgreSQL)
packaging/             PyInstaller spec and Inno Setup installer script
demo_data/             synthetic demo workbook
docs/                  architecture, accounting assumptions, requirement checklist
```

## 9. Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Every storage and API test runs twice: on SQLite and on PostgreSQL. PostgreSQL comes from the
`pgserver` package (a throwaway local server) or from `CAFEBOOKS_TEST_POSTGRES_URL`; without either,
the PostgreSQL variants are skipped. Tests use temporary databases and never touch real data.

## 10. Known limitations

* No login or roles yet (see the note at the top). The audit "who" is the name typed in the top bar.
* Customer credit is summary-only (no customer ledger). Tax/GST treatment is a setting your
  accountant must confirm. POS import, bank reconciliation, multi-currency and payroll are not implemented.
* Invoice attachments are links (e.g. Google Drive) on the web; the desktop app can also open local files.
