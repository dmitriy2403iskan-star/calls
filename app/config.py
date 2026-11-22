"""Настройки: берутся из .env, но любую можно поменять прямо из Telegram (/set КЛЮЧ значение).
Изменения из Telegram сохраняются в data/settings.json и имеют приоритет над .env."""
import json
import os
import threading
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
SETTINGS_FILE = DATA_DIR / "settings.json"

# ключ -> (значение по умолчанию, описание, секретный ли)
DEFAULTS = {
    "TELEGRAM_BOT_TOKEN": ("", "токен Telegram-бота от @BotFather (только через .env)", True),
    "TELEGRAM_OWNER_ID": ("", "твой Telegram ID (заполнится сам при первом /start)", False),
    "GEMINI_API_KEY": ("", "ключ Google AI Studio (aistudio.google.com)", True),
    "GEMINI_MODEL": ("gemini-2.5-flash-native-audio-preview-12-2025", "модель Gemini Live", False),
    "GEMINI_VOICE": ("Puck", "голос: Puck, Charon, Kore, Fenrir, Aoede, Leda, Orus, Zephyr…", False),
    "TWILIO_ACCOUNT_SID": ("", "Twilio Account SID", True),
    "TWILIO_AUTH_TOKEN": ("", "Twilio Auth Token", True),
    "TWILIO_FROM_NUMBER": ("", "номер, с которого звоним (твой мобильный, подтверждённый в Twilio)", False),
    "PUBLIC_URL": ("", "публичный https-адрес сервера (ngrok / Railway)", False),
    "OWNER_NAME": ("", "твоё имя — бот представится, что звонит от тебя", False),
    "LANGUAGE": ("русский", "язык разговора по умолчанию (бот подстроится под собеседника)", False),
    "ASK_TIMEOUT": ("10", "сколько секунд ждать твоего ответа, потом бот решает сам", False),
    "MAX_CALL_SECONDS": ("600", "максимальная длина звонка", False),
    "DEFAULT_COUNTRY_CODE": ("380", "код страны для номеров вида 067…", False),
    "LIVE_TRANSCRIPT": ("1", "1 — присылать реплики разговора в чат вживую, 0 — только итог", False),
}

_lock = threading.Lock()


def _load() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text("utf-8"))
        except Exception:
            return {}
    return {}


_overrides = _load()


def get(key: str) -> str:
    with _lock:
        if key in _overrides and _overrides[key] != "":
            return str(_overrides[key])
    return os.getenv(key, DEFAULTS.get(key, ("",))[0])


def get_int(key: str) -> int:
    try:
        return int(float(get(key)))
    except ValueError:
        return int(DEFAULTS[key][0])


def set(key: str, value: str) -> None:  # noqa: A001
    with _lock:
        if value == "":
            _overrides.pop(key, None)
        else:
            _overrides[key] = value
        SETTINGS_FILE.write_text(json.dumps(_overrides, ensure_ascii=False, indent=2), "utf-8")


def mask(value: str) -> str:
    if not value:
        return "— не задан —"
    return value[:4] + "…" + value[-3:] if len(value) > 10 else "***"


def describe() -> str:
    lines = []
    for key, (_, desc, secret) in DEFAULTS.items():
        val = get(key)
        shown = mask(val) if secret else (val or "— не задан —")
        lines.append(f"• <code>{key}</code> = {shown}\n   <i>{desc}</i>")
    return "\n".join(lines)
