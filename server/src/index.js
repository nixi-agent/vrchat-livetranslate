/**
 * Worker 入口：鉴权 + 房间码路由 → 转交给 Durable Object「Room」。
 *
 * 三条纪律（照方案 §5「服务端硬要求」）：
 * 1. **鉴权在 Worker 层做完**：非法请求直接返回 HTTP 错误，不进 DO ——
 *    否则攻击流量每一发都在替你付 DO 请求费。
 * 2. DO 用 `getByName(房间码, { locationHint: "apac" })`：房间码就是路由键，
 *    hint 只是 best-effort（CF 明确不保证落点）。
 * 3. 这里**不** `accept()` WebSocket —— 交给 DO 用 `state.acceptWebSocket()`，
 *    才吃得到 Hibernation（空闲时不计费）。用了 `ws.accept()` 会按整条连接时长计费。
 *
 * 路由：`GET /ws?room=<8位房间码>&k=<令牌>`（Upgrade: websocket）
 * 其余路径返回一个只读的 JSON 状态页，方便 `curl` 探活。
 */

/** Crockford Base32（去掉了易混的 I/L/O/U），8 位。 */
const ROOM_CODE_RE = /^[0-9A-HJKMNP-TV-Z]{8}$/;

/** 用户手抄房间码时最容易打错的三个字母 → 归一到数字。 */
const LOOKALIKE = { I: "1", L: "1", O: "0" };

/** 令牌支持两种传法：`?k=` 或 `Authorization: Bearer <tok>`。 */
const TOKEN_QUERY_KEYS = ["k", "tok", "token"];

/** 把用户填的房间码归一化：去空白、转大写、I/L→1、O→0。 */
export function normalizeRoomCode(raw) {
  if (typeof raw !== "string") return "";
  return raw
    .trim()
    .toUpperCase()
    .split("")
    .map((ch) => LOOKALIKE[ch] || ch)
    .join("");
}

/** 定长十六进制串的等值比较：不短路，避免用响应时间逐位猜令牌。 */
function sameHash(a, b) {
  if (typeof a !== "string" || typeof b !== "string") return false;
  if (a.length !== b.length || a.length === 0) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i += 1) {
    diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  }
  return diff === 0;
}

/** SHA-256 → 小写 hex。令牌只存哈希，明文不落盘、不进日志。 */
async function sha256Hex(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function json(status, body) {
  return new Response(JSON.stringify(body, null, 2) + "\n", {
    status,
    headers: { "content-type": "application/json; charset=utf-8" },
  });
}

/** 从查询串或 Authorization 头里取令牌（没有就返回 ""）。 */
function pickToken(url, request) {
  for (const key of TOKEN_QUERY_KEYS) {
    const v = url.searchParams.get(key);
    if (v) return v;
  }
  const auth = request.headers.get("Authorization") || "";
  if (auth.toLowerCase().startsWith("bearer ")) return auth.slice(7).trim();
  if (auth.toLowerCase().startsWith("vlt ")) {
    try {
      const bytes = Uint8Array.from(atob(auth.slice(4).trim().replace(/-/g, "+").replace(/_/g, "/")), c => c.charCodeAt(0));
      return new TextDecoder("utf-8", {fatal: true}).decode(bytes);
    } catch { return ""; }
  }
  return "";
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (url.pathname !== "/ws") {
      return json(200, {
        service: "vlt-room-relay",
        hint: "WebSocket 端点是 /ws?room=<8位房间码>；令牌使用 Authorization: Bearer 或 Authorization: VLT",
        auth: env.ROOM_TOKEN_HASH ? "已开启（ROOM_TOKEN_HASH）" : "未开启（任何人都能进房）",
      });
    }

    if ((request.headers.get("Upgrade") || "").toLowerCase() !== "websocket") {
      return json(426, { error: "upgrade_required", msg: "/ws 只接受 WebSocket 升级请求" });
    }

    // ---- ① 房间码：Worker 层就得知道去哪个 DO，所以必须走查询串 ----
    const roomCode = normalizeRoomCode(url.searchParams.get("room") || url.searchParams.get("r"));
    if (!ROOM_CODE_RE.test(roomCode)) {
      return json(400, {
        error: "bad_room",
        msg: `房间码必须是 8 位 Crockford Base32（不含 I/L/O/U），收到 ${JSON.stringify(roomCode)}`,
      });
    }

    // ---- ② 鉴权：没配 ROOM_TOKEN_HASH 就是开放房间（本地开发/自测的默认） ----
    if (env.ROOM_TOKEN_HASH) {
      const token = pickToken(url, request);
      if (!token) {
        return json(401, { error: "auth", msg: "这个房间要令牌：用 Authorization: Bearer 或 Authorization: VLT" });
      }
      const got = await sha256Hex(token);
      const want = String(env.ROOM_TOKEN_HASH).trim().toLowerCase();
      if (!sameHash(got, want)) {
        return json(403, { error: "auth", msg: "令牌不对" });
      }
    }

    // ---- ③ 转交 DO。acceptWebSocket 在 room.js 里做（Hibernation） ----
    // locationHint 只决定**首次创建**时的落点（best-effort，CF 不保证）。
    // 实测（2026-09-28，昆明电信出口 → 本机双客户端打真边缘）：
    //   本机入口被 anycast 固定在 LAX，若 DO 建在 apac，每帧要走 本机→LAX→亚太→LAX→本机，
    //   p50 ≈ 414ms；改 wnam 让 DO 与入口同在西岸，少两跳（见 wrangler.toml 的 ROOM_LOCATION_HINT）。
    const id = env.ROOM.idFromName(roomCode);
    const stub = env.ROOM.get(id, { locationHint: env.ROOM_LOCATION_HINT || "apac" });
    return stub.fetch(request);
  },
};

export { Room } from "./room.js";
