// Cloudflare Worker + Container: принимает запросы от Telegram и Twilio
// и пересылает их в контейнер с Python-ботом. Контейнер засыпает, когда никто не пишет,
// и просыпается от следующего сообщения в Telegram (за сон денег не берут).
import { DurableObject } from "cloudflare:workers";

const PORT = 8000;
const INACTIVITY_TIMEOUT_MS = 30 * 60 * 1000; // засыпать через 30 минут тишины

// какие переменные Worker'а передавать в контейнер (секреты задаются в дашборде Cloudflare)
const PASS_VARS = [
  "TELEGRAM_BOT_TOKEN", "TELEGRAM_OWNER_ID", "GEMINI_API_KEY", "GEMINI_MODEL", "GEMINI_VOICE",
  "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "OWNER_NAME", "LANGUAGE",
  "ASK_TIMEOUT", "MAX_CALL_SECONDS", "DEFAULT_COUNTRY_CODE", "LIVE_TRANSCRIPT",
];

// держим WebSocket звонка открытым с обеих сторон, чтобы контейнер не уснул посреди разговора
function bridge(response) {
  const upstream = response.webSocket;
  const [client, server] = Object.values(new WebSocketPair());
  upstream.binaryType = "arraybuffer";
  server.binaryType = "arraybuffer";
  upstream.accept();
  server.accept();
  const relay = (from, to) => {
    from.addEventListener("message", (e) => {
      try { to.send(e.data); } catch { from.close(1011, "other side closed"); }
    });
    from.addEventListener("close", (e) => {
      const code = e.code === 1005 || e.code === 1006 ? 1000 : e.code;
      try { to.close(code, e.reason); } catch {}
    });
  };
  relay(server, upstream);
  relay(upstream, server);
  return new Response(null, { status: 101, webSocket: client });
}

export class BotContainer extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    const c = ctx.container;
    if (c && c.running) {
      void ctx.blockConcurrencyWhile(() => c.setInactivityTimeout(INACTIVITY_TIMEOUT_MS));
    }
  }

  async internalToken() {
    let t = await this.ctx.storage.get("internalToken");
    if (!t) {
      t = crypto.randomUUID() + crypto.randomUUID();
      await this.ctx.storage.put("internalToken", t);
    }
    return t;
  }

  // контейнер присылает сюда настройки, изменённые через /set, чтобы они пережили сон
  async saveSettings(token, json) {
    if (token !== (await this.internalToken())) return false;
    await this.ctx.storage.put("settings", json);
    return true;
  }

  async ensureRunning(origin) {
    const c = this.ctx.container;
    if (!c.running) {
      const env = {
        PUBLIC_URL: origin,
        TELEGRAM_MODE: "webhook",
        INTERNAL_TOKEN: await this.internalToken(),
        SETTINGS_SYNC_URL: `${origin}/__internal/settings`,
        SETTINGS_JSON: (await this.ctx.storage.get("settings")) || "{}",
        PORT: String(PORT),
      };
      for (const k of PASS_VARS) if (this.env[k]) env[k] = String(this.env[k]);
      c.start({ env, enableInternet: true });
    }
    await c.setInactivityTimeout(INACTIVITY_TIMEOUT_MS);
    const port = c.getTcpPort(PORT);
    let lastError;
    for (let i = 0; i < 300; i++) {
      try {
        const r = await port.fetch("http://container/health", { signal: AbortSignal.timeout(1000) });
        await r.body?.cancel();
        if (r.ok) return;
      } catch (e) {
        lastError = e;
      }
      await scheduler.wait(300);
    }
    throw new Error("Бот не запустился в контейнере", { cause: lastError });
  }

  async fetch(request) {
    const origin = new URL(request.url).origin;
    this.starting ??= this.ensureRunning(origin).finally(() => { this.starting = undefined; });
    await this.starting;

    const url = new URL(request.url);
    url.protocol = "http:";
    url.host = "container";
    const forwarded = new Request(url, request);
    forwarded.headers.delete("host");
    const res = await this.ctx.container.getTcpPort(PORT).fetch(forwarded);
    return res.webSocket ? bridge(res) : res;
  }
}

export default {
  async fetch(request, env) {
    const stub = env.BOT.getByName("main");
    const url = new URL(request.url);
    if (url.pathname === "/__internal/settings" && request.method === "PUT") {
      const ok = await stub.saveSettings(request.headers.get("x-internal-token") || "", await request.text());
      return new Response(ok ? "ok" : "forbidden", { status: ok ? 200 : 403 });
    }
    return stub.fetch(request);
  },
};
