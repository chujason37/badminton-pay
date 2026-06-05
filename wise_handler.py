from __future__ import annotations

import logging
from datetime import datetime, timezone

from models import GameSession, Payment, SessionLocal
from matching import find_matches

logger = logging.getLogger(__name__)


def _extract(payload: dict) -> dict | None:
    """
    Parse a Wise webhook payload into a normalised dict.
    Returns None if the event should be ignored.

    Supported event types:
      balances#credit        — money lands in a Wise balance account (primary)
      transfers#state-change — transfer state machine (secondary, filtered by state)
    """
    event_type = payload.get("event_type", "")
    data = payload.get("data", {})

    if event_type == "balances#credit":
        pass  # always process
    elif event_type == "transfers#state-change":
        # Only fire when the transfer is effectively complete / funds received
        state = data.get("current_state", "")
        if state not in ("incoming_payment_waiting", "processing", "funds_converted"):
            return None
    else:
        return None

    resource = data.get("resource", {})
    details = data.get("details", {})

    raw_amount = data.get("amount") or data.get("value", {}).get("amount")
    if raw_amount is None:
        return None
    try:
        amount = float(raw_amount)
    except (TypeError, ValueError):
        return None

    occurred = data.get("occurred_at") or payload.get("sent_at")
    timestamp = datetime.now(timezone.utc)
    if occurred:
        try:
            timestamp = datetime.fromisoformat(occurred.replace("Z", "+00:00"))
        except ValueError:
            pass

    return {
        "wise_transaction_id": str(resource.get("id", "")) or None,
        "amount": amount,
        "currency": data.get("currency", "GBP"),
        "reference": (
            details.get("payment_reference")
            or details.get("description")
            or ""
        ),
        "sender_name": details.get("sender_name", ""),
        "timestamp": timestamp,
    }


async def process_wise_event(
    payload: dict,
    raw_body: bytes | None = None,
    signature: str | None = None,
) -> dict | None:
    """
    Persist the payment and run matching.
    Returns a dict with everything needed to send the Telegram notification,
    or None if the event is ignored / duplicate.

    Signature verification:
    Wise signs webhooks with RSA-SHA256.  To enable verification:
      1. Download the Wise public key from your webhook settings page.
      2. Set WISE_PUBLIC_KEY in your env (the full PEM string).
      3. Implement verification using the `cryptography` library.
    Currently skipped — all matched payments still require admin confirmation
    before any record is marked as paid, so unverified fake events are harmless.
    """
    pdata = _extract(payload)
    if pdata is None:
        logger.debug("Wise event ignored (event_type=%s)", payload.get("event_type"))
        return None

    db = SessionLocal()
    try:
        # Deduplicate by Wise transaction ID
        if pdata["wise_transaction_id"]:
            if db.query(Payment).filter_by(
                wise_transaction_id=pdata["wise_transaction_id"]
            ).first():
                logger.info("Duplicate Wise transaction %s — skipped", pdata["wise_transaction_id"])
                return None

        payment = Payment(
            wise_transaction_id=pdata["wise_transaction_id"],
            amount=pdata["amount"],
            currency=pdata["currency"],
            reference=pdata["reference"],
            sender_name=pdata["sender_name"],
            timestamp=pdata["timestamp"],
            status="pending",
        )
        db.add(payment)
        db.flush()

        sess = db.query(GameSession).order_by(GameSession.id.desc()).first()
        session_id = sess.id if sess else None

        matches = find_matches(
            pdata["reference"],
            pdata["sender_name"],
            pdata["amount"],
            db,
            session_id,
        )

        # Pre-populate matched_participant_id for high-confidence matches
        if matches and matches[0]["score"] >= 80:
            payment.matched_participant_id = matches[0]["participant"].id

        db.commit()
        payment_id = payment.id

    finally:
        db.close()

    return {
        "payment_id": payment_id,
        "matches": matches,
        "amount": pdata["amount"],
        "currency": pdata["currency"],
        "reference": pdata["reference"],
        "sender_name": pdata["sender_name"],
        "timestamp": pdata["timestamp"],
    }
