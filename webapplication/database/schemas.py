"""Workbook schemas (v4).  Money is stored as numeric cells with 2 decimals and handled
as Decimal in code; dates are ISO text; every table has a primary key."""

SCHEMA_VERSION = 4

SCHEMAS = {
    "vendors": ["vendor_id", "name", "contact_person", "phone", "email", "address", "category", "gstin",
                "payment_terms_days", "default_payment_method", "notes", "active", "created_at", "modified_at"],
    "bills": ["bill_id", "vendor_id", "invoice_number", "invoice_date", "due_date", "category", "description",
              "subtotal", "tax_amount", "other_charges", "invoice_total", "attachment_path", "notes",
              "status", "created_at", "modified_at"],
    "payments": ["payment_id", "vendor_id", "payment_date", "amount", "method", "reference", "notes", "status",
                 "reversal_reason", "reversed_at", "created_at", "modified_at"],
    "payment_allocations": ["allocation_id", "payment_id", "bill_id", "entry_type", "amount", "effective_date",
                            "allocated_at", "reversal_of", "status"],
    "customer_sales": ["sales_id", "business_date", "shift", "gross_sales", "discounts", "refunds", "net_sales",
                       "cash_collected", "upi_collected", "card_collected", "other_collected", "credit_sales",
                       "credit_collections", "tips_collected", "service_charges_collected", "taxes_collected",
                       "expected_cash", "actual_cash", "cash_difference", "notes", "status", "created_at",
                       "modified_at"],
    "sales_adjustments": ["adjustment_id", "sales_id", "adjustment_type", "amount", "adjustment_date", "notes",
                          "created_at", "reversal_of", "status"],
    "tips_ledger": ["entry_id", "sales_id", "entry_type", "collected", "distributed", "retained_or_payable",
                    "entry_date", "distribution_date", "notes", "created_at", "status"],
    "audit_log": ["audit_id", "timestamp", "actor", "request_id", "entity_type", "entity_id", "action",
                  "details", "before", "after"],
    "settings": ["key", "value", "modified_at"],
}
FILENAME_MAP = {name: f"{name}.xlsx" for name in SCHEMAS}

PRIMARY_KEYS = {
    "vendors": "vendor_id", "bills": "bill_id", "payments": "payment_id",
    "payment_allocations": "allocation_id", "customer_sales": "sales_id",
    "sales_adjustments": "adjustment_id", "tips_ledger": "entry_id", "audit_log": "audit_id",
    "settings": "key",
}
ID_PREFIX = {
    "vendors": "VEN", "bills": "BIL", "payments": "PAY", "payment_allocations": "ALC",
    "customer_sales": "SAL", "sales_adjustments": "ADJ", "tips_ledger": "TIP", "audit_log": "AUD",
}

MONEY_COLUMNS = {
    "bills": ["subtotal", "tax_amount", "other_charges", "invoice_total"],
    "payments": ["amount"],
    "payment_allocations": ["amount"],
    "customer_sales": ["gross_sales", "discounts", "refunds", "net_sales", "cash_collected", "upi_collected",
                       "card_collected", "other_collected", "credit_sales", "credit_collections",
                       "tips_collected", "service_charges_collected", "taxes_collected", "expected_cash",
                       "actual_cash", "cash_difference"],
    "sales_adjustments": ["amount"],
    "tips_ledger": ["collected", "distributed", "retained_or_payable"],
}
SIGNED_MONEY = {"cash_difference"}
BLANKABLE_MONEY = {"expected_cash", "actual_cash", "cash_difference"}

DATE_COLUMNS = {
    "bills": ["invoice_date", "due_date"],
    "payments": ["payment_date"],
    "payment_allocations": ["effective_date"],
    "customer_sales": ["business_date"],
    "sales_adjustments": ["adjustment_date"],
    "tips_ledger": ["entry_date", "distribution_date"],
}
BOOL_COLUMNS = {"vendors": ["active"]}

# Rows of these tables are append-only: existing rows can never be changed by import.
IMMUTABLE_TABLES = {"audit_log", "payment_allocations"}

PAYMENT_METHODS = ["Cash", "UPI", "Bank Transfer", "Debit/Credit Card", "Cheque", "Other"]
ADJUSTMENT_TYPES = ["REFUND", "DISCOUNT"]
TIP_TYPES = ["tip", "service_charge"]
