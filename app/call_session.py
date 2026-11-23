"""Один телефонный звонок: мост Twilio Media Stream <-> Gemini Live + инструменты бота."""
import asyncio
import json
import logging
import time
import uuid
from html import escape

from google import genai
from google.genai import types
from twilio.rest import Client as TwilioClient

from . import audio
from . import config as S

log = logging.getLogger("call")

CALLS: dict[str, "CallSession"] = {}


def twilio_client(cfg=None) -> TwilioClient:
    cfg = cfg or S
    return TwilioClient(cfg.get("TWILIO_ACCOUNT_SID"), cfg.get("TWILIO_AUTH_TOKEN"))


def build_prompt(task: str, number: str, cfg=None) -> str:
    cfg = cfg or S
    owner = cfg.get("OWNER_NAME") or "мой владелец"
    timeout = cfg.get_int("ASK_TIMEOUT")
    return f"""Ты — голосовой ассистент, который сам ведёт телефонный звонок от имени человека по имени {owner}.
Ты звонишь на номер {number}. Собеседник на том конце — живой человек (администратор, менеджер, оператор и т.п.) или автоответчик.

ТВОЯ ЗАДАЧА НА ЭТОТ ЗВОНОК:
{task}

КАК ВЕСТИ РАЗГОВОР:
- Говори естественно, вежливо и коротко, как обычный человек по телефону. Одна-две фразы за раз, не читай лекций.
- Язык по умолчанию: {cfg.get("LANGUAGE")}. Если собеседник говорит на другом языке (например, украинском) — переходи на его язык.
- Когда собеседник ответил («алло», «слушаю»), поздоровайся, скажи, что звонишь от имени {owner}, и сразу переходи к делу.
- Внимательно слушай и добивайся цели. Если что-то не получается — ищи альтернативы (другое время, другая дата, другой вариант), которые разумно подходят под задачу.
- Используй только данные из задачи. Не выдумывай телефоны, адреса, номера карт, имена и прочие факты. Платёжные данные и пароли никогда не называй.
- Если тебя прямо спросят, робот ли ты, честно скажи, что ты голосовой ИИ-ассистент, который звонит по поручению {owner}, и продолжай разговор.

КОГДА НУЖЕН ВЫБОР ИЛИ НЕ ХВАТАЕТ ДАННЫХ:
- Если собеседник предлагает несколько вариантов (время, столик, услугу, цену) или спрашивает то, чего нет в задаче, —
  СНАЧАЛА скажи собеседнику что-то вроде «Секундочку, уточню», затем вызови инструмент ask_owner с вопросом и списком вариантов.
- Владелец отвечает не дольше {timeout} секунд. Если он не ответил, инструмент вернёт decided_by="timeout" —
  тогда сам выбери вариант, лучше всего подходящий под задачу, и продолжай разговор.
- Не спрашивай владельца по мелочам, которые очевидно следуют из задачи.

СООБЩЕНИЯ ОТ ВЛАДЕЛЬЦА:
- Во время звонка может прийти текст с пометкой [Владелец]. Это указания от {owner} — учитывай их сразу, собеседнику их дословно не зачитывай.

ЗАВЕРШЕНИЕ:
- Когда цель достигнута (или стало ясно, что её не достичь), подведи итог собеседнику (например, повтори дату/время брони),
  попрощайся, и ТОЛЬКО ПОСЛЕ прощания вызови end_call с итогом.
- Если попал на автоответчик или голосовую почту — оставь короткое сообщение по сути задачи и вызови end_call.
"""


TOOLS = [
    types.Tool(
        function_declarations=[
            types.FunctionDeclaration(
                name="ask_owner",
                description="Спросить владельца в Telegram, когда нужно выбрать вариант или не хватает данных. "
                "Возвращает ответ владельца или decided_by='timeout', если он не ответил вовремя.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "question": types.Schema(type="STRING", description="Короткий вопрос владельцу"),
                        "options": types.Schema(
                            type="ARRAY",
                            items=types.Schema(type="STRING"),
                            description="Варианты на выбор (2–6 штук), если они есть",
                        ),
                    },
                    required=["question"],
                ),
            ),
            types.FunctionDeclaration(
                name="end_call",
                description="Положить трубку. Вызывать только после того, как попрощался с собеседником.",
                parameters=types.Schema(
                    type="OBJECT",
                    properties={
                        "outcome": types.Schema(
                            type="STRING",
                            enum=["success", "partial", "failed", "voicemail"],
                            description="Достигнута ли цель",
                        ),
                        "summary": types.Schema(
                            type="STRING",
                            description="Итог для владельца: что договорились, точные даты/время/цены/имена",
                        ),
                    },
                    required=["outcome", "summary"],
                ),
            ),
        ]
    )
]


class CallSession:
    def __init__(self, number: str, task: str, notifier, cfg=None, user_id: int | None = None):
        self.id = uuid.uuid4().hex
        self.cfg = cfg or S  # настройки того, кто заказал звонок
        self.user_id = user_id
        self.number = number
        self.task = task
        self.notifier = notifier
        self.twilio_sid: str | None = None
        self.stream_sid: str | None = None
        self.ws = None
        self.started_at = time.time()
        self.transcript: list[tuple[str, str]] = []
        self._buf = {"callee": "", "bot": ""}
        self.outcome: str | None = None
        self.summary: str | None = None
        self.status = "dialing"
        self.finished = False
        self.heard_callee = False
        self.pending: dict[str, tuple[asyncio.Future, list[str]]] = {}
        self.owner_inbox: asyncio.Queue[str] = asyncio.Queue()
        self.down = audio.Downsampler()
        CALLS[self.id] = self

    # ---------- запуск звонка ----------
    async def dial(self) -> None:
        base = S.get("PUBLIC_URL").rstrip("/")
        ws_url = base.replace("https://", "wss://").replace("http://", "ws://") + f"/media/{self.id}"
        twiml = f'<Response><Connect><Stream url="{ws_url}"/></Connect></Response>'
        params = dict(
            to=self.number,
            from_=self.cfg.get("TWILIO_FROM_NUMBER"),
            twiml=twiml,
            time_limit=S.get_int("MAX_CALL_SECONDS"),
            status_callback=f"{base}/status/{self.id}",
            status_callback_event=["initiated", "ringing", "answered", "completed"],
        )
        call = await asyncio.to_thread(lambda: twilio_client(self.cfg).calls.create(**params))
        self.twilio_sid = call.sid

    async def hangup(self) -> None:
        if not self.twilio_sid:
            return
        try:
            await asyncio.to_thread(
                lambda: twilio_client(self.cfg).calls(self.twilio_sid).update(status="completed")
            )
        except Exception as e:  # звонок уже мог завершиться
            log.info("hangup: %s", e)

    # ---------- мост аудио ----------
    async def run(self, ws) -> None:
        self.ws = ws
        self.status = "in-progress"
        client = genai.Client(api_key=self.cfg.get("GEMINI_API_KEY"))
        cfg = types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            system_instruction=build_prompt(self.task, self.number, self.cfg),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=self.cfg.get("GEMINI_VOICE"))
                )
            ),
            tools=TOOLS,
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )
        try:
            async with client.aio.live.connect(model=self.cfg.get("GEMINI_MODEL"), config=cfg) as session:
                tasks = [
                    asyncio.create_task(self._from_phone(session)),
                    asyncio.create_task(self._from_gemini(session)),
                    asyncio.create_task(self._from_owner(session)),
                    asyncio.create_task(self._kickoff(session)),
                ]
                done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for t in pending:
                    t.cancel()
                for t in done:
                    if t.exception():
                        raise t.exception()
        except Exception as e:
            log.exception("call %s failed", self.id)
            await self.notifier.text(f"⚠️ Ошибка во время звонка: <code>{escape(str(e))[:500]}</code>")
            await self.hangup()
        finally:
            await self.finish()

    async def _kickoff(self, session) -> None:
        # если собеседник молчит после соединения — начинаем разговор сами
        await asyncio.sleep(2.5)
        if not self.heard_callee:
            await session.send_realtime_input(
                text="(Абонент взял трубку, но молчит. Поздоровайся и начни разговор.)"
            )
        await asyncio.Event().wait()  # живём, пока идёт звонок

    async def _from_phone(self, session) -> None:
        batch = b""
        while True:
            try:
                raw = await self.ws.receive_text()
            except Exception:  # Twilio закрыл соединение — звонок окончен
                return
            msg = json.loads(raw)
            ev = msg.get("event")
            if ev == "start":
                self.stream_sid = msg["start"]["streamSid"]
                self.twilio_sid = msg["start"].get("callSid", self.twilio_sid)
            elif ev == "media":
                batch += audio.twilio_to_gemini(msg["media"]["payload"])
                if len(batch) >= 3200:  # ~100 мс
                    await session.send_realtime_input(
                        audio=types.Blob(data=batch, mime_type="audio/pcm;rate=16000")
                    )
                    batch = b""
            elif ev == "mark" and msg.get("mark", {}).get("name") == "hangup":
                await self.hangup()
            elif ev == "stop":
                return

    async def _send_audio(self, pcm24: bytes) -> None:
        payload = self.down.convert(pcm24)
        if payload and self.stream_sid:
            await self.ws.send_text(
                json.dumps({"event": "media", "streamSid": self.stream_sid, "media": {"payload": payload}})
            )

    async def _from_gemini(self, session) -> None:
        while True:
            async for resp in session.receive():
                sc = resp.server_content
                if sc:
                    if sc.interrupted and self.stream_sid:
                        # собеседник перебил — выбрасываем недоговорённое
                        self.down.reset()
                        await self.ws.send_text(json.dumps({"event": "clear", "streamSid": self.stream_sid}))
                    if sc.model_turn:
                        for part in sc.model_turn.parts or []:
                            if part.inline_data and part.inline_data.data:
                                await self._send_audio(part.inline_data.data)
                    if sc.input_transcription and sc.input_transcription.text:
                        self.heard_callee = True
                        await self._add("callee", sc.input_transcription.text)
                    if sc.output_transcription and sc.output_transcription.text:
                        await self._add("bot", sc.output_transcription.text)
                    if sc.turn_complete:
                        await self._flush()
                if resp.tool_call:
                    for fc in resp.tool_call.function_calls or []:
                        asyncio.create_task(self._tool(session, fc))

    async def _from_owner(self, session) -> None:
        while True:
            text = await self.owner_inbox.get()
            await session.send_realtime_input(text=f"[Владелец]: {text}")

    # ---------- инструменты ----------
    async def _tool(self, session, fc) -> None:
        args = dict(fc.args or {})
        if fc.name == "ask_owner":
            result = await self.ask_owner(args.get("question", ""), list(args.get("options") or []))
            await session.send_tool_response(
                function_responses=[types.FunctionResponse(id=fc.id, name=fc.name, response=result)]
            )
        elif fc.name == "end_call":
            self.outcome = args.get("outcome")
            self.summary = args.get("summary")
            await self._flush()
            # даём договорить прощание: Twilio вернёт mark, когда весь звук проиграется
            if self.stream_sid:
                await self.ws.send_text(
                    json.dumps({"event": "mark", "streamSid": self.stream_sid, "mark": {"name": "hangup"}})
                )
            await asyncio.sleep(12)
            await self.hangup()

    async def ask_owner(self, question: str, options: list[str]) -> dict:
        qid = uuid.uuid4().hex[:8]
        fut = asyncio.get_running_loop().create_future()
        self.pending[qid] = (fut, options)
        timeout = self.cfg.get_int("ASK_TIMEOUT")
        await self.notifier.question(self, qid, question, options, timeout)
        try:
            answer = await asyncio.wait_for(fut, timeout)
            return {"decided_by": "owner", "answer": answer}
        except asyncio.TimeoutError:
            await self.notifier.text(f"⏱ Ответа нет {timeout} сек — бот выбирает сам.")
            return {
                "decided_by": "timeout",
                "answer": "Владелец не ответил. Выбери сам вариант, который лучше всего подходит под задачу.",
            }
        finally:
            self.pending.pop(qid, None)

    def answer(self, qid: str | None, text: str) -> bool:
        """Ответ владельца. qid=None — ответ на последний открытый вопрос."""
        if qid is None and self.pending:
            qid = list(self.pending)[-1]
        item = self.pending.get(qid) if qid else None
        if not item or item[0].done():
            return False
        item[0].set_result(text)
        return True

    # ---------- транскрипт и итог ----------
    async def _add(self, who: str, text: str) -> None:
        other = "bot" if who == "callee" else "callee"
        if self._buf[other].strip():
            await self._flush_one(other)
        self._buf[who] += text

    async def _flush_one(self, who: str) -> None:
        text = " ".join(self._buf[who].split())
        self._buf[who] = ""
        if not text:
            return
        self.transcript.append((who, text))
        if self.cfg.get("LIVE_TRANSCRIPT") == "1":
            icon = "👤" if who == "callee" else "🤖"
            await self.notifier.text(f"{icon} {escape(text)}", silent=True)

    async def _flush(self) -> None:
        for who in ("callee", "bot"):
            await self._flush_one(who)

    def transcript_text(self) -> str:
        return "\n".join(("Собеседник: " if w == "callee" else "Бот: ") + t for w, t in self.transcript)

    async def finish(self, status: str | None = None) -> None:
        if self.finished:
            return
        self.finished = True
        if status:
            self.status = status
        await self._flush()
        for fut, _ in self.pending.values():
            if not fut.done():
                fut.cancel()
        await self.notifier.call_finished(self)
