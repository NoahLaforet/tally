"""Wells Fargo PDF parsers: Autograph Visa (credit), Bilt Mastercard (credit,
folded into the wf_autograph account), and Everyday Checking (debit).

Each parser is content detected, reads pdftotext -layout output, and enforces a
penny exact reconciliation gate before any row is allowed downstream:

  credit cards : previous - payments - other_credits + cash_advances
                 + purchases + fees + interest == new_balance, and the parsed
                 purchase total equals the printed Purchases line and the
                 section total.
  checking     : beginning + deposits - withdrawals == ending, and the parsed
                 deposit and withdrawal totals equal the printed Totals row.

Tally sign convention: a card purchase and a checking withdrawal are outflows
(negative); a card payment or credit and a checking deposit are inflows
(positive). Wells Fargo prints purchase magnitudes positive, which is the
opposite of the analyze.py credit card convention, so the sign is set here
explicitly rather than inherited.
"""

from __future__ import annotations

import re
from datetime import date

from ..canonical import CanonicalRecord
from .common import (
    MONEY,
    ParseResult,
    categorize,
    grab_money,
    infer_year,
    norm_merchant,
    period_from_records,
    statement_close_from_monthname,
    to_cents,
)

# Autograph purchase / other credit line: leading 4 digit card tag, two dates,
# reference number, description, trailing amount.
AUTO = re.compile(
    r"^\s*(\d{4})\s+(\d{2}/\d{2})\s+(\d{2}/\d{2})\s+(\S+)\s+(.*?)\s+([\d,]+\.\d{2})\s*$"
)
# Autograph payment line: no leading card tag (the payments section omits it).
AUTO_PAY = re.compile(
    r"^\s+(\d{2}/\d{2})\s+(\d{2}/\d{2})\s+(\S+)\s+(.*?)\s+([\d,]+\.\d{2})\s*$"
)
# Bilt purchase line: two dates, 9 digit reference, code, description, amount.
BILT = re.compile(
    r"^\s*(\d{2}/\d{2})\s+(\d{2}/\d{2})\s+\d{9}\s+\S+\s+(.*?)\s+\$?([\d,]+\.\d{2})\s*$"
)
# Bilt payment / credit line: trailing minus marks it as a credit.
BILT_CR = re.compile(
    r"^\s*(\d{2}/\d{2})\s+(\d{2}/\d{2})\s+(\S+)\s+(.*?)\s+\$?([\d,]+\.\d{2})-\s*$"
)
# Checking transaction row: indented, starts with a M/D date.
CHK_DATE = re.compile(r"^\s+(\d{1,2}/\d{1,2})\s")

AUTO_PERIOD = re.compile(
    r"Statement Period\s+(\d{2})/(\d{2})/(\d{4})\s+to\s+(\d{2})/(\d{2})/(\d{4})"
)
BILT_CLOSE = re.compile(r"Statement Closing Date\s+(\d{2})/(\d{2})/(\d{4})")


# ----------------------------------------------------------------- detection
def detect(txt: str) -> str | None:
    """Return the parser key for a Wells Fargo PDF, or None."""
    if "AUTOGRAPH VISA SIGNATURE" in txt:
        return "wf_autograph"
    # Bilt Mastercard statements (the predecessor WF card) carry the Bilt brand.
    if "bilt" in txt.lower():
        return "bilt"
    if "Transaction history" in txt or "Transaction History" in txt:
        return "checking"
    return None


def _mmdd(token: str, close_year: int, close_month: int) -> date:
    """Turn a bare MM/DD into a date using the statement close to pick a year."""
    mm, dd = token.split("/")
    month = int(mm)
    return date(infer_year(month, close_year, close_month), month, int(dd))


# ----------------------------------------------------------------- Autograph
def parse_autograph(txt: str, file_hash: str | None = None) -> ParseResult:
    pm = AUTO_PERIOD.search(txt)
    if pm:
        close_year, close_month = int(pm.group(6)), int(pm.group(4))
    else:  # fall back to the printed long date if the period line is missing
        cy_cm = statement_close_from_monthname(txt)
        close_year, close_month = cy_cm if cy_cm else (date.today().year, 12)

    records: list[CanonicalRecord] = []
    parsed_purchases = 0
    parsed_credits = 0
    sec = None
    for ln in txt.splitlines():
        low = ln.strip().lower()
        if low.startswith("payments"):
            sec = "pay"
            continue
        if low.startswith("other credits"):
            sec = "ocred"
            continue
        if low.startswith("purchases, balance transfers"):
            sec = "buy"
            continue
        if low.startswith("fees charged"):
            sec = "fee"
            continue
        if low.startswith("interest charged"):
            sec = "int"
            continue

        if sec == "buy":
            m = AUTO.match(ln)
            if not m:
                continue
            _, td, _, _, desc, amt = m.groups()
            if desc.upper().startswith("TOTAL"):
                continue
            is_credit = "PAYMENT" in desc.upper() or "RETURN" in desc.upper()
            cents = to_cents(amt)
            if is_credit:
                parsed_credits += cents
                amount_cents = cents  # inflow
            else:
                parsed_purchases += cents
                amount_cents = -cents  # outflow
            records.append(_credit_record(
                "wf_autograph", _mmdd(td, close_year, close_month), amount_cents,
                desc, file_hash, ln, is_credit))
        elif sec in ("pay", "ocred"):
            m = AUTO.match(ln) or AUTO_PAY.match(ln)
            if not m:
                continue
            groups = m.groups()
            td, desc, amt = groups[1], groups[-2], groups[-1]
            if desc.upper().startswith("TOTAL"):
                continue
            cents = to_cents(amt)
            parsed_credits += cents
            records.append(_credit_record(
                "wf_autograph", _mmdd(td, close_year, close_month), cents,
                desc, file_hash, ln, True))

    prev = grab_money(txt, "Previous Balance") or 0
    payments = grab_money(txt, "- Payments") or 0
    other_credits = grab_money(txt, "- Other Credits") or 0
    cash_adv = grab_money(txt, "+ Cash Advances") or 0
    pur_print = grab_money(txt, "+ Purchases, Balance Transfers &") or 0
    fees = grab_money(txt, "+ Fees Charged") or 0
    interest = grab_money(txt, "+ Interest Charged") or 0
    new_bal = grab_money(txt, "= New Balance")
    tot_pur = grab_money(txt, "TOTAL PURCHASES, BALANCE TRANSFERS")

    balance_ok = new_bal is not None and (
        prev - payments - other_credits + cash_adv + pur_print + fees + interest == new_bal
    )
    purchases_ok = parsed_purchases == pur_print == (tot_pur if tot_pur is not None else pur_print)
    credits_ok = parsed_credits == payments + other_credits
    reconciled = bool(balance_ok and purchases_ok and credits_ok)

    detail = {
        "type": "wf_autograph",
        "parsed_purchases_cents": parsed_purchases,
        "printed_purchases_cents": pur_print,
        "parsed_credits_cents": parsed_credits,
        "printed_credits_cents": payments + other_credits,
        "new_balance_cents": new_bal,
        "balance_ok": balance_ok,
        "purchases_ok": purchases_ok,
        "credits_ok": credits_ok,
    }
    return ParseResult("wf_autograph", records, reconciled, detail,
                       period_from_records(records))


# ----------------------------------------------------------------- Bilt
def parse_bilt(txt: str, file_hash: str | None = None) -> ParseResult:
    cm = BILT_CLOSE.search(txt)
    if cm:
        close_year, close_month = int(cm.group(3)), int(cm.group(1))
    else:
        cy_cm = statement_close_from_monthname(txt)
        close_year, close_month = cy_cm if cy_cm else (date.today().year, 1)

    records: list[CanonicalRecord] = []
    parsed_purchases = 0
    parsed_credits = 0
    seg = False
    for ln in txt.splitlines():
        if "Description of Transaction" in ln:
            seg = True
            continue
        if not seg:
            continue
        if ln.strip().lower().startswith("total"):
            seg = False
            continue
        m = BILT.match(ln)
        if m and not m.group(3).upper().startswith("TOTAL"):
            td, _, desc, amt = m.groups()
            cents = to_cents(amt)
            parsed_purchases += cents
            records.append(_credit_record(
                "wf_autograph", _mmdd(td, close_year, close_month), -cents,
                desc, file_hash, ln, False))
            continue
        mc = BILT_CR.match(ln)
        if mc:
            td, _, _, desc, amt = mc.groups()
            cents = to_cents(amt)
            parsed_credits += cents
            records.append(_credit_record(
                "wf_autograph", _mmdd(td, close_year, close_month), cents,
                desc, file_hash, ln, True))

    prev = grab_money(txt, "Previous Balance") or 0
    payments = grab_money(txt, "Payments") or 0
    other_credits = grab_money(txt, "Other Credits") or 0
    pur_print = grab_money(txt, "Purchases/Debits") or 0
    new_bal = grab_money(txt, "New Balance")

    balance_ok = new_bal is not None and (
        prev - payments - other_credits + pur_print == new_bal
    )
    purchases_ok = parsed_purchases == pur_print
    reconciled = bool(balance_ok and purchases_ok)

    detail = {
        "type": "bilt",
        "parsed_purchases_cents": parsed_purchases,
        "printed_purchases_cents": pur_print,
        "parsed_credits_cents": parsed_credits,
        "printed_payments_cents": payments,
        "new_balance_cents": new_bal,
        "balance_ok": balance_ok,
        "purchases_ok": purchases_ok,
    }
    return ParseResult("wf_autograph", records, reconciled, detail,
                       period_from_records(records))


# ----------------------------------------------------------------- Checking
def parse_checking(txt: str, file_hash: str | None = None) -> ParseResult:
    cy_cm = statement_close_from_monthname(txt)
    close_year, close_month = cy_cm if cy_cm else (date.today().year, 12)

    lines = txt.splitlines()
    dep_col = wd_col = None
    for ln in lines:
        if "Deposits/" in ln and "Withdrawals/" in ln and "Ending" in ln:
            dep_col = ln.index("Deposits/")
            wd_col = ln.index("Withdrawals/")
            break

    records: list[CanonicalRecord] = []
    parsed_dep = 0
    parsed_wd = 0
    stop = (
        "Totals", "Ending balance on", "Items returned unpaid", "Monthly service fee",
        "Account transaction fees", "Overdraft Protection", "IMPORTANT ACCOUNT",
        "Summary of checks", "Fee period",
    )

    def flush(cur):
        nonlocal parsed_dep, parsed_wd
        if not cur:
            return
        if cur["wd"]:
            amount_cents = -cur["wd"]
            parsed_wd += cur["wd"]
        else:
            amount_cents = cur["dep"]
            parsed_dep += cur["dep"]
        desc = cur["desc"].strip()
        merchant = norm_merchant(desc)
        records.append(
            CanonicalRecord(
                account_id="debit",
                posted_date=cur["date"],
                amount_cents=amount_cents,
                raw_description=desc,
                norm_merchant=merchant,
                category=categorize(desc, merchant),
                category_source="rule",
                # is_transfer is set later, only by the matcher, on paired legs.
                is_transfer=False,
                source_file_hash=file_hash,
                source_statement_id="wf_checking",
                source_line=cur["line"],
            )
        )

    in_hist = False
    cur = None
    if dep_col is not None:
        for idx, ln in enumerate(lines):
            if "Transaction history" in ln or "Transaction History" in ln:
                in_hist = True
                continue
            if not in_hist:
                continue
            if any(ln.strip().startswith(h) for h in stop):
                in_hist = False
                flush(cur)
                cur = None
                continue
            dm = CHK_DATE.match(ln)
            if dm:
                flush(cur)
                nums = [(m.group(1), m.end()) for m in MONEY.finditer(ln)]
                dep = wd = 0
                if nums:
                    val, end = nums[0]
                    v = to_cents(val)
                    span_start = end - len(val)
                    if abs(span_start - dep_col) <= abs(span_start - wd_col):
                        dep = v
                    else:
                        wd = v
                cur = dict(
                    date=_mmdd(dm.group(1), close_year, close_month),
                    desc=ln.strip()[len(dm.group(1)):].strip(),
                    dep=dep, wd=wd, line=idx + 1,
                )
            elif cur and ln.strip():
                cur["desc"] += " " + ln.strip()
        flush(cur)

    begin = grab_money(txt, "Beginning balance on")
    dep_print = grab_money(txt, "Deposits/Additions")
    wd_print = grab_money(txt, "Withdrawals/Subtractions")
    end_bal = grab_money(txt, "Ending balance on")

    tot_dep = tot_wd = None
    for ln in lines:
        if ln.strip().startswith("Totals"):
            ms = MONEY.findall(ln)
            if len(ms) >= 2:
                tot_dep, tot_wd = to_cents(ms[0]), to_cents(ms[1])
            break

    balance_ok = (
        begin is not None and end_bal is not None
        and dep_print is not None and wd_print is not None
        and begin + dep_print - wd_print == end_bal
    )
    dep_ok = parsed_dep == dep_print and (tot_dep is None or parsed_dep == tot_dep)
    wd_ok = parsed_wd == wd_print and (tot_wd is None or parsed_wd == tot_wd)
    reconciled = bool(balance_ok and dep_ok and wd_ok)

    detail = {
        "type": "checking",
        "parsed_deposits_cents": parsed_dep,
        "printed_deposits_cents": dep_print,
        "parsed_withdrawals_cents": parsed_wd,
        "printed_withdrawals_cents": wd_print,
        "begin_cents": begin,
        "end_cents": end_bal,
        "balance_ok": balance_ok,
        "deposits_ok": dep_ok,
        "withdrawals_ok": wd_ok,
    }
    return ParseResult("debit", records, reconciled, detail,
                       period_from_records(records))


def _credit_record(account, posted_date, amount_cents, desc, file_hash, line, is_credit):
    merchant = norm_merchant(desc)
    return CanonicalRecord(
        account_id=account,
        posted_date=posted_date,
        amount_cents=amount_cents,
        raw_description=desc.strip(),
        norm_merchant=merchant,
        category=categorize(desc, merchant),
        category_source="rule",
        # is_transfer is set later, only by the matcher, on paired legs.
        is_transfer=False,
        source_file_hash=file_hash,
        source_statement_id=account,
        source_line=None,
    )


def parse(txt: str, file_hash: str | None = None) -> ParseResult | None:
    """Dispatch a Wells Fargo PDF to the right parser by content."""
    kind = detect(txt)
    if kind == "wf_autograph":
        return parse_autograph(txt, file_hash)
    if kind == "bilt":
        return parse_bilt(txt, file_hash)
    if kind == "checking":
        return parse_checking(txt, file_hash)
    return None
