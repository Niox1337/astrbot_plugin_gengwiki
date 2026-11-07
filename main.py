import asyncio
import hashlib
import io
import random
import re
import time
from urllib.parse import quote

from PIL import Image as Picture

from gengwiki import AuthError, ForbiddenError, GengWiki, GengWikiError, NotFoundError, Status

import astrbot.api.message_components as Comp
from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.star.filter.command import GreedyStr

# The model answers this when it has nothing good to say, and the reply is dropped. It sometimes leaves out the brackets.
SKIP = "不接话"
# Told to the model for this turn only, so the group's history keeps just what was said
JOIN_RULE = f"没有人叫你，默认不插话，只回复 [{SKIP}]。只有某个梗和这句话非常贴切时，才像群友一样用这个梗插一句，一两句话就够。"
UNREADABLE = "读取失败"
# The options of quote_mode in _conf_schema.json
QUOTE_MODES = {"不使用": "off", "引用": "cite", "直接使用": "use", "引用和使用": "both"}
# AstrBot ends the turn when a tool sends the image itself, and it sends text written alongside a tool call first.
SEND_RULE = "发送时调用 send_sticker，传入表情列表里的一条说明。要说的话必须写在同一条回复里，和这次调用一起发出，调用之后这一轮就结束了，不能再说话。"
HELP = """梗Wiki 指令
/梗 词  查梗
/热梗 标签  看热门榜，标签可以不写
/表情 词  发一张表情，词可以不写
/语录 名字  发一句语录，名字可以不写
/人设 序号或名字  查看或切换人设
/梗标签  看全部标签
/梗订阅 标签  订阅标签，不写时列出已订阅的
/梗退订 标签  退订标签
/梗插话 开或关  这个会话要不要主动插话
/梗状态  看这个 Key 能用什么，哪里没配好
/梗刷新  马上读取梗Wiki 最新的数据
/添加梗 词、表情、人设或人物  添加自己的，不用审核
/删除梗 词、表情、人设或人物 名字  删除自己添加的"""
ADD_HELP = """添加自己的内容，只有这个 Key 能用，不用审核
/添加梗 词 名字 解释
/添加梗 表情 说明  消息里带一张图，或者回复一张图
/添加梗 人设 名字 提示词
/添加梗 人物 名字 简介｜语录｜语录"""
REMOVE_HELP = "/删除梗 词、表情、人设或人物 名字，只能删除自己添加的"
REMOVE = {"词": "remove_word", "表情": "remove_sticker", "人设": "remove_persona", "人物": "remove_person"}
# What the wiki takes for a sticker
STICKER_SIDE, STICKER_BYTES = 500, 1024 * 1024


def warm(client: GengWiki, tags=()) -> None:
    """Read what replies lean on, so the next reply does not wait for it."""
    boards = (lambda tag=tag: client.trending(tag) for tag in tags)
    for read in (client.persona, client.search_memes, client.trending, client.sticker_packs, client.linked_people, client.tags, *boards):
        try:
            read()
        except (ForbiddenError, NotFoundError):
            pass  # this key was not given that scope


def reload(client: GengWiki, tags=()) -> bool:
    """Read everything again. False if the wiki could not be reached."""
    client.refresh()
    warm(client, tags)
    return not client.stale


def shrink(raw: bytes) -> bytes:
    """Down to what the wiki takes. An image that already fits is sent as it is, so a GIF keeps moving."""
    with Picture.open(io.BytesIO(raw)) as im:
        if im.format in ("PNG", "JPEG", "GIF", "WEBP") and max(im.size) <= STICKER_SIDE and len(raw) <= STICKER_BYTES:
            return raw
        im.thumbnail((STICKER_SIDE, STICKER_SIDE))
        out = io.BytesIO()
        im.convert("RGBA").save(out, "PNG", optimize=True)
        if out.tell() > STICKER_BYTES:
            out = io.BytesIO()
            im.convert("RGB").save(out, "JPEG", quality=85)
        return out.getvalue()


def picture(event: AstrMessageEvent):
    """The image in the message, or else in the message it replies to."""
    chain = event.get_messages()
    quoted = [c for r in chain if isinstance(r, Comp.Reply) for c in r.chain or []]
    return next((c for c in [*chain, *quoted] if isinstance(c, Comp.Image)), None)


def describe(status: Status, site: str, guest: bool = False) -> str:
    """What a key can do, for /梗状态 and the log."""
    lines = [f"人设 {status.persona or '没有'}", f"梗 {status.memes} 个，表情 {status.stickers} 张，人物 {status.people} 个，语录 {status.quotes} 句"]
    if guest:
        lines.append(f"没有配置 API Key，只能用热门的 20 个梗和 10 个人物。到 {site}/me 注册账号、新建组合，把 Key 填进插件配置的 api_keys")
    if status.missing:
        lines.append(f"这个 Key 没有勾选 {'、'.join(status.missing)}，到 {site}/me 新建一个勾选它们的 Key")
    if status.stale:
        lines.append("暂时连不上梗Wiki，这是旧数据")
    return "\n".join(lines)


class GengWikiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        keys = dict.fromkeys(k.strip() for k in config.get("api_keys", []) if k.strip()) or [""]
        # No key still works, on the wiki's top 20 hot memes and 10 people
        self.guest = keys == [""]
        # A key binds one persona for life, so switching persona means switching key.
        # Hashed so a session keeps its pick when the list is reordered, and the keys stay out of the plugin's database.
        self.quotes = QUOTE_MODES.get(config.get("quote_mode", "引用和使用"), "both")
        # The config page takes any number, and the SDK refuses one outside 0 to 1
        defaults = {"meme_frequency": 0.3, "quote_frequency": 0.4, "sticker_frequency": 0.3}
        frequency = {name: max(0.0, min(1.0, float(config.get(name, default)))) for name, default in defaults.items()}
        self.meme_frequency = frequency["meme_frequency"]
        self.sticker_frequency = frequency["sticker_frequency"]
        # Off, a bot without a key plays no memes at all and only says once where to get a key
        self.guest_memes = bool(config.get("guest_memes", True))
        options = {"quotes": self.quotes, "guest_memes": self.guest_memes, **frequency}
        self.clients = {hashlib.sha256(k.encode()).hexdigest()[:16]: GengWiki(k, **options) for k in keys}
        self.chosen: dict[str, str] = {}
        self.subscribed: dict[str, list[str]] = {}
        self.last_joined: dict[str, float] = {}
        self.quiet: set[str] = set()
        self.last_read = {}
        self.warming = None

    async def initialize(self):
        self.chosen = await self.get_kv_data("chosen", {}) or {}
        self.subscribed = await self.get_kv_data("subscribed", {}) or {}
        self.quiet = set(await self.get_kv_data("quiet", []) or [])
        # In the background, a slow wiki must not hold up AstrBot's start
        self.warming = asyncio.create_task(self._warm_up())

    async def _warm_up(self):
        """Fills the caches so the first reply does not wait, and says in the log what each key can do. A bad key otherwise only shows as a bot that never jokes."""
        tags = {tag for tags in self.subscribed.values() for tag in tags}
        for i, client in enumerate(self.clients.values(), 1):
            try:
                status = await asyncio.to_thread(client.status)
                logger.info(f"梗Wiki 第 {i} 个 Key，" + describe(status, client.site_url, self.guest).replace("\n", "，"))
                await asyncio.to_thread(warm, client, tags)
            except Exception as e:  # nothing waits on this task, so a failure here has to end as a log line
                logger.warning(f"梗Wiki 第 {i} 个 Key 读取失败，请检查插件配置的 api_keys。{getattr(e, 'message', e)!r}")

    def _client(self, event: AstrMessageEvent) -> GengWiki:
        """The key this session picked, or the first one in the config."""
        picked = self.clients.get(self.chosen.get(event.unified_msg_origin))
        return picked or next(iter(self.clients.values()))

    def _call(self, event: AstrMessageEvent, tool: str, **args):
        # The SDK blocks, so it must not run on AstrBot's event loop
        return asyncio.to_thread(self._client(event).tools.call, tool, args)

    async def _quick(self, fn, *args, steady=False):
        """For the steps every message passes through. A slow or broken wiki must not hold up the chat, the thread still fills the cache.

        With steady the last answer stands in when the wiki is slow, so the bot does not fall out of character for one reply. A refused key gets none.
        """
        try:
            value = await asyncio.wait_for(asyncio.to_thread(fn, *args), 3)
        except (GengWikiError, asyncio.TimeoutError) as e:
            logger.warning(f"gengwiki skipped: {e!r}")
            return self.last_read.get(fn) if steady and isinstance(e, asyncio.TimeoutError) else None
        if steady:
            self.last_read[fn] = value
        return value

    async def _ask(self, scope, fn, *args):
        """For commands, where someone is waiting. Returns the answer, and what to say instead when there is none. A name that does not exist is the caller's to explain."""
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), 10), None
        except NotFoundError:
            raise
        except ForbiddenError:
            return None, f"这个 Key 没有勾选 {scope}"
        except AuthError:
            return None, "Key 无效或没有配置，请检查插件配置的 api_keys"
        except (GengWikiError, asyncio.TimeoutError) as e:
            logger.warning(f"gengwiki command failed: {e!r}")
            return None, "读取失败，请稍后再试"

    async def _image(self, client: GengWiki, sticker) -> str:
        """The copy on disk, so a sticker costs the site's daily image budget once and not every time it is posted. The link if that fails."""
        try:
            return str(await asyncio.wait_for(asyncio.to_thread(client.sticker_image, sticker.id), 10))
        except (GengWikiError, OSError, asyncio.TimeoutError) as e:
            logger.warning(f"gengwiki posting a sticker by link: {e!r}")
            return sticker.url

    def _may_join(self, origin: str) -> bool:
        # Speaking up is only ever for a meme, so with none to offer it would just cost a model call
        chances = self.config.get("join_chance_named", 0.8), self.config.get("join_chance_any", 0.01)
        if self.guest and not self.guest_memes:
            return False
        return self.meme_frequency > 0 and any(chance > 0 for chance in chances) and origin not in self.quiet

    async def _label(self, client: GengWiki) -> str:
        try:
            persona = await asyncio.wait_for(asyncio.to_thread(client.persona), 10)
        except ForbiddenError:
            return f"{UNREADABLE}，这个 Key 没有勾选 persona:read"
        except (GengWikiError, asyncio.TimeoutError):
            return f"{UNREADABLE}，请检查 Key 或稍后再试"
        return persona.title if persona else "无人设"

    @filter.command("人设")
    async def switch_persona(self, event: AstrMessageEvent, name: GreedyStr):
        """查看或切换梗Wiki 人设。不带参数时列出可选人设，带序号或名字时切换当前会话的人设"""
        name = name.strip()
        labels = await asyncio.gather(*map(self._label, self.clients.values()))
        if not name:
            current = self._client(event)
            rows = [f"{i}. {label}{'（当前）' if client is current else ''}" for i, (label, client) in enumerate(zip(labels, self.clients.values()), 1)]
            yield event.plain_result("\n".join(["可选人设：", *rows, "发送 /人设 序号或名字 切换"]))
            return
        index = int(name) - 1 if name.isdecimal() else next((i for i, label in enumerate(labels) if name in label), -1)
        if not 0 <= index < len(labels):
            yield event.plain_result("没有这个人设，发送 /人设 查看可选人设")
        elif labels[index].startswith(UNREADABLE):
            yield event.plain_result(labels[index])
        else:
            self.chosen[event.unified_msg_origin] = list(self.clients)[index]
            await self.put_kv_data("chosen", self.chosen)
            yield event.plain_result(f"已切换到 {labels[index]}。如果还带着之前的语气，发送 /reset 清空对话")

    @filter.command("语录")
    async def quote(self, event: AstrMessageEvent, name: GreedyStr):
        """随机发一句语录。带名字时发这个人物的，不带时从这个 Key 关联的人物里抽"""
        if self.quotes == "off":
            yield event.plain_result("语录没有开启，可以在插件配置的 quote_mode 里打开")
            return
        client = self._client(event)
        name = name.strip()
        try:
            found, problem = await self._ask("people:read", (lambda: [client.person(name)]) if name else client.linked_people)
        except NotFoundError:
            close, _ = await self._ask("people:read", client.people, name)
            names = "、".join(p.name for p in (close or [])[:3])
            yield event.plain_result(f"没有找到 {name}，试试 {names}" if names else f"没有找到 {name}")
            return
        if problem:
            yield event.plain_result(problem)
            return
        said = [(p.name, q) for p in found for q in p.quotes]
        if said:
            who, line = random.choice(said)
            yield event.plain_result(f"{line}\n\n{who}")
        else:
            yield event.plain_result(f"{name} 还没有收录语录" if name else "这个 Key 关联的人物还没有语录，发送 /语录 名字 试试")

    @filter.command("梗")
    async def look_up(self, event: AstrMessageEvent, q: GreedyStr):
        """查一个梗，马上回答，不经过模型"""
        q = q.strip()
        if not q:
            yield event.plain_result("发送 /梗 词 查梗，发送 /梗帮助 查看全部指令")
            return
        client = self._client(event)
        tags = self.subscribed.get(event.unified_msg_origin, [])
        found, problem = await self._ask("memes:read", lambda: client.find_memes(q, 4, tags=tags))
        if problem:
            yield event.plain_result(problem)
        elif found:
            top, *rest = found
            lines = [
                top.title,
                top.summary,
                f"使用场景 {top.usage_scenario}" if top.usage_scenario else "",
                f"{client.site_url}/m/{quote(top.slug)}",
                f"相关 {'、'.join(m.title for m in rest)}" if rest else "",
            ]
            yield event.plain_result("\n".join(line for line in lines if line))
        else:
            yield event.plain_result(f"这个 Key 能用的梗里没有 {q}\n搜全站 {client.site_url}/memes?q={quote(q)}\n新建词条 {client.site_url}/new")

    @filter.command("热梗")
    async def hot_memes(self, event: AstrMessageEvent, tag: GreedyStr):
        """查看最近最热的梗，带标签时看这个标签的榜"""
        tag = tag.strip()
        top, problem = await self._ask("trending:read", self._client(event).trending, tag, 10)
        if problem:
            yield event.plain_result(problem)
        elif top:
            rows = [f"{i}. {m.title}" for i, m in enumerate(top, 1)]
            yield event.plain_result("\n".join([f"{tag} 最热的梗" if tag else "最近最热的梗", *rows, "发送 /梗 名字 查看释义"]))
        else:
            yield event.plain_result(f"没有带 {tag} 标签的梗，发送 /梗标签 查看全部标签" if tag else "热门榜还是空的")

    @filter.command("表情")
    async def post_sticker(self, event: AstrMessageEvent, q: GreedyStr):
        """按说明文字发一张表情，不带参数时随机发一张"""
        if self.sticker_frequency == 0:
            yield event.plain_result("表情没有开启，可以在插件配置的 sticker_frequency 里打开")
            return
        client = self._client(event)
        q = q.strip()
        # The closest caption comes first, and no words means any of them
        found, problem = await self._ask("stickers:read", client.search_stickers, q, 1 if q else 1000)
        if problem:
            yield event.plain_result(problem)
        elif found:
            yield event.image_result(await self._image(client, random.choice(found)))
        else:
            yield event.plain_result(f"没有找到 {q}" if q else "这个 Key 没有关联表情包")

    @filter.command("梗插话")
    async def switch_join(self, event: AstrMessageEvent, state: GreedyStr):
        """开关当前会话的主动插话，不带参数时查看现在是开还是关"""
        origin = event.unified_msg_origin
        state = state.strip()
        if state not in ("", "开", "关"):
            yield event.plain_result("发送 /梗插话 开 或 /梗插话 关")
            return
        if state:
            (self.quiet.discard if state == "开" else self.quiet.add)(origin)
            await self.put_kv_data("quiet", sorted(self.quiet))
        if self._may_join(origin):
            yield event.plain_result("这个会话可能主动插话，发送 /梗插话 关 关闭")
        elif origin in self.quiet:
            yield event.plain_result("这个会话不会主动插话，发送 /梗插话 开 打开")
        else:
            yield event.plain_result("插件配置里关闭了插话，要在配置里打开")

    async def _write(self, client: GengWiki, fn, *args):
        """For /添加梗 and /删除梗. Returns the answer, and what to say instead when there is none."""
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), 15), None
        except AuthError:
            if self.guest:
                return None, f"没有配置 API Key。到 {client.site_url}/me 注册账号、新建组合，把 Key 填进插件配置的 api_keys"
            return None, "Key 无效，请检查插件配置的 api_keys"
        except ForbiddenError as e:
            return None, f"没有成功，{e.message}。Key 要勾选 self:write，旧 Key 到 {client.site_url}/me 新建一个"
        except asyncio.TimeoutError:
            return None, "梗Wiki 没有响应，请稍后再试"
        except GengWikiError as e:
            logger.warning(f"gengwiki write failed: {e!r}")
            return None, f"没有成功，{e.message}"

    @filter.command("添加梗")
    async def add_mine(self, event: AstrMessageEvent, text: GreedyStr):
        """添加自己的词、表情、人设或人物，只有这个 Key 能用，不用审核"""
        client = self._client(event)
        kind, _, rest = text.strip().partition(" ")
        rest = rest.strip()
        name, _, more = rest.partition(" ")
        more = more.strip()
        if kind == "表情" and rest:
            found = picture(event)
            if found is None:
                yield event.plain_result("消息里带一张图，或者回复一张图，再发 /添加梗 表情 说明")
                return
            try:
                with open(await found.convert_to_file_path(), "rb") as f:
                    image = await asyncio.to_thread(shrink, f.read())
            except Exception as e:  # a broken download or a file Pillow cannot read
                logger.warning(f"gengwiki cannot read the image: {e!r}")
                yield event.plain_result("读不出这张图片，换一张试试")
                return
            _, problem = await self._write(client, client.add_sticker, image, rest)
            reply = f"已添加表情 {rest}，发送 /表情 {rest} 试试"
        elif kind == "词" and more:
            _, problem = await self._write(client, client.add_word, name, more)
            reply = f"已添加 {name}，以后聊到它机器人就懂了"
        elif kind == "人设" and more:
            _, problem = await self._write(client, client.add_persona, name, more)
            reply = f"已添加人设 {name}。Key 的人设创建后不能改，到 {client.site_url}/me 新建一个组合选它，再把新 Key 填进插件配置"
        elif kind == "人物" and more:
            bio, *quotes = re.split("[|｜]", more)
            _, problem = await self._write(client, client.add_person, name, bio.strip(), [q.strip() for q in quotes if q.strip()])
            reply = f"已添加人物 {name}，这个 Key 已经关联了"
        else:
            yield event.plain_result(ADD_HELP)
            return
        yield event.plain_result(problem or reply)

    @filter.command("删除梗")
    async def remove_mine(self, event: AstrMessageEvent, text: GreedyStr):
        """删除自己添加的词、表情、人设或人物"""
        client = self._client(event)
        kind, _, name = text.strip().partition(" ")
        name = name.strip()
        if kind not in REMOVE or not name:
            yield event.plain_result(REMOVE_HELP)
            return
        n, problem = await self._write(client, getattr(client, REMOVE[kind]), name)
        yield event.plain_result(problem or (f"已删除{kind} {name}" if n else f"没有找到自己添加的{kind} {name}"))

    @filter.command("梗状态")
    async def show_status(self, event: AstrMessageEvent):
        """查看这个会话用的 Key 能用什么，哪里没配好"""
        client = self._client(event)
        status, problem = await self._ask(None, client.status)
        if problem:
            yield event.plain_result(problem)
            return
        origin = event.unified_msg_origin
        quote_rate = "关" if self.quotes == "off" else client.quote_frequency
        lines = [
            describe(status, client.site_url, self.guest),
            f"频率 玩梗 {client.meme_frequency}，语录 {quote_rate}，表情 {client.sticker_frequency}",
            f"插话 {'开' if self._may_join(origin) else '关'}",
            f"订阅 {'、'.join(self.subscribed.get(origin, [])) or '没有'}",
        ]
        yield event.plain_result("\n".join(lines))

    @filter.command("梗帮助")
    async def show_help(self, event: AstrMessageEvent):
        """列出这个插件的全部指令"""
        yield event.plain_result(HELP)

    @filter.command("梗标签")
    async def list_tags(self, event: AstrMessageEvent):
        """列出梗Wiki 的全部标签，括号里是梗的数量"""
        tags, problem = await self._ask("trending:read", self._client(event).tags)
        if problem:
            yield event.plain_result(problem)
        elif tags:
            yield event.plain_result(f"全部标签，括号里是梗的数量\n{'、'.join(f'{t.name}（{t.count}）' for t in tags)}\n发送 /梗订阅 标签 订阅")
        else:
            yield event.plain_result("还没有标签")

    @filter.command("梗订阅")
    async def subscribe_tag(self, event: AstrMessageEvent, tag: GreedyStr):
        """订阅标签，当前会话会用上这个标签最热的 5 个梗。不带参数时列出已订阅的标签"""
        tags = self.subscribed.get(event.unified_msg_origin, [])
        tag = tag.strip()
        if not tag:
            yield event.plain_result(f"已订阅 {'、'.join(tags)}\n发送 /梗退订 标签 退订" if tags else "还没有订阅标签，发送 /梗订阅 标签 订阅")
            return
        top, problem = await self._ask("trending:read", self._client(event).trending, tag)
        if problem:
            yield event.plain_result(problem)
            return
        if not top:
            yield event.plain_result(f"没有带 {tag} 标签的梗，发送 /梗标签 查看全部标签")
            return
        # Read again, another /梗订阅 may have finished during that wait
        tags = self.subscribed.get(event.unified_msg_origin, [])
        if tag not in tags:
            self.subscribed[event.unified_msg_origin] = [*tags, tag]
            await self.put_kv_data("subscribed", self.subscribed)
        yield event.plain_result(f"已订阅 {tag}，现在最热的是 {'、'.join(m.title for m in top)}")

    @filter.command("梗退订")
    async def unsubscribe_tag(self, event: AstrMessageEvent, tag: GreedyStr):
        """退订标签，当前会话不再用这个标签的梗"""
        tags = self.subscribed.get(event.unified_msg_origin, [])
        tag = tag.strip()
        if tag not in tags:
            yield event.plain_result(f"没有订阅 {tag}，发送 /梗订阅 查看已订阅的标签" if tag else "发送 /梗退订 标签 退订，发送 /梗订阅 查看已订阅的标签")
            return
        self.subscribed[event.unified_msg_origin] = [t for t in tags if t != tag]
        await self.put_kv_data("subscribed", self.subscribed)
        yield event.plain_result(f"已退订 {tag}")

    @filter.command("梗刷新")
    async def refresh_data(self, event: AstrMessageEvent):
        """重新读取梗Wiki 的全部数据，人设、梗、表情和人物的改动马上生效"""
        tags = {tag for tags in self.subscribed.values() for tag in tags}
        reads = (asyncio.to_thread(reload, client, tags) for client in self.clients.values())
        try:
            done = await asyncio.wait_for(asyncio.gather(*reads), 30)
        except (GengWikiError, asyncio.TimeoutError) as e:
            logger.warning(f"gengwiki refresh failed: {e!r}")
            yield event.plain_result("刷新失败，请检查 Key 或稍后再试")
            return
        yield event.plain_result("已刷新，现在用的是梗Wiki 最新的数据" if all(done) else "暂时连不上梗Wiki，先继续用旧数据，请稍后再试")

    @filter.on_llm_request()
    async def prepare_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """Every reply follows the session's persona, knows how to use the tools, and gets memes to pick from that it uses only where one really fits."""
        client = self._client(event)
        # A join has already decided to speak up with a meme, so that roll must not take the memes away again
        joined = True if event.get_extra("gengwiki_join") else None
        tags = self.subscribed.get(event.unified_msg_origin, [])
        persona, guide, turn = await asyncio.gather(
            self._quick(client.persona, steady=True),
            self._quick(client.tools.guide, SEND_RULE, steady=True),
            self._quick(lambda: client.turn(event.get_message_str(), memes=joined, tags=tags)),
        )
        if persona and persona.prompt:
            # AstrBot has added its own persona by now, so this one comes last and says it wins
            req.system_prompt = f"{req.system_prompt or ''}\n# 人设\n\n以下人设优先于前面的人设设定。\n\n{persona.prompt}\n"
        if guide:
            req.system_prompt = f"{req.system_prompt or ''}\n{guide}\n"
        if joined:
            event.set_extra("gengwiki_join_request", req)
            req.extra_user_content_parts.append(TextPart(text=JOIN_RULE).mark_as_temp())
        if turn:
            # Only on the first reply without a key, so whoever runs the bot learns where to get one.
            # getattr until requirements.txt asks for an SDK that has it
            notice = getattr(turn, "notice", "")
            if notice:
                await event.send(event.plain_result(notice))
            event.set_extra("gengwiki_stickers", turn.stickers)
            if turn.prompt:
                # Changes every turn, so it stays out of the system prompt and the saved history
                req.extra_user_content_parts.append(TextPart(text=turn.prompt).mark_as_temp())

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def join_in(self, event: AstrMessageEvent):
        """Now and then speaks up in a group without being called."""
        text = event.get_message_str().strip()
        origin = event.unified_msg_origin
        cooldown = self.config.get("join_cooldown", 300)
        # A message that wakes the bot gets a normal reply anyway
        if not self._may_join(origin) or event.is_at_or_wake_command or not text or time.time() - self.last_joined.get(origin, 0) < cooldown:
            return
        tags = self.subscribed.get(origin, [])
        named = await self._quick(lambda: self._client(event).match_memes(text, tags=tags))
        chance = self.config.get("join_chance_named", 0.8) if named else self.config.get("join_chance_any", 0.01)
        if random.random() >= chance:
            return
        self.last_joined[origin] = time.time()
        event.set_extra("gengwiki_join", True)
        # Whether to send is decided on the finished reply, and a stream would have sent it by then
        event.set_extra("enable_streaming", False)
        conversations = self.context.conversation_manager
        cid = await conversations.get_curr_conversation_id(origin)
        yield event.request_llm(
            prompt=f"{event.get_sender_name()} 在群里说：{text}",
            conversation=await conversations.get_conversation(origin, cid) if cid else None,
        )

    @filter.on_decorating_result()
    async def drop_skipped(self, event: AstrMessageEvent):
        """A skipped join never happened, so nothing is sent and it stays out of the group's history, which AstrBot only writes after this and only for a request with a conversation."""
        result = event.get_result()
        request = event.get_extra("gengwiki_join_request")
        if request and result and SKIP in result.get_plain_text():
            result.chain.clear()
            request.conversation = None

    @filter.llm_tool(name="search_memes")
    async def search_memes(self, event: AstrMessageEvent, q: str):
        """搜索中文网络热梗，返回释义和使用场景。遇到不认识的网络用语，或者想找适合当前场景的梗时调用。

        Args:
            q(string): 梗的关键词、别名、拼音缩写，或者一个场景，比如加班
        """
        return await self._call(event, "search_memes", q=q, tags=self.subscribed.get(event.unified_msg_origin, []))

    @filter.llm_tool(name="get_meme")
    async def get_meme(self, event: AstrMessageEvent, slug: str):
        """查看一个梗的完整解释和用法例句。

        Args:
            slug(string): search_memes 返回的 slug，也可以是梗的准确名称
        """
        return await self._call(event, "get_meme", slug=slug, tags=self.subscribed.get(event.unified_msg_origin, []))

    @filter.llm_tool(name="trending_memes")
    async def trending_memes(self, event: AstrMessageEvent, tag: str = ""):
        """查看最近流行的热梗。

        Args:
            tag(string): 分类标签，留空为全站榜
        """
        return await self._call(event, "trending_memes", tag=tag)

    @filter.llm_tool(name="search_people")
    async def search_people(self, event: AstrMessageEvent, q: str):
        """搜索收录的人物。问到某个人是谁，或者想知道一句话是谁说的时调用。

        Args:
            q(string): 名字、别名、标签，或者语录里的词
        """
        return await self._call(event, "search_people", q=q)

    @filter.llm_tool(name="get_person")
    async def get_person(self, event: AstrMessageEvent, name: str):
        """查看一个人物的完整简介、语录和提到这个人物的梗。

        Args:
            name(string): 准确的名字或别名
        """
        return await self._call(event, "get_person", name=name)

    @filter.llm_tool(name="send_sticker")
    async def send_sticker(self, event: AstrMessageEvent, caption: str):
        """把一张表情发到当前会话。

        Args:
            caption(string): 表情的说明，照抄提示词里表情列表的一条
        """
        # The roll is enforced here too, the model may remember a caption from an earlier reply
        if event.get_extra("gengwiki_stickers") is False:
            yield "这条回复不发表情"
            return
        # The model names a caption and never a link, so a prompt injection cannot make the bot post any URL
        caption = caption.strip()
        client = self._client(event)
        found = await self._quick(client.search_stickers, caption, 1) if caption else None
        if found:
            yield event.image_result(await self._image(client, found[0]))
        else:
            yield "没有这张表情，从表情列表里选一条说明再试，没有合适的就不发"

    async def terminate(self):
        if self.warming:
            self.warming.cancel()
        for client in self.clients.values():
            client.close()
