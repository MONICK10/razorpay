"""L0 -- Identity. Virtual account tells us WHO paid. It says nothing about
WHICH invoice. Payments whose virtual account maps to no customer at all
are a different failure than an unresolved invoice match: SUSPENSE, not
NO-MATCH."""
from __future__ import annotations


def resolve_customer(payment: dict, customers_by_va: dict[str, dict]) -> dict | None:
    va = payment.get("virtual_account")
    if not va:
        return None
    return customers_by_va.get(va)
