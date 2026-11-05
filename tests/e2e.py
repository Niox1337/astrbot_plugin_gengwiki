"""Real AstrBot and this plugin, with a fake wiki, a stub model and a fake QQ client, so it spends nothing.

Takes about three minutes. Needs gengwiki-python checked out next to this repo, for its fake wiki, and a Python with both installed.

    uv venv --python 3.12
    uv pip install astrbot
    uv pip install --no-deps ../gengwiki-python
    .venv/Scripts/python tests/e2e.py
"""
import asyncio
import base64
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from aiohttp import web

sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
PLUGIN = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN.parent / "gengwiki-python"))
from tests.fake import KEY_A, KEY_B, PNG, Fake, serve  # noqa: E402

ROOT = Path(tempfile.mkdtemp(prefix="gengwiki-e2e-"))
LOG = ROOT / "astrbot.log"
KEY_BAD = "gwk_live_cccccc" + "0" * 42
SKIP = "不接话"
JOIN = "没有人叫你，默认不插话"
SPOKEN = "躺平是吧，我懂"
NORMAL = "普通回复"
G, FRESH, QUIET = 20000, 20001, 20002
# astrbot run, without its dashboard download. AstrBot imports plugins as data.plugins.x, and -c puts the current folder on the path.
BOOT = """
import asyncio
from astrbot.core import LogBroker, LogManager, db_helper, logger
from astrbot.core.initial_loader import InitialLoader

async def main():
    broker = LogBroker()
    LogManager.set_queue_handler(logger, broker)
    await InitialLoader(db_helper, broker).start()

asyncio.run(main())
"""

llm = []
results = []


def ok(name, good, detail=""):
    results.append((name, bool(good)))
    print(("  ok   " if good else "  FAIL ") + name + ("" if good or not detail else f"   <= {str(detail)[:400]}"))


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def flat(content):
    if isinstance(content, str):
        return content
    return "\n".join(p.get("text", "") for p in content or [] if isinstance(p, dict))


def last_user(body):
    return flat(next(m for m in reversed(body["messages"]) if m["role"] == "user")["content"])


def system(body):
    return "\n".join(flat(m["content"]) for m in body["messages"] if m["role"] == "system")


def tool_call(name, arguments):
    return {"index": 0, "id": "call_1", "type": "function", "function": {"name": name, "arguments": arguments}}


def script(messages):
    """What the stub model says, picked by a marker in the last user message."""
    text = last_user({"messages": messages})
    done = messages[-1]["role"] == "tool"
    if "CALLTOOL" in text:
        if done:
            return flat(messages[-1]["content"]), None
        _, name, arguments = text[text.index("CALLTOOL"):].split("\n")[0].split(" ", 2)
        return None, [tool_call(name, arguments)]
    if "STICKERME" in text and not done:
        return None, [tool_call("send_sticker", json.dumps({"caption": "震惊"}, ensure_ascii=False))]
    if "SKIPME" in text:
        return f"[{SKIP}]", None
    if "SPEAKME" in text:
        return SPOKEN, None
    return NORMAL, None


async def chat(request):
    body = await request.json()
    llm.append(body)
    text, calls = script(body["messages"])
    finish = "tool_calls" if calls else "stop"
    if not body.get("stream"):
        message = {"role": "assistant", "content": text}
        if calls:
            message["tool_calls"] = [{k: v for k, v in c.items() if k != "index"} for c in calls]
        return web.json_response({"id": "x", "object": "chat.completion", "created": 1, "model": "stub-model", "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
    resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
    await resp.prepare(request)

    async def emit(delta, reason=None):
        chunk = {"id": "x", "object": "chat.completion.chunk", "created": 1, "model": "stub-model", "choices": [{"index": 0, "delta": delta, "finish_reason": reason}]}
        await resp.write(f"data: {json.dumps(chunk, ensure_ascii=False)}\n\n".encode())

    await emit({"role": "assistant", "tool_calls": calls} if calls else {"role": "assistant", "content": text})
    await emit({}, finish)
    await resp.write(b"data: [DONE]\n\n")
    await resp.write_eof()
    return resp


class QQ:
    """A OneBot v11 client on AstrBot's reverse WebSocket."""

    def __init__(self):
        self.sent = []
        self.mid = 0
        self.ws = None
        self.session = None

    async def connect(self, port):
        await self.close()
        self.session = aiohttp.ClientSession()
        for _ in range(180):
            try:
                self.ws = await self.session.ws_connect(f"ws://127.0.0.1:{port}/ws", headers={"X-Self-ID": "10000", "X-Client-Role": "Universal"}, max_msg_size=0)
                break
            except (aiohttp.ClientError, OSError):
                await asyncio.sleep(1)
        else:
            raise RuntimeError("AstrBot never opened its WebSocket")
        asyncio.create_task(self.pump(self.ws))

    async def close(self):
        if self.session:
            await self.session.close()

    async def pump(self, ws):
        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            data = json.loads(msg.data)
            if "action" not in data:
                continue
            if data["action"].startswith("send_"):
                self.sent.append(data["params"])
            answer = {"message_id": len(self.sent), "nickname": "梗Bot", "card": "", "user_id": 10000, "group_name": "测试群", "group_id": data["params"].get("group_id", 0)}
            await ws.send_json({"status": "ok", "retcode": 0, "data": answer, "echo": data.get("echo")})

    async def post(self, text, at, group):
        self.mid += 1
        segments = ([{"type": "at", "data": {"qq": "10000"}}, {"type": "text", "data": {"text": " "}}] if at else []) + [{"type": "text", "data": {"text": text}}]
        await self.ws.send_json({"time": int(time.time()), "self_id": 10000, "post_type": "message", "message_type": "group", "sub_type": "normal", "message_id": self.mid, "group_id": group, "user_id": 30000, "anonymous": None, "message": segments, "raw_message": text, "font": 0, "sender": {"user_id": 30000, "nickname": "群友", "card": "", "role": "member"}})


qq = QQ()


async def say(text, at=False, group=G, expect="send", timeout=25):
    """expect: send waits for a message, llm waits for a model call and then for nothing to go out, none just waits."""
    a, b = len(qq.sent), len(llm)
    await qq.post(text, at, group)
    end = time.time() + timeout
    while time.time() < end:
        if (expect == "send" and len(qq.sent) > a) or (expect == "llm" and len(llm) > b) or expect == "none":
            break
        await asyncio.sleep(0.05)
    await asyncio.sleep({"send": 1.5, "llm": 2.5, "none": 4}[expect])
    return qq.sent[a:], llm[b:]


def text(sent):
    out = []
    for p in sent:
        m = p["message"]
        out += [m] if isinstance(m, str) else [s["data"].get("text", "") for s in m if s["type"] == "text"]
    return "\n".join(out)


def images(sent):
    return [s["data"]["file"] for p in sent if not isinstance(p["message"], str) for s in p["message"] if s["type"] == "image"]


def is_png(file):
    return file.startswith("base64://") and base64.b64decode(file[9:]) == PNG


def history(group):
    db = sqlite3.connect(ROOT / "data" / "data_v4.db")
    try:
        rows = db.execute("select user_id, content from conversations").fetchall()
    finally:
        db.close()
    out = []
    for user_id, content in rows:
        if str(group) in user_id:
            out += json.loads(content or "[]") or []
    return out


def seen(needle, messages):
    return any(needle in flat(m.get("content")) for m in messages)


async def cmd(line, *needles):
    sent, asked = await say(line)
    body = text(sent)
    print(f"  {line}\n    => " + body.replace("\n", "\n       "))
    ok(f"{line} 回答正确，不经过模型", all(n in body for n in needles) and not asked, body)
    return sent


class Bot:
    def __init__(self, env, qq_port):
        self.env, self.qq_port, self.proc, self.mark = env, qq_port, None, 0

    def log(self):
        return LOG.read_text(encoding="utf-8", errors="replace") if LOG.exists() else ""

    async def start(self, keys, ready):
        (ROOT / "data" / "config").mkdir(parents=True, exist_ok=True)
        (ROOT / "data" / "config" / "astrbot_plugin_gengwiki_config.json").write_text(json.dumps({"api_keys": keys, "join_chance_named": 1, "join_chance_any": 1, "join_cooldown": 0}, ensure_ascii=False), encoding="utf-8")
        self.mark = len(self.log())
        with open(LOG, "ab") as log:
            self.proc = subprocess.Popen([sys.executable, "-c", BOOT], cwd=ROOT, env=self.env, stdout=log, stderr=subprocess.STDOUT)
        end = time.time() + 240
        while time.time() < end and ready not in self.log()[self.mark:] and self.proc.poll() is None:
            await asyncio.sleep(0.5)
        ok(f"启动后日志写出 {ready}", ready in self.log()[self.mark:], self.log()[-1500:])
        if ready not in self.log()[self.mark:]:
            self.stop()
            sys.exit(f"AstrBot did not get ready, see {LOG}")
        await qq.connect(self.qq_port)
        await asyncio.sleep(1)

    def stop(self):
        self.proc.terminate()
        self.proc.wait()

    def key_lines(self):
        return "\n  ".join(line for line in self.log()[self.mark:].splitlines() if "梗Wiki 第" in line)[-300:]


async def main():
    shutil.copytree(PLUGIN, ROOT / "data" / "plugins" / PLUGIN.name, ignore=shutil.ignore_patterns(".git", "__pycache__", ".venv", "tests"))
    (ROOT / ".astrbot").touch()

    fake = Fake()
    wiki, stop_wiki = serve(fake)
    llm_port, qq_port = free_port(), free_port()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", llm_port).start()

    provider = {"id": "stub", "provider": "openai", "type": "openai_chat_completion", "provider_type": "chat_completion", "enable": True, "key": ["stub"], "api_base": f"http://127.0.0.1:{llm_port}/v1", "timeout": 30, "proxy": "", "custom_headers": {}, "model": "stub-model", "model_config": {"model": "stub-model"}, "modalities": ["text", "image", "tool_use"]}
    config = {
        "config_version": 5,
        "provider": [provider],
        # Streaming on, because that is the harder case for a join that may decide to say nothing
        "provider_settings": {"enable": True, "streaming_response": True},
        "platform_settings": {"enable_id_white_list": False, "rate_limit": {"time": 60, "count": 100000, "strategy": "stall"}},
        "dashboard": {"enable": False},
        "platform": [{"id": "qq", "type": "aiocqhttp", "enable": True, "ws_reverse_host": "127.0.0.1", "ws_reverse_port": qq_port, "ws_reverse_token": ""}],
    }
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "cmd_config.json").write_text(json.dumps(config, ensure_ascii=False), encoding="utf-8")

    env = {**os.environ, "ASTRBOT_ROOT": str(ROOT), "ASTRBOT_DISABLE_METRICS": "1", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "GENGWIKI_API_URL": wiki, "GENGWIKI_SITE_URL": wiki, "GENGWIKI_HOME": str(ROOT / "wiki-home"), "NO_PROXY": "127.0.0.1,localhost"}
    env.pop("GENGWIKI_API_KEY", None)
    bot = Bot(env, qq_port)

    print("== 启动")
    await bot.start([KEY_A], "梗Wiki 第 1 个 Key")
    ok("日志写出人设和内容", "人设 毒舌老哥" in bot.log() and "表情 1 张" in bot.log(), bot.log()[-800:])
    ok("日志里没有 Key", KEY_A not in bot.log())

    print("== 群里第一条消息就是插话")
    sent, asked = await say("躺平 SKIPME", group=FRESH, expect="llm")
    ok("模型决定不开口时什么也不发", asked and not sent, sent)
    ok("这个判断没有写进记录", not seen("SKIPME", history(FRESH)), history(FRESH))
    sent, asked = await say("躺平 SPEAKME", group=FRESH)
    ok("模型开口时发到群里", text(sent) == SPOKEN, sent)

    print("== @ 机器人的普通回复")
    sent, asked = await say("你好", at=True)
    ok("回复发出来", NORMAL in text(sent), sent)
    ok("这次是流式，下面的插话要在流式下关掉它", asked and asked[0].get("stream"), asked and asked[0].get("stream"))
    ok("系统提示词里有人设", asked and "毒舌老哥" in system(asked[0]))
    base = history(G)
    ok("写进了记录", seen("你好", base) and seen(NORMAL, base), base)

    print("== 插话，模型决定不开口")
    for line in ["躺平 SKIPME 一", "躺平 SKIPME 二", "摆烂 SKIPME 三", "今天天气不错 SKIPME 四"]:
        sent, asked = await say(line, expect="llm")
        good = asked and not sent and JOIN in last_user(asked[0]) and not asked[0].get("stream")
        ok(f"{line} 带插话规则，关掉流式，什么也不发", good, (sent, asked and asked[0].get("stream"), asked and last_user(asked[0])[:200]))
        ok(f"{line} 没有进记录", history(G) == base, history(G)[-2:])

    print("== 下一次回复的上下文")
    sent, asked = await say("再说一句", at=True)
    dump = json.dumps(asked[0]["messages"], ensure_ascii=False) if asked else ""
    ok(f"整个请求里都没有 {SKIP}", asked and SKIP not in dump, dump[:400])
    ok("上下文里有上一轮", asked and seen(NORMAL, asked[0]["messages"]))

    print("== 插话，模型开口")
    before = len(history(G))
    sent, asked = await say("躺平 SPEAKME")
    ok("发到群里", text(sent) == SPOKEN, sent)
    ok("关掉了流式", asked and not asked[0].get("stream"))
    h = history(G)
    new = h[before:]
    ok("写进两条记录", len(new) == 2, new)
    ok("用户那条写的是谁说了什么", new and "群友 在群里说：躺平 SPEAKME" in flat(new[0]["content"]), new)
    ok("插话规则没有进记录", not seen(JOIN, h), h[-2:])
    sent, asked = await say("接着说", at=True)
    ok("下一次回复看得到这次插话", asked and seen(SPOKEN, asked[0]["messages"]))

    print("== 指令")
    await cmd("/梗帮助", "/梗 词", "/热梗", "/表情", "/梗插话", "/梗状态")
    await cmd("/梗 躺平", "躺平的释义", "使用场景 躺平的场景", f"{wiki}/m/tang-ping")
    await cmd("/梗 摆烂", "躺平的释义")
    await cmd("/梗", "发送 /梗 词")
    await cmd("/梗 不存在的词", "这个 Key 能用的梗里没有 不存在的词", f"{wiki}/memes?q=", f"{wiki}/new")
    await cmd("/热梗", "最近最热的梗", "1. 躺平", "发送 /梗 名字")
    await cmd("/热梗 网络", "网络 最热的梗", "1. 躺平")
    await cmd("/热梗 没这个标签", "没有带 没这个标签 标签的梗")
    sent = await cmd("/表情 震惊")
    ok("/表情 震惊 发出那张图", images(sent) and is_png(images(sent)[0]), sent)
    sent = await cmd("/表情")
    ok("/表情 随机发一张", images(sent) and is_png(images(sent)[0]), sent)
    await cmd("/表情 没这张", "没有找到 没这张")
    await cmd("/梗状态", "人设 毒舌老哥", "表情 1 张", "人物 1 个", "语录 1 句", "频率 玩梗 0.3，语录 0.4，表情 0.3", "插话 开", "订阅 没有")
    await cmd("/梗标签", "网络")
    await cmd("/梗订阅 网络", "网络")
    await cmd("/梗状态", "订阅 网络")
    await cmd("/梗退订 网络", "网络")
    await cmd("/语录 鲁迅", "我家门前有两棵树", "鲁迅")
    await cmd("/语录 周树人", "我家门前有两棵树")
    await cmd("/语录 杜甫", "杜甫 还没有收录语录")
    await cmd("/语录", "我家门前有两棵树")
    await cmd("/语录 没这个人", "没有找到 没这个人")
    await cmd("/语录 鲁", "没有找到 鲁，试试 鲁迅")
    await cmd("/人设", "1. 毒舌老哥")
    await cmd("/梗刷新", "已刷新")

    print("== /梗插话")
    await cmd("/梗插话", "这个会话可能主动插话")
    await cmd("/梗插话 乱写", "发送 /梗插话 开 或 /梗插话 关")
    await cmd("/梗插话 关", "这个会话不会主动插话")
    await cmd("/梗状态", "插话 关")
    sent, asked = await say("躺平 SPEAKME", expect="none")
    ok("关了就不再插话，也不调用模型", not sent and not asked, (sent, len(asked)))
    sent, asked = await say("在吗", at=True)
    ok("关了被叫到时照常回答", NORMAL in text(sent), sent)
    sent, asked = await say("躺平 SPEAKME", group=FRESH)
    ok("别的群不受影响", text(sent) == SPOKEN, sent)
    await cmd("/梗插话 开", "这个会话可能主动插话")
    sent, asked = await say("躺平 SPEAKME")
    ok("打开后又会插话", text(sent) == SPOKEN, sent)

    print("== 模型发表情")
    sent, asked = await say("来张表情 STICKERME", at=True)
    ok("把工具交给了模型", asked and any(t["function"]["name"] == "send_sticker" for t in asked[0].get("tools", [])), asked and asked[0].get("tools"))
    ok("发出那张图", images(sent) and is_png(images(sent)[0]), sent)
    ok("提示词里有表情的说明", asked and "震惊" in last_user(asked[0]), asked and last_user(asked[0])[:300])
    sent, asked = await say("躺平 来张表情 STICKERME")
    ok("插话时也能发，而且不是流式", images(sent) and is_png(images(sent)[0]) and asked and not asked[0].get("stream"), sent)

    print("== 模型的查询工具")
    for name, args, needle in [
        ("search_memes", {"q": "躺平"}, "躺平的释义"),
        ("get_meme", {"slug": "tang-ping"}, "躺平的正文"),
        ("get_meme", {"slug": "没有这个"}, "not_found"),
        ("trending_memes", {}, "躺平"),
        ("trending_memes", {"tag": "网络"}, "躺平"),
        ("search_people", {"q": "鲁迅"}, "鲁迅"),
        ("get_person", {"name": "鲁迅"}, "我家门前有两棵树"),
        ("get_person", {"name": "没这个人"}, "not_found"),
        ("send_sticker", {"caption": "没这张"}, "没有这张表情"),
    ]:
        # 表情 asks for a sticker, so this reply's sticker roll cannot turn send_sticker away
        sent, asked = await say(f"来张表情 CALLTOOL {name} {json.dumps(args, ensure_ascii=False)}", at=True)
        print(f"  {name} {args}\n    => " + text(sent)[:160].replace("\n", " "))
        ok(f"{name} {args} 把结果交给模型", needle in text(sent) and len(asked) == 2, (text(sent)[:300], len(asked)))

    print("== 梗Wiki 连不上")
    fake.status = 503
    await cmd("/梗刷新", "暂时连不上梗Wiki")
    sent, asked = await say("在吗", at=True)
    ok("连不上时照常回复", NORMAL in text(sent), sent)
    ok("连不上时人设还在", asked and "毒舌老哥" in system(asked[0]), asked and system(asked[0])[:300])
    sent, asked = await say("躺平 SPEAKME")
    ok("连不上时插话不会崩", text(sent) == SPOKEN, sent)
    await cmd("/梗状态", "旧数据")
    await cmd("/梗 躺平", "躺平的释义")
    fake.status = None
    await cmd("/梗刷新", "已刷新")
    sent = await cmd("/梗状态", "人设 毒舌老哥")
    ok("恢复后不再说是旧数据", "旧数据" not in text(sent), sent)

    print("== Key 缺权限")
    scopes = fake.keys[KEY_A]["scopes"]
    fake.keys[KEY_A]["scopes"] = ["memes:read"]
    await cmd("/梗刷新", "已刷新")
    await cmd("/表情", "这个 Key 没有勾选 stickers:read")
    await cmd("/语录", "这个 Key 没有勾选 people:read")
    await cmd("/热梗", "这个 Key 没有勾选 trending:read")
    await cmd("/梗状态", "这个 Key 没有勾选", "stickers:read", f"{wiki}/me")
    sent, asked = await say("在吗", at=True)
    ok("缺权限时照常回复", NORMAL in text(sent), sent)
    fake.keys[KEY_A]["scopes"] = scopes
    await cmd("/梗刷新", "已刷新")

    print("== 重启后")
    await say("/梗插话 关", group=QUIET)
    bot.stop()
    await bot.start([KEY_A], "梗Wiki 第 1 个 Key")
    sent, asked = await say("躺平 SPEAKME", group=QUIET, expect="none")
    ok("关掉插话的群重启后还是关的", not sent and not asked, (sent, len(asked)))
    sent, asked = await say("躺平 SPEAKME", group=FRESH)
    ok("别的群重启后照常插话", text(sent) == SPOKEN, sent)

    print("== 多个 Key 切换人设")
    bot.stop()
    await bot.start([KEY_A, KEY_B], "梗Wiki 第 2 个 Key")
    await cmd("/人设", "1. 毒舌老哥", "2. 温柔姐姐")
    await cmd("/人设 2", "温柔姐姐")
    await cmd("/梗状态", "人设 温柔姐姐")
    await cmd("/表情", "这个 Key 没有关联表情包")
    await cmd("/语录", "这个 Key 关联的人物还没有语录")
    sent, asked = await say("在吗", at=True)
    ok("切换后用新人设", asked and "温柔姐姐" in system(asked[0]) and "毒舌老哥" not in system(asked[0]), asked and system(asked[0])[:300])
    sent, asked = await say("在吗", at=True, group=FRESH)
    ok("别的群还是原来的人设", asked and "毒舌老哥" in system(asked[0]))
    await cmd("/人设 毒舌老哥", "毒舌老哥")
    await cmd("/梗状态", "人设 毒舌老哥")

    print("== 刚装好，还没有填 Key")
    bot.stop()
    await bot.start([], "梗Wiki 第 1 个 Key")
    print("  " + bot.key_lines())
    await cmd("/梗状态", "Key 无效或没有配置")
    await cmd("/梗 躺平", "Key 无效或没有配置")
    await cmd("/表情", "Key 无效或没有配置")
    sent, asked = await say("在吗", at=True)
    ok("没有 Key 时照常回复", NORMAL in text(sent), sent)

    print("== Key 填错了")
    bot.stop()
    await bot.start([KEY_BAD], "梗Wiki 第 1 个 Key")
    print("  " + bot.key_lines())
    ok("日志里没有 Key", KEY_BAD not in bot.log())
    await cmd("/梗状态", "Key 无效或没有配置")
    sent, asked = await say("在吗", at=True)
    ok("Key 填错时照常回复", NORMAL in text(sent), sent)

    bot.stop()
    await qq.close()
    await runner.cleanup()
    stop_wiki()

    log = bot.log().splitlines()
    traces = [i for i, line in enumerate(log) if "Traceback" in line]
    ok("日志里没有异常堆栈", not traces)
    for i in traces[:3]:
        print("\n".join(log[i:i + 25]))
    bad = [n for n, good in results if not good]
    print(f"\n{len(results) - len(bad)}/{len(results)} passed, the model was called {len(llm)} times, all on the local stub")
    for n in bad:
        print("  FAIL", n)
    if bad:
        print("kept for reading", LOG)
        sys.exit(1)
    shutil.rmtree(ROOT, ignore_errors=True)


asyncio.run(main())
