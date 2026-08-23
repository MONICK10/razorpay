# PRD — AI Finance Controller

**Razorpay Buildathon · Track 04**
**One-line:** An agent that matches incoming bank payments to open invoices, reports a measured match rate, and refuses to guess when it cannot prove a match.

---

## 1. The problem

Razorpay's Smart Collect gives each customer a unique virtual account number. When money arrives, the merchant knows **who** paid.

Nobody knows **which invoice** it pays.

A merchant with 500 open invoices gets a bank credit of ₹6,000 from a customer who has three ₹6,000 bills open. Identity is solved. Allocation is not. A finance clerk resolves this by hand, every day.

**Scope of this project:** allocation only. Not collections, not forecasting, not dunning.

---

## 2. What the brief requires

| Requirement | How we meet it |
|---|---|
| Close ONE finance-ops loop | Payment in → allocated → invoice balance updated → report out |
| 50+ records of synthetic data | 60 payments, 55 invoices |
| Report a match rate | Computed against a generated ground-truth file |
| Report unresolved exceptions | Typed exception queue, ranked by value |

---

## 3. Core design principle

> **The AI never touches money it can verify. It only reasons about the tail.**

Deterministic rules handle every payment where the proof is exact. The LLM is called only for leftovers, is given a shortlist it must choose from, and is allowed to answer "I don't know."

Confidence is **computed by us from countable signals**, never taken from the model's own self-report.

---

## 4. Architecture — six layers

| Layer | Job | Technique |
|---|---|---|
| L0 Identity | Virtual account → customer | Lookup |
| L1 Composite key | VA + amount + date window + reference must all agree | Deterministic |
| L2 Tolerance | Gap ≤ ₹100 or ≤0.5% → auto-clear, post difference to bank charges | Deterministic |
| L3 AI | Repair garbled references; choose among a shortlist | LLM, constrained |
| L4 Policy | FIFO on neutral ties; duplicates → on-account; unidentified → suspense | Deterministic |
| L5 Exception queue | Typed, grouped, ranked by rupees | Deterministic |

**Re-queue rule:** after every successful allocation, re-evaluate the exception queue. An exception may become solvable when later payments arrive.

---

## 5. Reason code vocabulary

Every payment exits with exactly one code. No record is ever "unknown".

| Code | Meaning |
|---|---|
| `EXACT` | Reference and amount both agree |
| `TOL-FEE` | Shortfall inside tolerance; difference written off |
| `PART-EXP` | Partial payment; more expected; invoice stays open |
| `RESID-DED` | Short-pay treated as a deduction; invoice cleared, residual raised |
| `BULK-N` | One payment covering N invoices |
| `REF-FUZZY` | Reference repaired (typo, truncation, casing) |
| `FIFO-TIE` | Candidates financially identical; policy applied |
| `DUP-ONACC` | Duplicate payment; parked as customer advance |
| `SUSPENSE` | Payer unidentifiable; parked in suspense account |
| `AMBIG-N` | Payer known, N candidates, none decidable |
| `NO-MATCH` | Payer known, nothing fits |
| `PROVISIONAL` | Customer balance certain, invoice-level split assumed |

`SUSPENSE` and `NO-MATCH` are different failures: one is "who?", the other is "which?".

---

## 6. Confidence scoring

Computed, not asked for.

| Signal | Points |
|---|---|
| Reference matches exactly | +50 |
| Reference matches after repair | +30 |
| Amount matches to the paise | +30 |
| Amount within tolerance | +15 |
| Exactly 1 candidate invoice | +20 |
| 2 candidates | 0 |
| 3 or more candidates | −20 |
| Payer name matches customer record | +10 |

Auto-post threshold is a configurable dial. Default 85.

---

## 7. Data model

```
customers      customer_id PK, name, virtual_account, bank_account
invoices       invoice_id PK, customer_id FK, amount_paise, issue_date,
               due_date, gst_period, disputed
payments       payment_id PK, amount_paise, method, virtual_account,
               payer_name, utr, narration, created_at
allocations    allocation_id PK, payment_id FK, invoice_id FK,
               amount_paise, method, reason_code, confidence, rationale
```

- **No foreign key exists between `payments` and `invoices`.** That absence is the problem. `allocations` is the table we build.
- Many-to-many is expected: one payment can settle many invoices; one invoice can take many payments.
- All money in **paise, integers**. Never floats.
- `allocations` is **append-only**. Corrections are new rows, never edits. This is the audit trail.

Invoice balance is computed, never stored:
`balance = invoice.amount - SUM(allocations.amount_allocated)`

---

## 8. Reported metrics

| Metric | Why |
|---|---|
| Auto-resolution rate | The headline |
| **Human touches** | The real proof of usefulness — 20 exceptions can be 3 decisions |
| Value requiring review | Rupee view, not row count |
| Precision on auto-posted matches | Of what we posted automatically, how much was correct |
| Threshold trade-off curve | Auto-matched vs human touches vs wrong matches, across dial settings |

Industry benchmark for straight-through matching is roughly 85–95%. State where we land against it.

---

## 9. Prior art we borrow from (state this openly)

| Source | Idea taken |
|---|---|
| SAP S/4HANA | Tolerance groups; partial payment vs residual item; reason codes; automatic payment notification to customer |
| US healthcare X12 835 | Standardised reason-code vocabulary; formal acceptance that payments and claims are many-to-many; provider-level adjustments as a separate bucket |
| Securities reconciliation | Composite match keys; typed breaks routed by type; never force-match |
| Telecom revenue assurance | Continuous rather than month-end; prioritise by financial impact |
| Cash-application vendors | Configurable auto-clear confidence threshold; append-only audit record |

**What is ours:** a confidence dial the finance team controls rather than the model, and a two-tier report separating what is certain (customer balance) from what is provisional (invoice split).

---

## 10. Out of scope

Collections, dunning, forecasting, multi-currency, OCR of remittance PDFs, live Razorpay API integration, authentication, multi-tenant.

---

## 11. Phases

| Phase | Deliverable | Exit test |
|---|---|---|
| 1 | Data generator + ground truth | `ground_truth.csv` has one row per payment, all 12 codes represented |
| 2 | Deterministic engine — L0, L1, L2, L4, L5 | Runs end to end with zero AI; every payment exits with a reason code |
| 3 | AI layer — L3 + re-queue loop | Reference repair and shortlist reasoning; exceptions re-checked after each allocation |
| 4 | Scoring harness + confidence dial curve | Match rate, human touches, and the threshold trade-off table all computed against ground truth |
| 5 | Report UI | Dashboard showing metrics, allocations, and the typed exception queue |
| 6 | Ship | README, architecture diagram, public repo, 5-minute pitch video |

### Priority order

**Phases 1–4 are the submission.** They alone satisfy every line of the brief: synthetic batch, closed loop, measured match rate, unresolved exceptions.

Phase 2 must be complete and correct before Phase 3 begins. A working deterministic engine with no AI is a valid submission. An AI layer sitting on a broken engine is not.

### If time collapses

Cut in this order:

1. Phase 5 becomes a static HTML report generated by Python — no Next.js, no React.
2. Phase 3 narrows to reference repair only; ambiguity handling falls back to `AMBIG-N` exceptions.
3. Phase 1 drops to the minimum 50 records.

Never cut Phase 4. The measured match rate and the dial curve are the entire argument of the submission.

---

## 12. Stack

- Engine: Python 3.11, standard library + `pydantic`
- Storage: SQLite (single file, no server)
- AI: one LLM API call per ambiguous payment, strict JSON response
- Dashboard: Next.js reading the engine's `results.json`
- No auth, no deployment, no Docker
