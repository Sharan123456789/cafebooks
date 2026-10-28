"""Settings.

Two kinds:
  * Business settings (cafe name, categories, revenue policies, timezone, ...) are stored in the
    database `settings` table, so every user of a shared deployment sees the same values and
    they travel with backups.
  * Server settings (database URL, folders, mode) come from environment variables, which is
    how Vercel and other hosts configure an app.  See .env.example.
"""
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .currency_utils import money
from .date_utils import DEFAULT_TIMEZONE, get_zone, parse_date

DEFAULT_CATEGORIES = [
    "Milk and Dairy", "Bakery and Bread", "Fruits and Vegetables", "Meat and Eggs",
    "Snacks and Packaged Food", "Beverages", "Coffee and Tea Supplies", "Cleaning Supplies",
    "Packaging Materials", "Utilities", "Equipment and Maintenance", "Other",
]

DEFAULTS = {
    "cafe_name": "My Cafe",
    "business_details": "",
    "currency_code": "INR",
    "currency_symbol": "₹",
    "indian_number_grouping": True,
    "date_display": "DD-MM-YYYY",
    "timezone": DEFAULT_TIMEZONE,
    "copy_attachments": False,
    "vendor_categories": DEFAULT_CATEGORIES,
    "expense_categories": DEFAULT_CATEGORIES,
    "default_payment_terms_days": 7,
    "allow_multiple_shifts": False,
    "shift_names": ["Morning", "Evening"],
    "tips_excluded_from_revenue": True,
    "service_charges_excluded_from_revenue": True,
    "taxes_excluded_from_revenue": True,
    "gross_sales_includes_tax": False,
    "track_taxes": True,
    "track_customer_credit": True,
    "opening_cash": "",
    "opening_cash_date": "",
    "auto_backup_daily": True,
    "backup_retention": 30,
    "report_default_range_days": 30,
}
SETTING_KEYS = tuple(DEFAULTS)
LIST_KEYS = ("vendor_categories", "expense_categories", "shift_names")
BOOL_KEYS = ("allow_multiple_shifts", "tips_excluded_from_revenue", "service_charges_excluded_from_revenue",
             "taxes_excluded_from_revenue", "gross_sales_includes_tax", "track_taxes", "track_customer_credit",
             "auto_backup_daily", "copy_attachments", "indian_number_grouping")
INT_KEYS = ("default_payment_terms_days", "backup_retention", "report_default_range_days")


class ConfigError(Exception):
    """The server is not configured correctly (message is shown to the user)."""


def _bool(value):
    if isinstance(value, str):
        return value.strip().casefold() in ("1", "true", "yes", "on")
    return bool(value)


def validate_config(cfg):
    """Return a complete, validated copy of the business settings or raise ValueError."""
    out = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULTS.items()}
    out.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    for key in ("cafe_name", "currency_code", "currency_symbol"):
        out[key] = " ".join(str(out[key] or "").split())[:80]
        if not out[key]:
            raise ValueError(f"{key.replace('_', ' ').capitalize()} is required.")
    out["business_details"] = str(out["business_details"] or "").strip()[:500]
    for key in LIST_KEYS:
        value = out[key]
        if isinstance(value, str):
            value = value.splitlines()
        value = [" ".join(str(x).split())[:60] for x in value if str(x).strip()]
        if key != "shift_names" and not value:
            raise ValueError(f"{key.replace('_', ' ').capitalize()} must contain at least one entry.")
        out[key] = list(dict.fromkeys(value))
    for key in INT_KEYS:
        try:
            out[key] = int(str(out[key]).strip())
        except (TypeError, ValueError):
            raise ValueError(f"{key.replace('_', ' ').capitalize()} must be a whole number.") from None
        low, high = (1, 1000) if key == "backup_retention" else (0, 3650)
        if not low <= out[key] <= high:
            raise ValueError(f"{key.replace('_', ' ').capitalize()} must be between {low} and {high}.")
    for key in BOOL_KEYS:
        out[key] = _bool(out[key])
    if out["date_display"] not in ("DD-MM-YYYY", "YYYY-MM-DD", "DD/MM/YYYY"):
        raise ValueError("Date display must be DD-MM-YYYY, YYYY-MM-DD or DD/MM/YYYY.")
    out["timezone"] = str(out["timezone"] or DEFAULT_TIMEZONE).strip()
    get_zone(out["timezone"])
    opening = str(out["opening_cash"] if out["opening_cash"] is not None else "").strip()
    out["opening_cash"] = f"{money(opening, 'Opening cash'):.2f}" if opening else ""
    out["opening_cash_date"] = parse_date(out["opening_cash_date"], "Opening cash date", required=False)
    return out


def encode_value(value):
    return json.dumps(value, ensure_ascii=False)


def config_from_settings(df):
    """Business settings from the database settings table (bad or unknown rows are ignored)."""
    stored = {}
    for key, raw in zip(df["key"], df["value"]):
        if key in DEFAULTS:
            try:
                stored[key] = json.loads(raw)
            except (TypeError, ValueError):
                continue
    try:
        return validate_config(stored)
    except ValueError:
        out = validate_config({})
        for key, value in stored.items():
            try:
                out = validate_config({**out, key: value})
            except ValueError:
                continue
        return out


# ---------------------------------------------------------------------- server settings
def project_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def default_data_dir():
    """Packaged desktop app: per-user folder that survives reinstalls.  Source checkout: ./data."""
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / ".local" / "share")
        return Path(base) / "CafeBooks"
    return project_dir() / "data"


@dataclass
class ServerSettings:
    database_url: str
    data_dir: Path
    backup_dir: Path
    backup_store: str          # "files" | "database"
    mode: str                  # "web" | "desktop"
    on_vercel: bool
    cron_secret: str

    @classmethod
    def from_env(cls, env=None):
        env = os.environ if env is None else env
        on_vercel = bool(env.get("VERCEL"))
        mode = "desktop" if env.get("CAFEBOOKS_MODE", "").strip().lower() == "desktop" else "web"
        data_dir = Path(env.get("CAFEBOOKS_DATA_DIR") or default_data_dir())
        url = (env.get("CAFEBOOKS_DATABASE_URL") or env.get("DATABASE_URL") or env.get("POSTGRES_URL") or "").strip()
        if not url:
            if on_vercel:
                raise ConfigError(
                    "No database is configured. On Vercel, add a Postgres database (for example Neon from the "
                    "Vercel Marketplace) so DATABASE_URL is set, then redeploy. Vercel's disk is temporary, so "
                    "CafeBooks refuses to keep business records there.")
            url = f"sqlite:///{(data_dir / 'cafebooks.db').as_posix()}"
        if on_vercel and url.startswith("sqlite"):
            raise ConfigError("SQLite cannot be used on Vercel because its disk is wiped between requests. "
                              "Set DATABASE_URL to a PostgreSQL database.")
        store = (env.get("CAFEBOOKS_BACKUP_STORE") or "").strip().lower()
        if store not in ("files", "database"):
            store = "files" if url.startswith("sqlite") and not on_vercel else "database"
        if on_vercel:
            store = "database"
        backup_dir = Path(env.get("CAFEBOOKS_BACKUP_DIR") or data_dir / "backups")
        return cls(database_url=url, data_dir=data_dir, backup_dir=backup_dir, backup_store=store, mode=mode,
                   on_vercel=on_vercel, cron_secret=env.get("CRON_SECRET", ""))
