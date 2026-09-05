"""Phase 1 -- synthetic data generator + ground truth.

Every payment in this dataset is *constructed* to exercise one specific
reason code. That is what lets ground_truth.csv be exact: we know the
intended outcome because we built the payment to produce it, not because we
guessed at it after the fact.

Run: python -m engine.generate_data
Produces: out/finance.db, out/ground_truth.csv
"""
from __future__ import annotations

import csv
import random
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from itertools import combinations
from pathlib import Path

from engine import customer_status
from engine.db import init_db

ANCHOR = date(2026, 7, 20)  # fixed "today" for this synthetic world
OUT_DIR = Path(__file__).parent.parent / "out"
RNG = random.Random(42)

FIRST_NAMES = ["Acme", "Bluepeak", "Coastal", "Deccan", "Everest", "Falcon",
               "Ganges", "Horizon", "Indus", "Jaipur", "Kavya", "Lotus",
               "Meridian", "Nimbus", "Orbit", "Pallavi", "Quartz", "Ridge",
               "Saffron", "Tandem", "Udaan", "Vertex", "Windward", "Yashika"]
SUFFIXES = ["Traders", "Textiles", "Logistics", "Enterprises", "Foods",
            "Pvt Ltd", "Industries", "Retail", "Solutions", "Exports"]
METHODS = ["NEFT", "RTGS", "UPI", "IMPS"]


def d(offset_days: int) -> str:
    return (ANCHOR + timedelta(days=offset_days)).isoformat()


def dt(offset_days: int, hour: int = 10, minute: int = 0) -> str:
    base = datetime.combine(ANCHOR + timedelta(days=offset_days),
                             datetime.min.time())
    return base.replace(hour=hour, minute=minute).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class World:
    customers: list = field(default_factory=list)
    invoices: list = field(default_factory=list)
    payments: list = field(default_factory=list)
    ground_truth: list = field(default_factory=list)
    _cust_seq: int = 0
    _inv_seq: int = 0
    _pay_seq: int = 0

    def add_customer(self, name: str | None = None) -> dict:
        self._cust_seq += 1
        cid = f"CUST-{self._cust_seq:03d}"
        name = name or f"{RNG.choice(FIRST_NAMES)} {RNG.choice(SUFFIXES)}"
        cust = {
            "customer_id": cid,
            "name": name,
            "virtual_account": f"RZPVA{self._cust_seq:06d}",
            "bank_account": f"{RNG.randint(10**11, 10**12 - 1)}",
        }
        self.customers.append(cust)
        return cust

    def add_invoice(self, customer_id: str, amount_paise: int,
                     issue_offset: int, due_offset: int,
                     disputed: bool = False) -> dict:
        self._inv_seq += 1
        iid = f"INV-{self._inv_seq:04d}"
        inv = {
            "invoice_id": iid,
            "customer_id": customer_id,
            "amount_paise": amount_paise,
            "issue_date": d(issue_offset),
            "due_date": d(due_offset),
            "gst_period": ANCHOR.strftime("%Y-%m"),
            "disputed": int(disputed),
        }
        self.invoices.append(inv)
        return inv

    def add_payment(self, amount_paise: int, virtual_account: str | None,
                     payer_name: str, narration: str,
                     created_offset: int, method: str | None = None,
                     utr: str | None = None) -> dict:
        self._pay_seq += 1
        pid = f"PAY-{self._pay_seq:04d}"
        pay = {
            "payment_id": pid,
            "amount_paise": amount_paise,
            "method": method or RNG.choice(METHODS),
            "virtual_account": virtual_account,
            "payer_name": payer_name,
            "utr": utr or f"UTR{RNG.randint(10**9, 10**10 - 1)}",
            "narration": narration,
            "created_at": dt(created_offset,
                              hour=RNG.randint(9, 18),
                              minute=RNG.randint(0, 59)),
        }
        self.payments.append(pay)
        return pay

    def add_gt(self, payment_id: str, reason_code: str,
               invoice_ids: list[str], notes: str = "") -> None:
        self.ground_truth.append({
            "payment_id": payment_id,
            "expected_reason_code": reason_code,
            "expected_invoice_ids": ";".join(invoice_ids),
            "notes": notes,
        })


def rupees(x: float) -> int:
    """Convert rupees to integer paise."""
    return round(x * 100)


def build_world() -> World:
    w = World()

    # A pool of 8 shared customers for the *reference-based* categories
    # below (EXACT, TOL-FEE, PART-EXP, RESID-DED, REF-FUZZY, explicit
    # BULK-N): these all match by exact invoice-ID string in the
    # narration, never by amount, so co-locating unrelated invoices under
    # one customer can't cause a false match -- it just gives that
    # customer more than one open invoice, which a 45-customer/1.2-
    # invoices-each dataset never did. Amount-sensitive categories
    # (FIFO-TIE, AMBIG-N, amount-only BULK-N, pooled NO-MATCH) stay on
    # their own dedicated customers below, unchanged -- sharing those
    # risks accidental subset-sum/amount collisions their own
    # collision-avoidance asserts guard against.
    customer_pool = [w.add_customer() for _ in range(8)]
    _pool_idx = 0

    def next_pool_customer() -> dict:
        nonlocal _pool_idx
        cust = customer_pool[_pool_idx % len(customer_pool)]
        _pool_idx += 1
        return cust

    # ---- 1. EXACT (8) ----------------------------------------------------
    exact_pairs = []  # (customer, invoice) reused later by DUP-ONACC
    for i in range(8):
        cust = next_pool_customer()
        amt = rupees(RNG.randint(5_000, 250_000))
        inv = w.add_invoice(cust["customer_id"], amt,
                             issue_offset=-30 - i, due_offset=-5 - i)
        pay = w.add_payment(
            amt, cust["virtual_account"], cust["name"],
            f"NEFT/{cust['name']}/{inv['invoice_id']} SETTLEMENT",
            created_offset=-20 - i)
        w.add_gt(pay["payment_id"], "EXACT", [inv["invoice_id"]])
        exact_pairs.append((cust, inv))

    # ---- 2. TOL-FEE (6) ----------------------------------------------------
    for i in range(6):
        cust = next_pool_customer()
        amt = rupees(RNG.randint(10_000, 200_000))
        # gap within tolerance: min(Rs 100, 0.5%) is generous end, use a gap
        # that satisfies gap <= max(10000 paise, 0.5% of amount)
        tolerance = max(10000, round(amt * 0.005))
        gap = RNG.randint(50, tolerance)
        inv = w.add_invoice(cust["customer_id"], amt,
                             issue_offset=-20 - i, due_offset=-3 - i)
        pay = w.add_payment(
            amt - gap, cust["virtual_account"], cust["name"],
            f"UPI/{cust['name']}/{inv['invoice_id']}",
            created_offset=-4 - i)
        w.add_gt(pay["payment_id"], "TOL-FEE", [inv["invoice_id"]],
                  notes=f"gap={gap}p bank charges write-off")

    # ---- 3. PART-EXP (6) ----------------------------------------------------
    for i in range(6):
        cust = next_pool_customer()
        amt = rupees(RNG.randint(40_000, 300_000))
        paid_fraction = RNG.uniform(0.35, 0.65)
        inv = w.add_invoice(cust["customer_id"], amt,
                             issue_offset=-15 - i, due_offset=10 - i)
        pay = w.add_payment(
            round(amt * paid_fraction), cust["virtual_account"], cust["name"],
            f"NEFT/{cust['name']}/{inv['invoice_id']} PART PAYMENT",
            created_offset=-3 - i)
        w.add_gt(pay["payment_id"], "PART-EXP", [inv["invoice_id"]],
                  notes="partial, more expected, invoice stays open")

    # ---- 4. RESID-DED (6) ----------------------------------------------------
    for i in range(6):
        cust = next_pool_customer()
        amt = rupees(RNG.randint(30_000, 180_000))
        ded_fraction = RNG.uniform(0.02, 0.08)  # e.g. TDS-style deduction
        inv = w.add_invoice(cust["customer_id"], amt,
                             issue_offset=-25 - i, due_offset=-8 - i)
        pay = w.add_payment(
            round(amt * (1 - ded_fraction)), cust["virtual_account"],
            cust["name"],
            f"RTGS/{cust['name']}/{inv['invoice_id']} NET OF TDS DEDUCTION",
            created_offset=-6 - i)
        w.add_gt(pay["payment_id"], "RESID-DED", [inv["invoice_id"]],
                  notes="short-pay treated as deduction, invoice cleared")

    # ---- 5. BULK-N (2): 1 explicit multi-ref + 1 amount-only subset-sum --
    # The explicit-reference one is reference-based (shares the pool);
    # the amount-only one is amount-sensitive (stays dedicated below).
    for i in range(1):
        cust = next_pool_customer()
        n = 2
        invs = []
        total = 0
        for j in range(n):
            amt = rupees(RNG.randint(8_000, 60_000))
            inv = w.add_invoice(cust["customer_id"], amt,
                                 issue_offset=-18 - j, due_offset=-2 - j)
            invs.append(inv)
            total += amt
        ref = " & ".join(v["invoice_id"] for v in invs)
        pay = w.add_payment(
            total, cust["virtual_account"], cust["name"],
            f"NEFT/{cust['name']}/SETTLEMENT FOR {ref}",
            created_offset=-2 - i)
        w.add_gt(pay["payment_id"], "BULK-N",
                  [v["invoice_id"] for v in invs],
                  notes="explicit multi-invoice reference")

    for i in range(1):
        cust = w.add_customer()  # amount-only subset-sum -- dedicated, not shared
        n = 2
        invs = []
        total = 0
        for j in range(n):
            amt = rupees(RNG.randint(8_000, 60_000))
            inv = w.add_invoice(cust["customer_id"], amt,
                                 issue_offset=-18 - j, due_offset=-2 - j)
            invs.append(inv)
            total += amt
        pay = w.add_payment(
            total, cust["virtual_account"], cust["name"],
            f"UPI/{cust['name']} BULK SETTLEMENT",
            created_offset=-3 - i)
        w.add_gt(pay["payment_id"], "BULK-N",
                  [v["invoice_id"] for v in invs],
                  notes="amount-only subset-sum match, unique combination")

    # ---- 6. REF-FUZZY (6) ----------------------------------------------------
    def corrupt(ref: str, kind: int) -> str:
        if kind == 0:
            return ref.replace("-", "")  # dropped separator
        if kind == 1:
            return ref.lower()  # case
        if kind == 2:
            return ref[:-1]  # truncated
        if kind == 3:
            return ref.replace("0", "O", 1)  # O/0 confusion
        return ref + "X"  # trailing garbage

    for i in range(6):
        cust = next_pool_customer()
        amt = rupees(RNG.randint(5_000, 150_000))
        inv = w.add_invoice(cust["customer_id"], amt,
                             issue_offset=-22 - i, due_offset=-4 - i)
        bad_ref = corrupt(inv["invoice_id"], i % 5)
        pay = w.add_payment(
            amt, cust["virtual_account"], cust["name"],
            f"IMPS/{cust['name']}/{bad_ref}",
            created_offset=-2 - i)
        w.add_gt(pay["payment_id"], "REF-FUZZY", [inv["invoice_id"]],
                  notes=f"corrupted ref '{bad_ref}' repaired by L3")

    # ---- 7. FIFO-TIE (2): identical-amount open invoices, no reference ---
    for i in range(2):
        cust = w.add_customer()
        n = 2
        amt = rupees(RNG.randint(15_000, 90_000))
        invs = []
        for j in range(n):
            inv = w.add_invoice(cust["customer_id"], amt,
                                 issue_offset=-40 + j * 5, due_offset=-10 + j * 5)
            invs.append(inv)
        pay = w.add_payment(
            amt, cust["virtual_account"], cust["name"],
            f"NEFT/{cust['name']} PAYMENT RECEIVED",
            created_offset=-1 - i)
        oldest = min(invs, key=lambda v: v["issue_date"])
        w.add_gt(pay["payment_id"], "FIFO-TIE", [oldest["invoice_id"]],
                  notes=f"{n} financially-identical candidates, FIFO -> oldest")

    # ---- 8. DUP-ONACC (8): duplicate of an already-settled EXACT payment -
    # Reuses all 8 EXACT (customer, invoice) pairs, whose original payment
    # was dated well in the past (created_offset -20..-27), so the
    # duplicate below -- dated recently -- is processed after it and finds
    # the invoice already fully allocated.
    for i, (cust, inv) in enumerate(exact_pairs):
        amt = inv["amount_paise"]
        dup = w.add_payment(
            amt, cust["virtual_account"], cust["name"],
            f"NEFT/{cust['name']}/{inv['invoice_id']} DUPLICATE RETRY",
            created_offset=-1 - i)
        w.add_gt(dup["payment_id"], "DUP-ONACC", [],
                  notes="duplicate of already fully-allocated invoice")

    # ---- 9. SUSPENSE (7): unidentifiable payer ---------------------------
    for i in range(7):
        amt = rupees(RNG.randint(5_000, 80_000))
        pay = w.add_payment(
            amt, virtual_account=f"UNKNOWNVA{i:03d}",
            payer_name=f"{RNG.choice(FIRST_NAMES)} {RNG.choice(SUFFIXES)}",
            narration="NEFT/UNMAPPED TRANSFER",
            created_offset=-5 - i)
        w.add_gt(pay["payment_id"], "SUSPENSE", [],
                  notes="virtual account does not map to any customer")

    # ---- 10. AMBIG-N (2): payer known, one tied candidate is disputed ---
    for i in range(2):
        cust = w.add_customer()
        amt = rupees(RNG.randint(20_000, 100_000))
        inv_a = w.add_invoice(cust["customer_id"], amt,
                               issue_offset=-35, due_offset=-15,
                               disputed=False)
        inv_b = w.add_invoice(cust["customer_id"], amt,
                               issue_offset=-28, due_offset=-8,
                               disputed=True)
        pay = w.add_payment(
            amt, cust["virtual_account"], cust["name"],
            f"NEFT/{cust['name']} SETTLEMENT",
            created_offset=-2 - i)
        w.add_gt(pay["payment_id"], "AMBIG-N", [],
                  notes=f"tied candidates {inv_a['invoice_id']}/"
                        f"{inv_b['invoice_id']}, one disputed, cannot auto-pick")

    # ---- 11. NO-MATCH, pooled (7 across 2 customers): payer known, amount
    # fits nothing -- individually or in combination -- for either customer.
    # Every payment here is, on its own, an ordinary NO-MATCH. What makes
    # these two customers interesting is the *aggregate*: PROVISIONAL is a
    # customer-level state (see engine/customer_status.py), not a payment
    # code, so it can only be demonstrated by payments that are each
    # correctly NO-MATCH while the customer's total still nets out.
    def gen_pooled_no_match(n_invoices: int, n_payments: int) -> None:
        cust = w.add_customer()
        invs = []
        for j in range(n_invoices):
            amt = rupees(RNG.randint(8_000, 40_000))
            inv = w.add_invoice(cust["customer_id"], amt,
                                 issue_offset=-40 + j * 4, due_offset=-15 + j * 4)
            invs.append(inv)
        amounts = [v["amount_paise"] for v in invs]
        subset_sums = {sum(c) for r in range(1, len(amounts) + 1)
                        for c in combinations(amounts, r)}
        payment_amounts: list[int] = []
        for _ in range(n_payments):
            for _attempt in range(1000):
                candidate = rupees(RNG.randint(3_000, 12_000))
                if candidate not in subset_sums and candidate not in payment_amounts:
                    payment_amounts.append(candidate)
                    break
            else:
                raise RuntimeError(
                    "could not find a collision-free payment amount after "
                    "1000 attempts -- widen the ranges")
        for k, amt in enumerate(payment_amounts):
            pay = w.add_payment(
                amt, cust["virtual_account"], cust["name"],
                f"NEFT/{cust['name']} PART PAYMENT {k + 1}",
                created_offset=-10 + k * 2)
            w.add_gt(pay["payment_id"], "NO-MATCH", [],
                      notes="payer known, amount matches no invoice or "
                            "combination -- contributes to this customer's "
                            "provisional balance, see ground_truth_customers.csv")
        # sanity: every payment really is unmatchable, individually AND in
        # any combination with the customer's OTHER unmatched payments too
        payment_subset_sums = {sum(c) for r in range(1, len(payment_amounts) + 1)
                                for c in combinations(payment_amounts, r)}
        assert not (subset_sums & set(payment_amounts)), "payment collided with an invoice subset"
        assert not (payment_subset_sums & subset_sums), (
            "a combination of payments collided with an invoice subset")

    gen_pooled_no_match(n_invoices=6, n_payments=4)
    gen_pooled_no_match(n_invoices=5, n_payments=3)

    # ---- 12. Re-queue made visible (2 groups, 3 invoices + 2 payments
    # each): the submission dataset's own AMBIG-N/NO-MATCH cases above are
    # deliberately *permanent* (a disputed tie, amounts that fit nothing)
    # -- realistic, but it means the re-queue loop never fires in the
    # actual run, only in tests/test_requeue.py's standalone scenario.
    # These two groups plant that same proven shape here: three
    # identical-value open invoices; an early no-reference payment for 2x
    # the value is tied across all three possible pairs (genuinely
    # AMBIG-N on arrival); a later payment references the third invoice
    # by name and clears it; re-queue then finds a unique pair in what's
    # left. Ground truth records the *final* post-requeue outcome, same
    # convention as every other row in this file.
    def gen_requeue_group(label: str, amount_range: tuple[int, int],
                           issue_base_offset: int, early_created_offset: int,
                           late_created_offset: int) -> None:
        cust = w.add_customer()
        amt = rupees(RNG.randint(*amount_range))
        invs = [w.add_invoice(cust["customer_id"], amt,
                               issue_offset=issue_base_offset + j,
                               due_offset=issue_base_offset + j + 25)
                for j in range(3)]
        invs_sorted = sorted(invs, key=lambda v: v["issue_date"])
        pair, singleton = invs_sorted[:2], invs_sorted[2]

        early_pay = w.add_payment(
            amt * 2, cust["virtual_account"], cust["name"],
            f"NEFT/{cust['name']} PAYMENT RECEIVED",
            created_offset=early_created_offset)
        late_pay = w.add_payment(
            amt, cust["virtual_account"], cust["name"],
            f"NEFT/{cust['name']}/{singleton['invoice_id']} SETTLEMENT",
            created_offset=late_created_offset)

        w.add_gt(early_pay["payment_id"], "BULK-N",
                  [v["invoice_id"] for v in pair],
                  notes=f"resolution_group:{label} -- 3 financially-identical "
                        "candidates, genuinely AMBIG-N when this payment "
                        f"arrives; resolved via re-queue once "
                        f"{singleton['invoice_id']} closes, leaving a "
                        "unique pair")
        w.add_gt(late_pay["payment_id"], "EXACT", [singleton["invoice_id"]],
                  notes=f"resolution_group:{label} -- closes the third tied "
                        "invoice, enabling the earlier payment's re-queue "
                        "resolution")

    gen_requeue_group("A", (40_000, 90_000), issue_base_offset=-45,
                       early_created_offset=-9, late_created_offset=-8)
    gen_requeue_group("B", (15_000, 50_000), issue_base_offset=-52,
                       early_created_offset=-7, late_created_offset=-6)

    return w


def build_holdout_world() -> World:
    """Messy, adversarial inputs NOT enumerated by the 12-code spec
    build_world() exercises -- the kind of thing a real bank feed
    produces that this project's own generator never had to construct on
    purpose, because it was building toward known reason codes. Each case
    is on its own dedicated customer, no sharing (these are meant to be
    adversarial, not efficient).

    Seven categories, three instances each (21 records -- large enough to
    quote a percentage on, unlike the original 6-record set). Five
    categories are expected to pass -- correctly resolved or correctly
    refused. Two are documented misses: the engine has no temporal check
    and no conflicting-reference check, so it confidently produces the
    wrong answer on every instance. `notes` on each ground-truth row says
    which is which -- scoring/score.py's existing mismatch reporting
    surfaces the misses without any new code.

    Two of the "correctly refuses" shapes (truncated narration,
    wrong-customer VA) were verified directly against
    engine.pipeline.resolve_payment() before being committed here, not
    just reasoned about -- an earlier draft of the phantom-reference case
    in this same file turned out to get accidentally *repaired* by L3
    (its fake id was fuzzy-close enough to a real one), and an earlier
    draft of wrong-customer-VA got accidentally repaired to the *wrong*
    customer's own invoice (sequential ids one digit apart). Both
    generator functions below carry a comment explaining the specific
    collision they were built to avoid.
    """
    w = World()

    # ---- H1. DOCUMENTED MISS: payment predates its invoice's
    # issue_date. Nothing in the matching path checks temporal order, so
    # this still resolves EXACT even though a payment for an unissued
    # invoice is nonsensical -- not fixed this round.
    def gen_predates_invoice(i: int) -> None:
        cust = w.add_customer()
        inv = w.add_invoice(cust["customer_id"], rupees(RNG.randint(20_000, 70_000)),
                             issue_offset=5 + i, due_offset=20 + i)  # "issued" in the future
        pay = w.add_payment(
            inv["amount_paise"], cust["virtual_account"], cust["name"],
            f"NEFT/{cust['name']}/{inv['invoice_id']} SETTLEMENT",
            created_offset=-1 - i)  # arrives before the invoice exists
        w.add_gt(pay["payment_id"], "NO-MATCH", [],
                  notes="DOCUMENTED MISS: payment predates its invoice's "
                        "issue_date; the engine has no temporal check and "
                        "will resolve this EXACT anyway -- future work")

    # ---- H2. DOCUMENTED MISS: narration names two invoice IDs, amount
    # fits only one. Falls through to amount-only matching and
    # confidently returns EXACT, silently ignoring the conflicting
    # second reference. inv_y's amount is built as an offset from inv_x's
    # so the two can never accidentally collide (which would let the
    # payment amount-match both, changing the scenario's shape).
    def gen_conflicting_refs(i: int) -> None:
        cust = w.add_customer()
        inv_x = w.add_invoice(cust["customer_id"], rupees(RNG.randint(30_000, 60_000)),
                               issue_offset=-14 - i, due_offset=1 - i)
        inv_y = w.add_invoice(cust["customer_id"],
                               inv_x["amount_paise"] + rupees(RNG.randint(5_000, 20_000)),
                               issue_offset=-13 - i, due_offset=2 - i)
        pay = w.add_payment(
            inv_x["amount_paise"], cust["virtual_account"], cust["name"],
            f"NEFT/{cust['name']}/{inv_x['invoice_id']} AND "
            f"{inv_y['invoice_id']} SETTLEMENT",
            created_offset=-1 - i)
        w.add_gt(pay["payment_id"], "AMBIG-N", [],
                  notes="DOCUMENTED MISS: narration names two invoice IDs, "
                        "amount fits only one -- the engine falls through to "
                        "amount-only matching and confidently returns EXACT, "
                        "silently ignoring the conflicting second reference")

    # ---- H3. Refund / negative amount. Expect: the non-positive-amount
    # guard refuses rather than corrupting the invoice balance.
    def gen_refund(i: int) -> None:
        cust = w.add_customer()
        inv = w.add_invoice(cust["customer_id"], rupees(RNG.randint(15_000, 50_000)),
                             issue_offset=-10 - i, due_offset=5 - i)
        pay = w.add_payment(
            -rupees(RNG.randint(2_000, 8_000)), cust["virtual_account"], cust["name"],
            f"NEFT/{cust['name']}/{inv['invoice_id']} REFUND",
            created_offset=-1 - i)
        w.add_gt(pay["payment_id"], "NO-MATCH", [],
                  notes="refund / negative amount -- outside supported scope; "
                        "engine must refuse rather than silently inflate the "
                        "invoice balance")

    # ---- H4. Near-identical payer names: the free-text payer_name
    # resembles a *different* customer's name, but virtual_account
    # correctly identifies the real one. Expect: L0 resolves by VA only,
    # payer_name is only a confidence signal, never used for routing.
    def gen_lookalike_name(i: int) -> None:
        base_name = f"{RNG.choice(FIRST_NAMES)} {RNG.choice(SUFFIXES)}"
        lookalike = base_name + "z"
        cust_a = w.add_customer(name=base_name)
        w.add_customer(name=lookalike)  # lookalike name, unrelated customer
        inv_a = w.add_invoice(cust_a["customer_id"], rupees(RNG.randint(20_000, 90_000)),
                               issue_offset=-12 - i, due_offset=2 - i)
        pay = w.add_payment(
            inv_a["amount_paise"], cust_a["virtual_account"],
            payer_name=lookalike,
            narration=f"NEFT/{lookalike}/{inv_a['invoice_id']} SETTLEMENT",
            created_offset=-1 - i)
        w.add_gt(pay["payment_id"], "EXACT", [inv_a["invoice_id"]],
                  notes="payer_name resembles a different customer's name, but "
                        "virtual_account correctly identifies the real one -- "
                        "identity must resolve from VA, not the free-text name")

    # ---- H5. 2x overpayment with an exact reference. Expect: EXACT on
    # the referenced invoice, excess parked on account, single reason code.
    def gen_overpayment(i: int) -> None:
        cust = w.add_customer()
        inv = w.add_invoice(cust["customer_id"], rupees(RNG.randint(15_000, 60_000)),
                             issue_offset=-15 - i, due_offset=-1 - i)
        pay = w.add_payment(
            inv["amount_paise"] * 2, cust["virtual_account"], cust["name"],
            f"NEFT/{cust['name']}/{inv['invoice_id']} SETTLEMENT",
            created_offset=-2 - i)
        w.add_gt(pay["payment_id"], "EXACT", [inv["invoice_id"]],
                  notes="2x overpayment, exact reference -- invoice clears, "
                        "excess parked on account, single reason code")

    # ---- H6. Truncated narration -- the bank cuts the reference off
    # mid-way. Truncated to invoice_id[:5] (e.g. "INV-0187" -> "INV-0",
    # a single trailing digit) -- short enough that
    # reference.extract_tokens() doesn't even pull it out as a candidate
    # token (its regex needs >=2 digits), so L3 never gets anything to
    # repair. The payment amount is drawn from a disjoint range so
    # amount-only matching can't paper over it either. A 2-digit
    # truncation ("INV-01") was tried first and reliably got REPAIRED
    # (falls inside L3's containment-repair bar, diff<=2) -- verified
    # directly against engine.pipeline.resolve_payment() before settling
    # on the 1-digit cut, which produces a clean NO-MATCH end to end.
    def gen_truncated_narration(i: int) -> None:
        cust = w.add_customer()
        inv = w.add_invoice(cust["customer_id"], rupees(RNG.randint(25_000, 90_000)),
                             issue_offset=-20 - i, due_offset=-5 - i)
        truncated_ref = inv["invoice_id"][:5]
        pay = w.add_payment(
            rupees(RNG.randint(5_000, 15_000)), cust["virtual_account"], cust["name"],
            f"NEFT/{cust['name']}/{truncated_ref}",
            created_offset=-2 - i)
        w.add_gt(pay["payment_id"], "NO-MATCH", [],
                  notes=f"truncated narration: the bank cut the reference off "
                        f"to '{truncated_ref}', too short to extract as a "
                        "candidate token at all, let alone repair -- amount "
                        "also fits nothing real, expect a clean refusal")

    # ---- H7. Wrong-customer virtual account -- narration correctly names
    # a real invoice, but the payment lands in a DIFFERENT customer's VA
    # (a payer error, not an engine bug). The receiving customer has zero
    # open invoices, so there is nothing for L3 or amount-matching to find
    # regardless of what the (irrelevant, wrong) referenced id says.
    # First draft gave the receiving customer their own invoice -- an
    # unlucky sequential-id fuzzy match (INV-0100 vs INV-0200, one digit
    # apart, ratio ~0.86) got silently repaired to the wrong customer's
    # own bill, exactly the false match this case is meant to demonstrate
    # the engine avoiding. Zero invoices removes that risk entirely
    # rather than relying on generated ids staying numerically distant.
    def gen_wrong_customer_va(i: int) -> None:
        intended = w.add_customer()  # the invoice's real owner -- never paid
        wrong = w.add_customer()     # receives the money by payer error, no invoices
        inv = w.add_invoice(intended["customer_id"], rupees(RNG.randint(20_000, 90_000)),
                             issue_offset=-16 - i, due_offset=-1 - i)
        pay = w.add_payment(
            inv["amount_paise"], wrong["virtual_account"], intended["name"],
            f"NEFT/{intended['name']}/{inv['invoice_id']} SETTLEMENT",
            created_offset=-1 - i)
        w.add_gt(pay["payment_id"], "NO-MATCH", [],
                  notes=f"wrong-customer virtual account: narration correctly "
                        f"names {inv['invoice_id']}, but the payment was sent "
                        f"into {wrong['name']}'s account instead of "
                        f"{intended['name']}'s -- identity resolves from VA "
                        "as designed, and the receiving customer has no open "
                        "invoices this could settle, so the engine correctly "
                        "refuses rather than guessing across customers")

    N = 3
    for gen in (gen_predates_invoice, gen_conflicting_refs, gen_refund,
                gen_lookalike_name, gen_overpayment, gen_truncated_narration,
                gen_wrong_customer_va):
        for i in range(N):
            gen(i)

    return w


def compute_expected_customer_status(w: World) -> list[dict]:
    """Ground truth for the customer-level rollup, derived the same way
    the payment-level ground truth is: from how each payment was
    *constructed* to behave, not from running the engine. Uses the same
    engine.customer_status.rollup() the scorer uses against real engine
    output, so the two can never define "provisional" differently."""
    FULL_CLEAR = {"EXACT", "REF-FUZZY", "FIFO-TIE", "BULK-N", "TOL-FEE", "RESID-DED"}
    PARTIAL_APPLY = {"PART-EXP"}
    UNMATCHED = {"AMBIG-N", "NO-MATCH"}

    invoices_by_id = {v["invoice_id"]: v for v in w.invoices}
    payments_by_id = {p["payment_id"]: p for p in w.payments}
    va_to_customer = {c["virtual_account"]: c["customer_id"] for c in w.customers}

    applied: dict[str, int] = {}
    unmatched_by_customer: dict[str, int] = {}
    for gt in w.ground_truth:
        code = gt["expected_reason_code"]
        pay = payments_by_id[gt["payment_id"]]
        inv_ids = [i for i in gt["expected_invoice_ids"].split(";") if i]
        if code in FULL_CLEAR:
            for inv_id in inv_ids:
                applied[inv_id] = applied.get(inv_id, 0) + invoices_by_id[inv_id]["amount_paise"]
        elif code in PARTIAL_APPLY:
            for inv_id in inv_ids:
                applied[inv_id] = applied.get(inv_id, 0) + pay["amount_paise"]
        elif code in UNMATCHED:
            cid = va_to_customer.get(pay["virtual_account"])
            if cid:
                unmatched_by_customer[cid] = unmatched_by_customer.get(cid, 0) + pay["amount_paise"]
        # DUP-ONACC, SUSPENSE: no invoice effect, no unmatched-cash effect

    invoices_by_customer: dict[str, list[dict]] = {}
    for inv in w.invoices:
        invoices_by_customer.setdefault(inv["customer_id"], []).append(inv)

    rows = []
    for cust in w.customers:
        cid = cust["customer_id"]
        custs_invoices = invoices_by_customer.get(cid, [])
        total_invoiced = sum(i["amount_paise"] for i in custs_invoices)
        open_ledger = sum(
            max(0, i["amount_paise"] - applied.get(i["invoice_id"], 0))
            for i in custs_invoices)
        unmatched = unmatched_by_customer.get(cid, 0)
        r = customer_status.rollup(cid, total_invoiced, open_ledger, unmatched)
        rows.append({
            "customer_id": cid,
            "expected_customer_status": r.status,
            "expected_balance_paise": r.balance_paise,
        })
    return rows


def persist(w: World, suffix: str = "") -> None:
    OUT_DIR.mkdir(exist_ok=True)
    conn = init_db(OUT_DIR / f"finance{suffix}.db", fresh=True)
    conn.executemany(
        "INSERT INTO customers (customer_id, name, virtual_account, "
        "bank_account) VALUES (:customer_id, :name, :virtual_account, "
        ":bank_account)", w.customers)
    conn.executemany(
        "INSERT INTO invoices (invoice_id, customer_id, amount_paise, "
        "issue_date, due_date, gst_period, disputed) VALUES "
        "(:invoice_id, :customer_id, :amount_paise, :issue_date, "
        ":due_date, :gst_period, :disputed)", w.invoices)
    conn.executemany(
        "INSERT INTO payments (payment_id, amount_paise, method, "
        "virtual_account, payer_name, utr, narration, created_at) VALUES "
        "(:payment_id, :amount_paise, :method, :virtual_account, "
        ":payer_name, :utr, :narration, :created_at)", w.payments)
    conn.commit()
    conn.close()

    with open(OUT_DIR / f"ground_truth{suffix}.csv", "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["payment_id", "expected_reason_code",
                           "expected_invoice_ids", "notes"])
        writer.writeheader()
        writer.writerows(w.ground_truth)

    customer_rows = compute_expected_customer_status(w)
    with open(OUT_DIR / f"ground_truth_customers{suffix}.csv", "w", newline="") as f:
        writer = csv.DictWriter(
            f, fieldnames=["customer_id", "expected_customer_status",
                           "expected_balance_paise"])
        writer.writeheader()
        writer.writerows(customer_rows)


def _customers_with_3plus_invoices(w: World) -> int:
    by_customer: dict[str, int] = {}
    for inv in w.invoices:
        by_customer[inv["customer_id"]] = by_customer.get(inv["customer_id"], 0) + 1
    return sum(1 for count in by_customer.values() if count >= 3)


def _run_submission() -> None:
    w = build_world()
    assert len(w.payments) == 64, f"expected 64 payments, got {len(w.payments)}"
    assert len(w.invoices) == 61, f"expected 61 invoices, got {len(w.invoices)}"
    codes = {row["expected_reason_code"] for row in w.ground_truth}
    expected_codes = {"EXACT", "TOL-FEE", "PART-EXP", "RESID-DED", "BULK-N",
                       "REF-FUZZY", "FIFO-TIE", "DUP-ONACC", "SUSPENSE",
                       "AMBIG-N", "NO-MATCH"}
    missing = expected_codes - codes
    assert not missing, f"ground truth missing codes: {missing}"
    assert "PROVISIONAL" not in codes, (
        "PROVISIONAL is a customer_status, not a payment reason_code")

    customer_rows = compute_expected_customer_status(w)
    statuses = {row["expected_customer_status"] for row in customer_rows}
    expected_statuses = {"CLEAN", "PARTIAL", "PROVISIONAL"}
    missing_statuses = expected_statuses - statuses
    assert not missing_statuses, f"customer statuses missing: {missing_statuses}"

    dense_count = _customers_with_3plus_invoices(w)
    assert dense_count >= 6, (
        f"expected at least 6 customers with 3+ invoices, got {dense_count}")

    persist(w)
    avg_invoices = len(w.invoices) / len(w.customers)
    print(f"customers={len(w.customers)} invoices={len(w.invoices)} "
          f"payments={len(w.payments)} ground_truth_rows={len(w.ground_truth)}")
    print(f"avg invoices/customer={avg_invoices:.2f}, "
          f"customers with 3+ invoices={dense_count}")
    print(f"customer_status breakdown: " + ", ".join(
        f"{s}={sum(1 for r in customer_rows if r['expected_customer_status']==s)}"
        for s in sorted(expected_statuses)))
    print(f"wrote {OUT_DIR / 'finance.db'}, {OUT_DIR / 'ground_truth.csv'}, "
          f"{OUT_DIR / 'ground_truth_customers.csv'}")


def _run_holdout() -> None:
    w = build_holdout_world()
    persist(w, suffix="_holdout")
    print(f"[holdout] customers={len(w.customers)} invoices={len(w.invoices)} "
          f"payments={len(w.payments)} ground_truth_rows={len(w.ground_truth)}")
    print(f"wrote {OUT_DIR / 'finance_holdout.db'}, "
          f"{OUT_DIR / 'ground_truth_holdout.csv'}, "
          f"{OUT_DIR / 'ground_truth_customers_holdout.csv'}")


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["submission", "holdout"], default="submission",
                         help="submission: the 64-payment spec-matched dataset (default). "
                              "holdout: messy cases not enumerated by the 12-code spec, "
                              "scored separately -- see scoring/compare.py.")
    parser.add_argument("--seed", type=int, default=None,
                         help="overrides the mode's default seed (42 submission, 1337 holdout)")
    args = parser.parse_args()

    seed = args.seed if args.seed is not None else (42 if args.mode == "submission" else 1337)
    RNG.seed(seed)

    if args.mode == "submission":
        _run_submission()
    else:
        _run_holdout()


if __name__ == "__main__":
    main()
