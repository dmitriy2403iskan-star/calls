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
    "ACCESS": ("closed", "кто может пользоваться ботом: closed — только разрешённые, open — все", False),
    "ALLOWED_USERS": ("", "ID пользователей через запятую (управляется /allow и /deny)", False),
    "SHARE_TWILIO": ("0", "1 — другие пользователи звонят через твой Twilio (оплачиваешь ты)", False),
}

# что каждый пользователь настраивает сам для себя
PERSONAL_KEYS = [
    "GEMINI_API_KEY", "GEMINI_VOICE", "GEMINI_MODEL", "OWNER_NAME", "LANGUAGE", "ASK_TIMEOUT",
    "LIVE_TRANSCRIPT", "DEFAULT_COUNTRY_CODE", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
]
# эти значения никогда не берутся у владельца (кроме Twilio при SHARE_TWILIO=1)
PRIVATE_KEYS = {"GEMINI_API_KEY", "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER"}

_lock = threading.Lock()


def _load() -> dict:
    data = {}
    # в Cloudflare настройки хранятся в Durable Object и приходят при старте контейнера
    try:
        data.update(json.loads(os.getenv("SETTINGS_JSON") or "{}"))
    except Exception:
        pass
    if SETTINGS_FILE.exists():
        try:
            data.update(json.loads(SETTINGS_FILE.read_text("utf-8")))
        except Exception:
            pass
    return data


def _push_remote(payload: str) -> None:
    url, token = os.getenv("SETTINGS_SYNC_URL"), os.getenv("INTERNAL_TOKEN")
    if not url or not token:
        return
    import urllib.request

    req = urllib.request.Request(
        url, data=payload.encode(), method="PUT",
        headers={"x-internal-token": token, "content-type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=15).read()
    except Exception as e:  # не страшно: настройка всё равно действует до сна контейнера
        print("settings sync failed:", e)


_overrides = _load()


def get(key: str) -> str:
    with _lock:
        if key in _overrides and _overrides[key] != "":
            return str(_overrides[key])
    val = os.getenv(key) or DEFAULTS.get(key, ("",))[0]
    if key == "PUBLIC_URL" and not val:
        val = os.getenv("RENDER_EXTERNAL_URL", "")  # Render сам сообщает свой адрес
    return val


def merge(values: dict) -> None:
    """Подставить значения в память (и локальный файл) без отправки наружу."""
    with _lock:
        for k, v in values.items():
            if v != "":
                _overrides[k] = str(v)
        try:
            SETTINGS_FILE.write_text(json.dumps(_overrides, ensure_ascii=False, indent=2), "utf-8")
        except Exception:
            pass


def overrides_copy() -> dict:
    with _lock:
        return dict(_overrides)


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
        payload = json.dumps(_overrides, ensure_ascii=False, indent=2)
        SETTINGS_FILE.write_text(payload, "utf-8")
    threading.Thread(target=_push_remote, args=(payload,), daemon=True).start()


def mask(value: str) -> str:
    if not value:
        return "— не задан —"
    return value[:4] + "…" + value[-3:] if len(value) > 10 else "***"


def describe(keys=None, getter=None) -> str:
    lines = []
    getter = getter or get
    for key, (_, desc, secret) in DEFAULTS.items():
        if keys is not None and key not in keys:
            continue
        val = getter(key)
        shown = mask(val) if secret else (val or "— не задан —")
        lines.append(f"• <code>{key}</code> = {shown}\n   <i>{desc}</i>")
    return "\n".join(lines)
