from __future__ import annotations

import logging
import re
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

from config import settings
from matching import find_matches, to_pinyin
from models import GameSession, Participant, Payment, SessionEntry, SessionLocal

logger = logging.getLogger(__name__)

# ConversationHandler states for /session
S_DATE, S_LOCATION, S_PLAYERS = range(3)

# ConversationHandler states for /newsession
NS_MESSAGE, NS_FEE, NS_EXCEPTIONS, NS_CONFIRM = range(4)


# ─── Auth guard ──────────────────────────────────────────────────────────────

def _admin_only(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not update.effective_chat or update.effective_chat.id != settings.telegram_chat_id:
            return None
        return await func(update, context)
    return wrapper


# ─── DB helpers ──────────────────────────────────────────────────────────────

def _latest_session(db):
    return db.query(GameSession).order_by(GameSession.id.desc()).first()


def _get_or_create_participant(db, name: str) -> tuple[Participant, bool]:
    p = db.query(Participant).filter_by(chinese_name=name).first()
    if p:
        return p, False
    p = Participant(chinese_name=name, pinyin=to_pinyin(name), known_references=[])
    db.add(p)
    db.flush()
    return p, True


# ─── 接龙 message parser ─────────────────────────────────────────────────────

def _parse_signup(text: str) -> dict | None:
    """
    Parse a WeChat 接龙 signup message.
    Handles full-width (：) and half-width (:) colons.
    Returns {"date", "location", "players": list[str]} or None if no players found.
    """
    date = location = ""
    players = []
    for line in text.splitlines():
        line = line.strip()
        if m := re.match(r"时间[：:]\s*(.+)", line):
            date = m.group(1).strip()
        elif m := re.match(r"地点[：:]\s*(.+)", line):
            location = m.group(1).strip()
        elif m := re.match(r"^\d+[\.、。]\s*(.+)", line):
            name = m.group(1).strip()
            if name:
                players.append(name)
    if not players:
        return None
    return {"date": date or "Unknown", "location": location or "Unknown", "players": players}


# ─── /start ──────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🏸 *Badminton Pay Bot*\n\n"
        "/newsession — create session from 接龙 message\n"
        "/session — create session manually\n"
        "/unpaid — who hasn't paid\n"
        "/paid — who has paid\n"
        "/status <name> — individual status\n\n"
        f"Your chat ID: `{update.effective_chat.id}`",
        parse_mode="Markdown",
    )


# ─── /session conversation ───────────────────────────────────────────────────

@_admin_only
async def cmd_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📅 Session date? (e.g. `2026-06-07` or `Saturday`)", parse_mode="Markdown")
    return S_DATE


async def _got_date(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["sess_date"] = update.message.text.strip()
    await update.message.reply_text("📍 Location?")
    return S_LOCATION


async def _got_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["sess_location"] = update.message.text.strip()
    await update.message.reply_text(
        "👥 Enter players — one per line: `name amount`\n\nExample:\n`陈威 20\n张三 20\n李四 15`",
        parse_mode="Markdown",
    )
    return S_PLAYERS


async def _got_players(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = SessionLocal()
    try:
        sess = GameSession(
            date=context.user_data["sess_date"],
            location=context.user_data["sess_location"],
        )
        db.add(sess)
        db.flush()

        added, errors = [], []
        for line in update.message.text.strip().splitlines():
            parts = line.strip().rsplit(None, 1)
            if len(parts) != 2:
                errors.append(f"⚠️ Skipped: `{line}`")
                continue
            name, amt_str = parts
            try:
                amt = float(amt_str)
            except ValueError:
                errors.append(f"⚠️ Bad amount: `{line}`")
                continue

            participant, _ = _get_or_create_participant(db, name)
            db.add(
                SessionEntry(
                    session_id=sess.id,
                    participant_id=participant.id,
                    amount_owed=amt,
                )
            )
            added.append(f"• {name} £{amt:.0f}")

        db.commit()
        msg = (
            f"✅ *Session created*\n"
            f"📅 {sess.date}  📍 {sess.location}\n"
            f"👥 {len(added)} players:\n" + "\n".join(added)
        )
        if errors:
            msg += "\n\n" + "\n".join(errors)
        await update.message.reply_text(msg, parse_mode="Markdown")
    finally:
        db.close()
    return ConversationHandler.END


async def _session_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelled.")
    return ConversationHandler.END


# ─── /newsession conversation ────────────────────────────────────────────────

@_admin_only
async def cmd_newsession(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📋 Paste the WeChat 接龙 signup message:")
    return NS_MESSAGE


async def _ns_got_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    parsed = _parse_signup(update.message.text)
    if not parsed:
        await update.message.reply_text(
            "❌ Couldn't parse that. Make sure the message has `时间:`, `地点:`, "
            "and a numbered player list.\n\nTry again or /cancel.",
            parse_mode="Markdown",
        )
        return NS_MESSAGE  # let them retry

    context.user_data["ns"] = parsed
    player_list = "\n".join(f"  {i + 1}. {p}" for i, p in enumerate(parsed["players"]))
    await update.message.reply_text(
        f"✅ *Parsed successfully*\n"
        f"📅 {parsed['date']}\n"
        f"📍 {parsed['location']}\n"
        f"👥 {len(parsed['players'])} players:\n{player_list}\n\n"
        f"💰 How much does each person owe? (e.g. `20`)",
        parse_mode="Markdown",
    )
    return NS_FEE


async def _ns_got_fee(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        fee = float(update.message.text.strip().lstrip("£$€"))
    except ValueError:
        await update.message.reply_text("❌ Enter a number, e.g. `20`", parse_mode="Markdown")
        return NS_FEE

    ns = context.user_data["ns"]
    ns["fee"] = fee
    ns["amounts"] = {}

    player_lines = "\n".join(f"• {p} £{fee:g}" for p in ns["players"])
    await update.message.reply_text(
        f"👥 *Players (default £{fee:g} each):*\n{player_lines}\n\n"
        "Anyone with a special price? Reply with name and amount, one per line:\n"
        "`Tim 5\n李上 12`\n\n"
        "Or reply `ok` to use the default for everyone.",
        parse_mode="Markdown",
    )
    return NS_EXCEPTIONS


async def _ns_got_exceptions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ns = context.user_data["ns"]
    text = update.message.text.strip()

    if text.lower() not in ("ok", "y", "yes"):
        bad_lines = []
        for line in text.splitlines():
            parts = line.strip().rsplit(None, 1)
            if len(parts) == 2:
                name, amt_str = parts
                try:
                    ns["amounts"][name.strip()] = float(amt_str.strip().lstrip("£$€"))
                except ValueError:
                    bad_lines.append(line)
            elif line.strip():
                bad_lines.append(line)

        if bad_lines:
            await update.message.reply_text(
                "❌ Couldn't parse these lines:\n"
                + "\n".join(f"`{l}`" for l in bad_lines)
                + "\n\nFix and try again, or reply `ok` to skip.",
                parse_mode="Markdown",
            )
            return NS_EXCEPTIONS

    await update.message.reply_text(_session_summary(ns), parse_mode="Markdown")
    return NS_CONFIRM


def _session_summary(ns: dict) -> str:
    fee = ns["fee"]
    amounts = ns["amounts"]
    lines = []
    total = 0.0
    for name in ns["players"]:
        amount = amounts.get(name, fee)
        marker = " ✏️" if name in amounts else ""
        lines.append(f"• {name} £{amount:g}{marker}")
        total += amount
    return (
        f"📋 *Session summary*\n"
        f"📅 {ns['date']}  📍 {ns['location']}\n"
        f"👥 {len(ns['players'])} players  💰 Total: £{total:g}\n\n"
        + "\n".join(lines)
        + "\n\nReply `yes` to confirm and create, or /cancel to abort."
    )


async def _ns_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip().lower()
    if text in ("yes", "y", "ok", "confirm", "✅", "yeah", "yep"):
        return await _ns_create_session(update, context)
    await update.message.reply_text("❌ Session cancelled.")
    context.user_data.pop("ns", None)
    return ConversationHandler.END


async def _ns_create_session(update: Update, context: ContextTypes.DEFAULT_TYPE):
    ns = context.user_data.pop("ns", None)
    if not ns:
        await update.message.reply_text("❌ Session data lost. Start again with /newsession.")
        return ConversationHandler.END

    fee = ns["fee"]
    amounts = ns["amounts"]
    db = SessionLocal()
    try:
        sess = GameSession(date=ns["date"], location=ns["location"])
        db.add(sess)
        db.flush()

        added = []
        for name in ns["players"]:
            amount = amounts.get(name, fee)
            participant, _ = _get_or_create_participant(db, name)
            db.add(
                SessionEntry(
                    session_id=sess.id,
                    participant_id=participant.id,
                    amount_owed=amount,
                )
            )
            marker = " ✏️" if name in amounts else ""
            added.append(f"• {name} £{amount:g}{marker}")

        db.commit()
        await update.message.reply_text(
            f"✅ *Session created*\n"
            f"📅 {sess.date}  📍 {sess.location}\n"
            f"👥 {len(added)} players:\n" + "\n".join(added),
            parse_mode="Markdown",
        )
    finally:
        db.close()
    return ConversationHandler.END


# ─── /unpaid ─────────────────────────────────────────────────────────────────

@_admin_only
async def cmd_unpaid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = SessionLocal()
    try:
        sess = _latest_session(db)
        if not sess:
            await update.message.reply_text("No sessions yet. Use /session to create one.")
            return
        unpaid = [e for e in sess.entries if not e.paid]
        if not unpaid:
            await update.message.reply_text(f"✅ Everyone has paid for {sess.date}!")
            return
        lines = [f"• {e.participant.chinese_name} — £{e.amount_owed:.0f}" for e in unpaid]
        await update.message.reply_text(
            f"❌ *Unpaid ({len(unpaid)})*  {sess.date} @ {sess.location}\n\n" + "\n".join(lines),
            parse_mode="Markdown",
        )
    finally:
        db.close()


# ─── /paid ───────────────────────────────────────────────────────────────────

@_admin_only
async def cmd_paid(update: Update, context: ContextTypes.DEFAULT_TYPE):
    db = SessionLocal()
    try:
        sess = _latest_session(db)
        if not sess:
            await update.message.reply_text("No sessions yet.")
            return
        paid = [e for e in sess.entries if e.paid]
        if not paid:
            await update.message.reply_text(f"No payments recorded yet for {sess.date}.")
            return
        lines = [f"• {e.participant.chinese_name} — £{e.amount_owed:.0f}" for e in paid]
        await update.message.reply_text(
            f"✅ *Paid ({len(paid)})*  {sess.date}\n\n" + "\n".join(lines),
            parse_mode="Markdown",
        )
    finally:
        db.close()


# ─── /status ─────────────────────────────────────────────────────────────────

@_admin_only
async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /status <name>")
        return
    name = " ".join(context.args)
    db = SessionLocal()
    try:
        sess = _latest_session(db)
        if not sess:
            await update.message.reply_text("No sessions yet.")
            return
        participant = db.query(Participant).filter(Participant.chinese_name.contains(name)).first()
        if not participant:
            await update.message.reply_text(f"'{name}' not found in participant database.")
            return
        entry = next((e for e in sess.entries if e.participant_id == participant.id), None)
        if not entry:
            await update.message.reply_text(f"{participant.chinese_name} is not in the current session ({sess.date}).")
            return
        icon = "✅" if entry.paid else "❌"
        text = (
            f"{icon} *{participant.chinese_name}*\n"
            f"Session: {sess.date} @ {sess.location}\n"
            f"Amount: £{entry.amount_owed:.0f}\n"
            f"Status: {'Paid' if entry.paid else 'Unpaid'}"
        )
        if entry.paid and entry.paid_at:
            text += f"\nPaid at: {entry.paid_at.strftime('%H:%M %d %b')}"
        await update.message.reply_text(text, parse_mode="Markdown")
    finally:
        db.close()


# ─── Inline keyboard callbacks ───────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    parts = query.data.split(":")
    action = parts[0]

    db = SessionLocal()
    try:
        if action == "confirm":
            payment_id = int(parts[1])
            payment = db.get(Payment, payment_id)
            if payment and payment.matched_participant_id:
                await _do_mark_paid(query, db, payment, payment.matched_participant_id)
            else:
                await query.edit_message_text("❌ Payment record not found.")

        elif action == "select":
            payment_id, participant_id = int(parts[1]), int(parts[2])
            payment = db.get(Payment, payment_id)
            if payment:
                await _do_mark_paid(query, db, payment, participant_id)
            else:
                await query.edit_message_text("❌ Payment record not found.")

        elif action == "identify":
            payment_id = int(parts[1])
            context.bot_data["awaiting_name_for"] = payment_id
            await query.edit_message_text(
                query.message.text + "\n\n✏️ *Reply with the player's name:*",
                parse_mode="Markdown",
            )
    finally:
        db.close()


async def _do_mark_paid(query, db, payment: Payment, participant_id: int):
    participant = db.get(Participant, participant_id)
    if not participant:
        await query.edit_message_text("❌ Participant not found.")
        return

    sess = _latest_session(db)
    entry = None
    if sess:
        entry = next(
            (e for e in sess.entries if e.participant_id == participant_id and not e.paid),
            None,
        )
    if entry:
        entry.paid = True
        entry.paid_at = datetime.utcnow()
        entry.payment_id = payment.id

    payment.status = "confirmed"
    payment.matched_participant_id = participant_id

    # Learn this reference string for future matching
    if payment.reference:
        refs = list(participant.known_references or [])
        if payment.reference not in refs:
            refs.append(payment.reference)
            participant.known_references = refs

    db.commit()
    sess_label = f" · {sess.date}" if sess else ""
    await query.edit_message_text(
        f"✅ *{participant.chinese_name}* marked as paid — £{payment.amount:.2f}{sess_label}",
        parse_mode="Markdown",
    )


# ─── Free-text handler (awaiting name for unknown payer) ─────────────────────

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_chat or update.effective_chat.id != settings.telegram_chat_id:
        return
    payment_id = context.bot_data.get("awaiting_name_for")
    if not payment_id:
        return

    name = update.message.text.strip()
    db = SessionLocal()
    try:
        payment = db.get(Payment, payment_id)
        if not payment:
            await update.message.reply_text("❌ Payment not found.")
            context.bot_data.pop("awaiting_name_for", None)
            return

        participant, is_new = _get_or_create_participant(db, name)

        # Learn the reference
        if payment.reference:
            refs = list(participant.known_references or [])
            if payment.reference not in refs:
                refs.append(payment.reference)
                participant.known_references = refs

        sess = _latest_session(db)
        if sess:
            entry = next((e for e in sess.entries if e.participant_id == participant.id), None)
            if not entry:
                # Player wasn't in the session list — add them with the paid amount
                entry = SessionEntry(
                    session_id=sess.id,
                    participant_id=participant.id,
                    amount_owed=payment.amount,
                    paid=True,
                    paid_at=datetime.utcnow(),
                    payment_id=payment.id,
                )
                db.add(entry)
            else:
                entry.paid = True
                entry.paid_at = datetime.utcnow()
                entry.payment_id = payment.id

        payment.status = "confirmed"
        payment.matched_participant_id = participant.id
        db.commit()
        context.bot_data.pop("awaiting_name_for", None)

        extra = " _(new participant added)_" if is_new else ""
        await update.message.reply_text(
            f"✅ *{name}* marked as paid — £{payment.amount:.2f}{extra}",
            parse_mode="Markdown",
        )
    finally:
        db.close()


# ─── Outbound notification (called from main.py on Wise event) ───────────────

async def send_payment_notification(
    application: Application,
    payment_id: int,
    matches: list[dict],
    amount: float,
    currency: str,
    reference: str,
    sender_name: str,
    timestamp: datetime,
) -> None:
    symbol = "£" if currency == "GBP" else f"{currency} "
    time_str = timestamp.strftime("%H:%M, %d %b %Y")

    header = (
        "🏸 *New Payment Received*\n"
        f"💰 Amount: {symbol}{amount:.2f}\n"
        f"👤 Reference: `{reference or '(none)'}`\n"
        f"🏦 Sender: {sender_name or '(unknown)'}\n"
        f"⏰ {time_str}"
    )

    top_score = matches[0]["score"] if matches else 0

    if top_score >= 80:
        top = matches[0]
        name = top["participant"].chinese_name
        text = (
            header
            + f"\n\n🔍 Best match: *{name}* ({top_score:.0f}% confidence)"
        )
        keyboard = [
            [
                InlineKeyboardButton(f"✅ {name}", callback_data=f"confirm:{payment_id}"),
                InlineKeyboardButton("❌ Wrong person", callback_data=f"identify:{payment_id}"),
            ]
        ]

    elif top_score >= 50:
        text = header + "\n\n🔍 *Top matches — pick one:*"
        buttons = []
        for m in matches[:3]:
            n = m["participant"].chinese_name
            s = m["score"]
            text += f"\n• {n} ({s:.0f}%)"
            buttons.append(
                InlineKeyboardButton(
                    n, callback_data=f"select:{payment_id}:{m['participant'].id}"
                )
            )
        keyboard = [
            buttons,
            [InlineKeyboardButton("None of these", callback_data=f"identify:{payment_id}")],
        ]

    else:
        text = header + "\n\n❓ *No match found* — who sent this?"
        keyboard = [
            [InlineKeyboardButton("Identify payer", callback_data=f"identify:{payment_id}")]
        ]

    await application.bot.send_message(
        chat_id=settings.telegram_chat_id,
        text=text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


# ─── Application factory ─────────────────────────────────────────────────────

def create_application() -> Application:
    app = Application.builder().token(settings.telegram_bot_token).build()

    session_conv = ConversationHandler(
        entry_points=[CommandHandler("session", cmd_session)],
        states={
            S_DATE: [MessageHandler(filters.TEXT & ~filters.COMMAND, _got_date)],
            S_LOCATION: [MessageHandler(filters.TEXT & ~filters.COMMAND, _got_location)],
            S_PLAYERS: [MessageHandler(filters.TEXT & ~filters.COMMAND, _got_players)],
        },
        fallbacks=[CommandHandler("cancel", _session_cancel)],
    )

    newsession_conv = ConversationHandler(
        entry_points=[CommandHandler("newsession", cmd_newsession)],
        states={
            NS_MESSAGE: [MessageHandler(filters.TEXT & ~filters.COMMAND, _ns_got_message)],
            NS_FEE: [MessageHandler(filters.TEXT & ~filters.COMMAND, _ns_got_fee)],
            NS_EXCEPTIONS: [MessageHandler(filters.TEXT & ~filters.COMMAND, _ns_got_exceptions)],
            NS_CONFIRM: [MessageHandler(filters.TEXT & ~filters.COMMAND, _ns_confirm)],
        },
        fallbacks=[CommandHandler("cancel", _session_cancel)],
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_start))
    app.add_handler(session_conv)
    app.add_handler(newsession_conv)
    app.add_handler(CommandHandler("unpaid", cmd_unpaid))
    app.add_handler(CommandHandler("paid", cmd_paid))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(handle_callback))
    # Catch-all text handler — only active when awaiting a payer name
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    return app
