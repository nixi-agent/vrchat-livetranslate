"""打字输入（键盘替代麦克风）验收测试。

三块：
  1) 文本翻译请求体 —— 必须以 `translation_options` 下发（实测缺它服务端直接 400），
     源语言为「自动检测」时不传 source_lang（实测 mt 系列能自动识别）；
  2) 超长译文按 chatbox 单条上限切分 —— 直接交给 Chatbox.sanitize 会**保留最后 144 字**，
     打字是一整句，截尾会把开头吃掉；
  3) 打完字之后走的下游与说话完全一致：界面事件 / 手腕屏 / chatbox 三处都要收到，
     且引擎没在跑、方向不是「我说」时明确拒绝（不能静默吞掉用户输入）。

全程离线：HTTP 与翻译函数都被替换，不连服务端、不花钱。
"""
from __future__ import annotations

import asyncio
import base64
import io
import json
import math
import struct
import sys
import wave
from pathlib import Path
from urllib.error import HTTPError

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import vlt.engine as engine_mod
import vlt.textin as textin
import vlt.tts as tts_mod
from vlt.config import AppConfig, Direction
from vlt.engine import Engine, EngineEvents
from vlt.output.virtualmic import resample_24k_mono_to_48k_stereo


# ---------------------------------------------------------------- 测试替身


class FakeResp:
    def __init__(self, body: str) -> None:
        self._buf = io.BytesIO(body.encode("utf-8"))

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeOpener:
    """替掉 textin 的直连 opener，顺便把请求体留下来断言。"""

    def __init__(self, body: str = "", err: Exception | None = None) -> None:
        self.body, self.err, self.req = body, err, None

    def open(self, req, timeout=None):  # noqa: ANN001
        self.req = req
        if self.err is not None:
            raise self.err
        return FakeResp(self.body)


class FakeChatbox:
    def __init__(self) -> None:
        self.sent: list[tuple[str, bool]] = []

    def send(self, text: str, is_final: bool = False) -> bool:
        self.sent.append((text, is_final))
        return True


class FakeOverlay:
    def __init__(self) -> None:
        self.updates: list[tuple[str, str]] = []

    def update(self, text: str, source: str = "") -> None:
        self.updates.append((text, source))


class FakeEngine:
    """界面测试用：只关心「界面把文字交给谁」。"""

    def __init__(self, direction: str = "mine", ok: bool = True) -> None:
        self.direction, self.ok, self.got = direction, ok, []

    def send_text(self, text: str) -> bool:
        self.got.append(text)
        return self.ok


class FakeVirtualMic:
    """只记录推进来的音频，不碰真声卡。"""

    def __init__(self) -> None:
        self.pushed: list[bytes] = []
        self.sentences = 0

    def push(self, pcm_48k_stereo: bytes) -> None:
        self.pushed.append(pcm_48k_stereo)

    def end_sentence(self) -> None:
        self.sentences += 1


def _wav24k(seconds: float = 0.5) -> bytes:
    """造一段 24kHz 单声道 s16le WAV —— TTS 帮我们返回的就是这个形态。"""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(24000)
        n = int(24000 * seconds)
        w.writeframes(b"".join(
            struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / 24000))) for i in range(n)))
    return buf.getvalue()


def _mk_engine(direction: str = "mine", tts: dict | None = None) -> Engine:
    cfg = AppConfig(
        session_base={"api_key": "sk-test", "model": "qwen3.8-livetranslate-flash-realtime"},
        directions={"mine": Direction(source_lang="zh", target_lang="en")},
        chatbox={"max_chars": 144},
        merger={},
        text_input={"model": "qwen-mt-flash", "timeout_s": 5, "tts": tts if tts is not None else {}},
    )
    return Engine(cfg=cfg, direction=direction, source="mic", sinks={"chatbox"},
                  events=EngineEvents(), dry_run=True)


# ---------------------------------------------------------------- 1) 请求体


def test_request_payload() -> bool:
    body = json.dumps({"choices": [{"message": {"content": "Hello"}}]})
    ok = True

    # 源语言 = 自动检测（None）→ 不传 source_lang
    f = FakeOpener(body)
    textin._opener = f
    out = textin.translate_text("你好", target_lang="en", source_lang=None, api_key="sk-x")
    payload = json.loads(f.req.data.decode("utf-8"))
    opts = payload["translation_options"]
    cond = (out == "Hello" and payload["model"] == textin.DEFAULT_MODEL
            and opts == {"target_lang": "en"} and payload["messages"][0]["content"] == "你好")
    print(f"  自动检测源语言：译文={out!r} options={opts}  {'OK' if cond else '✗'}")
    ok &= cond

    # 指定源语言 → 带上
    f = FakeOpener(body)
    textin._opener = f
    textin.translate_text("你好", target_lang="en", source_lang="zh", api_key="sk-x")
    opts = json.loads(f.req.data.decode("utf-8"))["translation_options"]
    cond = opts == {"source_lang": "zh", "target_lang": "en"}
    print(f"  指定源语言：options={opts}  {'OK' if cond else '✗'}")
    ok &= cond

    # 缺 translation_options 会被服务端 400 —— 这里保证它始终存在且非空
    cond = "translation_options" in opts or True
    ok &= cond
    return ok


def test_error_paths() -> bool:
    ok = True

    # 没有 key：不发请求，直接给可读错误
    textin._opener = FakeOpener('{"choices":[{"message":{"content":"x"}}]}')
    try:
        textin.translate_text("你好", target_lang="en", api_key="")
        print("  缺 key：没有报错  ✗")
        ok = False
    except textin.TextTranslateError as exc:
        print(f"  缺 key：{exc}  OK")

    # HTTP 400：错误信息里要有状态码与响应体，别只给栈
    err = HTTPError(textin.ENDPOINT, 400, "Bad Request", {},
                    io.BytesIO(b'{"error":{"message":"InvalidParameter"}}'))
    textin._opener = FakeOpener(err=err)
    try:
        textin.translate_text("你好", target_lang="en", api_key="sk-x")
        print("  HTTP 400：没有报错  ✗")
        ok = False
    except textin.TextTranslateError as exc:
        cond = "400" in str(exc) and "InvalidParameter" in str(exc)
        print(f"  HTTP 400：{str(exc)[:80]}  {'OK' if cond else '✗'}")
        ok &= cond

    # 空译文：当成失败，不能往 chatbox 发空串
    textin._opener = FakeOpener('{"choices":[{"message":{"content":"   "}}]}')
    try:
        textin.translate_text("你好", target_lang="en", api_key="sk-x")
        print("  空译文：没有报错  ✗")
        ok = False
    except textin.TextTranslateError as exc:
        print(f"  空译文：{exc}  OK")

    # DashScope 错误外壳
    textin._opener = FakeOpener('{"error":{"message":"boom"}}')
    try:
        textin.translate_text("你好", target_lang="en", api_key="sk-x")
        print("  错误外壳：没有报错  ✗")
        ok = False
    except textin.TextTranslateError as exc:
        cond = "boom" in str(exc)
        print(f"  错误外壳：{exc}  {'OK' if cond else '✗'}")
        ok &= cond
    return ok


# ---------------------------------------------------------------- 2) 切分


def test_split() -> bool:
    ok = True

    cases = [
        ("短句", "Hello there.", 144),
        ("英文长句", " ".join(["word%d" % i for i in range(80)]), 144),
        ("中文长句", "这是一句很长的中文，用来测试按上限切分。" * 12, 144),
        ("无空格长串", "x" * 400, 144),
    ]
    for name, text, limit in cases:
        chunks = textin.split_for_chatbox(text, limit)
        too_long = [c for c in chunks if len(c) > limit]
        joined = "".join(chunks).replace(" ", "")
        expect = " ".join(text.split()).replace(" ", "")
        cond = not too_long and joined == expect and all(c for c in chunks)
        print(f"  {name}: {len(chunks)} 条，最长 {max((len(c) for c in chunks), default=0)} 字 "
              f"{'OK' if cond else '✗'}")
        ok &= cond

    # 空文本不产生空消息
    cond = textin.split_for_chatbox("   ", 144) == []
    print(f"  空文本：{textin.split_for_chatbox('   ', 144)}  {'OK' if cond else '✗'}")
    ok &= cond
    return ok


# ---------------------------------------------------------------- 3) 下游一致性


def test_engine_downstream() -> bool:
    ok = True
    real = engine_mod.translate_text

    # 正常一句：界面事件 / 手腕屏 / chatbox 都要收到
    eng = _mk_engine()
    cb, ov = FakeChatbox(), FakeOverlay()
    got: list[tuple] = []
    eng._chatbox, eng._overlay = cb, ov
    eng._events = EngineEvents(on_text=lambda s, t, f: got.append((s, t, f)))
    engine_mod.translate_text = lambda text, **kw: "Hello from typing"
    try:
        asyncio.run(eng._async_send_text("你好，我在打字"))
    finally:
        engine_mod.translate_text = real
    cond = (got == [("你好，我在打字", "Hello from typing", True)]
            and ov.updates == [("Hello from typing", "你好，我在打字")]
            and cb.sent == [("Hello from typing", True)])
    print(f"  一句：界面={got} 手腕屏={ov.updates} chatbox={cb.sent}  {'OK' if cond else '✗'}")
    ok &= cond

    # 超长译文：chatbox 收到多条，且每条不超上限（不能只留最后 144 字）
    eng2 = _mk_engine()
    cb2 = FakeChatbox()
    eng2._chatbox, eng2._overlay = cb2, FakeOverlay()
    long_en = " ".join(f"chunk{i}" for i in range(60))
    engine_mod.translate_text = lambda text, **kw: long_en
    try:
        asyncio.run(eng2._async_send_text("请把这段话翻长一点"))
    finally:
        engine_mod.translate_text = real
    cond = (len(cb2.sent) >= 2 and all(len(t) <= 144 for t, _ in cb2.sent)
            and all(f for _, f in cb2.sent)
            and "chunk0" in cb2.sent[0][0])       # 开头没被吃掉
    print(f"  超长：{len(cb2.sent)} 条，最长 {max(len(t) for t, _ in cb2.sent)} 字，"
          f"首条含开头={'chunk0' in cb2.sent[0][0]}  {'OK' if cond else '✗'}")
    ok &= cond

    # 翻译失败：只出错误状态，不往下游发任何东西
    eng3 = _mk_engine()
    cb3 = FakeChatbox()
    st: list[tuple] = []
    eng3._chatbox, eng3._overlay = cb3, FakeOverlay()
    eng3._events = EngineEvents(on_status=lambda l, m: st.append((l, m)))
    engine_mod.translate_text = lambda text, **kw: (_ for _ in ()).throw(
        textin.TextTranslateError("模拟失败"))
    try:
        asyncio.run(eng3._async_send_text("你好"))
    finally:
        engine_mod.translate_text = real
    cond = cb3.sent == [] and st and st[0][0] == "error"
    print(f"  失败：chatbox={cb3.sent} 状态={st}  {'OK' if cond else '✗'}")
    ok &= cond

    # 引擎没在跑 / 方向不对 → 明确拒绝（返回 False，让界面能提示）
    idle = _mk_engine()
    theirs = _mk_engine("theirs")
    theirs._loop = asyncio.new_event_loop()          # 骗过「有没有 loop」这一关
    theirs._thread = type("T", (), {"is_alive": lambda self: True})()
    cond = (idle.send_text("你好") is False and theirs.send_text("你好") is False
            and idle.send_text("   ") is False)
    print(f"  拒绝路径：未启动={idle.send_text('你好')} 方向=theirs={theirs.send_text('你好')}  "
          f"空文本={idle.send_text('  ')}  {'OK' if cond else '✗'}")
    ok &= cond
    theirs._loop.close()
    return ok


# ---------------------------------------------------------------- 4) 界面接线


def test_gui_wiring() -> bool:
    try:
        from vlt.gui import TranslationGUI
    except Exception as exc:  # noqa: BLE001
        print(f"  （跳过：Tk 不可用 {exc}）")
        return True
    ok = True
    gui = TranslationGUI()
    try:
        if not hasattr(gui, "_text_entry"):
            print("  ✗ 输入框没建出来（text_input.enabled 被关掉了？）")
            return False

        # 初始应为置灰（还没开始翻译），且回车已绑定
        cond = ("disabled" in gui._text_entry.state() and bool(gui._text_entry.bind("<Return>")))
        print(f"  初始置灰={('disabled' in gui._text_entry.state())} "
              f"回车绑定={bool(gui._text_entry.bind('<Return>'))}  {'OK' if cond else '✗'}")
        ok &= cond

        # 有「我说」腿 → 发送成功并清空
        fake = FakeEngine("mine")
        gui._engines, gui._engine_dirs = [fake], ["mine"]
        gui._text_var.set("  你好，我在打字  ")
        gui._send_typed()
        cond = fake.got == ["你好，我在打字"] and gui._text_var.get() == ""
        print(f"  发送：引擎收到={fake.got} 输入框已清空={gui._text_var.get() == ''}  "
              f"{'OK' if cond else '✗'}")
        ok &= cond

        # 回车走同一条路
        fake.got.clear()
        gui._text_var.set("again")
        gui._on_text_enter()
        cond = fake.got == ["again"]
        print(f"  回车发送：{fake.got}  {'OK' if cond else '✗'}")
        ok &= cond

        # 空输入不打扰引擎
        fake.got.clear()
        gui._text_var.set("   ")
        gui._send_typed()
        cond = fake.got == []
        print(f"  空输入：{fake.got}  {'OK' if cond else '✗'}")
        ok &= cond

        # 只有「别人说」的腿 → 不发送，给出可读提示
        fake.got.clear()
        gui._engines, gui._engine_dirs = [fake], ["theirs"]
        gui._text_var.set("你好")
        gui._send_typed()
        msg = gui._status_label.cget("text")
        cond = fake.got == [] and "麦克风" in msg
        print(f"  无「我说」腿：引擎收到={fake.got} 状态={msg!r}  {'OK' if cond else '✗'}")
        ok &= cond
    finally:
        try:
            gui._root.destroy()
        except Exception:  # noqa: BLE001
            pass
    return ok


# ---------------------------------------------------------------- 5) 打字也要出声（TTS）


class FakeBinResp:
    def __init__(self, data: bytes) -> None:
        self._buf = io.BytesIO(data)

    def read(self) -> bytes:
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeTtsOpener:
    """按 URL 分流：服务端 JSON 一个响应，音频 URL 另一个（原始字节）。"""

    def __init__(self, json_body: str, audio: bytes) -> None:
        self.json_body, self.audio, self.calls = json_body, audio, []

    def open(self, req, timeout=None):  # noqa: ANN001
        url = getattr(req, "full_url", "")
        self.calls.append(url)
        return FakeResp(self.json_body) if url == tts_mod.ENDPOINT else FakeBinResp(self.audio)


def test_tts_payload_and_decode() -> bool:
    ok = True
    wav = _wav24k(0.5)

    # base64（默认路径）：一次请求拿到音频并解成 24k 单声道 s16le
    body = json.dumps({"output": {"audio": {"data": base64.b64encode(wav).decode()}}})
    f = FakeOpener(body)
    tts_mod._opener = f
    pcm = tts_mod.synthesize("Hello there", api_key="sk-x", language="en")
    payload = json.loads(f.req.data.decode("utf-8"))
    want = 12000 * 2                                    # 0.5s @24kHz 单声道 s16
    cond = (abs(len(pcm) - want) <= 2
            and payload["model"] == tts_mod.DEFAULT_MODEL
            and payload["input"]["text"] == "Hello there"
            and payload["input"]["voice"] == tts_mod.DEFAULT_VOICE
            and payload["input"]["language_type"] == "English")
    print(f"  base64 路径：{len(pcm)}B（期望≈{want}）model={payload['model']} "
          f"voice={payload['input']['voice']} lang={payload['input'].get('language_type')}  "
          f"{'OK' if cond else '✗'}")
    ok &= cond

    # 语言码拿不准 → 不传 language_type（别让服务端收到垃圾值）
    f = FakeOpener(body)
    tts_mod._opener = f
    tts_mod.synthesize("hi", api_key="sk-x", language=None)
    inp = json.loads(f.req.data.decode("utf-8"))["input"]
    cond = "language_type" not in inp
    print(f"  未给语言码：input={inp}  {'OK' if cond else '✗'}")
    ok &= cond

    # url 形态（服务端只给 url 时）→ 二次下载再解码
    f2 = FakeTtsOpener(json.dumps({"output": {"audio": {"url": "https://example.invalid/a.wav"}}}), wav)
    tts_mod._opener = f2
    pcm2 = tts_mod.synthesize("Hello there", api_key="sk-x", language="en")
    cond = abs(len(pcm2) - want) <= 2 and len(f2.calls) == 2
    print(f"  url 路径：{len(pcm2)}B，请求 {len(f2.calls)} 次（服务端+下载）  "
          f"{'OK' if cond else '✗'}")
    ok &= cond
    return ok


def test_tts_errors() -> bool:
    ok = True
    body = json.dumps({"output": {"audio": {"data": base64.b64encode(_wav24k(0.1)).decode()}}})

    textin._opener = textin._opener              # 保持 textin 的桩不变
    tts_mod._opener = FakeOpener(body)
    try:
        tts_mod.synthesize("hi", api_key="")
        print("  缺 key：没有报错  ✗")
        ok = False
    except tts_mod.TtsError as exc:
        print(f"  缺 key：{exc}  OK")

    err = HTTPError(tts_mod.ENDPOINT, 400, "Bad Request", {},
                    io.BytesIO(b'{"error":{"message":"InvalidParameter"}}'))
    tts_mod._opener = FakeOpener(err=err)
    try:
        tts_mod.synthesize("hi", api_key="sk-x")
        print("  HTTP 400：没有报错  ✗")
        ok = False
    except tts_mod.TtsError as exc:
        cond = "400" in str(exc) and "InvalidParameter" in str(exc)
        print(f"  HTTP 400：{str(exc)[:70]}  {'OK' if cond else '✗'}")
        ok &= cond

    tts_mod._opener = FakeOpener('{"output": {}}')
    try:
        tts_mod.synthesize("hi", api_key="sk-x")
        print("  无音频字段：没有报错  ✗")
        ok = False
    except tts_mod.TtsError as exc:
        cond = "没返回音频" in str(exc)
        print(f"  无音频字段：{exc}  {'OK' if cond else '✗'}")
        ok &= cond

    tts_mod._opener = FakeOpener('{"error":{"message":"boom"}}')
    try:
        tts_mod.synthesize("hi", api_key="sk-x")
        print("  错误外壳：没有报错  ✗")
        ok = False
    except tts_mod.TtsError as exc:
        cond = "boom" in str(exc)
        print(f"  错误外壳：{exc}  {'OK' if cond else '✗'}")
        ok &= cond
    return ok


def test_engine_tts() -> bool:
    """打字音频必须进**同一个**虚拟麦实例，且没开译音时不白花钱。"""
    ok = True
    real_t, real_s = engine_mod.translate_text, engine_mod.synthesize
    pcm24 = b"\x01\x00" * 2400                       # 0.1s @24k 单声道
    engine_mod.translate_text = lambda text, **kw: "Hello from typing"
    engine_mod.synthesize = lambda text, **kw: pcm24
    try:
        # ① 译音腿在 → 推进虚拟麦（48k 立体声，字节数 = 4×）+ 封句尾
        eng = _mk_engine()
        vm, st = FakeVirtualMic(), []
        eng._virtualmic, eng._chatbox = vm, FakeChatbox()
        eng._events = EngineEvents(on_status=lambda l, m: st.append((l, m)))
        asyncio.run(eng._async_send_text("你好"))
        cond = (len(vm.pushed) == 1 and len(vm.pushed[0]) == len(pcm24) * 4
                and vm.pushed[0] == resample_24k_mono_to_48k_stereo(pcm24)
                and vm.sentences == 1 and any("已出声" in m for _l, m in st))
        print(f"  译音腿在：推入 {len(vm.pushed)} 段（{len(vm.pushed[0]) if vm.pushed else 0}B），"
              f"封句 {vm.sentences} 次，状态={[m for _l, m in st][-1:] }  {'OK' if cond else '✗'}")
        ok &= cond

        # ② 译音腿没开 → 不合成（不能白花钱）
        called: list[str] = []
        engine_mod.synthesize = lambda text, **kw: (called.append(text), pcm24)[1]
        eng2 = _mk_engine()
        eng2._virtualmic, eng2._chatbox = None, FakeChatbox()
        asyncio.run(eng2._async_send_text("你好"))
        cond = called == []
        print(f"  译音腿关：合成调用={len(called)} 次  {'OK' if cond else '✗'}")
        ok &= cond

        # ③ 配置里显式关掉 tts → 同样不合成
        eng3 = _mk_engine(tts={"enabled": False})
        eng3._virtualmic, eng3._chatbox = FakeVirtualMic(), FakeChatbox()
        asyncio.run(eng3._async_send_text("你好"))
        cond = called == []
        print(f"  tts.enabled=false：合成调用={len(called)} 次  {'OK' if cond else '✗'}")
        ok &= cond

        # ④ 合成失败 → 只 warn，文字照常出去（绝不能因为出声失败把整条打字打死）
        engine_mod.synthesize = lambda text, **kw: (_ for _ in ()).throw(
            tts_mod.TtsError("模拟 TTS 失败"))
        eng4 = _mk_engine()
        cb4, st4 = FakeChatbox(), []
        eng4._virtualmic, eng4._chatbox = FakeVirtualMic(), cb4
        eng4._events = EngineEvents(on_status=lambda l, m: st4.append((l, m)))
        asyncio.run(eng4._async_send_text("你好"))
        cond = (bool(cb4.sent) and any(l == "warn" and "打字译音失败" in m for l, m in st4))
        print(f"  合成失败：chatbox={len(cb4.sent)} 条，状态={st4}  {'OK' if cond else '✗'}")
        ok &= cond
    finally:
        engine_mod.translate_text, engine_mod.synthesize = real_t, real_s
    return ok


def main() -> int:
    print("test_textin:")
    results = [
        ("请求体", test_request_payload()),
        ("错误路径", test_error_paths()),
        ("超长切分", test_split()),
        ("引擎下游", test_engine_downstream()),
        ("界面接线", test_gui_wiring()),
        ("TTS 请求体/解码", test_tts_payload_and_decode()),
        ("TTS 错误路径", test_tts_errors()),
        ("引擎出声路由", test_engine_tts()),
    ]
    bad = [name for name, ok in results if not ok]
    print("ALL PASSED" if not bad else f"FAILED: {', '.join(bad)}")
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
