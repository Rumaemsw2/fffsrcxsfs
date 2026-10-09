"""Flow AI — Telegram-бот с ИИ и режимом «удалёнки».

Личка:
  /start, /help — справка
  /ai <пароль>  — удалёнка: выбираешь чат кнопками, дальше всё, что пишешь
                  боту в личку, уходит в этот чат от его имени
  /keyout       — выключить удалёнку
  /chats        — список известных чатов
  /clear <id>   — очистить память чата (админ)

Группы: бот общается сам — отвечает на обращения и вписывается в разговор.
"""

import asyncio
import logging
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from dotenv import load_dotenv

load_dotenv()

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from ai import chat as ai_chat, extract_facts, parse_reply, should_speak
from media import generate_image, search_image
from memory import Memory

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("flow-ai")

for _var in ("TELEGRAM_BOT_TOKEN", "AI_API_KEY"):
    if not os.getenv(_var):
        raise SystemExit(f"Не задана переменная окружения {_var}")

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BOT_NAME = os.getenv("BOT_NAME", "КПК")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
AUTONOMOUS_MINUTES = max(0, int(os.getenv("AUTONOMOUS_MINUTES", "45")))
SILENCE_SECONDS = int(os.getenv("SILENCE_SECONDS", "35"))
SPEAK_CHANCE = float(os.getenv("SPEAK_CHANCE", "0.35"))
AUTONOMOUS_CHANCE = float(os.getenv("AUTONOMOUS_CHANCE", "0.3"))

memory = Memory()
last_auto: dict[int, float] = {}

HELP_TEXT = (
    f"Я — {BOT_NAME}, живой участник чатов на ИИ.\n\n"
    "В группах просто пиши как обычно — я сам решаю, когда вписаться. "
    "Обращение по имени или ответ на моё сообщение — отвечу точно.\n\n"
    "Удалёнка (управление из лички):\n"
    "/ai <пароль> — выбрать чат и писать туда от моего имени\n"
    "/keyout — выключить удалёнку\n"
    "/chats — список известных мне чатов\n"
    "/clear <id> — забыть переписку чата (админ)\n"
)


# ----------------------------- утилиты -----------------------------

async def _download_photo(msg) -> bytes | None:
    try:
        f = await msg.photo[-1].get_file()
        return bytes(await f.download_as_bytearray())
    except Exception:
        log.exception("Не смог скачать фото")
        return None


def _chat_keyboard(user_id):
    chats = memory.list_chats()
    if not chats:
        return None
    rows = [[InlineKeyboardButton(t or str(cid), callback_data=f"pick:{cid}")] for cid, t in chats]
    if memory.get_proxy_chat(user_id):
        rows.append([InlineKeyboardButton("🚪 Выйти из чата", callback_data="exitpick")])
    return InlineKeyboardMarkup(rows)


def _is_direct(msg, text, bot) -> bool:
    if msg.reply_to_message and msg.reply_to_message.from_user:
        if msg.reply_to_message.from_user.id == bot.id:
            return True
    lowered = (text or "").lower()
    if f"@{(bot.username or '').lower()}" in lowered:
        return True
    words = lowered.split()
    first = words[0].strip(",.!?") if words else ""
    if first and first in {BOT_NAME.lower(), f"@{(bot.username or '').lower()}"}:
        return True
    return lowered.startswith(BOT_NAME.lower())


async def _deliver(bot, chat_id, raw, reply_to=None, remember=True):
    """Разбирает ответ ИИ и отправляет текст/стикер/картинку."""
    text, stickers, gen_prompt, search_query = parse_reply(raw)
    if text:
        try:
            kwargs = {"reply_to_message_id": reply_to} if reply_to else {}
            await bot.send_message(chat_id, text, **kwargs)
        except Exception:
            log.exception("Не смог отправить текст")
        if remember:
            memory.add(chat_id, BOT_NAME, text)
    if stickers:
        file_id = memory.pick_sticker(stickers[0])
        if file_id:
            try:
                await bot.send_sticker(chat_id, file_id)
            except Exception:
                pass
    image_data = None
    if gen_prompt:
        image_data = await generate_image(gen_prompt)
    elif search_query:
        image_data = await search_image(search_query)
    if image_data:
        try:
            await bot.send_photo(chat_id, image_data)
        except Exception:
            log.exception("Не смог отправить картинку")


async def _save_facts(chat_id):
    try:
        facts = await extract_facts(memory.context(chat_id))
        for item in facts:
            if isinstance(item, dict) and item.get("username") and item.get("fact"):
                memory.add_fact(chat_id, str(item["username"])[:100], str(item["fact"])[:500])
    except Exception:
        log.exception("Ошибка сохранения фактов")


# ----------------------------- команды -----------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(HELP_TEXT)


async def cmd_ai(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private":
        await update.message.reply_text("Эта команда — только в личке со мной.")
        return
    user = update.effective_user
    if not ADMIN_PASSWORD:
        await update.message.reply_text("Удалёнка выключена: ADMIN_PASSWORD не задан на сервере.")
        return
    if not memory.is_admin(user.id):
        password = (context.args or [""])[0]
        if password != ADMIN_PASSWORD:
            await update.message.reply_text("Использование: /ai <пароль>")
            return
        memory.add_admin(user.id)
    kb = _chat_keyboard(user.id)
    if not kb:
        await update.message.reply_text(
            "Пока не знаю ни одной группы. Добавь меня в группу, напиши там что-нибудь — потом повтори /ai."
        )
        return
    target = memory.get_proxy_chat(user.id)
    if target:
        text = f"Сейчас пишу в «{memory.chat_title(target)}». Выбери другой чат или выйди."
    else:
        text = "Выбери чат для удалёнки:"
    await update.message.reply_text(text, reply_markup=kb)


async def on_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    user_id = q.from_user.id
    if q.data == "exitpick":
        memory.clear_proxy(user_id)
        await q.edit_message_text("Вышел из чата. Чтобы пообщаться с ИИ — просто пиши. Вернуться в удалёнку: /ai")
        return
    if not memory.is_admin(user_id):
        await q.edit_message_text("Нет доступа. Сначала /ai <пароль>.")
        return
    try:
        chat_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await q.edit_message_text("Ошибка выбора.")
        return
    memory.set_proxy_chat(user_id, chat_id)
    await q.edit_message_text(
        f"✅ Пишу в «{memory.chat_title(chat_id)}».\n"
        "Всё, что ты пришлёшь мне в личку (текст или фото), уйдёт в этот чат от моего имени.\n"
        "Сменить чат: /ai • Выключить совсем: /keyout"
    )


async def cmd_keyout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    memory.remove_admin(user.id)
    memory.clear_proxy(user.id)
    await update.message.reply_text("Удалёнка выключена.")


async def cmd_chats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chats = memory.list_chats(30)
    if not chats:
        await update.message.reply_text("Чатов пока нет.")
        return
    lines = [f"• {t} — `{cid}`" for cid, t in chats]
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def cmd_clear(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not memory.is_admin(user.id):
        await update.message.reply_text("Только для админов (/ai <пароль>).")
        return
    try:
        cid = int((context.args or [""])[0])
    except ValueError:
        await update.message.reply_text("Использование: /clear <chat_id> (id смотри в /chats)")
        return
    memory.clear_chat(cid)
    await update.message.reply_text(f"Память чата {cid} очищена.")


# ----------------------------- личка -----------------------------

async def on_private_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    text = (msg.text or "").strip()
    if not text:
        return

    # удалёнка: пересылаем в выбранный чат от имени бота
    if memory.is_admin(user.id):
        target = memory.get_proxy_chat(user.id)
        if target:
            try:
                await context.bot.send_message(target, text)
                await msg.reply_text("✅")
            except Exception:
                log.exception("Не смог отправить в чат")
                await msg.reply_text("⚠️ Не удалось отправить — возможно, меня удалили из чата.")
            return

    memory.touch_chat(msg.chat_id, user.first_name or "личка", "private")
    memory.add(msg.chat_id, user.first_name or user.username or "user", text)
    await respond_private(context, msg.chat_id, text, image=None, reply_to=msg.message_id)


async def on_private_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    user = update.effective_user
    caption = (msg.caption or "").strip()

    if memory.is_admin(user.id):
        target = memory.get_proxy_chat(user.id)
        if target:
            data = await _download_photo(msg)
            if not data:
                await msg.reply_text("⚠️ Не смог скачать фото.")
                return
            try:
                await context.bot.send_photo(target, data, caption=caption or None)
                await msg.reply_text("✅")
            except Exception:
                log.exception("Не смог отправить фото")
                await msg.reply_text("⚠️ Не удалось отправить фото в чат.")
            return

    memory.touch_chat(msg.chat_id, user.first_name or "личка", "private")
    data = await _download_photo(msg)
    image = (data, "image/jpeg") if data else None
    memory.add(msg.chat_id, user.first_name or user.username or "user", "[фото] " + caption if caption else "[фото]")
    await respond_private(context, msg.chat_id, caption or "[фото]", image=image, reply_to=msg.message_id)


async def respond_private(context, chat_id, text, image=None, reply_to=None):
    await context.bot.send_chat_action(chat_id, "typing")
    try:
        raw = await ai_chat(memory.context(chat_id), text, direct=True, bot_name=BOT_NAME, image=image)
    except Exception:
        log.exception("Ошибка ИИ")
        await context.bot.send_message(chat_id, "ИИ сейчас недоступен, попробуй чуть позже.")
        return
    await _deliver(context.bot, chat_id, raw, reply_to=reply_to)


# ----------------------------- группы -----------------------------

async def on_group_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    chat_obj = update.effective_chat
    if not msg or chat_obj.type not in ("group", "supergroup"):
        return

    user = msg.from_user
    if user and user.is_bot:
        return

    memory.touch_chat(chat_obj.id, chat_obj.title, chat_obj.type)
    username = (user.first_name or user.username or "кто-то") if user else "канал"
    text = (msg.text or msg.caption or "").strip()

    image = None
    if msg.photo:
        data = await _download_photo(msg)
        if data:
            image = (data, "image/jpeg")

    stored = text
    if image:
        stored = ("[фото] " + text) if text else "[фото]"
    if stored:
        memory.add(chat_obj.id, username, stored)
    if not text and not image:
        return

    if _is_direct(msg, text, context.bot):
        await respond_group(context, chat_obj.id, text, image, reply_to=msg.message_id)
    elif random.random() < SPEAK_CHANCE:
        try:
            want = await should_speak(memory.context(chat_obj.id), text or "[фото]", bot_name=BOT_NAME)
        except Exception:
            log.exception("should_speak error")
            want = False
        if want:
            await respond_group(context, chat_obj.id, text, image)


async def respond_group(context, chat_id, text, image, reply_to=None):
    await context.bot.send_chat_action(chat_id, "typing")
    try:
        raw = await ai_chat(memory.context(chat_id), text or "[фото]", direct=True, bot_name=BOT_NAME, image=image)
    except Exception:
        log.exception("Ошибка ИИ")
        return
    await _deliver(context.bot, chat_id, raw, reply_to=reply_to)
    if random.random() < 0.15:
        asyncio.create_task(_save_facts(chat_id))


# ----------------------------- автономка -----------------------------

async def autonomous_tick(context: ContextTypes.DEFAULT_TYPE):
    now = time.time()
    for chat_id, _title in memory.list_chats(50):
        since_last = memory.seconds_since_last(chat_id)
        if since_last is None or since_last < SILENCE_SECONDS:
            continue
        if now - last_auto.get(chat_id, 0) < AUTONOMOUS_MINUTES * 60:
            continue
        last_auto[chat_id] = now
        if random.random() > AUTONOMOUS_CHANCE:
            continue
        try:
            raw = await ai_chat(memory.context(chat_id), "", direct=False, bot_name=BOT_NAME, autonomous=True)
        except Exception:
            log.exception("Ошибка автономного ответа")
            continue
        text, _s, _g, _p = parse_reply(raw)
        if text:
            try:
                await context.bot.send_message(chat_id, text)
                memory.add(chat_id, BOT_NAME, text)
            except Exception:
                log.exception("Не смог отправить автономное сообщение")


# ----------------------------- запуск -----------------------------

class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, *args):
        pass


def start_health_server():
    port = int(os.getenv("PORT", "10000"))
    try:
        server = HTTPServer(("0.0.0.0", port), _Health)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        log.info("Health-check сервер на порту %s", port)
    except OSError as e:
        log.warning("Health-check сервер не запущен: %s", e)


async def post_init(app):
    start_health_server()
    for cid, _t in memory.list_chats(50):
        last_auto[cid] = time.time()
    sets = [s.strip() for s in os.getenv("STICKER_SETS", "").split(",") if s.strip()]
    for name in sets:
        try:
            sticker_set = await app.bot.get_sticker_set(name)
            for st in sticker_set.stickers:
                memory.add_sticker(st.file_id, st.emoji, name)
            log.info("Стикеры: %s — %d шт.", name, len(sticker_set.stickers))
        except Exception as e:
            log.warning("Набор стикеров %s недоступен: %s", name, e)
    me = await app.bot.get_me()
    log.info("Бот @%s запущен", me.username)


def main():
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("ai", cmd_ai))
    app.add_handler(CommandHandler("keysi", cmd_ai))  # старое имя тоже работает
    app.add_handler(CommandHandler("keyout", cmd_keyout))
    app.add_handler(CommandHandler("chats", cmd_chats))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CallbackQueryHandler(on_pick, pattern=r"^(pick:-?\d+|exitpick)$"))

    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND, on_private_text))
    app.add_handler(MessageHandler(filters.ChatType.PRIVATE & filters.PHOTO, on_private_photo))
    app.add_handler(
        MessageHandler(
            filters.ChatType.GROUPS & ~filters.COMMAND & (filters.TEXT | filters.CAPTION | filters.PHOTO),
            on_group_message,
        )
    )

    if AUTONOMOUS_MINUTES > 0:
        app.job_queue.run_repeating(autonomous_tick, interval=60, first=90)

    log.info("Стартую…")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)


if __name__ == "__main__":
    main()
