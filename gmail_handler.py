from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from config import settings
from matching import find_matches
from models import GameSession, Payment, SessionLocal

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
POLL_INTERVAL = 30  # seconds


# ─── Credential management ────────────────────────────────────────────────────

def _load_credentials() -> Credentials | None:
    if not settings.gmail_token_json:
        return None
    try:
        token_data = json.loads(settings.gmail_token_json)
        creds = Credentials.from_authorized_user_info(token_data, SCOPES)
        if not creds.valid:
            if creds.refresh_token:
                creds.refresh(Request())
                logger.info("Gmail OAuth token refreshed")
            else:
                logger.error("Gmail token invalid and has no refresh_token — re-run gmail_setup.py")
                return None
        return creds
    except Exception:
        logger.exception("Failed to load Gmail credentials")
        return None


# ─── Email body extraction ────────────────────────────────────────────────────

def _decode_part(data: str) -> str:
    try:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    except Exception:
        return ""


def _get_text_body(payload: dict) -> str:
    mime = payload.get("mimeType", "")
    body_data = payload.get("body", {}).get("data", "")

    if mime == "text/plain" and body_data:
        return _decode_part(body_data)
    if mime == "text/html" and body_data:
        return _decode_part(body_data)

    parts = payload.get("parts", [])
    # Prefer plain text
    for part in parts:
        if part.get("mimeType") == "text/plain":
            text = _decode_part(part.get("body", {}).get("data", ""))
            if text:
                return text
    # Recurse into multipart children
    for part in parts:
        if part.get("mimeType", "").startswith("multipart/"):
            text = _get_text_body(part)
            if text:
                return text
    # Fallback to HTML
    for part in parts:
        if part.get("mimeType") == "text/html":
            text = _decode_part(part.get("body", {}).get("data", ""))
            if text:
                return text
    return ""


def _strip_html(text: str) -> str:
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<script[^>]*>.*?</script>", " ", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    for entity, replacement in [
        ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&nbsp;", " "),
        ("&quot;", '"'), ("&#39;", "'"), ("&zwnj;", ""), ("&#8202;", ""),
    ]:
        text = text.replace(entity, replacement)
    return re.sub(r"\s+", " ", text).strip()


# ─── Email parsing ────────────────────────────────────────────────────────────

def _parse_wise_email(subject: str, body: str) -> dict | None:
    """
    Extract payment details from a Wise payment notification email.
    Supports Traditional Chinese (已收到來自…) and English (You've received…) formats.
    Returns dict with amount/currency/sender_name/reference, or None if not a payment email.
    """
    is_payment = bool(
        re.search(r"received", subject, re.IGNORECASE)
        or re.search(r"已收到|收到.*付款", subject)
    )
    if not is_payment:
        return None

    body_text = _strip_html(body) if ("<html" in body.lower() or "<div" in body.lower()) else body

    # ── Amount ────────────────────────────────────────────────────────────────
    # Wise emails (Chinese): "已收到的金額： 1 GBP"
    # Wise emails (English): "Amount received: £20.00" or "received £20.00 GBP"
    amount: float | None = None
    currency = "GBP"

    for pat in [
        # Chinese label in body: 已收到的金額： 1 GBP
        r"已收到的金額[：:]\s*([\d,]+\.?\d*)\s*(GBP|USD|EUR|HKD|SGD|AUD|CAD)",
        # Inline Chinese body sentence: 已收到來自…的1 GBP付款
        r"已收到來自.+?的\s*([\d,]+\.?\d*)\s*(GBP|USD|EUR|HKD|SGD|AUD|CAD)\s*付款",
        # English label in body
        r"Amount\s+received[：:]\s*[£$€]?\s*([\d,]+\.?\d*)\s*(GBP|USD|EUR|HKD|SGD|AUD|CAD)?",
        # English subject: "You've received £20.00"
        r"received\s+[£$€]?\s*([\d,]+\.?\d*)\s*(GBP|USD|EUR|HKD|SGD|AUD|CAD)?",
    ]:
        m = re.search(pat, body_text + " " + subject, re.IGNORECASE)
        if m:
            try:
                amount = float(m.group(1).replace(",", ""))
                if m.lastindex >= 2 and m.group(2):
                    currency = m.group(2).upper()
                break
            except (ValueError, AttributeError):
                pass

    # Currency symbol fallback from subject
    if "£" in subject:
        currency = "GBP"
    elif "€" in subject:
        currency = "EUR"
    elif "$" in subject and currency == "GBP":
        currency = "USD"

    if amount is None:
        return None

    # ── Sender name ───────────────────────────────────────────────────────────
    # After HTML stripping everything is one line, so use lookaheads to stop at the next label.
    # Chinese: 來自： Ka Chun Chu [stops before 已收到的金額 / 附註 / 匯款編號]
    # English: From: John Smith
    _NEXT_LABEL = r"(?=\s*(?:已收到的金額|匯款編號|附註|Amount received|Reference|看看Wise|Wise團隊|$))"
    sender_name = ""
    for pat in [
        r"來自[：:]\s*(.+?)" + _NEXT_LABEL,
        r"(?:From|Sender|Sent\s+by)[：:\s]+(.+?)" + _NEXT_LABEL,
        r"已收到來自(.+?)的[\d]",
        r"received.+?from\s+(.+?)(?:\s*$|\s+on\b|\s+via\b|\s+\()",
    ]:
        m = re.search(pat, body_text, re.IGNORECASE | re.DOTALL)
        if m:
            candidate = m.group(1).strip().rstrip(".")
            if len(candidate) >= 2 and not re.search(r"\b(wise|bank|payment)\b", candidate, re.IGNORECASE):
                sender_name = candidate
                break

    # ── Payment reference ─────────────────────────────────────────────────────
    # Chinese: 附註： Jason  [stops before 匯款編號]
    # English: Reference: chenwei
    _NEXT_REF_LABEL = r"(?=\s*(?:匯款編號|看看Wise|Wise團隊|$))"
    reference = ""
    for pat in [
        r"附註[：:]\s*(.+?)" + _NEXT_REF_LABEL,
        r"(?:Payment\s+reference|Reference|Ref)[：:\s]+(.+?)" + _NEXT_REF_LABEL,
    ]:
        m = re.search(pat, body_text, re.IGNORECASE | re.DOTALL)
        if m:
            candidate = m.group(1).strip()
            if candidate.lower() not in ("none", "n/a", "-", "no reference", ""):
                reference = candidate
                break

    return {
        "amount": amount,
        "currency": currency,
        "sender_name": sender_name,
        "reference": reference,
    }


# ─── Gmail fetch (synchronous — called via asyncio.to_thread) ────────────────

def _fetch_new_emails(service) -> list[tuple[str, str, str, datetime]]:
    """
    Query Gmail for Wise payment emails from the last 24 hours.
    Returns list of (msg_id, subject, body, received_at).
    """
    try:
        result = service.users().messages().list(
            userId="me",
            q="from:wise.com newer_than:1d",
            maxResults=20,
        ).execute()
    except HttpError as e:
        logger.error("Gmail list error: %s", e)
        return []

    emails = []
    for meta in result.get("messages", []):
        msg_id = meta["id"]
        try:
            msg = service.users().messages().get(
                userId="me", id=msg_id, format="full"
            ).execute()
        except HttpError as e:
            logger.warning("Gmail get error for %s: %s", msg_id, e)
            continue

        headers = {
            h["name"]: h["value"]
            for h in msg.get("payload", {}).get("headers", [])
        }
        subject = headers.get("Subject", "")

        if not re.search(r"received", subject, re.IGNORECASE):
            continue

        date_str = headers.get("Date", "")
        try:
            received_at = parsedate_to_datetime(date_str).astimezone(timezone.utc)
        except Exception:
            received_at = datetime.now(timezone.utc)

        body = _get_text_body(msg.get("payload", {}))
        emails.append((msg_id, subject, body, received_at))

    return emails


# ─── Payment processing ───────────────────────────────────────────────────────

async def _process_email(
    msg_id: str, subject: str, body: str, received_at: datetime
) -> dict | None:
    parsed = _parse_wise_email(subject, body)
    if not parsed:
        logger.warning("Could not parse Wise email %s (subject=%r)", msg_id, subject)
        return None

    amount = parsed["amount"]
    currency = parsed["currency"]
    sender_name = parsed["sender_name"]
    reference = parsed["reference"]
    tx_id = f"gmail-{msg_id}"

    logger.info(
        "Wise email %s — %.2f %s  sender=%r  ref=%r",
        msg_id, amount, currency, sender_name, reference,
    )

    db = SessionLocal()
    try:
        if db.query(Payment).filter_by(wise_transaction_id=tx_id).first():
            logger.debug("Already processed Gmail message %s — skipped", msg_id)
            return None

        payment = Payment(
            wise_transaction_id=tx_id,
            amount=amount,
            currency=currency,
            reference=reference,
            sender_name=sender_name,
            timestamp=received_at,
            status="pending",
        )
        db.add(payment)
        db.flush()

        sess = db.query(GameSession).order_by(GameSession.id.desc()).first()
        session_id = sess.id if sess else None
        matches = find_matches(reference, sender_name, amount, db, session_id)

        if matches and matches[0]["score"] >= 80:
            payment.matched_participant_id = matches[0]["participant"].id

        db.commit()
        payment_id = payment.id
    finally:
        db.close()

    return {
        "payment_id": payment_id,
        "matches": matches,
        "amount": amount,
        "currency": currency,
        "reference": reference,
        "sender_name": sender_name,
        "timestamp": received_at,
    }


# ─── Poll loop ────────────────────────────────────────────────────────────────

async def gmail_poll_loop(send_notification_fn) -> None:
    """
    Background asyncio task: poll Gmail every POLL_INTERVAL seconds.
    send_notification_fn(result: dict) is called for each new payment.
    """
    creds = _load_credentials()
    if not creds:
        logger.warning("GMAIL_TOKEN_JSON not set — Gmail polling disabled")
        return

    try:
        service = await asyncio.to_thread(build, "gmail", "v1", credentials=creds)
    except Exception:
        logger.exception("Failed to initialise Gmail service")
        return

    logger.info("Gmail polling started (interval=%ds)", POLL_INTERVAL)
    while True:
        try:
            emails = await asyncio.to_thread(_fetch_new_emails, service)
            for msg_id, subject, body, received_at in emails:
                result = await _process_email(msg_id, subject, body, received_at)
                if result:
                    try:
                        await send_notification_fn(result)
                    except Exception:
                        logger.exception("Telegram notification failed for Gmail msg %s", msg_id)
        except Exception:
            logger.exception("Gmail poll cycle error")
        await asyncio.sleep(POLL_INTERVAL)
