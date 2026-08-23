-- AI Finance Controller schema.
-- All money is stored as integer paise. No foreign key exists between
-- payments and invoices -- that absence is the problem this project solves.
-- `allocations` is append-only: corrections are new rows, never edits.

CREATE TABLE IF NOT EXISTS customers (
    customer_id     TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    virtual_account TEXT NOT NULL UNIQUE,
    bank_account    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS invoices (
    invoice_id   TEXT PRIMARY KEY,
    customer_id  TEXT NOT NULL REFERENCES customers(customer_id),
    amount_paise INTEGER NOT NULL,
    issue_date   TEXT NOT NULL,
    due_date     TEXT NOT NULL,
    gst_period   TEXT NOT NULL,
    disputed     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS payments (
    payment_id      TEXT PRIMARY KEY,
    amount_paise    INTEGER NOT NULL,
    method           TEXT NOT NULL,
    virtual_account TEXT,
    payer_name      TEXT,
    utr             TEXT,
    narration       TEXT,
    created_at      TEXT NOT NULL
);

-- Append-only ledger of allocations. Invoice balance is always computed as
-- invoice.amount_paise - SUM(allocations.amount_paise) for that invoice,
-- using every row regardless of entry_type -- a write-off legitimately
-- closes an invoice even though no cash covered it.
--
-- entry_type is a separate, bank-level check: CASH rows are real money:
-- SUM(allocations.amount_paise WHERE entry_type='CASH') must equal
-- SUM(payments.amount_paise) exactly. ADJUSTMENT rows (tolerance
-- write-offs, residual deductions) exist only to zero out an invoice
-- balance and must never be counted as received cash.
CREATE TABLE IF NOT EXISTS allocations (
    allocation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id    TEXT NOT NULL REFERENCES payments(payment_id),
    invoice_id    TEXT REFERENCES invoices(invoice_id),
    amount_paise  INTEGER NOT NULL,
    method        TEXT NOT NULL,
    reason_code   TEXT NOT NULL,
    entry_type    TEXT NOT NULL,
    confidence    INTEGER NOT NULL,
    rationale     TEXT NOT NULL,
    -- 1 if this row was produced by the re-queue loop (a later payment
    -- changed invoice state enough to resolve what was genuinely
    -- unresolvable on the forward pass), 0 if resolved on first pass.
    resolved_on_requeue INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_alloc_payment ON allocations(payment_id);
CREATE INDEX IF NOT EXISTS idx_alloc_invoice ON allocations(invoice_id);
CREATE INDEX IF NOT EXISTS idx_invoice_customer ON invoices(customer_id);
