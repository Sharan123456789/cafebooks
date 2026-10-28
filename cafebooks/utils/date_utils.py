"""Date helpers.  Dates are stored as ISO text (YYYY-MM-DD) and displayed per settings.

"Today" and timestamps follow the business timezone (Settings), not the server clock:
cloud servers run in UTC, which would make an Indian cafe's day roll over at 05:30.
"""
from datetime import date, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
import math

ISO = "%Y-%m-%d"
INPUT_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y")
DISPLAY_STYLES = {"DD-MM-YYYY": "%d-%m-%Y", "YYYY-MM-DD": "%Y-%m-%d", "DD/MM/YYYY": "%d/%m/%Y"}
MIN_YEAR, MAX_YEAR = 2000, 2100
DEFAULT_TIMEZONE = "Asia/Kolkata"
_tz = None


def get_zone(name):
    """Return the ZoneInfo for name or raise ValueError."""
    try:
        return ZoneInfo(str(name).strip())
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"Unknown timezone '{name}'. Use an IANA name such as Asia/Kolkata.") from None


def set_timezone(name):
    global _tz
    _tz = get_zone(name or DEFAULT_TIMEZONE)


def now_local():
    """Naive datetime in the business timezone (stored timestamps stay offset-free as before)."""
    if _tz is None:
        set_timezone(DEFAULT_TIMEZONE)
    return datetime.now(_tz).replace(tzinfo=None)


def local_from_timestamp(ts):
    if _tz is None:
        set_timezone(DEFAULT_TIMEZONE)
    return datetime.fromtimestamp(ts, _tz).replace(tzinfo=None)


def _blank(value):
    return value is None or (isinstance(value, float) and math.isnan(value)) or (
        isinstance(value, str) and not value.strip())


def parse_date(value, field="Date", required=True):
    """Return an ISO date string, '' when optional+blank, or raise ValueError."""
    if _blank(value):
        if required:
            raise ValueError(f"{field} is required.")
        return ""
    if isinstance(value, datetime):
        parsed = value.date()
    elif isinstance(value, date):
        parsed = value
    else:
        parsed = None
        raw = str(value).strip()
        if len(raw) > 10 and raw[10] in "T ":
            raw = raw[:10]
        for fmt in INPUT_FORMATS:
            try:
                parsed = datetime.strptime(raw, fmt).date()
                break
            except ValueError:
                continue
        if parsed is None:
            raise ValueError(f"{field} must be a valid date (DD-MM-YYYY or YYYY-MM-DD).")
    if not (MIN_YEAR <= parsed.year <= MAX_YEAR):
        raise ValueError(f"{field} year must be between {MIN_YEAR} and {MAX_YEAR}.")
    return parsed.strftime(ISO)


def to_date(iso):
    return datetime.strptime(iso, ISO).date()


def today_iso():
    return now_local().strftime(ISO)


def display_date(value, style="DD-MM-YYYY"):
    if _blank(value):
        return ""
    try:
        return to_date(parse_date(value)).strftime(DISPLAY_STYLES.get(style, "%d-%m-%Y"))
    except ValueError:
        return str(value)


def month_key(iso):
    return iso[:7]


def quarter_key(iso):
    d = to_date(iso)
    return f"{d.year}-Q{(d.month - 1) // 3 + 1}"


def year_key(iso):
    return iso[:4]
