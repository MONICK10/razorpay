# AI Finance Controller

**Razorpay Buildathon &middot; Track 04**

An agent that matches incoming bank payments to open invoices, reports a
measured match rate, and refuses to guess when it cannot prove a match.

> Scope: allocation only. Not collections, not forecasting, not dunning.
> See [`PRD (1).md`](<PRD (1).md>) for the full spec this implements.

## Results (current run)

| Metric | Value |
|---|---|
| Match rate vs ground truth | **100.0%** (64/64 payments, all 11 reason codes) |
| Auto-resolution rate | **75.0%** |
| Human touches | **11** (of 16 raw exceptions, grouped) |
| Value requiring review | Rs 3,74,475.00 |
| Precision at default dial (85) | **100.0%** |
| Resolved on re-queue | **2** &mdash; genuinely unresolvable on arrival, closed by a later payment |
| Customer status accuracy | **100.0%** (CLEAN / PARTIAL / PROVISIONAL) |
| Customer balance accuracy | **100.0%** &mdash; including for PROVISIONAL customers |

Industry benchmark for straight-through cash application is roughly
85-95%. This run's auto-resolution rate (75.0%) sits below that band by
design: the synthetic dataset is deliberately adversarial (garbled
references, duplicates, disputed ties, unidentifiable payers) so every
layer of the engine gets exercised. The **confidence threshold curve**
below is the real answer to "where do we actually land" &mdash; it shows
auto-matched vs. human-touches vs. wrong-matches across every dial
setting, not just one.

17 customers hold those 61 invoices (avg 3.6/customer, 12 customers with
3+ simultaneously open) &mdash; not 45 customers each holding ~1, which
made every match in an earlier version of this dataset structurally
trivial and starved the hard cases (`AMBIG-N`, `FIFO-TIE`, `PROVISIONAL`)
of the multi-invoice conditions that actually create them.

The "resolved on re-queue" row is the re-queue rule proven in the
**actual submission run**, not only in `tests/test_requeue.py`'s
standalone scenario: two `resolution_group` sequences are planted in
`engine/generate_data.py` where an early payment is genuinely `AMBIG-N`
on arrival (three financially-identical open invoices, no unique pair)
and a later payment in the same batch closes the tie, so the re-queue
loop resolves the first one without anyone touching it again.

The last two rows are the other half of the argument. A handful of
customers in this dataset get payments that never individually match any
invoice, so every one of those payments is correctly `NO-MATCH` &mdash;
that's not a failure, it's a refusal to guess. But their **balance** is
still provable by arithmetic (invoiced minus received), independent of
which invoice it will eventually settle. See "Two-tier report" below and
in the dashboard.

Full dashboard: run `python -m report.generate_report` and open
[`report/report.html`](report/report.html), or see `out/results.json`
for the raw numbers (reason-code breakdown, exception queue, full
threshold curve, customer-status accuracy).

### Held-out validation

A 100% match rate against `ground_truth.csv` alone is a tautology: the
data was built to the same 12-code spec the engine is written against.
`engine/generate_data.py --mode holdout` builds a second, six-payment
dataset out of inputs that spec never enumerated &mdash; a phantom
invoice reference, a 2x overpayment, near-identical payer names, a
refund, a payment predating its own invoice's issue date, and a
narration naming two conflicting invoices &mdash; scored separately.

| Metric | Submission | Holdout |
|---|---|---|
| Match rate | **100.0%** | **66.7%** |
| Auto-resolution rate | 75.0% | 66.7% |

Four of six holdout categories pass outright (two because of engine
fixes made alongside this dataset: a single reason code on an
overpayment, and refusing non-positive amounts rather than corrupting an
invoice balance). Two are **documented misses** &mdash; the engine has no
temporal check and no conflicting-reference check, so it confidently
returns the wrong answer. `out/results_holdout.json`'s `mismatches` field
(and the dashboard's "Held-out validation" section) explains both.

```bash
python -m engine.generate_data --mode holdout
python -m engine.run_engine --suffix _holdout
python -m scoring.score --suffix _holdout
python -m scoring.compare      # side-by-side table + explained mismatches
```

## Architecture

```mermaid
flowchart TD
    P[Payment] --> L0{{"L0 Identity<br/>virtual account -> customer"}}
    L0 -->|no customer| SUS[["SUSPENSE"]]
    L0 -->|customer known| L1{{"L1 Composite key<br/>exact reference in narration"}}
    L1 -->|exact ref, exact amount| EXACT[["EXACT"]]
    L1 -->|exact ref, amount off| L2{{"L2 Tolerance"}}
    L2 -->|gap &le; tolerance| TOLFEE[["TOL-FEE"]]
    L2 -->|deduction keyword| RESID[["RESID-DED"]]
    L2 -->|else| PART[["PART-EXP"]]
    L1 -->|2+ refs, sum matches| BULKR[["BULK-N (explicit)"]]
    L1 -->|garbled/no ref| L3{{"L3 AI<br/>repair reference / shortlist"}}
    L3 -->|repaired, unique| REFFUZZY[["REF-FUZZY"]]
    L3 -->|declines| L4{{"L4 Policy<br/>FIFO / duplicate / subset-sum"}}
    L4 -->|duplicate of settled invoice| DUP[["DUP-ONACC"]]
    L4 -->|neutral tie| FIFO[["FIFO-TIE"]]
    L4 -->|unique amount combo, any size| BULKA[["BULK-N (amount-only)"]]
    L4 -->|disputed tie / non-unique / nothing fits| L5[["L5 Exception queue<br/>AMBIG-N / NO-MATCH"]]
    L5 -.re-queue on every later allocation.-> L4

    EXACT & TOLFEE & RESID & PART & BULKR & REFFUZZY & FIFO & BULKA & DUP --> LEDGER[("allocations<br/>append-only")]
    SUS --> LEDGER
    L5 --> LEDGER
    L5 -.aggregated per customer, cash never posted.-> L6{{"Customer rollup<br/>engine/customer_status.py"}}
    LEDGER -.invoice balances.-> L6
    L6 --> STATUS[["customer_status:<br/>CLEAN / PARTIAL / PROVISIONAL"]]
```

Deterministic rules (L0, L1, L2, L4, L5) handle every payment where the
proof is exact. The AI layer (L3) is called only on the tail &mdash;
garbled references and shortlist ties &mdash; is given a closed shortlist
it must choose from, and is explicitly allowed to answer "I don't know."
Confidence is **computed from countable signals** (`engine/confidence.py`),
never taken from the model's self-report.

**A unique covering combination is always `BULK-N`,** even when it happens
to be every open invoice a customer has, and even when those invoices
share an amount: every invoice in the combination reaches zero balance
either way, so there's no attribution left to assume. That case is fully
determined, not provisional.

A customer-level rollup (L6, `engine/customer_status.py`) runs after the
payment-level pipeline and answers a different question: not "what
happened to this payment" but "how much of this customer's picture is
provable." See **Two-tier report** below.

## Reason codes

| Code | Meaning |
|---|---|
| `EXACT` | Reference and amount both agree |
| `TOL-FEE` | Shortfall inside tolerance; difference written off to bank charges |
| `PART-EXP` | Partial payment; more expected; invoice stays open |
| `RESID-DED` | Short-pay treated as a deduction; invoice cleared, residual raised |
| `BULK-N` | One payment covering N invoices (explicit reference or unique amount subset-sum) |
| `REF-FUZZY` | Reference repaired (typo, truncation, casing, dropped separator) |
| `FIFO-TIE` | Candidates financially identical and undisputed; policy applied |
| `DUP-ONACC` | Duplicate payment; parked as customer advance |
| `SUSPENSE` | Payer unidentifiable (virtual account maps to no customer) |
| `AMBIG-N` | Payer known, N candidates, none decidable (e.g. a disputed tie) |
| `NO-MATCH` | Payer known, nothing fits |

`SUSPENSE` and `NO-MATCH` are different failures: one is "who?", the
other is "which?".

Note what's *not* in this table: `PROVISIONAL` is deliberately not a
payment-level code. It answers a different question than the other
eleven ("how sure are we of this customer's invoice split?" rather than
"what happened to this payment?"), so it lives one level up &mdash; see
below.

## Two-tier report: customer status

Every payment still exits with exactly one of the eleven codes above.
Separately, after the payment-level pipeline runs, each **customer** gets
rolled up (`engine/customer_status.py`) into one of three states:

| Status | Meaning |
|---|---|
| `CLEAN` | Open ledger balance is zero |
| `PARTIAL` | Balance &gt; 0, but fully explained by real invoice-level matches |
| `PROVISIONAL` | Balance &gt; 0 **and** some received cash was never tied to a specific invoice (`AMBIG-N` / `NO-MATCH` payments) |

The arithmetic:

```
open_balance_ledger = sum of each invoice's (amount - allocations posted to it)
unmatched_cash      = sum of AMBIG-N / NO-MATCH payment amounts for that customer
balance              = max(0, open_balance_ledger - unmatched_cash)
```

`balance` is provable by subtraction, regardless of whether any single
payment resolved. A customer can be `PROVISIONAL` &mdash; certain balance,
unproven split &mdash; while every one of their payments is individually,
correctly, `NO-MATCH`. That's not a contradiction; it's the whole point of
reporting at two levels instead of one. The dashboard's "assumed split"
for a `PROVISIONAL` customer is a FIFO waterfall suggestion for display
only (`engine/l4_policy.py:waterfall_split`) &mdash; it is never written to
the `allocations` ledger, because it isn't proven.

`ground_truth_customers.csv` (alongside `ground_truth.csv`) carries the
expected status and balance per customer, computed the same way from
construction intent, not by running the engine &mdash; and scored the same
way (`engine/customer_status.rollup`) whether the input is the
generator's intent or the engine's actual output, so the two definitions
of "provisional" can't drift apart.

## Project layout

```
engine/             deterministic engine + AI layer (Phases 1-3)
  generate_data.py    synthetic data generator + ground truth (Phase 1);
                        --mode submission (default, 64 payments/61 invoices/
                        17 customers) or --mode holdout (6 messy, off-spec
                        cases, see "Held-out validation" above)
  schema.sql, db.py   SQLite schema (paise-integer money, append-only allocations)
  l0_identity.py      L0: virtual account -> customer
  l1_composite.py     L1: exact reference matching (single + explicit multi)
  reference.py        token extraction, normalization, fuzzy repair
  l2_tolerance.py      L2: tolerance band + deduction-keyword detection
  l3_ai.py, ai_client.py  L3: Claude API (claude-opus-5) with a deterministic
                        stub fallback -- same interface either way
  l4_policy.py        L4: FIFO tie-break, subset-sum search, waterfall split
  l5_exceptions.py    L5: exception queue grouping + ranking by value;
                        human_touch_groups() -- shared by scoring/score.py
                        and the simulator so "human touches" can't drift
  l6_action.py         L6: proposed next action per exception (simulator only)
  pipeline.py         orchestrator: forward pass + re-queue fixed-point loop
  customer_status.py  L6: customer-level rollup (CLEAN/PARTIAL/PROVISIONAL)
  run_engine.py        entry point; --suffix _holdout for the holdout db
scoring/
  score.py             Phase 4: payment match rate + customer status/balance
                         accuracy, human touches, threshold curve; --suffix _holdout
  compare.py           submission vs. holdout side-by-side + explained mismatches
report/generate_report.py   Phase 5: static HTML dashboard (no Next.js/build step);
                       renders a "Held-out validation" section when
                       out/results_holdout.json exists
simulator/           interactive decision-trace demo (FastAPI + vanilla JS) --
                       same engine.pipeline.resolve_payment() call as batch mode;
                       run with `python -m simulator`
tests/               pytest: reference repair, customer-status rollup,
  accounting-integrity (CASH vs ADJUSTMENT), the re-queue mechanism (both
  a standalone scenario and the submission run itself), end-to-end
  regression guard, holdout-scores-lower-than-submission guard
out/                 generated: finance(_holdout).db, ground_truth(_holdout).csv,
                       ground_truth_customers(_holdout).csv, results(_holdout).json
```

## Running it

```bash
pip install -r requirements.txt

python -m engine.generate_data      # Phase 1: 64 payments, 61 invoices, ground truth
python -m engine.run_engine         # Phase 2/3: deterministic engine + AI layer
python -m scoring.score             # Phase 4: match rate, human touches, threshold curve
python -m report.generate_report    # Phase 5: report/report.html

python -m pytest tests/ -v

# Held-out validation (see above)
python -m engine.generate_data --mode holdout
python -m engine.run_engine --suffix _holdout
python -m scoring.score --suffix _holdout
python -m scoring.compare

# Interactive simulator (decision trace, play mode, actions, before/after toggle)
python -m simulator      # http://127.0.0.1:8000
```

The AI layer runs against `claude-opus-5` when `ANTHROPIC_API_KEY` (or
another SDK-recognized credential) is present, and falls back to a
deterministic heuristic otherwise -- reference normalization/fuzzy-match
for repair, and a "never override a dispute" rule for shortlist choice.
Both paths are held to the same contract: return an answer or return
`None`, never a guess. A transient API error also falls back to the
stub rather than crashing the pipeline.

## Design principle

> The AI never touches money it can verify. It only reasons about the tail.

- Money is always integer paise, never floats.
- `allocations` is append-only. A resolution is written to the ledger
  only once it is final (resolved or permanently parked) -- nothing is
  superseded or edited.
- Confidence is computed by the engine from reference/amount/candidate
  signals (`engine/confidence.py`), not asked of the model.
- The confidence auto-post threshold is a dial the finance team controls
  (default 85), separate from whether a payment resolved at all.

## Prior art

| Source | Idea taken |
|---|---|
| SAP S/4HANA | Tolerance groups; partial vs. residual item; reason codes |
| US healthcare X12 835 | Standardized reason-code vocabulary; payments/claims as many-to-many |
| Securities reconciliation | Composite match keys; typed breaks routed by type; never force-match |
| Telecom revenue assurance | Continuous reconciliation; prioritize by financial impact |
| Cash-application vendors | Configurable auto-clear threshold; append-only audit record |

## Out of scope

Collections, dunning, forecasting, multi-currency, OCR of remittance
PDFs, live Razorpay API integration, authentication, multi-tenant.
