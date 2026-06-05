from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone

import httpx

from config import settings
from matching import find_matches
from models import GameSession, Payment, SessionLocal

logger = logging.getLogger(__name__)


# ─── Wise API helpers ─────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    return re.sub(r"<[^>]+>", "", text).strip()


def _parse_activity_amount(primary_amount: str) -> float | None:
    """Extract numeric value from Wise activity primaryAmount string.

    Incoming: '<positive>+ 5 GBP</positive>'  → 5.0
    Outgoing: '47.50 GBP'                     → 47.5 (ignored — not positive-tagged)
    """
    m = re.search(r"[\d]+\.?[\d]*", _strip_html(primary_amount))
    return float(m.group()) if m else None


# ─── Statement API (full details, SCA-protected on UK accounts) ───────────────

async def _fetch_from_statement(
    profile_id: int | str,
    account_id: int | str,
    transaction_id: str,
    currency: str,
    timestamp: datetime,
    headers: dict,
) -> dict | None:
    """
    Try GET /v1/profiles/{profileId}/balance-statements/{balanceId}/statement.json.
    Returns the matching transaction dict, None on SCA-403, or {} on other errors.
    """
    interval_start = (timestamp - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    interval_end   = (timestamp + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.999Z")

    url = (
        f"https://api.wise.com/v1/profiles/{profile_id}"
        f"/balance-statements/{account_id}/statement.json"
    )
    params = {
        "currency": currency,
        "intervalStart": interval_start,
        "intervalEnd": interval_end,
        "type": "COMPACT",
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, params=params, headers=headers)

        if resp.status_code == 403 and resp.headers.get("x-2fa-approval"):
            logger.warning(
                "Wise statement API needs SCA approval (tx %s) — will try activities fallback",
                transaction_id,
            )
            return None  # signal: try fallback

        if resp.status_code != 200:
            logger.warning(
                "Wise statement API returned %s (tx %s): %s",
                resp.status_code, transaction_id, resp.text[:300],
            )
            return {}

        transactions = resp.json().get("transactions", [])
        tx_id_str = str(transaction_id)

        # Prefer an exact referenceNumber match (e.g. "DEPOSIT-12589271")
        for tx in transactions:
            if tx.get("type") == "CREDIT" and tx_id_str in tx.get("referenceNumber", ""):
                return tx

        # Fall back to the closest DEPOSIT-type CREDIT in the window
        deposit_txs = [
            t for t in transactions
            if t.get("type") == "CREDIT"
            and (t.get("details") or {}).get("type") == "DEPOSIT"
        ]
        if deposit_txs:
            def _delta(t: dict) -> float:
                try:
                    tx_time = datetime.fromisoformat(t["date"].replace("Z", "+00:00"))
                    return abs((tx_time - timestamp).total_seconds())
                except Exception:
                    return float("inf")
            return min(deposit_txs, key=_delta)

        logger.warning("No matching deposit in statement window for tx %s", transaction_id)
        return {}

    except Exception:
        logger.exception("Error calling Wise statement API for tx %s", transaction_id)
        return {}


# ─── Activities API (sender name only, no SCA required) ──────────────────────

async def _fetch_from_activities(
    profile_id: int | str,
    amount: float,
    currency: str,
    timestamp: datetime,
    headers: dict,
) -> dict:
    """
    Fallback: GET /v1/profiles/{profileId}/activities.
    Returns a synthetic tx dict with senderName when found; {} otherwise.
    No SCA required, but payment reference is unavailable via this endpoint.
    """
    since = (timestamp - timedelta(minutes=30)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    until = (timestamp + timedelta(minutes=10)).strftime("%Y-%m-%dT%H:%M:%S.999Z")

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(
                f"https://api.wise.com/v1/profiles/{profile_id}/activities",
                params={"since": since, "until": until, "size": 50},
                headers=headers,
            )

        if resp.status_code != 200:
            logger.warning("Activities API returned %s: %s", resp.status_code, resp.text[:200])
            return {}

        for act in resp.json().get("activities", []):
            primary = act.get("primaryAmount", "")
            # Incoming payments are tagged with <positive>
            if "<positive>" not in primary:
                continue
            act_amount = _parse_activity_amount(primary)
            if act_amount is None or abs(act_amount - amount) > 0.01:
                continue
            sender = _strip_html(act.get("title", ""))
            if sender:
                logger.info("Got sender name from activities fallback: %r", sender)
                return {"details": {"senderName": sender, "paymentReference": ""}}

        logger.warning("No matching incoming activity found for amount %.2f %s", amount, currency)
    except Exception:
        logger.exception("Error calling activities API")

    return {}


# ─── Main fetch (statement → activities fallback) ─────────────────────────────

async def _fetch_transaction_details(
    profile_id: int | str,
    account_id: int | str,
    transaction_id: str,
    currency: str,
    timestamp: datetime,
    amount: float,
) -> dict:
    """
    Fetch sender name and payment reference for an incoming payment.

    Primary:  balance-statements endpoint (full details including paymentReference).
              Requires one-time SCA approval for UK/EEA accounts.
    Fallback: activities endpoint (sender name only, always accessible).
    """
    if not settings.wise_api_token:
        logger.warning("WISE_API_TOKEN not set — cannot fetch transaction details.")
        return {}

    headers = {"Authorization": f"Bearer {settings.wise_api_token}"}

    tx = await _fetch_from_statement(
        profile_id, account_id, transaction_id, currency, timestamp, headers
    )

    if tx is None:
        # SCA blocked — try activities for at least the sender name
        tx = await _fetch_from_activities(profile_id, amount, currency, timestamp, headers)

    return tx or {}


# ─── Parse tx dict → (reference, sender_name) ────────────────────────────────

def _parse_tx_details(tx: dict) -> tuple[str, str]:
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
    logger.info("Wise webhook received: %s", json.dumps(payload, ensure_ascii=False))

    pdata = _extract(payload)
    if pdata is None:
        logger.debug("Wise event ignored (event_type=%s)", payload.get("event_type"))
        return None

    reference = ""
    sender_name = ""
    if pdata["wise_transaction_id"] and pdata["profile_id"] and pdata["account_id"]:
        tx = await _fetch_transaction_details(
            pdata["profile_id"],
            pdata["account_id"],
            pdata["wise_transaction_id"],
            pdata["currency"],
            pdata["timestamp"],
            pdata["amount"],
        )
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
    elif not pdata["account_id"]:
        logger.warning("Wise webhook missing account_id — cannot fetch transaction details")

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
