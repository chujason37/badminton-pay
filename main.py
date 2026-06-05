from __future__ import annotations

import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request, status
from telegram.ext import Application

from bot import create_application, send_payment_notification
from models import init_db
from wise_handler import process_wise_event

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

telegram_app: Application | None = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    global telegram_app
    init_db()
    telegram_app = create_application()
    async with telegram_app:
        await telegram_app.start()
        await telegram_app.updater.start_polling()
        logger.info("Telegram bot polling started")
        yield
        await telegram_app.updater.stop()
        await telegram_app.stop()
    logger.info("Telegram bot stopped")


app = FastAPI(title="Badminton Pay", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/webhook/wise")
async def wise_webhook(request: Request):
    raw_body = await request.body()
    signature = (
        request.headers.get("X-Signature-SHA256")
        or request.headers.get("X-Wise-Signature")
    )
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid JSON")

    result = await process_wise_event(payload, raw_body, signature)

    if result and telegram_app:
        try:
            await send_payment_notification(
                telegram_app,
                result["payment_id"],
                result["matches"],
                result["amount"],
                result["currency"],
                result["reference"],
                result["sender_name"],
                result["timestamp"],
            )
        except Exception:
            logger.exception("Failed to send Telegram notification")

    return {"status": "ok"}


@app.post("/webhook/wise/test")
async def test_payment(request: Request):
    """
    Simulate an incoming Wise payment — useful during development.

    POST body (all optional):
      { "amount": 20.0, "reference": "chenwei", "sender_name": "John Smith", "currency": "GBP" }
    """
    body = await request.json()
    fake_id = f"test-{uuid.uuid4().hex[:8]}"
    payload = {
        "event_type": "balances#credit",
        "data": {
            "resource": {"id": fake_id, "type": "balance"},
            "amount": body.get("amount", 20.0),
            "currency": body.get("currency", "GBP"),
            "details": {
                "payment_reference": body.get("reference", ""),
                "sender_name": body.get("sender_name", ""),
            },
            "occurred_at": datetime.now(timezone.utc).isoformat(),
        },
    }
    result = await process_wise_event(payload)
    if result and telegram_app:
        await send_payment_notification(
            telegram_app,
            result["payment_id"],
            result["matches"],
            result["amount"],
            result["currency"],
            result["reference"],
            result["sender_name"],
            result["timestamp"],
        )
    return {
        "status": "ok",
        "payment_id": result["payment_id"] if result else None,
        "top_match": (
            {
                "name": result["matches"][0]["participant"].chinese_name,
                "score": result["matches"][0]["score"],
            }
            if result and result["matches"]
            else None
        ),
    }
