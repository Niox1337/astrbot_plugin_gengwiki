import asyncio
import hashlib
import random
import time

from gengwiki import ForbiddenError, GengWiki, GengWikiError, NotFoundError

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.star.filter.command import GreedyStr

# The model answers this when it has nothing good to say, and the reply is dropped.
SKIP = "[不接话]"
UNREADABLE = "读取失败"
# The options of quote_mode in _conf_schema.json
QUOTE_MODES = {"不使用": "off", "引用": "cite", "直接使用": "use", "引用和使用": "both"}
# AstrBot ends the turn when a tool sends the image itself, and it sends text written alongside a tool call first.
SEND_RULE = "发送时调用 send_sticker，传入表情列表里的一条说明。要说的话必须写在同一条回复里，和这次调用一起发出，调用之后这一轮就结束了，不能再说话。"


def reload(client: GengWiki, tags=()) -> bool:
    """Read again what replies lean on, so the next reply does not wait for it. False if the wiki could not be reached."""
    client.refresh()
    boards = (lambda tag=tag: client.trending(tag) for tag in tags)
    for read in (client.persona, client.search_memes, client.trending, client.sticker_packs, client.linked_people, client.tags, *boards):
        try:
            read()
        except (ForbiddenError, NotFoundError):
            pass  # this key was not given that scope
    return not client.stale


class GengWikiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        keys = dict.fromkeys(k.strip() for k in config.get("api_keys", []) if k.strip()) or [""]
        # A key binds one persona for life, so switching persona means switching key.
        # Hashed so a session keeps its pick when the list is reordered, and the keys stay out of the plugin's database.
        self.quotes = QUOTE_MODES.get(config.get("quote_mode", "引用"), "cite")
        # The config page takes any number, and the SDK refuses one outside 0 to 1
        defaults = {"meme_frequency": 0.2, "quote_frequency": 1.0, "sticker_frequency": 1.0}
        frequency = {name: max(0.0, min(1.0, float(config.get(name, default)))) for name, default in defaults.items()}
        self.meme_frequency = frequency["meme_frequency"]
        options = {"quotes": self.quotes, **frequency}
        self.clients = {hashlib.sha256(k.encode()).hexdigest()[:16]: GengWiki(k, **options) for k in keys}
        self.chosen: dict[str, str] = {}
        self.subscribed: dict[str, list[str]] = {}
        self.last_joined: dict[str, float] = {}

    async def initialize(self):
        self.chosen = await self.get_kv_data("chosen", {}) or {}
        self.subscribed = await self.get_kv_data("subscribed", {}) or {}

    def _client(self, event: AstrMessageEvent) -> GengWiki:
        """The key this session picked, or the first one in the config."""
        picked = self.clients.get(self.chosen.get(event.unified_msg_origin))
        return picked or next(iter(self.clients.values()))

    def _call(self, event: AstrMessageEvent, tool: str, **args):
        # The SDK blocks, so it must not run on AstrBot's event loop
        return asyncio.to_thread(self._client(event).tools.call, tool, args)

    async def _quick(self, fn, *args):
        """For the steps every message passes through. A slow or broken wiki must not hold up the chat, the thread still fills the cache."""
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), 3)
        except (GengWikiError, asyncio.TimeoutError) as e:
            logger.warning(f"gengwiki skipped: {e!r}")
            return None

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
            found = [await asyncio.wait_for(asyncio.to_thread(client.person, name), 10)] if name else await asyncio.wait_for(asyncio.to_thread(client.linked_people), 10)
        except NotFoundError:
            yield event.plain_result(f"没有找到 {name}")
            return
        except ForbiddenError:
            yield event.plain_result("这个 Key 没有勾选 people:read")
            return
        except (GengWikiError, asyncio.TimeoutError):
            yield event.plain_result("读取失败，请稍后再试")
            return
        said = [(p.name, q) for p in found for q in p.quotes]
        if said:
            who, line = random.choice(said)
            yield event.plain_result(f"{line}\n\n{who}")
        else:
            yield event.plain_result(f"{name} 还没有收录语录" if name else "这个 Key 关联的人物还没有语录，发送 /语录 名字 试试")

    @filter.command("梗标签")
    async def list_tags(self, event: AstrMessageEvent):
        """列出梗Wiki 的全部标签，括号里是梗的数量"""
        try:
            tags = await asyncio.wait_for(asyncio.to_thread(self._client(event).tags), 10)
        except ForbiddenError:
            yield event.plain_result("这个 Key 没有勾选 trending:read")
            return
        except (GengWikiError, asyncio.TimeoutError):
            yield event.plain_result("读取失败，请稍后再试")
            return
        if tags:
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
        try:
            top = await asyncio.wait_for(asyncio.to_thread(self._client(event).trending, tag), 10)
        except ForbiddenError:
            yield event.plain_result("这个 Key 没有勾选 trending:read")
            return
        except (GengWikiError, asyncio.TimeoutError):
            yield event.plain_result("读取失败，请稍后再试")
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
            self._quick(client.persona), self._quick(client.tools.guide, SEND_RULE), self._quick(lambda: client.turn(event.get_message_str(), memes=joined, tags=tags))
        )
        if persona and persona.prompt:
            # AstrBot has added its own persona by now, so this one comes last and says it wins
            req.system_prompt = f"{req.system_prompt or ''}\n# 梗Wiki 人设\n\n以下人设优先于前面的人设设定。\n\n{persona.prompt}\n"
        if guide:
            req.system_prompt = f"{req.system_prompt or ''}\n{guide}\n"
        if turn:
            event.set_extra("gengwiki_stickers", turn.stickers)
            if turn.prompt:
                # Changes every turn, so it stays out of the system prompt and the saved history
                req.extra_user_content_parts.append(TextPart(text=turn.prompt).mark_as_temp())

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def join_in(self, event: AstrMessageEvent):
        """Now and then speaks up in a group without being called."""
        # Speaking up is only ever for a meme, so with none to offer it would just cost a model call
        if self.meme_frequency == 0:
            return
        text = event.get_message_str().strip()
        origin = event.unified_msg_origin
        cooldown = self.config.get("join_cooldown", 300)
        # A message that wakes the bot gets a normal reply anyway
        if event.is_at_or_wake_command or not text or time.time() - self.last_joined.get(origin, 0) < cooldown:
            return
        tags = self.subscribed.get(origin, [])
        named = await self._quick(lambda: self._client(event).match_memes(text, tags=tags))
        chance = self.config.get("join_chance_named", 0.5) if named else self.config.get("join_chance_any", 0.02)
        if random.random() >= chance:
            return
        self.last_joined[origin] = time.time()
        event.set_extra("gengwiki_join", True)
        conversations = self.context.conversation_manager
        cid = await conversations.get_curr_conversation_id(origin)
        yield event.request_llm(
            prompt=(
                f"{event.get_sender_name()} 在群里说：{text}\n\n"
                f"没有人叫你，默认不插话，只回复 {SKIP}。"
                "只有某个梗和这句话非常贴切时，才像群友一样用这个梗插一句，一两句话就够。"
            ),
            conversation=await conversations.get_conversation(origin, cid) if cid else None,
        )

    @filter.on_decorating_result()
    async def drop_skipped(self, event: AstrMessageEvent):
        result = event.get_result()
        if event.get_extra("gengwiki_join") and result and SKIP in result.get_plain_text():
            result.chain.clear()

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
        """搜索梗Wiki 收录的人物。问到某个人是谁，或者想知道一句话是谁说的时调用。

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
        found = await self._quick(self._client(event).search_stickers, caption, 1) if caption else None
        if found:
            yield event.image_result(found[0].url)
        else:
            yield "没有这张表情，从表情列表里选一条说明再试，没有合适的就不发"

    async def terminate(self):
        for client in self.clients.values():
            client.close()
