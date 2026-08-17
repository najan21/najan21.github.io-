"""
The Vault — a personal Telegram bot for tracking deposits.

Send it a dollar amount and it drops onto a pile that grows as you go,
with a brass "band" marking every round-number milestone you cross.
Data lives in a local SQLite file, scoped to whoever is chatting with it —
each Telegram chat only ever sees its own deposits.

SETUP
-----
1. Message @BotFather on Telegram, send /newbot, and follow the prompts.
   You'll get back a token that looks like: 123456789:AAExxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

2. (Recommended) Find your own Telegram numeric user ID by messaging
   @userinfobot, then add it to ALLOWED_USER_IDS below so only you can
   use the bot even if someone else finds its username.

3. Install dependencies:
       pip install -r requirements.txt

4. Set your token and run:
       export TELEGRAM_BOT_TOKEN="paste-your-token-here"
       python vault_bot.py

5. Open Telegram on your Mac or iPhone (same account) and message your bot.
   Both devices share the same chat history automatically — that's Telegram
   doing the syncing, not this script.

The script needs to keep running to receive messages. For casual use, leave
it running in a terminal. For it to work even when your Mac is asleep,
deploy it to a small always-on host (e.g. Railway, Render, a Raspberry Pi) —
ask me and I can walk you through that separately.
"""

import os
import io
import math
import sqlite3
import logging
from datetime import datetime, timezone
from contextlib import closing

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from PIL import Image, ImageDraw, ImageFont

# ============================== CONFIG ======================================

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")

# Optional: lock the bot to only your Telegram user ID(s). Find yours by
# messaging @userinfobot. Leave empty to allow anyone who finds the bot.
ALLOWED_USER_IDS: set[int] = set()  # e.g. {123456789}

DB_PATH = os.environ.get("VAULT_DB_PATH", "vault.db")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "").rstrip("/")
PORT = int(os.environ.get("PORT", "8000"))

# =============================================================================

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("vault_bot")

INK = (18, 38, 28)
INK_SOFT = (27, 51, 39)
BRASS = (199, 151, 43)
BRASS_LIGHT = (228, 199, 103)
BILL_LIGHT = (92, 138, 109)
BILL_DARK = (39, 70, 51)
PAPER = (243, 238, 221)
PAPER_FAINT = (243, 238, 221)

MILESTONE_STEPS = [50, 100, 250, 500, 1000, 2500, 5000, 10000, 25000, 50000,
                    100000, 250000, 500000, 1000000]


# ------------------------------ storage -------------------------------------

def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(get_conn()) as conn, conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS deposits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                amount REAL NOT NULL,
                ts TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_chat ON deposits(chat_id)")


def add_deposit(chat_id: int, amount: float) -> int:
    ts = datetime.now(timezone.utc).isoformat()
    with closing(get_conn()) as conn, conn:
        cur = conn.execute(
            "INSERT INTO deposits (chat_id, amount, ts) VALUES (?, ?, ?)",
            (chat_id, amount, ts),
        )
        return cur.lastrowid


def get_deposits(chat_id: int):
    with closing(get_conn()) as conn:
        rows = conn.execute(
            "SELECT id, amount, ts FROM deposits WHERE chat_id = ? ORDER BY ts ASC",
            (chat_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def delete_deposit(deposit_id: int, chat_id: int):
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM deposits WHERE id = ? AND chat_id = ?", (deposit_id, chat_id))


def get_last_deposit(chat_id: int):
    with closing(get_conn()) as conn:
        row = conn.execute(
            "SELECT id, amount, ts FROM deposits WHERE chat_id = ? ORDER BY ts DESC LIMIT 1",
            (chat_id,),
        ).fetchone()
        return dict(row) if row else None


def clear_deposits(chat_id: int):
    with closing(get_conn()) as conn, conn:
        conn.execute("DELETE FROM deposits WHERE chat_id = ?", (chat_id,))


# ------------------------------ helpers --------------------------------------

def fmt_money(n: float) -> str:
    return f"${n:,.2f}"


def milestone_step(total: float) -> int:
    for s in MILESTONE_STEPS:
        if total / s <= 10:
            return s
    return MILESTONE_STEPS[-1]


def is_allowed(update: Update) -> bool:
    if not ALLOWED_USER_IDS:
        return True
    user = update.effective_user
    return bool(user) and user.id in ALLOWED_USER_IDS


def parse_amount(text: str):
    cleaned = text.strip().replace("$", "").replace(",", "")
    try:
        amount = float(cleaned)
    except ValueError:
        return None
    if amount <= 0 or amount > 10_000_000:
        return None
    return round(amount, 2)


# ------------------------------ font loading ----------------------------------

_FONT_CANDIDATES_BOLD = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/Library/Fonts/Arial Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "C:\\Windows\\Fonts\\arialbd.ttf",
]
_FONT_CANDIDATES_MONO = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/Library/Fonts/Courier New Bold.ttf",
    "C:\\Windows\\Fonts\\consolab.ttf",
]

_font_cache = {}


def get_font(kind: str, size: int):
    key = (kind, size)
    if key in _font_cache:
        return _font_cache[key]
    candidates = _FONT_CANDIDATES_BOLD if kind == "bold" else _FONT_CANDIDATES_MONO
    font = None
    for path in candidates:
        try:
            font = ImageFont.truetype(path, size)
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def text_w(draw, text, font):
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0]


# ------------------------------ image rendering --------------------------------

def rounded_rect(draw, box, radius, fill):
    draw.rounded_rectangle(box, radius=radius, fill=fill)


def generate_pile_image(deposits_all) -> io.BytesIO:
    """deposits_all: list of dicts, chronological oldest -> newest."""
    width = 640
    pad = 26
    gap = 7
    header_h = 150

    total = sum(d["amount"] for d in deposits_all)
    step = milestone_step(total if total > 0 else 1)

    visible = deposits_all[-20:]
    hidden_count = len(deposits_all) - len(visible)
    base_running = sum(d["amount"] for d in deposits_all[: len(deposits_all) - len(visible)])

    running = base_running
    enriched = []
    for d in visible:
        running += d["amount"]
        enriched.append({**d, "cumulative": running})

    newest_first = list(reversed(enriched))

    def bar_height(amount):
        return max(30, min(92, 20 + math.sqrt(max(amount, 1)) * 5.6))

    draw_items = []
    for i, d in enumerate(newest_first):
        prev_cum = newest_first[i + 1]["cumulative"] if i + 1 < len(newest_first) else base_running
        draw_items.append(("bar", d))
        k = math.floor(d["cumulative"] / step)
        crossed = []
        while k * step > prev_cum and k * step <= d["cumulative"]:
            crossed.append(k * step)
            k -= 1
        for m in crossed:
            draw_items.append(("band", m))

    y = header_h
    heights = []
    for kind, payload in draw_items:
        h = bar_height(payload["amount"]) if kind == "bar" else 15
        heights.append(h)
        y += h + gap

    footer_h = 34 if hidden_count > 0 else 10
    final_h = int(y + footer_h)

    img = Image.new("RGB", (width, final_h), INK)
    draw = ImageDraw.Draw(img)

    # header
    f_eyebrow = get_font("mono", 15)
    f_title = get_font("bold", 26)
    f_total = get_font("mono", 40)
    f_count = get_font("mono", 14)

    draw.text((pad, 20), "TOTAL BANKED", font=f_eyebrow, fill=BRASS_LIGHT)
    draw.text((pad, 42), fmt_money(total), font=f_total, fill=BRASS_LIGHT)
    count_txt = f'{len(deposits_all)} deposit{"s" if len(deposits_all) != 1 else ""}'
    draw.text((pad, 100), count_txt, font=f_count, fill=(200, 196, 176))
    draw.line([(pad, header_h - 14), (width - pad, header_h - 14)], fill=(255, 255, 255, 20), width=1)

    # stack
    y = header_h
    f_bar = get_font("mono", 15)
    f_band = get_font("bold", 13)
    for (kind, payload), h in zip(draw_items, heights):
        box = (pad, y, width - pad, y + h)
        if kind == "bar":
            rounded_rect(draw, box, 9, BILL_DARK)
            inset = (pad + 2, y + 2, width - pad - 2, y + h - 2)
            draw.rectangle((inset[0], inset[1], inset[2], inset[1] + max(1, h // 2)), fill=BILL_LIGHT)
            rounded_rect(draw, box, 9, None) if False else None
            amt_txt = fmt_money(payload["amount"])
            draw.text((pad + 16, y + h / 2 - 10), amt_txt, font=f_bar, fill=PAPER)
            date_txt = datetime.fromisoformat(payload["ts"]).strftime("%b %d, %H:%M")
            dw = text_w(draw, date_txt, f_bar)
            draw.text((width - pad - 16 - dw, y + h / 2 - 8), date_txt, font=f_bar, fill=(210, 205, 185))
        else:
            rounded_rect(draw, box, 5, BRASS)
            label = f"{fmt_money(payload).replace('.00', '')} banked"
            lw = text_w(draw, label, f_band)
            draw.text((width / 2 - lw / 2, y + h / 2 - 8), label, font=f_band, fill=INK)
        y += h + gap

    if hidden_count > 0:
        note = f"+ {hidden_count} earlier deposit{'s' if hidden_count != 1 else ''} not shown"
        nw = text_w(draw, note, f_bar)
        draw.text((width / 2 - nw / 2, y + 6), note, font=f_bar, fill=(150, 165, 155))

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf


# ------------------------------ handlers ----------------------------------------

WELCOME = (
    "*The Vault* is open.\n\n"
    "Send me a dollar amount any time you deposit money and I'll drop it onto your pile.\n\n"
    "_50_\n_$120.50_\n\n"
    "Other things I understand:\n"
    "/total — your running total\n"
    "/history — your recent deposits\n"
    "/pile — resend the current pile\n"
    "/clear — wipe everything and start over"
)


async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text(WELCOME, parse_mode="Markdown")


async def total_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    deposits = get_deposits(update.effective_chat.id)
    total = sum(d["amount"] for d in deposits)
    await update.message.reply_text(
        f"Total banked: *{fmt_money(total)}* across {len(deposits)} deposit(s).",
        parse_mode="Markdown",
    )


async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    deposits = get_deposits(update.effective_chat.id)
    if not deposits:
        await update.message.reply_text("Nothing banked yet — send me an amount to get started.")
        return
    running = 0.0
    lines = []
    for d in deposits:
        running += d["amount"]
        when = datetime.fromisoformat(d["ts"]).strftime("%b %d, %H:%M")
        lines.append(f"{when}   +{fmt_money(d['amount'])}   → {fmt_money(running)}")
    recent = lines[-25:]
    prefix = f"(showing the last {len(recent)} of {len(lines)})\n\n" if len(lines) > len(recent) else ""
    await update.message.reply_text("```\n" + prefix + "\n".join(recent) + "\n```", parse_mode="Markdown")


async def pile_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    deposits = get_deposits(update.effective_chat.id)
    if not deposits:
        await update.message.reply_text("Nothing banked yet — send me an amount to get started.")
        return
    img = generate_pile_image(deposits)
    await update.message.reply_photo(photo=img)


async def clear_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("Yes, clear everything", callback_data="clear_confirm"),
        InlineKeyboardButton("Cancel", callback_data="clear_cancel"),
    ]])
    await update.message.reply_text("Clear every deposit? This can't be undone.", reply_markup=kb)


async def handle_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    text = update.message.text or ""
    amount = parse_amount(text)
    if amount is None:
        await update.message.reply_text(
            "Send me a dollar amount to deposit it — e.g. `50` or `$120.50`.",
            parse_mode="Markdown",
        )
        return

    deposit_id = add_deposit(update.effective_chat.id, amount)
    deposits = get_deposits(update.effective_chat.id)
    total = sum(d["amount"] for d in deposits)

    kb = InlineKeyboardMarkup([[InlineKeyboardButton("Undo", callback_data=f"undo:{deposit_id}")]])
    await update.message.reply_text(
        f"+{fmt_money(amount)} banked — total is now *{fmt_money(total)}*.",
        parse_mode="Markdown",
        reply_markup=kb,
    )
    img = generate_pile_image(deposits)
    await update.message.reply_photo(photo=img)


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not is_allowed(update):
        await query.answer()
        return
    chat_id = update.effective_chat.id
    data = query.data

    if data == "clear_confirm":
        clear_deposits(chat_id)
        await query.edit_message_text("Cleared. Your vault is empty — send an amount whenever you're ready.")
        await query.answer("Vault cleared")
        return

    if data == "clear_cancel":
        await query.edit_message_text("Cancelled — nothing was cleared.")
        await query.answer()
        return

    if data.startswith("undo:"):
        deposit_id = int(data.split(":", 1)[1])
        delete_deposit(deposit_id, chat_id)
        deposits = get_deposits(chat_id)
        total = sum(d["amount"] for d in deposits)
        await query.edit_message_text(f"Removed. Total is now {fmt_money(total)}.")
        await query.answer("Deposit removed")
        if deposits:
            img = generate_pile_image(deposits)
            await context.bot.send_photo(chat_id=chat_id, photo=img)
        return

    await query.answer()


async def unknown_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update):
        return
    await update.message.reply_text("I didn't catch that — try /total, /history, /pile, or just send an amount.")



app = FastAPI(title="The Vault Telegram Bot")


@app.on_event("startup")
async def startup():
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")
    if not PUBLIC_URL:
        raise RuntimeError("PUBLIC_URL is not set")

    init_db()

    # Initialize the python-telegram-bot application for webhook use.
    await bot_app.initialize()
    await bot_app.start()

    webhook_url = f"{PUBLIC_URL}/telegram/webhook"
    await bot_app.bot.set_webhook(
        url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
    )
    log.info("Telegram webhook set to %s", webhook_url)


@app.on_event("shutdown")
async def shutdown():
    try:
        await bot_app.bot.delete_webhook()
    finally:
        await bot_app.stop()
        await bot_app.shutdown()


@app.get("/")
async def home():
    return {
        "name": "The Vault",
        "status": "online",
        "message": "Telegram webhook is running.",
    }


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    data = await request.json()
    update = Update.de_json(data, bot_app.bot)
    await bot_app.process_update(update)
    return JSONResponse({"ok": True})


bot_app = Application.builder().token(BOT_TOKEN).build()

bot_app.add_handler(CommandHandler("start", start_cmd))
bot_app.add_handler(CommandHandler("total", total_cmd))
bot_app.add_handler(CommandHandler("history", history_cmd))
bot_app.add_handler(CommandHandler("pile", pile_cmd))
bot_app.add_handler(CommandHandler("clear", clear_cmd))
bot_app.add_handler(CallbackQueryHandler(button_callback))
bot_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount))
bot_app.add_handler(MessageHandler(filters.COMMAND, unknown_cmd))
