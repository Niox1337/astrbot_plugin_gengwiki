import asyncio
import random
import time

from gengwiki import GengWiki, GengWikiError

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star
from astrbot.core.agent.message import TextPart

# The model answers this when it has nothing good to say, and the reply is dropped.
SKIP = "[不接话]"


class GengWikiPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.client = GengWiki(config.get("api_key", ""))
        self.last_joined: dict[str, float] = {}

    def _call(self, name: str, **args):
        # The SDK blocks, so it must not run on AstrBot's event loop
        return asyncio.to_thread(self.client.tools.call, name, args)

    async def _quick(self, fn, *args):
        """For the steps every message passes through. A slow or broken wiki must not hold up the chat, the thread still fills the cache."""
        try:
            return await asyncio.wait_for(asyncio.to_thread(fn, *args), 3)
        except (GengWikiError, asyncio.TimeoutError) as e:
            logger.warning(f"gengwiki skipped: {e!r}")
            return None

    @filter.on_llm_request()
    async def add_memes(self, event: AstrMessageEvent, req: ProviderRequest):
        """Every reply the model writes gets memes to pick from, and it uses one only where it really fits."""
        prompt = await self._quick(self.client.meme_prompt, event.get_message_str())
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
        named = await self._quick(self.client.match_memes, text)
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
        return await self._call("search_memes", q=q)

    @filter.llm_tool(name="get_meme")
    async def get_meme(self, event: AstrMessageEvent, slug: str):
        """查看一个梗的完整解释和用法例句。

        Args:
            slug(string): search_memes 返回的 slug，也可以是梗的准确名称
        """
        return await self._call("get_meme", slug=slug)

    @filter.llm_tool(name="trending_memes")
    async def trending_memes(self, event: AstrMessageEvent, tag: str = ""):
        """查看最近流行的热梗。

        Args:
            tag(string): 分类标签，留空为全站榜
        """
        return await self._call("trending_memes", tag=tag)

    @filter.llm_tool(name="search_stickers")
    async def search_stickers(self, event: AstrMessageEvent, q: str):
        """按含义搜索表情包，结果里的 url 可以传给 send_sticker。想用表情回应时调用。

        Args:
            q(string): 想表达的情绪或内容
        """
        return await self._call("search_stickers", q=q)

    @filter.llm_tool(name="send_sticker")
    async def send_sticker(self, event: AstrMessageEvent, url: str):
        """把一张表情发到当前会话。

        Args:
            url(string): search_stickers 返回的图片链接
        """
        # Only our own images, a prompt injection must not make the bot post any URL
        if not url.startswith(f"{self.client.base_url}/v1/stickers/"):
            yield event.plain_result("只能发送梗Wiki 的表情")
            return
        yield event.image_result(url)

    async def terminate(self):
        self.client.close()
