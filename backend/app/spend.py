"""One definition of "the owner's spending", shared everywhere.

The dashboard, the pace module, and any future alert engine must agree on what
counts as spending and what does not. Keeping that predicate (and the merchant
lexicons it leans on) in a single place is what guarantees the "spent so far"
on the Overview matches the numbers on the Spending view to the penny.

Money is signed integer cents: outflows negative, inflows positive. A spend is
a genuine outflow that is not a transfer, a reimbursed front, a flagged
merchant, or a bookkeeping move (card payoff, savings sweep, tuition, P2P).
"""

from __future__ import annotations

import json

from sqlmodel import Session

from .models import Setting, Transaction

# Merchant lexicons drive the gambling tracker, the delivery breakdown, and the
# not-consumption exclusions (card payoffs, P2P, internal moves the transfer
# matcher cannot pair). These defaults are generic US services; each instance
# layers its own additions on top via the 'lexicons' Setting so nobody's
# personal banking phrasing lives in this public file.
DEFAULT_LEXICONS = {
    "gambling": ["draftkings", "fanduel", "betmgm", "caesars sportsbook",
                 "prizepicks", "kalshi"],
    "delivery": ["doordash", "uber eats", "ubereats", "grubhub", "postmates",
                 "instacart"],
    "nonconsumption": ["credit card auto pay", "credit card autopay",
                       "credit card retry", "credit card payment",
                       "savings transfer", "online transfer", "transfer to",
                       "zelle to", "venmo payment", "bill pay", "tuition",
                       "money transfer authorized",
                       # Plaid emits a synthetic carryforward row when a card
                       # is first linked; it is a balance, not a purchase.
                       "last statement bal", "beginning balance"],
}


def instance_lexicons(session: Session) -> dict[str, tuple[str, ...]]:
    """Defaults plus this instance's additions from the 'lexicons' Setting."""
    row = session.get(Setting, "lexicons")
    extra = {}
    if row:
        try:
            extra = json.loads(row.value_json)
        except json.JSONDecodeError:
            extra = {}
    out = {}
    for key, base in DEFAULT_LEXICONS.items():
        more = [str(x).lower() for x in extra.get(key, [])]
        out[key] = tuple(dict.fromkeys([*base, *more]))
    return out


def spend_amount(t: Transaction, gambling, nonconsumption) -> int | None:
    """Positive spend cents for a transaction, or None if it is not the owner's
    spending. This mirrors compute_dashboard's per-transaction exclusions
    exactly, so the two never diverge:

      - transfers (paired or flagged) are moves, not spend
      - group / thirdparty reimbursements were fronted and paid back
      - flagged (gambling) merchants are tracked separately, not spend
      - inflows are income, not spend
      - nonconsumption phrasings are bookkeeping, not spend
    """
    if t.is_transfer or t.category == "transfer":
        return None
    if t.reimbursement in ("group", "thirdparty"):
        return None
    if t.amount_cents >= 0:
        return None
    desc = (t.norm_merchant + " " + t.raw_description).lower()
    if any(g in desc for g in gambling):
        return None
    if any(k in desc for k in nonconsumption):
        return None
    return -t.amount_cents
