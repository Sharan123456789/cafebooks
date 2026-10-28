"""JSON configuration: defaults, validation, atomic save."""
import json
import os
import sys
import tempfile
from pathlib import Path

DEFAULT_CATEGORIES = [
    "Milk and Dairy", "Bakery and Bread", "Fruits and Vegetables", "Meat and Eggs",
    "Snacks and Packaged Food", "Beverages", "Coffee and Tea Supplies", "Cleaning Supplies",
    "Packaging Materials", "Utilities", "Equipment and Maintenance", "Other",
]

DEFAULTS = {
    "cafe_name": "My Cafe",
    "business_details": "",
    "currency_code": "INR",
    "currency_symbol": "\u20b9",
    "indian_number_grouping": True,
    "date_display": "DD-MM-YYYY",
    "operator_name": "",
    "data_dir": "data",
    "backup_dir": "backups",
    "export_dir": "exports",
    "staging_dir": "staging",
    "attachment_dir": "attachments",
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
    "appearance_mode": "System",
    "color_theme": "blue",
    "welcome_seen": False,
}


def app_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[1]


def config_path():
    return app_dir() / "config.json"


def validate_config(cfg):
    out = dict(DEFAULTS)
    out.update({k: v for k, v in cfg.items() if k in DEFAULTS})
    for key in ("vendor_categories", "expense_categories", "shift_names"):
        value = out[key]
        if isinstance(value, str):
            value = [x.strip() for x in value.splitlines()]
        value = [str(x).strip() for x in value if str(x).strip()]
        if key != "shift_names" and not value:
            raise ValueError(f"{key} must contain at least one entry.")
        out[key] = list(dict.fromkeys(value))
    for key in ("default_payment_terms_days", "backup_retention", "report_default_range_days"):
        try:
            out[key] = int(out[key])
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a whole number.") from None
        if out[key] < (1 if key == "backup_retention" else 0):
            raise ValueError(f"{key} is out of range.")
    for key in ("allow_multiple_shifts", "tips_excluded_from_revenue", "service_charges_excluded_from_revenue",
                "taxes_excluded_from_revenue", "gross_sales_includes_tax", "track_taxes",
                "track_customer_credit", "auto_backup_daily", "copy_attachments", "indian_number_grouping", "welcome_seen"):
        out[key] = bool(out[key])
    if out["date_display"] not in ("DD-MM-YYYY", "YYYY-MM-DD", "DD/MM/YYYY"):
        raise ValueError("date_display must be DD-MM-YYYY, YYYY-MM-DD or DD/MM/YYYY.")
    return out


def load_config(path=None):
    path = Path(path) if path else config_path()
    raw = {}
    if path.exists():
        raw = json.loads(path.read_text(encoding="utf-8"))
    return validate_config(raw)


def save_config(cfg, path=None):
    path = Path(path) if path else config_path()
    clean = validate_config(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".cfg_", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(clean, fh, indent=2, ensure_ascii=False)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return clean


def resolve_dir(cfg, key):
    p = Path(cfg[key])
    return (p if p.is_absolute() else app_dir() / p).resolve()
