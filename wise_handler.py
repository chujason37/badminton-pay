from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

import httpx

from config import settings
from matching import find_matches
from models import GameSession, Payment, SessionLocal

logger = logging.getLogger(__name__)


# ─── Wise API call ────────────────────────────────────────────────────────────

async def _fetch_transaction_details(profile_id: int | str, transaction_id: int | str) -> dict:
    """
    Fetch full transaction details from the Wise API.

    The balances#credit webhook only carries the transaction ID, amount, and
    currency — sender name and payment reference are not included in the
    webhook payload itself.  We retrieve them here via:

      GET /v3/profiles/{profileId}/balance-transactions/{transactionId}

    Requires WISE_API_TOKEN to be set in .env.
    Returns an empty dict on any failure so the rest of the flow degrades
    gracefully (payment is still saved, just without reference/sender).
    """
    if not settings.wise_api_token:
        logger.warning(
            "WISE_API_TOKEN not set — cannot fetch transaction details. "
            "Reference and sender name will be empty."
        )
        return {}

    url = f"https://api.wise.com/v3/profiles/{profile_id}/balance-transactions/{transaction_id}"
    headers = {"Authorization": f"Bearer {settings.wise_api_token}"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)

        if resp.status_code == 200:
            return resp.json()

        # Log full response so we can diagnose field-name surprises
        logger.warning(
            "Wise transaction API returned %s for tx %s: %s",
            resp.status_code, transaction_id, resp.text[:500],
        )
    except Exception:
        logger.exception("Error calling Wise transaction API for tx %s", transaction_id)

    return {}


def _parse_tx_details(tx: dict) -> tuple[str, str]:
    """
    Extract (reference, sender_name) from a Wise transaction detail response.

    Wise uses camelCase in API responses.  Known field locations:
      tx["details"]["paymentReference"]  — payment reference entered by sender
      tx["details"]["senderName"]        — sender's account name
      tx["details"]["description"]       — fallback human-readable description
    """
    details = tx.get("details") or {}

    reference = (
        details.get("paymentReference")
        or details.get("payment_reference")
        or ""
    )
    sender_name = (
        details.get("senderName")
        or details.get("sender_name")
        or ""
    )
    return reference.strip(), sender_name.strip()


# ─── Webhook payload parsing ──────────────────────────────────────────────────

def _extract(payload: dict) -> dict | None:
    """
    Parse a Wise webhook payload into a normalised dict.
    Returns None if the event should be ignored.

    Supported event types:
      balances#credit        — money lands in a Wise balance account (primary)
      transfers#state-change — transfer state machine (secondary)

    NOTE: the webhook body does NOT contain sender name or payment reference.
    Those are fetched separately via the Wise API in process_wise_event().
    """
    event_type = payload.get("event_type", "")
    data = payload.get("data", {})

    if event_type == "balances#credit":
        pass
    elif event_type == "transfers#state-change":
        state = data.get("current_state", "")
        if state not in ("incoming_payment_waiting", "processing", "funds_converted"):
            return None
    else:
        return None

    resource = data.get("resource", {})

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
        "profile_id": resource.get("profile_id"),
        "account_id": resource.get("account_id"),
        "amount": amount,
        "currency": data.get("currency", "GBP"),
        "timestamp": timestamp,
    }


# ─── Main entry point ─────────────────────────────────────────────────────────

async def process_wise_event(
    payload: dict,
    raw_body: bytes | None = None,
    signature: str | None = None,
) -> dict | None:
    """
    Persist the payment and run matching.
    Returns a dict with everything needed to send the Telegram notification,
    or None if the event is ignored / duplicate.
    """
    # Always log the raw payload so field names are visible in server logs
    logger.info("Wise webhook received: %s", json.dumps(payload, ensure_ascii=False))

    pdata = _extract(payload)
    if pdata is None:
        logger.debug("Wise event ignored (event_type=%s)", payload.get("event_type"))
        return None

    # Fetch sender name + reference from Wise API (not in webhook body)
    reference = ""
    sender_name = ""
    if pdata["wise_transaction_id"] and pdata["profile_id"]:
        tx = await _fetch_transaction_details(pdata["profile_id"], pdata["wise_transaction_id"])
        if tx:
            reference, sender_name = _parse_tx_details(tx)
            logger.info(
                "Wise tx %s — sender: %r  reference: %r",
                pdata["wise_transaction_id"], sender_name, reference,
            )
        else:
            logger.warning(
                "Could not fetch details for Wise tx %s — proceeding without sender/reference",
                pdata["wise_transaction_id"],
            )

    db = SessionLocal()
    try:
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
            reference=reference,
            sender_name=sender_name,
            timestamp=pdata["timestamp"],
            status="pending",
        )
        db.add(payment)
        db.flush()

        sess = db.query(GameSession).order_by(GameSession.id.desc()).first()
        session_id = sess.id if sess else None

        matches = find_matches(reference, sender_name, pdata["amount"], db, session_id)

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
        "reference": reference,
        "sender_name": sender_name,
        "timestamp": pdata["timestamp"],
    }
