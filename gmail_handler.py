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
    for entity, replacement in [("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
                                  ("&nbsp;", " "), ("&quot;", '"'), ("&#39;", "'")]:
        text = text.replace(entity, replacement)
    return re.sub(r"\s+", " ", text).strip()


# ─── Email parsing ────────────────────────────────────────────────────────────

def _parse_wise_email(subject: str, body: str) -> dict | None:
    """
    Extract payment details from a Wise "You've received" notification email.
    Returns dict with amount/currency/sender_name/reference, or None if not a payment email.
    """
    body_text = _strip_html(body) if "<html" in body.lower() or "<div" in body.lower() else body

    # Amount from subject: "You've received £20.00" or "received 20.00 GBP"
    amt_m = re.search(
        r"received\s+[£$€]?\s*([\d,]+\.?\d*)\s*(GBP|USD|EUR|HKD|SGD|AUD|CAD)?",
        subject, re.IGNORECASE,
    )
    if not amt_m:
        return None

    try:
        amount = float(amt_m.group(1).replace(",", ""))
    except ValueError:
        return None

    # Currency: symbol takes priority over code suffix
    currency = "GBP"
    if "£" in subject:
        currency = "GBP"
    elif "€" in subject:
        currency = "EUR"
    elif "$" in subject:
        currency = "USD"
    elif amt_m.group(2):
        currency = amt_m.group(2).upper()

    # Sender name — try subject first, then body
    sender_name = ""
    subj_sender = re.search(
        r"received.+?from\s+(.+?)(?:\s*$|\s+on\b|\s+via\b|\s+\()", subject, re.IGNORECASE
    )
    if subj_sender:
        sender_name = subj_sender.group(1).strip().rstrip(".")
    if not sender_name:
        for pat in [
            r"(?:From|Sender|Sent by)[:\s]+([A-Za-z一-鿿][^\n<]{2,50})",
            r"payment\s+from\s+([A-Za-z一-鿿][^\n<]{2,50})",
            r"received\s+from\s+([A-Za-z一-鿿][^\n<]{2,50})",
        ]:
            m = re.search(pat, body_text, re.IGNORECASE)
            if m:
                candidate = m.group(1).strip().rstrip(".")
                # Reject obvious non-names
                if len(candidate) >= 2 and not re.search(r"\b(wise|bank|payment)\b", candidate, re.IGNORECASE):
                    sender_name = candidate
                    break

    # Payment reference — in body
    reference = ""
    for pat in [
        r"(?:Payment\s+reference|Reference|Ref)[:\s]+([^\n<]{1,100})",
        r"reference\s+number[:\s]+([^\n<]{1,100})",
    ]:
        m = re.search(pat, body_text, re.IGNORECASE)
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
            q="from:wise.com subject:received newer_than:1d",
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
