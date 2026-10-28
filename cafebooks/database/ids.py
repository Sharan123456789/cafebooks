import re


def next_ids(series, prefix, count=1):
    """Sequential, human-readable IDs (PREFIX-000123).  Must be called while the
    storage lock is held so two writers can never pick the same number."""
    pattern = re.compile(rf"^{re.escape(prefix)}-(\d+)$")
    highest = 0
    for value in series:
        m = pattern.match(str(value))
        if m:
            highest = max(highest, int(m.group(1)))
    return [f"{prefix}-{highest + i + 1:06d}" for i in range(count)]


def append_rows(df, rows):
    """Append dict rows to an object-dtype frame without pandas empty-concat warnings."""
    import pandas as pd
    if not rows:
        return df
    new = pd.DataFrame(rows, columns=list(df.columns), dtype=object)
    return new if df.empty else pd.concat([df, new], ignore_index=True)
