"""One-way migration of v3 data (mutable allocation status) to the v4 append-only ledger."""
from .ids import next_ids
from .schemas import SCHEMAS, ID_PREFIX


def migrate_frames(frames):
    """Idempotent: only rows that still lack an entry_type are touched."""
    import pandas as pd

    pays = {str(r["payment_id"]): r for r in frames["payments"].to_dict("records")}
    alloc = frames["payment_allocations"].to_dict("records")
    existing_ids = [r["allocation_id"] for r in alloc]
    new_rows = []
    for r in alloc:
        if r["entry_type"] != "":
            continue
        pay = pays.get(str(r["payment_id"]), {})
        r["entry_type"] = "ALLOCATE"
        r["effective_date"] = pay.get("payment_date") or str(r["allocated_at"])[:10]
        legacy_reversed = str(r["status"]).strip() == "Reversed"
        r["status"] = "Active"
        if legacy_reversed:
            when = str(pay.get("modified_at", ""))[:10] or r["effective_date"]
            new_rows.append({"allocation_id": None, "payment_id": r["payment_id"], "bill_id": r["bill_id"],
                             "entry_type": "REVERSE", "amount": r["amount"],
                             "effective_date": max(when, r["effective_date"]), "allocated_at": pay.get("modified_at", ""),
                             "reversal_of": r["allocation_id"], "status": "Reversal"})
    if new_rows:
        ids = next_ids(existing_ids, ID_PREFIX["payment_allocations"], len(new_rows))
        for row, new_id in zip(new_rows, ids):
            row["allocation_id"] = new_id
        alloc.extend(new_rows)
    frames["payment_allocations"] = pd.DataFrame(alloc, columns=SCHEMAS["payment_allocations"], dtype=object)

    bills = frames["bills"]
    bills.loc[bills["status"].astype(str) != "Cancelled", "status"] = "Active"
    pay_df = frames["payments"]
    mask = (pay_df["status"].astype(str) == "Reversed") & (pay_df["reversed_at"].astype(str) == "")
    pay_df.loc[mask, "reversed_at"] = pay_df.loc[mask, "modified_at"]
    return frames
