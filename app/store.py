"""Постоянное хранилище без базы данных.

Настройки каждого пользователя (его ключ Gemini, Twilio, номер и т.д.) лежат зашифрованным файлом,
закреплённым в его же чате с ботом. Поэтому они переживают перезапуск и сон бесплатного сервера.
ID владельца бота хранится невидимыми символами в описании бота (getMyDescription).
Шифрование: Fernet, ключ — SETTINGS_SECRET или производный от токена бота."""
import base64
import hashlib
import io
import json
import logging
import os

from cryptography.fernet import Fernet, InvalidToken
from telegram import InputFile

from . import config as S

log = logging.getLogger("store")

FNAME = "callbot-settings.bin"
CAPTION = "🔐 Твои настройки бота (зашифрованы). Не удаляй и не открепляй это сообщение."
_cache: dict[int, dict] = {}
_msg_ids: dict[int, int] = {}

# невидимые символы для записи ID владельца в описание бота
_MARK = "⁣"
_ZW = ["​", "‌", "‍", "⁠"]
DESCRIPTION = "📞 Бот-звонилка: даёшь номер и задачу — ИИ звонит и договаривается за тебя."


def _fernet() -> Fernet:
    secret = os.getenv("SETTINGS_SECRET") or ("callbot:" + S.get("TELEGRAM_BOT_TOKEN"))
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(secret.encode()).digest()))


async def load(bot, uid: int) -> dict:
    if uid in _cache:
        return _cache[uid]
    data: dict = {}
    try:
        chat = await bot.get_chat(uid)
        pm = chat.pinned_message
        if pm and pm.document and pm.document.file_name == FNAME and pm.from_user and pm.from_user.id == bot.id:
            f = await bot.get_file(pm.document.file_id)
            raw = await f.download_as_bytearray()
            data = json.loads(_fernet().decrypt(bytes(raw)))
            _msg_ids[uid] = pm.message_id
    except InvalidToken:
        log.warning("settings of %s can't be decrypted (bot token changed?)", uid)
    except Exception as e:
        log.info("no stored settings for %s: %s", uid, e)
    _cache[uid] = data
    return data


async def save(bot, uid: int, data: dict) -> None:
    _cache[uid] = data
    blob = _fernet().encrypt(json.dumps(data, ensure_ascii=False).encode())
    msg = await bot.send_document(
        uid, InputFile(io.BytesIO(blob), filename=FNAME), caption=CAPTION, disable_notification=True
    )
    try:
        await bot.pin_chat_message(uid, msg.message_id, disable_notification=True)
    except Exception as e:
        log.warning("pin failed: %s", e)
    old = _msg_ids.get(uid)
    _msg_ids[uid] = msg.message_id
    if old:
        try:
            await bot.delete_message(uid, old)
        except Exception:
            pass


# ---------- владелец ----------
def _encode(uid: int) -> str:
    n, out = uid, ""
    while True:
        out = _ZW[n % 4] + out
        n //= 4
        if n == 0:
            return _MARK + out


def _decode(text: str) -> int | None:
    if _MARK not in text:
        return None
    n = 0
    for ch in text.split(_MARK, 1)[1]:
        if ch not in _ZW:
            break
        n = n * 4 + _ZW.index(ch)
    return n or None


async def read_owner(bot) -> int | None:
    try:
        d = await bot.get_my_description()
        return _decode(d.description or "")
    except Exception:
        return None


async def write_owner(bot, uid: int) -> None:
    try:
        await bot.set_my_description(DESCRIPTION + _encode(uid))
    except Exception as e:
        log.warning("set_my_description failed: %s", e)


async def bootstrap(bot) -> None:
    """При старте: найти владельца и восстановить общие настройки из его чата."""
    owner = S.get("TELEGRAM_OWNER_ID") or (str(await read_owner(bot) or ""))
    if not owner:
        return
    S.merge({"TELEGRAM_OWNER_ID": owner})
    data = await load(bot, int(owner))
    if data.get("global"):
        S.merge(data["global"])
        S.merge({"TELEGRAM_OWNER_ID": owner})


async def save_global(bot) -> None:
    owner = S.get("TELEGRAM_OWNER_ID")
    if not owner:
        return
    data = dict(await load(bot, int(owner)))
    data["global"] = S.overrides_copy()
    await save(bot, int(owner), data)
