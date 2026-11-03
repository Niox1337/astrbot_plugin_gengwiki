import asyncio
import hashlib
import random
import time

from gengwiki import ForbiddenError, GengWiki, GengWikiError

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart
from astrbot.core.star.filter.command import GreedyStr

# The model answers this when it has nothing good to say, and the reply is dropped.
SKIP = "[不接话]"
UNREADABLE = "读取失败"
# AstrBot ends the turn when a tool sends the image itself, and it sends text written alongside a tool call first.
SEND_RULE = "发送时调用 send_sticker，传入下面的一条说明。要说的话必须写在同一条回复里，和这次调用一起发出，调用之后这一轮就结束了，不能再说话。"


class GengWikiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        keys = dict.fromkeys(k.strip() for k in config.get("api_keys", []) if k.strip()) or [""]
        # A key binds one persona for life, so switching persona means switching key.
        # Hashed so a session keeps its pick when the list is reordered, and the keys stay out of the plugin's database.
        self.clients = {hashlib.sha256(k.encode()).hexdigest()[:16]: GengWiki(k) for k in keys}
        self.chosen: dict[str, str] = {}
        self.last_joined: dict[str, float] = {}

    async def initialize(self):
        self.chosen = await self.get_kv_data("chosen", {}) or {}

    def _client(self, event: AstrMessageEvent) -> GengWiki:
        """The key this session picked, or the first one in the config."""
        picked = self.clients.get(self.chosen.get(event.unified_msg_origin))
        return picked or next(iter(self.clients.values()))

    def _call(self, event: AstrMessageEvent, name: str, **args):
        # The SDK blocks, so it must not run on AstrBot's event loop
        return asyncio.to_thread(self._client(event).tools.call, name, args)

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

    @filter.on_llm_request()
    async def prepare_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """Every reply follows the session's persona, knows how to use the tools, and gets memes to pick from that it uses only where one really fits."""
        client = self._client(event)
        persona, guide, prompt = await asyncio.gather(
            self._quick(client.persona), self._quick(client.tools.guide, SEND_RULE), self._quick(client.meme_prompt, event.get_message_str())
        )
        if persona and persona.prompt:
            # AstrBot has added its own persona by now, so this one comes last and says it wins
            req.system_prompt = f"{req.system_prompt or ''}\n# 梗Wiki 人设\n\n以下人设优先于前面的人设设定。\n\n{persona.prompt}\n"
        if guide:
            req.system_prompt = f"{req.system_prompt or ''}\n{guide}\n"
        if prompt:
            # Changes every turn, so it stays out of the system prompt and the saved history
            req.extra_user_content_parts.append(TextPart(text=prompt).mark_as_temp())

    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE)
    async def join_in(self, event: AstrMessageEvent):
        """Now and then speaks up in a group without being called."""
        text = event.get_message_str().strip()
        origin = event.unified_msg_origin
        cooldown = self.config.get("join_cooldown", 300)
        # A message that wakes the bot gets a normal reply anyway
        if event.is_at_or_wake_command or not text or time.time() - self.last_joined.get(origin, 0) < cooldown:
            return
        named = await self._quick(self._client(event).match_memes, text)
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
        return await self._call(event, "search_memes", q=q)

    @filter.llm_tool(name="get_meme")
    async def get_meme(self, event: AstrMessageEvent, slug: str):
        """查看一个梗的完整解释和用法例句。

        Args:
            slug(string): search_memes 返回的 slug，也可以是梗的准确名称
        """
        return await self._call(event, "get_meme", slug=slug)

    @filter.llm_tool(name="trending_memes")
    async def trending_memes(self, event: AstrMessageEvent, tag: str = ""):
        """查看最近流行的热梗。

        Args:
            tag(string): 分类标签，留空为全站榜
        """
        return await self._call(event, "trending_memes", tag=tag)

    @filter.llm_tool(name="send_sticker")
    async def send_sticker(self, event: AstrMessageEvent, caption: str):
        """把一张表情发到当前会话。

        Args:
            caption(string): 表情的说明，照抄提示词里表情列表的一条
        """
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
