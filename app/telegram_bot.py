"""Telegram-пульт: даёшь номер и задачу — бот звонит, присылает вопросы и итог."""
import asyncio
import logging
import re
import time
from html import escape

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from . import config as S
from .call_session import CALLS, CallSession

log = logging.getLogger("tg")

HELP = """<b>Бот-звонилка</b> 📞

Просто напиши номер и задачу одним сообщением:
<code>+380671234567 Забронируй столик на двоих на завтра на 19:00 на имя Алексей. Лучше у окна.</code>

Во время звонка:
• бот присылает, что говорят обе стороны;
• если нужен выбор — придут кнопки. Не ответишь за {timeout} сек — бот решит сам;
• любое твоё сообщение во время звонка бот получит как подсказку («скажи, что будем вчетвером»);
• /hangup — положить трубку.

Свой номер:
/mynumber +380XXXXXXXXX — привязать твой мобильный, чтобы бот звонил с него

Настройки:
/settings — показать все настройки
/set КЛЮЧ значение — поменять (например <code>/set GEMINI_API_KEY AIza…</code>)
/set КЛЮЧ — сбросить на значение из .env
Сообщения с ключами бот сразу удаляет из чата."""

PHONE_RE = re.compile(r"(\+?\(?\d[\d\s\-()]{7,}\d)")


def normalize_number(raw: str) -> str:
    digits = re.sub(r"[^\d+]", "", raw)
    if digits.startswith("+"):
        return digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    cc = S.get("DEFAULT_COUNTRY_CODE")
    if digits.startswith("0"):
        return f"+{cc}{digits[1:]}"
    if cc and digits.startswith(cc):
        return "+" + digits
    return "+" + digits


def active_calls() -> list[CallSession]:
    return [c for c in CALLS.values() if not c.finished]


class Notifier:
    def __init__(self, app: Application):
        self.app = app

    @property
    def chat_id(self) -> int:
        return int(S.get("TELEGRAM_OWNER_ID"))

    async def text(self, html: str, silent: bool = False, buttons=None):
        return await self.app.bot.send_message(
            self.chat_id, html, parse_mode=ParseMode.HTML, disable_notification=silent, reply_markup=buttons
        )

    async def question(self, call: CallSession, qid: str, question: str, options: list[str], timeout: int):
        rows = [[InlineKeyboardButton(o[:60], callback_data=f"a|{call.id[:12]}|{qid}|{i}")] for i, o in enumerate(options)]
        rows.append([InlineKeyboardButton("🤖 Реши сам", callback_data=f"a|{call.id[:12]}|{qid}|auto")])
        await self.text(
            f"❓ <b>{escape(question)}</b>\n\nВыбери вариант или ответь текстом. "
            f"Через {timeout} сек бот решит сам.",
            buttons=InlineKeyboardMarkup(rows),
        )

    async def call_finished(self, call: CallSession):
        names = {
            "busy": "📵 Занято",
            "no-answer": "📵 Не взяли трубку",
            "failed": "❌ Звонок не прошёл",
            "canceled": "❌ Звонок отменён",
        }
        icons = {"success": "✅ Готово", "partial": "🟡 Частично", "failed": "❌ Не получилось", "voicemail": "📼 Автоответчик"}
        dur = int(time.time() - call.started_at)
        if call.status in names and not call.transcript:
            head = names[call.status]
        else:
            head = icons.get(call.outcome or "", "📞 Звонок завершён")
        msg = f"{head} — <code>{call.number}</code> ({dur // 60}:{dur % 60:02d})\n\n"
        if call.summary:
            msg += f"<b>Итог:</b> {escape(call.summary)}"
        elif call.transcript:
            msg += "<i>Бот не успел подвести итог. Расшифровка ниже.</i>"
        await self.text(msg)
        if call.transcript and S.get("LIVE_TRANSCRIPT") != "1":
            body = escape(call.transcript_text())
            for i in range(0, len(body), 3800):
                await self.text(f"<pre>{body[i:i + 3800]}</pre>", silent=True)


def is_owner(update: Update) -> bool:
    owner = S.get("TELEGRAM_OWNER_ID")
    return bool(owner) and str(update.effective_user.id) == owner


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not S.get("TELEGRAM_OWNER_ID"):
        S.set("TELEGRAM_OWNER_ID", str(update.effective_user.id))
        await update.message.reply_text("Ты записан как владелец бота. Чужие сообщения бот будет игнорировать.")
    if not is_owner(update):
        return
    await update.message.reply_text(HELP.format(timeout=S.get_int("ASK_TIMEOUT")), parse_mode=ParseMode.HTML)
    missing = [k for k in ("GEMINI_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "PUBLIC_URL") if not S.get(k)]
    if missing:
        await update.message.reply_text(
            "⚠️ Ещё не заданы: " + ", ".join(missing) + "\nЗадай через /set КЛЮЧ значение"
            + (" (номер — через /mynumber +380…)" if "TWILIO_FROM_NUMBER" in missing else "") + "."
        )


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if is_owner(update):
        await update.message.reply_text(S.describe(), parse_mode=ParseMode.HTML)


async def cmd_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    if not ctx.args:
        await update.message.reply_text("Формат: /set КЛЮЧ значение. Список ключей — /settings")
        return
    key = ctx.args[0].upper()
    value = " ".join(ctx.args[1:]).strip()
    if key not in S.DEFAULTS or key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID"):
        await update.message.reply_text("Такой ключ нельзя менять отсюда. Список — /settings")
        return
    S.set(key, value)
    secret = S.DEFAULTS[key][2]
    if secret:
        try:
            await update.message.delete()
        except Exception:
            pass
    shown = S.mask(value) if secret else (value or "сброшено")
    await ctx.bot.send_message(update.effective_chat.id, f"✅ {key} = {shown}")


def _is_verified(number: str) -> bool:
    from .call_session import twilio_client

    return bool(twilio_client().outgoing_caller_ids.list(phone_number=number, limit=1))


async def cmd_mynumber(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Привязать свой мобильный: Twilio звонит тебе, ты вводишь код — и бот звонит с твоим номером."""
    if not is_owner(update):
        return
    if not (S.get("TWILIO_ACCOUNT_SID") and S.get("TWILIO_AUTH_TOKEN")):
        await update.message.reply_text("Сначала задай TWILIO_ACCOUNT_SID и TWILIO_AUTH_TOKEN через /set.")
        return
    if not ctx.args:
        cur = S.get("TWILIO_FROM_NUMBER") or "не задан"
        await update.message.reply_text(
            f"Сейчас бот звонит с номера: {cur}\n\nЧтобы звонил с твоего: /mynumber +380XXXXXXXXX"
        )
        return
    number = normalize_number("".join(ctx.args))
    from .call_session import twilio_client

    if await asyncio.to_thread(_is_verified, number):
        S.set("TWILIO_FROM_NUMBER", number)
        await update.message.reply_text(f"✅ Номер {number} уже подтверждён. Теперь бот звонит с него.")
        return
    try:
        req = await asyncio.to_thread(
            lambda: twilio_client().validation_requests.create(phone_number=number, friendly_name="callbot", call_delay=5)
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Twilio не принял номер: {e}")
        return
    await update.message.reply_text(
        f"📲 Через несколько секунд Twilio позвонит на {number}.\n"
        f"Возьми трубку и введи на клавиатуре код: <b>{req.validation_code}</b>\n\n"
        "Я сам проверю, когда номер подтвердится.",
        parse_mode=ParseMode.HTML,
    )
    for _ in range(36):  # ждём до 3 минут
        await asyncio.sleep(5)
        try:
            if await asyncio.to_thread(_is_verified, number):
                S.set("TWILIO_FROM_NUMBER", number)
                await update.message.reply_text(
                    f"✅ Готово! Номер {number} подтверждён. Теперь у всех, кому звонит бот, высвечивается твой номер."
                )
                return
        except Exception:
            pass
    await update.message.reply_text("⏱ Подтверждение не пришло. Попробуй ещё раз: /mynumber " + number)


async def cmd_hangup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    calls = active_calls()
    for c in calls:
        await c.hangup()
    await update.message.reply_text("Кладу трубку." if calls else "Сейчас нет активных звонков.")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        return
    text = update.message.text.strip()
    calls = active_calls()

    # 1) ответ на открытый вопрос
    for c in reversed(calls):
        if c.pending and c.answer(None, text):
            await update.message.reply_text("👌 Передал боту.")
            return

    # 2) новый звонок
    m = next((x for x in PHONE_RE.finditer(text) if len(re.sub(r"\D", "", x.group(1))) >= 9), None)
    if m:
        task = " ".join((text[: m.start()] + " " + text[m.end():]).split()).strip(" ,.:-—")
        if not task:
            await update.message.reply_text("Напиши ещё, что нужно сделать в этом звонке.")
            return
        await start_call(update, ctx, normalize_number(m.group(1)), task)
        return

    # 3) подсказка в идущий звонок
    if calls:
        await calls[-1].owner_inbox.put(text)
        await update.message.reply_text("👌 Подсказку передал боту.")
        return

    await update.message.reply_text("Не нашёл номер. Пример:\n+380671234567 Узнай, работают ли они в воскресенье.")


async def start_call(update: Update, ctx: ContextTypes.DEFAULT_TYPE, number: str, task: str):
    missing = [k for k in ("GEMINI_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "PUBLIC_URL") if not S.get(k)]
    if missing:
        await update.message.reply_text("⚠️ Сначала задай: " + ", ".join(missing))
        return
    call = CallSession(number, task, ctx.application.bot_data["notifier"])
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Положить трубку", callback_data=f"h|{call.id[:12]}")]])
    await update.message.reply_text(
        f"📞 Звоню на <code>{number}</code>\n<b>Задача:</b> {escape(task)}",
        parse_mode=ParseMode.HTML,
        reply_markup=kb,
    )
    try:
        await call.dial()
    except Exception as e:
        call.status = "failed"
        call.finished = True
        await update.message.reply_text(f"❌ Twilio не принял звонок: {e}")


def _find(prefix: str) -> CallSession | None:
    return next((c for cid, c in CALLS.items() if cid.startswith(prefix)), None)


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_owner(update):
        await q.answer()
        return
    parts = q.data.split("|")
    call = _find(parts[1])
    if parts[0] == "h":
        if call and not call.finished:
            await call.hangup()
            await q.answer("Кладу трубку")
        else:
            await q.answer("Звонок уже завершён")
        return
    if parts[0] == "a" and call:
        qid, idx = parts[2], parts[3]
        item = call.pending.get(qid)
        if not item:
            await q.answer("Поздно — бот уже решил сам")
            return
        choice = "Реши сам, как лучше для задачи." if idx == "auto" else item[1][int(idx)]
        call.answer(qid, choice)
        await q.answer("Передал боту")
        await q.edit_message_text(f"{q.message.text_html}\n\n➡️ <b>{escape(choice)}</b>", parse_mode=ParseMode.HTML)
        return
    await q.answer()


def build_app() -> Application:
    app = Application.builder().token(S.get("TELEGRAM_BOT_TOKEN")).concurrent_updates(True).build()
    app.bot_data["notifier"] = Notifier(app)
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("hangup", cmd_hangup))
    app.add_handler(CommandHandler("mynumber", cmd_mynumber, block=False))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
