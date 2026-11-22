"""Сервер: принимает аудиопоток звонка от Twilio и параллельно крутит Telegram-бота."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket
from fastapi.responses import PlainTextResponse

from . import config as S
from .call_session import CALLS
from .telegram_bot import build_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)


@asynccontextmanager
async def lifespan(_: FastAPI):
    tg = build_app()
    await tg.initialize()
    await tg.start()
    await tg.updater.start_polling(drop_pending_updates=True)
    logging.info("Telegram-бот запущен")
    yield
    await tg.updater.stop()
    await tg.stop()
    await tg.shutdown()


api = FastAPI(lifespan=lifespan)


@api.get("/")
async def health():
    return PlainTextResponse("callbot ok")


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
    import os

    import uvicorn

    if not S.get("TELEGRAM_BOT_TOKEN"):
        raise SystemExit("Заполни TELEGRAM_BOT_TOKEN в файле .env")
    uvicorn.run(api, host="0.0.0.0", port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    run()
