"""Telegram-пульт: даёшь номер и задачу — бот звонит, присылает вопросы и итог.
У каждого пользователя свои ключи (Gemini, Twilio) и свой номер."""
import asyncio
import logging
import re
import time
from html import escape

from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    Update,
)
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
from . import store
from .call_session import CALLS, CallSession, twilio_client

log = logging.getLogger("tg")

HELP = """<b>Бот-звонилка</b> 📞

<b>1. Задай свой ключ Gemini</b> (бесплатно на aistudio.google.com/apikey):
<code>/key ТВОЙ_КЛЮЧ</code>

<b>2. Звонок</b> — номер и задача одним сообщением:
<code>+380671234567 Забронируй столик на двоих на завтра на 19:00 на имя Алексей. Лучше у окна.</code>

Во время звонка:
• бот присылает, что говорят обе стороны;
• если нужен выбор — придут кнопки. Не ответишь за {timeout} сек — бот решит сам;
• любое твоё сообщение во время звонка — подсказка боту («скажи, что будем вчетвером»);
• /hangup — положить трубку.

<b>Свои настройки</b> (видны только тебе):
/setup — инструкция по настройке
/key КЛЮЧ — свой ключ Gemini
/mynumber — звонить с твоего номера (кнопкой)
/settings — показать настройки
/set КЛЮЧ значение — поменять (например <code>/set GEMINI_VOICE Kore</code>)
Сообщения с ключами бот сразу удаляет из чата."""

OWNER_HELP = """

<b>Для владельца:</b>
/access open|closed — пускать всех или только разрешённых
/allow ID, /deny ID, /users — список доступа
/set SHARE_TWILIO 1 — разрешить другим звонить через твой Twilio (платишь ты)"""

PHONE_RE = re.compile(r"(\+?\(?\d[\d\s\-()]{7,}\d)")
SECRET_KEYS = {k for k, v in S.DEFAULTS.items() if v[2]}


# ---------- пользователи и их настройки ----------
def owner_id() -> int | None:
    o = S.get("TELEGRAM_OWNER_ID")
    return int(o) if o else None


def is_owner(uid: int) -> bool:
    return owner_id() == uid


def allowed(uid: int) -> bool:
    if is_owner(uid) or S.get("ACCESS") == "open":
        return True
    return str(uid) in [x.strip() for x in S.get("ALLOWED_USERS").split(",") if x.strip()]


class Profile:
    """Настройки конкретного пользователя: его значения → (для владельца или общих полей) значения владельца."""

    def __init__(self, uid: int, data: dict):
        self.uid = uid
        self.data = data

    def get(self, key: str) -> str:
        if is_owner(self.uid):
            return S.get(key)
        val = (self.data.get("profile") or {}).get(key, "")
        if val:
            return val
        if key in S.PRIVATE_KEYS:
            if key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN") and S.get("SHARE_TWILIO") == "1":
                return S.get(key)
            return ""
        return S.get(key)

    def get_int(self, key: str) -> int:
        try:
            return int(float(self.get(key)))
        except ValueError:
            return int(S.DEFAULTS[key][0])


async def profile(ctx: ContextTypes.DEFAULT_TYPE, uid: int) -> Profile:
    return Profile(uid, await store.load(ctx.bot, uid))


async def set_value(ctx: ContextTypes.DEFAULT_TYPE, uid: int, key: str, value: str) -> None:
    """Владелец меняет общие настройки, остальные — только свои."""
    if is_owner(uid):
        S.set(key, value)
        await store.save_global(ctx.bot)
        return
    data = dict(await store.load(ctx.bot, uid))
    prof = dict(data.get("profile") or {})
    if value:
        prof[key] = value
    else:
        prof.pop(key, None)
    data["profile"] = prof
    await store.save(ctx.bot, uid, data)


def normalize_number(raw: str, cc: str | None = None) -> str:
    digits = re.sub(r"[^\d+]", "", raw)
    if digits.startswith("+"):
        return digits
    if digits.startswith("00"):
        return "+" + digits[2:]
    cc = cc if cc is not None else S.get("DEFAULT_COUNTRY_CODE")
    if digits.startswith("0"):
        return f"+{cc}{digits[1:]}"
    return "+" + digits


def active_calls(uid: int | None = None) -> list[CallSession]:
    return [c for c in CALLS.values() if not c.finished and (uid is None or c.user_id == uid)]


async def _delete(update: Update) -> None:
    try:
        await update.message.delete()
    except Exception:
        pass


# ---------- уведомления о звонке ----------
class Notifier:
    def __init__(self, app: Application, chat_id: int, cfg):
        self.app = app
        self.chat_id = chat_id
        self.cfg = cfg

    async def text(self, html: str, silent: bool = False, buttons=None):
        return await self.app.bot.send_message(
            self.chat_id, html, parse_mode=ParseMode.HTML, disable_notification=silent, reply_markup=buttons
        )

    async def question(self, call: CallSession, qid: str, question: str, options: list[str], timeout: int):
        rows = [[InlineKeyboardButton(o[:60], callback_data=f"a|{call.id[:12]}|{qid}|{i}")] for i, o in enumerate(options)]
        rows.append([InlineKeyboardButton("🤖 Реши сам", callback_data=f"a|{call.id[:12]}|{qid}|auto")])
        await self.text(
            f"❓ <b>{escape(question)}</b>\n\nВыбери вариант или ответь текстом. Через {timeout} сек бот решит сам.",
            buttons=InlineKeyboardMarkup(rows),
        )

    async def call_finished(self, call: CallSession):
        names = {"busy": "📵 Занято", "no-answer": "📵 Не взяли трубку", "failed": "❌ Звонок не прошёл", "canceled": "❌ Звонок отменён"}
        icons = {"success": "✅ Готово", "partial": "🟡 Частично", "failed": "❌ Не получилось", "voicemail": "📼 Автоответчик"}
        dur = int(time.time() - call.started_at)
        head = names[call.status] if call.status in names and not call.transcript else icons.get(call.outcome or "", "📞 Звонок завершён")
        msg = f"{head} — <code>{call.number}</code> ({dur // 60}:{dur % 60:02d})\n\n"
        if call.summary:
            msg += f"<b>Итог:</b> {escape(call.summary)}"
        elif call.transcript:
            msg += "<i>Бот не успел подвести итог. Расшифровка ниже.</i>"
        await self.text(msg)
        if call.transcript and self.cfg.get("LIVE_TRANSCRIPT") != "1":
            body = escape(call.transcript_text())
            for i in range(0, len(body), 3800):
                await self.text(f"<pre>{body[i:i + 3800]}</pre>", silent=True)


# ---------- доступ ----------
async def gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> bool:
    uid = update.effective_user.id
    if allowed(uid):
        return True
    if update.message:
        await update.message.reply_text(
            f"🔒 Доступ к боту закрыт. Твой ID: <code>{uid}</code>\nЯ отправил владельцу запрос.",
            parse_mode=ParseMode.HTML,
        )
        oid = owner_id()
        if oid and not ctx.bot_data.setdefault("asked", set()).__contains__(uid):
            ctx.bot_data["asked"].add(uid)
            u = update.effective_user
            name = escape(" ".join(x for x in [u.first_name, u.last_name] if x) + (f" (@{u.username})" if u.username else ""))
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Разрешить", callback_data=f"allow|{uid}")]])
            await ctx.bot.send_message(oid, f"🙋 {name} (ID <code>{uid}</code>) просит доступ к боту.", parse_mode=ParseMode.HTML, reply_markup=kb)
    return False


# ---------- команды ----------
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not owner_id():
        S.set("TELEGRAM_OWNER_ID", str(uid))
        await store.write_owner(ctx.bot, uid)
        await store.save_global(ctx.bot)
        await update.message.reply_text("👑 Ты владелец бота. Другие пользователи смогут войти только с твоего разрешения.")
    if not await gate(update, ctx):
        return
    p = await profile(ctx, uid)
    if missing_for_call(p):
        # первым делом — инструкция по настройке
        await update.message.reply_text(setup_guide(p), parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    text = HELP.format(timeout=p.get_int("ASK_TIMEOUT")) + (OWNER_HELP if is_owner(uid) else "")
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)


def setup_guide(p: Profile) -> str:
    """Пошаговая инструкция по настройке; выполненные шаги отмечены ✅."""
    shared = S.get("SHARE_TWILIO") == "1" and not is_owner(p.uid)
    has_gemini = bool(p.get("GEMINI_API_KEY"))
    has_twilio = bool(p.get("TWILIO_ACCOUNT_SID") and p.get("TWILIO_AUTH_TOKEN"))
    has_number = bool(re.fullmatch(r"\+\d{9,15}", p.get("TWILIO_FROM_NUMBER") or ""))
    mark = lambda done: "✅" if done else "⬜️"

    parts = ["<b>🛠 Настройка бота — 3 шага</b>\n"]
    parts.append(
        f"{mark(has_gemini)} <b>Шаг 1. Ключ Gemini (бесплатно)</b>\n"
        "1) Открой aistudio.google.com/apikey и войди через Gmail\n"
        "2) Нажми <b>Create API key</b> и скопируй ключ (обычно начинается с AQ. или AIza)\n"
        "3) Отправь сюда: <code>/key ТВОЙ_КЛЮЧ</code>\n"
    )
    if shared:
        parts.append("✅ <b>Шаг 2. Twilio</b> — не нужен, ты звонишь через Twilio владельца.\n")
    else:
        parts.append(
            f"{mark(has_twilio)} <b>Шаг 2. Twilio — через него идут звонки</b>\n"
            "1) Зарегистрируйся на twilio.com/try-twilio\n"
            "2) На главной странице Console найди <b>Account SID</b> и <b>Auth Token</b> (нажми Show)\n"
            "3) Отправь сюда двумя сообщениями:\n"
            "<code>/set TWILIO_ACCOUNT_SID AC…</code>\n"
            "<code>/set TWILIO_AUTH_TOKEN …</code>\n"
            "4) В Console открой Voice → Settings → <b>Geo permissions</b> и включи свою страну (например, Ukraine)\n"
            "<i>На пробном аккаунте можно звонить только на подтверждённые номера. Чтобы звонить куда угодно, "
            "пополни баланс (Upgrade). Минуты платные.</i>\n"
        )
    parts.append(
        f"{mark(has_number)} <b>Шаг 3. Звонить с твоего номера</b>\n"
        "Нажми /mynumber, потом кнопку «📱 Отправить мой номер».\n"
        "Twilio один раз позвонит тебе, введи код из чата, и всё.\n"
    )
    if has_gemini and (has_twilio or shared) and has_number:
        parts.append("🎉 <b>Всё готово!</b> Напиши номер и задачу, например:\n"
                     "<code>+380671234567 Узнай, работают ли они в воскресенье</code>")
    else:
        parts.append("Когда закончишь, снова открой /setup — там будет видно, что ещё осталось.")
    return "\n".join(parts)


async def _next_step(ctx, chat_id: int, uid: int) -> None:
    """После каждого шага показываем обновлённую инструкцию (пока настройка не закончена или только что закончилась)."""
    p = await profile(ctx, uid)
    await ctx.bot.send_message(chat_id, setup_guide(p), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


async def cmd_setup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    p = await profile(ctx, update.effective_user.id)
    await update.message.reply_text(setup_guide(p), parse_mode=ParseMode.HTML, disable_web_page_preview=True)


def missing_for_call(p: Profile) -> list[str]:
    need = {
        "GEMINI_API_KEY": "ключа Gemini (/key КЛЮЧ)",
        "TWILIO_ACCOUNT_SID": "Twilio SID (/set TWILIO_ACCOUNT_SID …)",
        "TWILIO_AUTH_TOKEN": "Twilio токена (/set TWILIO_AUTH_TOKEN …)",
        "TWILIO_FROM_NUMBER": "номера (/mynumber)",
    }
    out = [v for k, v in need.items() if not p.get(k)]
    num = p.get("TWILIO_FROM_NUMBER")
    if num and not re.fullmatch(r"\+\d{9,15}", num):
        out.append(f"правильного номера: сейчас там «{num[:8]}…», а не номер телефона (/mynumber)")
    if not S.get("PUBLIC_URL"):
        out.append("PUBLIC_URL сервера (настраивает владелец)")
    return out


def fix_value(key: str, value: str) -> tuple[str, str, str]:
    """Проверить значение. Если явно перепутан ключ (SID в поле номера и т.п.) — исправить.
    Возвращает (ключ, значение, текст ошибки или "")."""
    v = value.strip()
    if not v:
        return key, v, ""
    looks_sid = bool(re.fullmatch(r"AC[0-9a-fA-F]{32}", v))
    looks_token = bool(re.fullmatch(r"[0-9a-fA-F]{32}", v))
    looks_phone = bool(re.fullmatch(r"\+?[\d\s\-()]{9,}", v))
    if key.startswith("TWILIO_"):
        if looks_sid:
            return "TWILIO_ACCOUNT_SID", v, ""
        if looks_token:
            return "TWILIO_AUTH_TOKEN", v, ""
        if key == "TWILIO_FROM_NUMBER" and looks_phone:
            return key, normalize_number(v), ""
        hints = {
            "TWILIO_ACCOUNT_SID": "Account SID начинается с AC и состоит из 34 символов. Он есть на главной странице Twilio Console.",
            "TWILIO_AUTH_TOKEN": "Auth Token — это 32 символа (цифры и буквы a–f). Он на главной странице Twilio Console, кнопка Show.",
            "TWILIO_FROM_NUMBER": "Номер нужен в формате +380XXXXXXXXX. Проще всего: /mynumber и кнопка «📱 Отправить мой номер».",
        }
        return key, v, "⚠️ Это не похоже на нужное значение. " + hints.get(key, "")
    if key in ("ASK_TIMEOUT", "MAX_CALL_SECONDS") and not v.isdigit():
        return key, v, "⚠️ Здесь нужно число секунд."
    return key, v, ""


async def check_gemini_key(key: str) -> tuple[bool | None, str]:
    """True — ключ рабочий, False — Google его отверг, None — проверить не удалось.
    Подходят и новые ключи AQ.…, и старые AIza…"""
    from google import genai

    try:
        client = genai.Client(api_key=key)
        pager = await asyncio.wait_for(client.aio.models.list(config={"page_size": 1}), 15)
        async for _ in pager:
            break
        return True, ""
    except asyncio.TimeoutError:
        return None, "timeout"
    except Exception as e:
        code = getattr(e, "code", None) or getattr(e, "status_code", None)
        text = str(e)
        bad_key = code == 401 or any(m in text for m in ("API_KEY_INVALID", "API key not valid", "UNAUTHENTICATED"))
        return (False if bad_key else None), text[:200]


async def cmd_key(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    uid = update.effective_user.id
    if not ctx.args:
        p = await profile(ctx, uid)
        await update.message.reply_text(
            f"Твой ключ Gemini: {S.mask(p.get('GEMINI_API_KEY'))}\n\nПоменять: /key КЛЮЧ\nУдалить: /key -",
        )
        return
    value = "" if ctx.args[0] == "-" else ctx.args[0].strip()
    await _delete(update)
    chat = update.effective_chat.id
    if value:
        ok, why = await check_gemini_key(value)
        if ok is False:
            await ctx.bot.send_message(chat, f"❌ Google не принял этот ключ: {escape(why)}\nПроверь, что скопировал его целиком, или создай новый на aistudio.google.com/apikey", parse_mode=ParseMode.HTML)
            return
        if ok is None:
            await ctx.bot.send_message(chat, f"⚠️ Не получилось проверить ключ, сохраняю как есть. Ответ Google: {escape(why)}", parse_mode=ParseMode.HTML)
    await set_value(ctx, uid, "GEMINI_API_KEY", value)
    await ctx.bot.send_message(chat, f"✅ Твой ключ Gemini работает и сохранён: {S.mask(value)}" if value else "✅ Ключ Gemini удалён")
    await _next_step(ctx, update.effective_chat.id, uid)


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    uid = update.effective_user.id
    p = await profile(ctx, uid)
    keys = None if is_owner(uid) else S.PERSONAL_KEYS
    await update.message.reply_text(S.describe(keys, p.get), parse_mode=ParseMode.HTML)


async def cmd_set(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    uid = update.effective_user.id
    if not ctx.args:
        await update.message.reply_text("Формат: /set КЛЮЧ значение. Список ключей — /settings")
        return
    key = ctx.args[0].upper()
    value = " ".join(ctx.args[1:]).strip()
    allowed_keys = [k for k in S.DEFAULTS if k not in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID")] if is_owner(uid) else S.PERSONAL_KEYS
    if key not in allowed_keys:
        await update.message.reply_text("Такой ключ тебе менять нельзя. Список — /settings")
        return
    if key in SECRET_KEYS or key.startswith("TWILIO_"):
        await _delete(update)
    asked_key = key
    key, value, problem = fix_value(key, value)
    if key != asked_key:
        await ctx.bot.send_message(update.effective_chat.id, f"ℹ️ Это значение для {key}, а не для {asked_key}. Сохранил куда нужно.")
    if problem:
        await ctx.bot.send_message(update.effective_chat.id, problem)
        return
    await set_value(ctx, uid, key, value)
    shown = S.mask(value) if key in SECRET_KEYS else (value or "сброшено")
    await ctx.bot.send_message(update.effective_chat.id, f"✅ {key} = {shown}")
    if key.startswith("TWILIO_"):
        await _next_step(ctx, update.effective_chat.id, uid)


async def cmd_access(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_owner(uid):
        return
    mode = (ctx.args[0].lower() if ctx.args else "")
    if mode not in ("open", "closed"):
        await update.message.reply_text(f"Сейчас доступ: {S.get('ACCESS')}\n/access open — всем\n/access closed — только разрешённым")
        return
    await set_value(ctx, uid, "ACCESS", mode)
    await update.message.reply_text("🔓 Бот открыт для всех." if mode == "open" else "🔒 Только для разрешённых.")


def _allowed_list() -> list[str]:
    return [x.strip() for x in S.get("ALLOWED_USERS").split(",") if x.strip()]


async def _allow(ctx, target: str, add: bool) -> None:
    ids = _allowed_list()
    if add and target not in ids:
        ids.append(target)
    if not add:
        ids = [x for x in ids if x != target]
    await set_value(ctx, owner_id(), "ALLOWED_USERS", ",".join(ids))


async def cmd_allow(update: Update, ctx: ContextTypes.DEFAULT_TYPE, add: bool = True):
    if not is_owner(update.effective_user.id):
        return
    if not ctx.args or not ctx.args[0].isdigit():
        await update.message.reply_text(f"Формат: /{'allow' if add else 'deny'} ID")
        return
    await _allow(ctx, ctx.args[0], add)
    await update.message.reply_text(("✅ Доступ выдан: " if add else "🚫 Доступ закрыт: ") + ctx.args[0])
    if add:
        try:
            await ctx.bot.send_message(int(ctx.args[0]), "✅ Владелец открыл тебе доступ. Нажми /start")
        except Exception:
            pass


async def cmd_deny(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_allow(update, ctx, add=False)


async def cmd_users(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update.effective_user.id):
        return
    ids = _allowed_list()
    await update.message.reply_text(
        f"Доступ: {S.get('ACCESS')}\nРазрешены: " + (", ".join(ids) if ids else "никто, кроме тебя")
    )


def _is_verified(cfg, number: str) -> bool:
    """Номер можно ставить звонящим, если он подтверждён в Twilio или куплен в Twilio."""
    c = twilio_client(cfg)
    return bool(c.outgoing_caller_ids.list(phone_number=number, limit=1)
                or c.incoming_phone_numbers.list(phone_number=number, limit=1))


def _owned_numbers(cfg) -> list[str]:
    return [n.phone_number for n in twilio_client(cfg).incoming_phone_numbers.list(limit=5)]


CONTACT_KB = ReplyKeyboardMarkup(
    [[KeyboardButton("📱 Отправить мой номер", request_contact=True)]], resize_keyboard=True, one_time_keyboard=True
)


async def link_number(ctx, chat_id: int, uid: int, number: str) -> None:
    """Подключить номер: если уже подтверждён — сразу, иначе Twilio один раз позвонит с кодом."""
    send = lambda text, **k: ctx.bot.send_message(chat_id, text, **k)
    p = await profile(ctx, uid)
    if not (p.get("TWILIO_ACCOUNT_SID") and p.get("TWILIO_AUTH_TOKEN")):
        await send("Сначала задай свои TWILIO_ACCOUNT_SID и TWILIO_AUTH_TOKEN (см. /setup).", reply_markup=ReplyKeyboardRemove())
        return
    try:
        verified = await asyncio.to_thread(_is_verified, p, number)
    except Exception as e:
        await send(f"❌ Не получилось связаться с Twilio: {e}", reply_markup=ReplyKeyboardRemove())
        return
    if verified:
        await set_value(ctx, uid, "TWILIO_FROM_NUMBER", number)
        await send(f"✅ Номер {number} подключён. Теперь бот звонит с него.", reply_markup=ReplyKeyboardRemove())
        await _next_step(ctx, chat_id, uid)
        return
    try:
        req = await asyncio.to_thread(
            lambda: twilio_client(p).validation_requests.create(phone_number=number, friendly_name="callbot", call_delay=5)
        )
    except Exception as e:
        await send(f"❌ Twilio не принял номер: {e}", reply_markup=ReplyKeyboardRemove())
        return
    await send(
        f"📲 Twilio требует один раз подтвердить номер {number}.\n"
        f"Сейчас он позвонит. Возьми трубку и введи на клавиатуре код: <b>{req.validation_code}</b>\n\n"
        "Это нужно сделать только один раз, дальше бот будет звонить без этого.",
        parse_mode=ParseMode.HTML, reply_markup=ReplyKeyboardRemove(),
    )
    for _ in range(36):  # ждём до 3 минут
        await asyncio.sleep(5)
        try:
            if await asyncio.to_thread(_is_verified, p, number):
                await set_value(ctx, uid, "TWILIO_FROM_NUMBER", number)
                await send(f"✅ Готово! Номер {number} подтверждён. Теперь бот звонит с твоего номера.")
                await _next_step(ctx, chat_id, uid)
                return
        except Exception:
            pass
    await send("⏱ Подтверждение не пришло. Попробуй ещё раз: /mynumber", reply_markup=CONTACT_KB)


async def cmd_mynumber(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    uid = update.effective_user.id
    p = await profile(ctx, uid)
    if ctx.args:
        await link_number(ctx, update.effective_chat.id, uid, normalize_number("".join(ctx.args), p.get("DEFAULT_COUNTRY_CODE")))
        return
    cur = p.get("TWILIO_FROM_NUMBER") or "не задан"
    await update.message.reply_text(
        f"Сейчас бот звонит с номера: {cur}\n\nНажми кнопку ниже, чтобы бот звонил с твоего номера.",
        reply_markup=CONTACT_KB,
    )
    # вариант без подтверждения: номер, купленный в Twilio
    try:
        owned = await asyncio.to_thread(_owned_numbers, p) if p.get("TWILIO_ACCOUNT_SID") else []
    except Exception:
        owned = []
    if owned:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton(f"Звонить с {n}", callback_data=f"num|{n}")] for n in owned])
        await update.message.reply_text("Или звони с номера, купленного в Twilio (подтверждать не нужно):", reply_markup=kb)


async def on_contact(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    c = update.message.contact
    uid = update.effective_user.id
    if c.user_id != uid:
        await update.message.reply_text("Пришли свой контакт кнопкой «📱 Отправить мой номер», а не чужой.", reply_markup=CONTACT_KB)
        return
    number = c.phone_number if c.phone_number.startswith("+") else "+" + c.phone_number
    await link_number(ctx, update.effective_chat.id, uid, number)


async def cmd_hangup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    calls = active_calls(update.effective_user.id)
    for c in calls:
        await c.hangup()
    await update.message.reply_text("Кладу трубку." if calls else "Сейчас нет активных звонков.")


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    uid = update.effective_user.id
    text = update.message.text.strip()
    calls = active_calls(uid)

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
        p = await profile(ctx, uid)
        await start_call(update, ctx, p, normalize_number(m.group(1), p.get("DEFAULT_COUNTRY_CODE")), task)
        return

    # 3) подсказка в идущий звонок
    if calls:
        await calls[-1].owner_inbox.put(text)
        await update.message.reply_text("👌 Подсказку передал боту.")
        return

    await update.message.reply_text("Не нашёл номер. Пример:\n+380671234567 Узнай, работают ли они в воскресенье.")


async def start_call(update: Update, ctx: ContextTypes.DEFAULT_TYPE, p: Profile, number: str, task: str):
    missing = missing_for_call(p)
    if missing:
        await update.message.reply_text("⚠️ Сначала задай: " + ", ".join(missing))
        return
    notifier = Notifier(ctx.application, update.effective_chat.id, p)
    call = CallSession(number, task, notifier, cfg=p, user_id=p.uid)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Положить трубку", callback_data=f"h|{call.id[:12]}")]])
    await update.message.reply_text(
        f"📞 Звоню на <code>{number}</code>\n<b>Задача:</b> {escape(task)}", parse_mode=ParseMode.HTML, reply_markup=kb
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
    uid = update.effective_user.id
    parts = q.data.split("|")
    if parts[0] == "allow":
        if is_owner(uid):
            await _allow(ctx, parts[1], True)
            await q.answer("Доступ выдан")
            await q.edit_message_text(q.message.text_html + "\n\n✅ Разрешено", parse_mode=ParseMode.HTML)
            try:
                await ctx.bot.send_message(int(parts[1]), "✅ Владелец открыл тебе доступ. Нажми /start")
            except Exception:
                pass
        else:
            await q.answer()
        return
    if parts[0] == "num":
        await q.answer()
        await link_number(ctx, q.message.chat_id, uid, parts[1])
        return
    call = _find(parts[1])
    if not call or call.user_id != uid:
        await q.answer("Звонок уже завершён")
        return
    if parts[0] == "h":
        if not call.finished:
            await call.hangup()
            await q.answer("Кладу трубку")
        else:
            await q.answer("Звонок уже завершён")
        return
    if parts[0] == "a":
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
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler("key", cmd_key))
    app.add_handler(CommandHandler("setup", cmd_setup))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("set", cmd_set))
    app.add_handler(CommandHandler("access", cmd_access))
    app.add_handler(CommandHandler("allow", cmd_allow))
    app.add_handler(CommandHandler("deny", cmd_deny))
    app.add_handler(CommandHandler("users", cmd_users))
    app.add_handler(CommandHandler("hangup", cmd_hangup))
    app.add_handler(CommandHandler("mynumber", cmd_mynumber, block=False))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.CONTACT, on_contact, block=False))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app
