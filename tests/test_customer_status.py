from engine.customer_status import rollup
from engine.reason_codes import CustomerStatus


def test_clean_when_ledger_balance_is_zero():
    r = rollup("C1", total_invoiced_paise=10_000, open_balance_ledger_paise=0,
               unmatched_cash_paise=0)
    assert r.status == CustomerStatus.CLEAN.value
    assert r.balance_paise == 0


def test_partial_when_balance_open_but_fully_explained():
    r = rollup("C1", total_invoiced_paise=10_000, open_balance_ledger_paise=4_000,
               unmatched_cash_paise=0)
    assert r.status == CustomerStatus.PARTIAL.value
    assert r.balance_paise == 4_000


def test_provisional_when_unmatched_cash_explains_part_of_the_gap():
    # Kumar-style: Rs 27,000 invoiced, nothing individually matched,
    # Rs 14,250 received across unmatched payments -> Rs 12,750 certain.
    r = rollup("C1", total_invoiced_paise=27_000_00,
               open_balance_ledger_paise=27_000_00,
               unmatched_cash_paise=14_250_00)
    assert r.status == CustomerStatus.PROVISIONAL.value
    assert r.balance_paise == 12_750_00


def test_balance_never_goes_negative():
    r = rollup("C1", total_invoiced_paise=10_000, open_balance_ledger_paise=1_000,
               unmatched_cash_paise=5_000)
    assert r.balance_paise == 0
