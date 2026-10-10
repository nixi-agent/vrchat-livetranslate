# 房间文本中继 · 服务端（Cloudflare Worker + Durable Object）

把每个人的 ASR 源文广播给同一房间里的其他人。**只转文字**：不翻译、不合成语音、
不缓冲、不排序、不落库。一个房间码 = 一个 Durable Object 实例。

> 本目录是**骨架**（批次 1）。客户端在 `vlt/room/`，协议定义见
> `.hermes/plans/2026-09-28_112035-room-relay.md` §4。

---

## 本地跑起来

```bash
cd server
npm install                 # 只装 wrangler（devDependency）
npx wrangler dev --local    # → http://127.0.0.1:8787
```

探活（不是 WebSocket 的请求会拿到一个只读 JSON 状态页）：

```bash
curl http://127.0.0.1:8787/
```

WebSocket 端点：

```
ws://127.0.0.1:8787/ws?room=<8位房间码>
```

- `room` 必填，8 位 Crockford Base32（不含易混的 `I/L/O/U`；填错了会自动把
  `I/L→1`、`O→0` 归一，两端同一套映射，所以手抄错了也能进同一个房）。
  房间码就是 DO 的路由键，所以**必须在查询串里**给 ——
  等 `hello` 帧到了再认房间就晚了，那时候连接已经建好了。
- 客户端连上后第一帧发 `hello`，服务端回 `welcome`，然后开始即收即转。

拿真客户端打本地 DO（批次 1 用这个验过，见下「已验证」）—— `server_url` **不用**手写
`?room=`，`RoomClient._connect_url()` 会把归一化后的房间码补进查询串。
令牌只放在 Authorization header；旧 URL 内的 `k`/`tok`/`token` 会迁移到 header，
不再进入连接 URL 或 HELLO。远程地址必须使用 `wss://`，`ws://` 仅允许本机 loopback：

```python
RoomConfig(server_url="ws://127.0.0.1:8787/ws", room_code="TEST1234", ...)
```

## 令牌（可选）

不配就是**开放房间**：任何拿到房间码的人都能进。要开门禁，在 `server/.dev.vars`
（本地）或 Worker Secrets（线上）里配 `ROOM_TOKEN_HASH`：

```bash
# 本地：server/.dev.vars（已被 .gitignore 排除，别提交）
ROOM_TOKEN_HASH=<令牌的 sha256 十六进制小写>
```

```bash
# 算哈希（PowerShell）
$t = 'my-secret-token'
$sha = [System.Security.Cryptography.SHA256]::Create()
([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($t))) -replace '-','').ToLower()
```

服务端**只存哈希**，明文令牌既不落盘也不进日志。鉴权在 Worker 层做完：
令牌不对直接 401/403，**不进 DO** —— 否则每一发攻击流量都在替你付 DO 请求费。

普通 ASCII 令牌使用 `Authorization: Bearer <token>`，兼容旧 Worker。
Unicode 或不符合 token68 的令牌使用 `Authorization: VLT <UTF-8 token 的 base64url>`；
这类令牌需更新 Worker 后使用。base64url 是编码，不是加密，远程仍必须使用 TLS。
客户端不会跟随重定向，避免认证明文转交其他端点。服务端保留旧 query/Bearer 客户端兼容性，
旧 query 客户端仍可能把令牌写入代理访问日志，应升级；不能保证旧客户端请求不留令牌。

## 部署（已上线，2026-09-28）

**已部署**：`vlt-room-relay` → 自定义域 **`wss://vlt-room.kcm-nixi.cn/ws`**
（Worker 版本见 CF 控制台；账号 `842803916@qq.com`，Free 计划）。

```bash
cd server
npx wrangler deploy                        # 首次会建 DO migration v1
npx wrangler secret put ROOM_TOKEN_HASH    # 可选：开门禁（不配 = 开放房间）
```

**必须绑自定义域名，不要用 `workers.dev`** —— `*.workers.dev` 在大陆基本不可达，
而这个项目的用户就在大陆。`wrangler.toml` 里用 **custom domain**（wrangler 自己建 DNS + 证书）：

```toml
[[routes]]
pattern = "vlt-room.kcm-nixi.cn"
custom_domain = true
```

客户端把 `room.server_url` 填成 `wss://vlt-room.kcm-nixi.cn/ws`（房间码由客户端自己补进查询串）。

### DO 落点 A/B（真边缘实测，2026-09-28）

`locationHint` 只对**首次创建**生效，所以每档都要用一个**没用过的房间码**测。

| `ROOM_LOCATION_HINT` | min | **p50** | p90 | max |
|---|---|---|---|---|
| `apac` | 410 | **414** | 886 | 970 ms |
| **`wnam`（现用）** | 368 | **372** | **393** | 814 ms |

口径：本机（昆明电信出口，任何 cast 落 `colo=LAX`）开**两个**客户端进同一房间，
量「A 发 final → B 收到」的单程耗时，12 次采样。注意这是**同机双端**，数值 ≈ 2×「本机→边缘→DO」；
真·中韩两人时双方各自的路由都要重新量。结论：**入口在美西时 DO 也放美西（`wnam`）比放亚太少两跳**，
p90 从 886ms 掉到 393ms。换成别的网络环境改 `[vars] ROOM_LOCATION_HINT` 一行即可，不用动代码。

---

## 三条不能破的纪律

改这个目录之前先读完，这三条都是**计费/可用性**级别的坑，不是代码风格问题。

### 1. 必须用 `state.acceptWebSocket(ws)`，不能用 `ws.accept()`

`ws.accept()` 会让 DO 在整条连接存续期间保持唤醒，**按连接时长计费**；
Hibernation（`state.acceptWebSocket`）把连接托管给 CF 边缘，没消息时 DO 不占 CPU。
一个房间 8 个人挂着不说话，两种写法的账单差一个数量级。

代价：**休眠后内存全清空**。所以每连接状态（成员 id、昵称、限速窗口）必须
`serializeAttachment()` 挂到连接上，醒来时用 `getWebSockets()` + `deserializeAttachment()`
捞回来。`src/room.js` 里的 `attach()/attached()` 就是这层 —— 本机 workerd 只把这两个
方法挂在 WebSocket 上、`state` 上没有，新版文档写的是 `state.serializeAttachment(ws, x)`，
所以两处都试，取到哪个用哪个。

### 2. 不能用 `setTimeout` / `setInterval`

有待处理的定时器 = DO 不许休眠，等于白写了第 1 条。空房回收改用
`storage.setAlarm()`：最后一个人走了排一次闹钟，`alarm()` 里发现还是空的就
`storage.deleteAll()`。

### 3. 不能给还没握手的连接发房间帧

`fetch()` 里 `acceptWebSocket` 之后连接就进了 `this.sockets`，但那时它**还没发
`hello`**、还没被分配成员 id。如果这时候别人进房触发了 `members` 广播，这条连接的
第一帧就是 `members` 而不是 `welcome` —— 客户端按协议判错、直接断线重连。

两人同时进房时这个竞态**在真机 `wrangler dev --local` 上实测复现过**。
所以所有扇出/广播/人数判断都必须走 `members()`（只返回已握手的连接）。

---

## 其它口径

| 项 | 值 | 说明 |
|---|---|---|
| 成员上限 | 8 | 再多手腕屏也显示不下；满了回 `err{code:"room_full"}` 并关连接 |
| 每连接限速 | 20 帧/秒 | 固定 1 秒窗口，超出的帧**丢掉** + 回一条非致命 `err{code:"rate"}`，不断线 |
| 帧大小上限 | 8192 字节 | 与客户端 `protocol.MAX_FRAME_BYTES` 对齐 |
| ack | **只对 `final`** | partial 丢了无所谓，下一拍就是更全的快照 |
| 回声 | 不回发给说话人自己 | 客户端因此不需要做回声消除 |
| 落点 | `locationHint: "apac"` | 只是 best-effort，CF 明确不保证 |
| 存储类 | `new_sqlite_classes` | 本 DO 只用 `setAlarm/deleteAll`，不碰 `storage.sql` |

客户端拿到 `err` 的行为：`code` 属于 `{auth, room_full, bad_room, banned}` 就**不再重连**
（重连只会一直被拒，白烧请求），其余（比如 `rate`、`bad_frame`）留痕但保持连接。

`err` 帧里**没有** `fatal` 字段 —— 致命与否只由 `code` 决定。服务端对这四个致命码发完 `err`
就顺手 `close(1008)`。口径写在两处：`src/room.js` 的 `FATAL_CODES` 和客户端
`vlt/room/protocol.py` 的 `FATAL_ERR_CODES`，**改一边必须改另一边**，否则会出现
「服务端认为致命、客户端还在退避重连」这种一直撞墙的循环。

## 已验证 / 未验证

前一批是在 `npx wrangler dev --local`（wrangler 4.142.0 / node v22.23.1，本机 workerd）上实测的，
探针脚本用完就删（不入库）；**带 ⭐ 的是 2026-09-28 部署到真 CF 边缘之后补上的**。

- ✅ **起得来**：`/` 返回只读 JSON 状态页；`/ws` 非升级请求回 426；房间码非法回 400。
- ✅ **正常收发**：两个真 `RoomClient` 同时进房 —— 成员表 2 人、3 个 partial + 1 个 final
  共 4 帧按 `seq=[1,2,3,4]` 有序到达、文本与发送端一致、说话人归属（成员 id + 昵称）正确、
  `final` 被 ack（`pending_replay` 归零）、发送端无回声、零重连零错误留痕、`stop()` 后线程退出。
- ✅ **`bad_room`（致命）**：URL 上房间码与 `hello` 里的不一致 → 回 `err{code:"bad_room"}`
  并 `close(1008)`。
- ✅ **`bad_frame`（非致命）**：发一段坏 JSON → 回 `err{code:"bad_frame"}`，**连接保留**，
  紧接着补一个合法 `hello` 仍能拿到 `welcome`。
- ✅ **`room_full`（致命）**：塞满 8 人后第 9 条连接 → 回 `err{code:"room_full"}` + `close(1008)`；
  真 `RoomClient` 拿到它之后 `last_error` 留下可读原因、连接态转 `error`、**零重连**、线程退出。

⭐ 真边缘（`wss://vlt-room.kcm-nixi.cn/ws`）上复验通过：

- ✅ **部署本身**：custom domain 建起来、证书正常、`/` 状态页可读，DO migration v1 自动建。
- ✅ **双端收发**：跨洋真收发正确、成员表同步、另一房间**零串音**（DO 按房间码隔离）、反向也通。
- ⭐ **DO 休眠后恢复成员表**（原先风险最高的一条）：A 进房 → **全静默 50s**（关掉应用层心跳，
  确保没有流量）→ B 进房唤醒 DO。结果：A 全程 `online`（连接被 CF 边缘托管，没掉）、
  B 看到 2 人、**A 也看到 2 人**（DO 醒来后靠 `getWebSockets()` + `deserializeAttachment()`
  把休眠前的 A 认了回来）、唤醒后扇出正常。
  ⚠️ 诚实说明：无法直接观测「DO 到底有没有真被驱逐」，本测试证明的是**跨 50s 静默后行为正确**。
- ✅ **令牌鉴权三条分支**（真边缘）：无令牌 → **401**、错令牌 → **403**、对令牌 → **101** 且能正常收发。
  不配 `ROOM_TOKEN_HASH` 时状态页显示「未开启（任何人都能进房）」。当前线上**未开启**。
- ✅ **8 人上限（真 DO）**：8 条连接全进房，第 9 条被拒。
- ✅ **坏房间码（真握手）**：`SHORT` / `AAAA-BBBB` / `AAAAAAA1I2` 走真实 WebSocket 握手均被 **400** 拒。

⭐ 真边缘上**发现并修掉**的客户端缺陷（`vlt/room/client.py`）：

- **Worker 在建连前用 HTTP 拒绝时，客户端会无限重连**。鉴权与房间码校验都在 Worker 层做完，
  被拒时连接根本没升级成 WebSocket，客户端**收不到**协议里的 `err{code:"auth"}`，
  于是把 403 当成网络抖动按退避一直重连（实测 `reconnects=2` 且还在涨）。
  现已在 `_open_ws()` 里把 400/401/403 翻成 `_FatalRoomError`（零重连 + 原因可读），
  5xx 等临时故障保持可重连。回归断言见 `tests/test_room_client.py::test_http_rejection_is_fatal`。

仍然**未验证**：

- ❌ **`alarm()` 空房回收**：最后一人离开后 60s 宽限期到点清存储这条，只在代码层确认过；
  线上要等一个真实空房挂满 60s 才能观察到（日志里会有「空房超过 60s → 已清空存储」）。
- ❌ **真·中韩双人**：目前所有延迟数字都是**本机双客户端**打边缘得到的，
  韩国那一侧的入口与路由一次都没量过。
- ❌ **20 帧/秒限速在真 DO 上的计时精度**：签桶逻辑在进程内假中继上验过，真 DO 没跑过。
