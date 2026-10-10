/**
 * Durable Object「Room」：一个房间码一个实例。
 *
 * 只做四件事：**成员表 + 即收即转扇出 + 只对 final 回 ack + 每连接限速**。
 * 不缓冲、不排序、不重放历史 —— partial 是全量快照，丢了下一拍就有更全的。
 *
 * ## Hibernation（这是整个方案能落在免费额度里的前提）
 * - accept 必须用 `state.acceptWebSocket(ws)`（**不是** `ws.accept()`）。
 *   `ws.accept()` 会让 DO 在整条连接存续期间保持唤醒并按连接时长计费；
 *   Hibernation 则把连接托管给 CF 边缘，没消息时 DO 不占 CPU、不计费。
 * - 代价：**休眠后内存全清空**。所以每连接状态必须 `serializeAttachment()`
 *   存到连接上，恢复时用 `state.getWebSockets()` + `deserializeAttachment()` 捞回来。
 * - **绝对不能用 `setTimeout` / `setInterval`**：有待处理的定时器 = 不许休眠。
 *   空房回收改用 `storage.setAlarm()`（见 `alarm()`）。
 */

const MAX_PEERS = 8;              // 成员上限：再多手腕屏也显示不下了
const RATE_LIMIT_FPS = 20;        // 每连接每秒帧数上限（超出的帧丢掉，不断线）
const MAX_FRAME_BYTES = 8192;     // 与客户端 protocol.MAX_FRAME_BYTES 对齐
const EMPTY_ROOM_GRACE_MS = 60_000;  // 最后一个人走了之后多久回收存储

const FRAME_HELLO = "hello";
const FRAME_WELCOME = "welcome";
const FRAME_MEMBERS = "members";
const FRAME_SEG = "seg";
const FRAME_FINAL = "final";
const FRAME_ACK = "ack";
const FRAME_PING = "ping";
const FRAME_PONG = "pong";
const FRAME_ERR = "err";

/** 客户端拿到这些码就不再重连（重连也只会一直被拒，白烧请求）。 */
const FATAL_CODES = new Set(["auth", "room_full", "bad_room", "banned"]);

function newPeerId() {
  const bytes = new Uint8Array(3);
  crypto.getRandomValues(bytes);
  return "p_" + [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

/**
 * attachment API 在不同 workerd 上挂的位置不一样：本机 `wrangler dev --local`
 * （wrangler 4.142 / workerd 实测）只有 `ws.serializeAttachment(x)`，`state` 上没有；
 * 新版文档写的是 `state.serializeAttachment(ws, x)`。两种都试，取到哪个用哪个 ——
 * 这里猜错的话不是降级，是**一条连接都建不起来**。
 */
function attach(state, ws, obj) {
  if (typeof state.serializeAttachment === "function") state.serializeAttachment(ws, obj);
  else ws.serializeAttachment(obj);
}

function attached(state, ws) {
  return typeof state.deserializeAttachment === "function"
    ? state.deserializeAttachment(ws)
    : ws.deserializeAttachment();
}

/** 昵称 ≤16 字符、掐掉控制字符：它会原样出现在别人的手腕屏上。 */
function cleanNick(raw, fallback) {
  const nick = String(raw ?? "").replace(/[\u0000-\u001f\u007f]/g, "").trim().slice(0, 16);
  return nick || fallback;
}

export class Room {
  constructor(state, env) {
    this.state = state;
    this.env = env;
    this.room = state.id.name || "";   // idFromName() 建的 DO 才拿得到房间码
    // ⚠️ getWebSockets() 是 async，构造函数里不能 await → 第一次用到时再恢复
    this.sockets = null;
  }

  /** 懒恢复成员表：休眠后内存是空的，只能从 Hibernation 存着的 attachment 捞。 */
  async liveSockets() {
    if (this.sockets === null) {
      this.sockets = await this.state.getWebSockets();
    }
    return this.sockets;
  }

  /**
   * **已握手**的连接（attachment 里有 id）。所有扇出/广播/人数判断都必须走这里：
   * 只 accept 了 WebSocket、还没发 hello 的连接绝不能收到任何房间帧 —— 否则它的
   * 第一帧会是 `members` 而不是 `welcome`，客户端按协议判错直接断线。
   * （真机 `wrangler dev --local` 上两人同时进房时实测到过这个竞态。）
   */
  async members() {
    const out = [];
    for (const ws of await this.liveSockets()) {
      const att = attached(this.state, ws);
      if (att && att.id) out.push({ ws, att });
    }
    return out;
  }

  /** Worker 层已经把过房间码和令牌，这里只负责把 WS 接进 Hibernation。 */
  async fetch(request) {
    if ((request.headers.get("Upgrade") || "").toLowerCase() !== "websocket") {
      return new Response("expected websocket", { status: 426 });
    }
    const pair = new WebSocketPair();
    const [client, server] = Object.values(pair);

    await this.liveSockets();
    this.state.acceptWebSocket(server);
    // attachment 是这条连接的**唯一**持久状态：休眠醒来后靠它认人
    attach(this.state, server, { id: null, nick: "", joined: Date.now(), win: 0, n: 0 });
    this.sockets.push(server);

    return new Response(null, { status: 101, webSocket: client });
  }

  // ------------------------------------------------------------ 收消息
  async webSocketMessage(ws, message) {
    const att = attached(this.state, ws) || { id: null, nick: "", win: 0, n: 0 };
    const now = Date.now();
    if (now - (att.win || 0) >= 1000) { att.win = now; att.n = 0; }
    att.n = (att.n || 0) + 1;
    attach(this.state, ws, att);
    if (att.n > RATE_LIMIT_FPS) {
      return this.sendErr(ws, "rate", `超过 ${RATE_LIMIT_FPS} 帧/秒，这一帧被丢了`);
    }
    const size = typeof message === "string" ? new TextEncoder().encode(message).byteLength : message.byteLength;
    if (size > MAX_FRAME_BYTES) {
      return this.sendErr(ws, "bad_frame", `帧超过 ${MAX_FRAME_BYTES} 字节`);
    }
    const raw = typeof message === "string" ? message : new TextDecoder().decode(message);
    let frame;
    try {
      frame = JSON.parse(raw);
    } catch {
      return this.sendErr(ws, "bad_frame", "不是合法 JSON");
    }
    if (!frame || typeof frame !== "object" || typeof frame.t !== "string") {
      return this.sendErr(ws, "bad_frame", "帧里缺 t（帧类型）");
    }
    if (frame.t !== FRAME_HELLO && !att.id) {
      return this.sendErr(ws, "bad_frame", "还没握手（先发 hello）");
    }

    switch (frame.t) {
      case FRAME_HELLO:
        return this.onHello(ws, frame, att);
      case FRAME_SEG:
      case FRAME_FINAL:
        return this.onText(ws, frame, att);
      case FRAME_PING:
        return this.send(ws, { t: FRAME_PONG, ts: Date.now() });
      default:
        // 未知帧类型：收下不动。协议往前加帧时，老服务端不该把新客户端踹掉。
        return undefined;
    }
  }

  /** 握手：验房间码 → 查人数 → 分配成员 id → 回 welcome → 全员广播 members。 */
  async onHello(ws, frame, att) {
    const room = String(frame.room || "").trim().toUpperCase();
    if (this.room && room !== this.room) {
      return this.sendErr(ws, "bad_room", `这个连接开在房间 ${this.room}，hello 里写的是 ${room || "(空)"}`);
    }
    if (att.id) {
      // 同一条连接重复 hello：不重新分配 id，只补一次 welcome（重连风暴下常见）
      this.send(ws, { t: FRAME_WELCOME, me: att.id, room: this.room, srv_ts: Date.now() });
      return undefined;
    }

    const joined = await this.members();
    if (joined.length >= MAX_PEERS) {
      return this.sendErr(ws, "room_full", `房间已满（上限 ${MAX_PEERS} 人）`);
    }

    att.id = newPeerId();
    att.nick = cleanNick(frame.nick, att.id.slice(2, 8));
    att.joined = Date.now();
    attach(this.state, ws, att);

    this.send(ws, { t: FRAME_WELCOME, me: att.id, room: this.room, srv_ts: Date.now() });
    await this.broadcastMembers();
    return undefined;
  }

  /**
   * 即收即转：盖上 `src`（谁说的）和 `srv_ts`（时钟基准），扇给房间里**其他人**。
   * 不回发给说话人自己 —— 否则客户端要做回声消除，白白多一处出 bug 的地方。
   * **只对 final 回 ack**：partial 丢了无所谓，下一拍就是更全的快照。
   */
  async onText(ws, frame, att) {
    const isFinal = frame.t === FRAME_FINAL || frame.final === true;
    const out = {
      t: isFinal ? FRAME_FINAL : FRAME_SEG,
      utt: String(frame.utt || ""),
      seq: Number(frame.seq) || 0,
      text: String(frame.text ?? ""),
      lang: String(frame.lang || "zh"),
      final: isFinal,
      ts: Number(frame.ts) || 0,
      srv_ts: Date.now(),
      src: att.id,
    };
    if (!out.utt) {
      return this.sendErr(ws, "bad_frame", "seg/final 缺 utt（句 id）");
    }

    for (const peer of await this.members()) {
      if (peer.ws !== ws) this.send(peer.ws, out);
    }
    if (isFinal) {
      this.send(ws, { t: FRAME_ACK, utt: out.utt, seq: out.seq, ts: Date.now() });
    }
    return undefined;
  }

  // ------------------------------------------------------------ 掉线
  async webSocketClose(ws, code, reason, wasClean) {
    await this.drop(ws, `close ${code}${reason ? ` ${reason}` : ""}`);
  }

  async webSocketError(ws, error) {
    await this.drop(ws, `error ${error?.message || error}`);
  }

  /** 摘掉一条连接：出成员表、全员广播、空了就排一次回收闹钟。 */
  async drop(ws, why) {
    const att = attached(this.state, ws);
    this.sockets = (await this.liveSockets()).filter((s) => s !== ws);
    if (!att || !att.id) return;      // 握过手才算成员，没握过手的不用广播
    console.log(`[room ${this.room}] 成员离开 ${att.nick}(${att.id})：${why}`);
    await this.broadcastMembers();
    // 数**已握手**的成员，不是数 this.sockets —— 后者还混着没发 hello 的连接，
    // 拿它判空房会让回收闹钟永远排不上。
    if ((await this.members()).length === 0) {
      // 唯一允许的"定时"：setAlarm 不阻止休眠（setTimeout 会）
      await this.state.storage.setAlarm(Date.now() + EMPTY_ROOM_GRACE_MS);
    }
  }

  /** 空房回收：闹钟响了还没人回来，就把存储清空（DO 随后由平台自己回收）。 */
  async alarm() {
    if ((await this.members()).length > 0) return;
    await this.state.storage.deleteAll();
    console.log(`[room ${this.room}] 空房超过 ${EMPTY_ROOM_GRACE_MS / 1000}s → 已清空存储`);
  }

  // ------------------------------------------------------------ 工具
  /** 成员表全员广播。`online` 恒 true —— 掉线的人直接从表里消失，不留僵尸条目。 */
  async broadcastMembers() {
    const peers = await this.members();
    const frame = {
      t: FRAME_MEMBERS,
      members: peers.map(({ att }) => ({ id: att.id, nick: att.nick, online: true })),
      room: this.room,
      srv_ts: Date.now(),
    };
    for (const { ws } of peers) this.send(ws, frame);
    return frame.members.length;
  }

  /**
   * 发帧。连接可能已经被对端关了 —— 那时候 send 会抛，**不能让它掀掉整个 DO**：
   * 一条坏连接不该影响房间里其他人。
   */
  send(ws, frame) {
    try {
      if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(frame));
    } catch (err) {
      console.log(`[room ${this.room}] ⚠️ 发帧失败（忽略，等 close 事件收尸）：${err?.message || err}`);
    }
  }

  /**
   * 回一条 `err`。**致命与否只由 code 决定**（`FATAL_CODES`），不再收一个 fatal 参数：
   * 客户端也是只看 code 判定要不要停止重连，两边共用同一份口径才不会对不上。
   * 致命时顺手关连接（客户端拿到致命码就不再重连，重连只会一直被拒）。
   */
  sendErr(ws, code, msg) {
    this.send(ws, { t: FRAME_ERR, code, msg, ts: Date.now() });
    if (FATAL_CODES.has(code)) {
      try {
        ws.close(1008, code);
      } catch {
        /* 已经关了就算了 */
      }
    }
    return undefined;
  }
}
