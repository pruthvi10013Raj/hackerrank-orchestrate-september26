"""
Phase 2 -- Deterministic, regex-based extraction of financial signals
from messages.csv.

No LLM. No network calls. Every message is UNTRUSTED evidence: this
module extracts only unambiguous structured signals (amounts, dates, a
small closed vocabulary of signal_types) from the text. It NEVER
executes an instruction embedded in the text, and it NEVER lets message
content override challenge policy. Its only consumer, reconstruction.py,
uses the output to fill a blank event amount, and only when exactly one
unambiguous candidate amount is tied to a real event_id via
related_event_id AND the signal type is one the dataset actually
supports for that purpose. Everything else is descriptive-only evidence.

Design notes from auditing the actual dataset (messages.csv, all 215
rows, read in full):

  - messages.csv is templated: each row is an English or Bahasa
    Indonesia rendering of one of roughly 30 fixed scenario templates --
    payroll confirmation / increase / decrease / date-amendment /
    contract-or-employment-ended / partial-income-ended / one-time
    arrears / unconfirmed bonus-or-commission, gig-payout-pending,
    invoice-approved-pending-settlement, refund-pending, investment
    unrealized-value-change, investment-sale-settled, prize-pending,
    prize-settled, an advance-fee-fraud-style "pay a fee to release your
    prize" template, internal-transfer-between-own-accounts,
    card-dispute-pending, failed-debit-will-retry, multiple-card-minimums
    -due, recurring-rent-increase, an FX-settlement-rate note, an
    expense-reimbursement-not-salary note, and a merchant/service receipt
    confirming a final settled amount.
  - Exactly one amount format is used throughout: "<CCY> <number>" with
    CCY in {INR, ZAR, IDR, USD, EUR} -- the five currencies the dataset
    contract names.
  - Dates always appear as YYYY-MM-DD, even inside Indonesian sentences.
  - related_event_id is populated for only ~18% of messages (39 of 215);
    for the rest, the message is user/request-level context with no
    single financial_events.csv row it can amend.
"""

from __future__ import annotations

import re
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import List, Tuple

import loaders
from state import MessageEvidence

# --------------------------------------------------------------------------
# Signal type constants -- a closed, small vocabulary. Any future policy
# code should switch on these constants, never on raw message text.
# --------------------------------------------------------------------------

SIGNAL_SALARY_CONFIRMED = "salary_confirmed"
SIGNAL_SALARY_AMOUNT_INCREASE = "salary_amount_increase"
SIGNAL_SALARY_AMOUNT_DECREASE = "salary_amount_decrease"
SIGNAL_SALARY_DATE_AMENDED = "salary_date_amended"
SIGNAL_SALARY_STREAM_ENDED = "salary_stream_ended"
SIGNAL_SALARY_PARTIAL_END = "salary_partial_end"
SIGNAL_ONE_TIME_ARREARS_ADJUSTMENT = "one_time_arrears_adjustment"
SIGNAL_BONUS_COMMISSION_UNCONFIRMED = "bonus_commission_unconfirmed"
SIGNAL_REFUND_PENDING = "refund_pending"
SIGNAL_REFUND_SETTLED = "refund_settled"
SIGNAL_PAYOUT_PENDING = "gig_payout_pending"
SIGNAL_INVOICE_APPROVED_PENDING_SETTLEMENT = "invoice_approved_pending_settlement"
SIGNAL_INVESTMENT_UNREALIZED_CHANGE = "investment_unrealized_change"
SIGNAL_INVESTMENT_SALE_SETTLED = "investment_sale_settled"
SIGNAL_PRIZE_PENDING = "prize_pending"
SIGNAL_PRIZE_SETTLED = "prize_settled"
SIGNAL_ADVANCE_FEE_SUSPICIOUS = "advance_fee_suspicious_pattern"
SIGNAL_INTERNAL_TRANSFER = "internal_transfer_same_owner"
SIGNAL_CARD_DISPUTE_PENDING = "card_dispute_pending"
SIGNAL_DEBIT_FAILED_RETRY = "debit_failed_will_retry"
SIGNAL_MULTI_CARD_MIN_DUE = "multiple_min_payments_due"
SIGNAL_RENT_INCREASE = "recurring_rent_increase"
SIGNAL_FX_SETTLEMENT_NOTE = "fx_settlement_rate_note"
SIGNAL_REIMBURSEMENT_SETTLED = "reimbursement_settled_not_salary"
SIGNAL_RECEIPT_CONFIRMS_AMOUNT = "receipt_confirms_amount"
SIGNAL_UNCLASSIFIED = "unclassified"

# Signal types for which a single, unambiguous extracted amount MAY be
# used by reconstruction.py to resolve a blank event amount, provided the
# message also names that exact event_id via related_event_id. All other
# signal types stay informational even when related_event_id is present:
# e.g. SIGNAL_ADVANCE_FEE_SUSPICIOUS must never fill an amount, and
# SIGNAL_BONUS_COMMISSION_UNCONFIRMED explicitly must not -- the entire
# point of that template is that the amount is NOT yet confirmed.
_AMOUNT_APPLICABLE_SIGNALS = {
    SIGNAL_RECEIPT_CONFIRMS_AMOUNT,
    SIGNAL_SALARY_CONFIRMED,
    SIGNAL_REFUND_SETTLED,
    SIGNAL_INVESTMENT_SALE_SETTLED,
    SIGNAL_PRIZE_SETTLED,
    SIGNAL_REIMBURSEMENT_SETTLED,
}

# --------------------------------------------------------------------------
# Regex phrase bank (English + Bahasa Indonesia renderings observed in the
# actual dataset). Matching targets stable, template-carried phrases
# rather than fuzzy heuristics, so a match is a strong signal, not a
# guess. Order matters: earlier patterns are checked first and a message
# may match more than one (signal_types is a tuple of every match).
# --------------------------------------------------------------------------

_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    # Advance-fee scam pattern is checked first so a "prize" template that
    # asks for an up-front fee is never confused with a legitimate
    # prize-settlement signal.
    (SIGNAL_ADVANCE_FEE_SUSPICIOUS, re.compile(
        r"pay (the|a) (release|processing) charge|bayar biaya (pencairan|pemrosesan)", re.I)),

    (SIGNAL_RECEIPT_CONFIRMS_AMOUNT, re.compile(
        r"receipt (has|contains) the final|the receipt has the final|"
        r"was received on|was paid in \w+ on|receipt has the final", re.I)),

    (SIGNAL_SALARY_STREAM_ENDED, re.compile(
        r"employment has ended|no regular salary payments scheduled|"
        r"hubungan kerja anda telah berakhir|tidak ada pembayaran gaji rutin", re.I)),

    (SIGNAL_SALARY_PARTIAL_END, re.compile(
        r"one household employment record has ended|"
        r"salah satu sumber pendapatan kerja rumah tangga telah berakhir", re.I)),

    (SIGNAL_ONE_TIME_ARREARS_ADJUSTMENT, re.compile(
        r"one-time arrears adjustment|penyesuaian tunggakan satu kali", re.I)),

    (SIGNAL_BONUS_COMMISSION_UNCONFIRMED, re.compile(
        r"quarterly bonus is still subject to|final amount and payment date have not been approved|"
        r"commission shown for open deals is still pending|"
        r"bonus kuartalan anda masih menunggu|komisi dari transaksi yang masih berjalan belum disetujui",
        re.I)),

    (SIGNAL_SALARY_DATE_AMENDED, re.compile(
        r"confirmed salary is now expected on|this replaces the payroll date|"
        r"gaji yang sudah dikonfirmasi kini diperkirakan masuk pada|menggantikan tanggal penggajian",
        re.I)),

    (SIGNAL_SALARY_AMOUNT_INCREASE, re.compile(
        r"monthly salary has increased to|gaji bulanan anda naik menjadi", re.I)),

    (SIGNAL_SALARY_AMOUNT_DECREASE, re.compile(
        r"next salary is reduced to|temporary monthly pay is|"
        r"gaji bulanan sementara anda adalah|adjustment is due to approved unpaid leave|"
        r"jumlah yang lebih rendah masih berlaku", re.I)),

    (SIGNAL_REIMBURSEMENT_SETTLED, re.compile(
        r"reimbursement for your earlier work expense|penggantian atas biaya kerja anda sebelumnya", re.I)),

    (SIGNAL_SALARY_CONFIRMED, re.compile(
        r"first salary (will be|from the new employer is)|first salary of \w+ [\d,.]+ is (scheduled|confirmed)|"
        r"confirmed credit date is|salary of \w+ [\d,.]+ is confirmed for|regular salary of \w+ [\d,.]+ resumes|"
        r"gaji pertama|dikonfirmasi untuk|tanggal kredit yang dikonfirmasi adalah", re.I)),

    (SIGNAL_REFUND_PENDING, re.compile(
        r"refund has been initiated but has not reached your account|"
        r"foreign-currency refund is still processing|"
        r"pengembalian dana sudah diproses, tetapi belum masuk", re.I)),

    (SIGNAL_PAYOUT_PENDING, re.compile(
        r"payout is still pending|balance isn.t withdrawable until|"
        r"pembayaran berikutnya dari \S+ masih tertunda|saldo belum dapat ditarik", re.I)),

    (SIGNAL_INVOICE_APPROVED_PENDING_SETTLEMENT, re.compile(
        r"client approved an invoice payment|klien menyetujui pembayaran faktur", re.I)),

    (SIGNAL_INVESTMENT_SALE_SETTLED, re.compile(
        r"proceeds from your investment sale have settled|sale order is complete|"
        r"hasil penjualan investasi anda sudah masuk ke rekening tunai|perintah penjualan sudah selesai",
        re.I)),

    (SIGNAL_INVESTMENT_UNREALIZED_CHANGE, re.compile(
        r"displayed market value has increased|displayed value of the investment has fallen|"
        r"no units have been sold and no cash proceeds|"
        r"nilai investasi yang ditampilkan telah (turun|naik)|belum dijual dan tidak ada transaksi tunai",
        re.I)),

    (SIGNAL_PRIZE_SETTLED, re.compile(
        r"prize proceeds have reached your account|"
        r"claim is now closed and there are no further scheduled payments", re.I)),

    (SIGNAL_PRIZE_PENDING, re.compile(
        r"prize claim has been verified and is still in payment processing|"
        r"payment has not been credited to your account yet|"
        r"klaim hadiah anda sudah diverifikasi dan masih dalam proses pembayaran", re.I)),

    (SIGNAL_INTERNAL_TRANSFER, re.compile(
        r"matching debit and credit came from a transfer between your two accounts|"
        r"debit dan kredit dengan jumlah yang sama berasal dari transfer antara dua rekening", re.I)),

    (SIGNAL_CARD_DISPUTE_PENDING, re.compile(
        r"extra card charge is still being investigated|reversal has not been posted|"
        r"tagihan kartu tambahan masih dalam penyelidikan|dana pembalikannya belum tercatat", re.I)),

    (SIGNAL_DEBIT_FAILED_RETRY, re.compile(
        r"previous debit attempt failed|another debit will be attempted|"
        r"upaya debit sebelumnya gagal", re.I)),

    (SIGNAL_MULTI_CARD_MIN_DUE, re.compile(
        r"minimum payments due on two separate card accounts|won.t cover the amount due on the other card",
        re.I)),

    (SIGNAL_RENT_INCREASE, re.compile(
        r"renewed lease increases monthly rent by|perpanjangan sewa menaikkan", re.I)),

    (SIGNAL_FX_SETTLEMENT_NOTE, re.compile(
        r"convert it using the rate applied on the settlement date|"
        r"will use the exchange rate when it settles|"
        r"mengonversinya dengan kurs pada tanggal penyelesaian|"
        r"menggunakan kurs (saat|pada) transaksi selesai", re.I)),
)

_AMOUNT_RE = re.compile(r"\b(INR|ZAR|IDR|USD|EUR)\s+([\d,]+(?:\.\d+)?)\b")
_DATE_RE = re.compile(r"\b(\d{4}-\d{2}-\d{2})\b")


def _extract_amounts(text: str) -> Tuple[Tuple[str, Decimal], ...]:
    out: List[Tuple[str, Decimal]] = []
    seen = set()
    for currency, raw_number in _AMOUNT_RE.findall(text):
        cleaned = raw_number.replace(",", "")
        try:
            amount = Decimal(cleaned)
        except InvalidOperation:
            continue
        key = (currency, amount)
        if key in seen:
            continue
        seen.add(key)
        out.append((currency, amount))
    return tuple(out)


def _extract_dates(text: str):
    out = []
    seen = set()
    for raw in _DATE_RE.findall(text):
        if raw in seen:
            continue
        seen.add(raw)
        try:
            out.append(datetime.strptime(raw, "%Y-%m-%d").date())
        except ValueError:
            continue
    return tuple(out)


def _classify(text: str) -> Tuple[str, ...]:
    matched = tuple(signal for signal, pattern in _PATTERNS if pattern.search(text))
    return matched if matched else (SIGNAL_UNCLASSIFIED,)


def parse_message(message: loaders.Message) -> MessageEvidence:
    """Pure function: message row in, MessageEvidence out. Deterministic,
    no I/O, no LLM. Never raises on unexpected text: worst case a message
    is SIGNAL_UNCLASSIFIED with no extracted facts, which is the correct
    fail-safe (never guess, never apply an unrecognized template).
    """
    text = message.message_text
    signal_types = _classify(text)
    amounts = _extract_amounts(text)
    dates = _extract_dates(text)

    is_advance_fee = SIGNAL_ADVANCE_FEE_SUSPICIOUS in signal_types
    if is_advance_fee:
        # Untrusted-content firewall: this template asks the user to pay a
        # fee to "release" money. Regardless of whether it happens to
        # contain a number, it is never treated as a source of financial
        # facts and can never be marked "applied". This is the concrete
        # case the prompt-injection defense in AGENTS.md/CLAUDE.md is
        # about: content may be analyzed, but it may never become policy
        # or an instruction the system acts on.
        amounts = ()

    applicable = (
        not is_advance_fee
        and message.related_event_id is not None
        and any(s in _AMOUNT_APPLICABLE_SIGNALS for s in signal_types)
        and len(amounts) == 1
    )

    if is_advance_fee:
        note = (
            "matches an advance-fee-fraud pattern ('pay a fee to release the prize'); "
            "treated as untrusted content only, never as a financial fact or instruction"
        )
    elif applicable:
        note = (
            f"single unambiguous amount candidate ({amounts[0][0]} {amounts[0][1]}) tied to "
            f"related_event_id={message.related_event_id}; eligible for blank-amount resolution"
        )
    elif message.related_event_id is not None and len(amounts) > 1:
        note = f"{len(amounts)} amount candidates found; too ambiguous to apply to a single event"
    elif signal_types == (SIGNAL_UNCLASSIFIED,):
        note = "message text did not match any recognized deterministic template; no facts extracted"
    else:
        note = (
            f"classified as {signal_types}; informational only -- does not resolve a blank amount "
            "(either no related_event_id, or this signal type is explicitly non-actionable, e.g. "
            "an unconfirmed bonus/commission or a suspicious pattern)"
        )

    return MessageEvidence(
        message_id=message.message_id,
        user_id=message.user_id,
        request_id=message.request_id,
        related_event_id=message.related_event_id,
        signal_types=signal_types,
        extracted_amounts=amounts,
        extracted_dates=dates,
        applied=applicable,
        application_note=note,
    )
