"""Сервер: принимает аудиопоток звонка от Twilio и параллельно крутит Telegram-бота."""
import hashlib
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import PlainTextResponse
from telegram import Update

from . import config as S
from .call_session import CALLS
from .telegram_bot import build_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


WEBHOOK = os.getenv("TELEGRAM_MODE") == "webhook"
TG = {}


def webhook_secret() -> str:
    return hashlib.sha256(("tg-hook:" + S.get("TELEGRAM_BOT_TOKEN")).encode()).hexdigest()[:40]


@asynccontextmanager
async def lifespan(_: FastAPI):
    tg = build_app()
    TG["app"] = tg
    await tg.initialize()
    from . import store

    await store.bootstrap(tg.bot)  # владелец и общие настройки из Telegram
    try:
        from telegram import BotCommand

        await tg.bot.set_my_commands([
            BotCommand("start", "Помощь"), BotCommand("key", "Свой ключ Gemini"),
            BotCommand("mynumber", "Звонить с моего номера"), BotCommand("settings", "Мои настройки"),
            BotCommand("hangup", "Положить трубку"),
        ])
    except Exception:
        pass
    await tg.start()
    if WEBHOOK:
        # Cloudflare: Telegram сам будит контейнер, присылая сообщения на адрес Worker'а
        url = S.get("PUBLIC_URL").rstrip("/") + "/tg/" + webhook_secret()
        await tg.bot.set_webhook(url, secret_token=webhook_secret(), allowed_updates=Update.ALL_TYPES)
        logging.info("Telegram-бот запущен (webhook)")
    else:
        await tg.updater.start_polling(drop_pending_updates=True)
        logging.info("Telegram-бот запущен (polling)")
    yield
    if not WEBHOOK:
        await tg.updater.stop()
    await tg.stop()
    await tg.shutdown()


api = FastAPI(lifespan=lifespan)


@api.get("/")
async def index():
    return PlainTextResponse("✅ Бот-звонилка работает. Открой своего бота в Telegram и нажми /start.")


@api.get("/health")
async def health():
    return PlainTextResponse("ok")


@api.post("/tg/{secret}")
async def telegram_webhook(secret: str, request: Request):
    if secret != webhook_secret() or request.headers.get("x-telegram-bot-api-secret-token") != webhook_secret():
        return PlainTextResponse("forbidden", status_code=403)
    tg = TG["app"]
    await tg.update_queue.put(Update.de_json(await request.json(), tg.bot))
    return PlainTextResponse("ok")


@api.post("/status/{call_id}")
async def status(call_id: str, request: Request):
    form = await request.form()
    st = form.get("CallStatus", "")
    call = CALLS.get(call_id)
    if call and not call.finished:
        if st in ("busy", "no-answer", "failed", "canceled"):
            await call.finish(st)
        elif st == "completed" and call.ws is None:
            await call.finish("completed")
    return PlainTextResponse("")


@api.websocket("/media/{call_id}")
async def media(ws: WebSocket, call_id: str):
    call = CALLS.get(call_id)
    await ws.accept()
    if not call or call.finished or call.ws is not None:
        await ws.close()
        return
    await call.run(ws)


def run():
    import uvicorn

    if not S.get("TELEGRAM_BOT_TOKEN"):
        raise SystemExit("Заполни TELEGRAM_BOT_TOKEN в файле .env")
    uvicorn.run(api, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    run()
