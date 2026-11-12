import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Music, Plain, Record
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.star.filter.command import GreedyStr

from .catalog import Jukebox, Track, matches
from .netease import looks_like_share, resolve_share, song_info, song_url
from .preview import preview_clip

QQ_OFFICIAL = {"qq_official", "qq_official_webhook"}
HELP = """来首东方
/来首东方　随机推荐，也可接社团或曲名
/来首原曲、/来首电音、/来首幽闭星光
/来首网易云　从有网易云版本的曲目中选一首
/东方播放 坏苹果　也可粘贴网易云分享链接或单曲 ID
/东方试听 坏苹果　发一段约 20 秒的试听
/来首东方 曲库　查看收录曲目数量和社团"""


class NetEaseCard(Music):
    # AstrBot 4.x 的 Music 默认序列化会漏掉 OneBot 要求的 data.type。
    def toDict(self):
        return {"type": "music", "data": {"type": "163", "id": str(self.id)}}


class TelegramPreview(Record):
    async def convert_to_file_path(self):
        # Telegram 可直接播放 MP3，保留格式，同时沿用 AstrBot 的话题路由。
        return self.path


def plain(event: AstrMessageEvent, text: str):
    return event.plain_result(text).use_markdown(False).use_t2i(False)


def room_id(event: AstrMessageEvent) -> str:
    group = event.get_group_id()
    return json.dumps(
        [
            event.get_platform_id(),
            "group" if group else "private",
            group or event.get_sender_id() or event.get_session_id(),
        ]
    )


class TouhouJukebox(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config if config is not None else {}

    async def initialize(self):
        database = (
            StarTools.get_data_dir("astrbot_plugin_touhou_jukebox") / "plays.sqlite3"
        )
        self.library = await asyncio.to_thread(
            Jukebox, Path(__file__).with_name("data"), database
        )
        self.by_id = {
            track.netease_id: track for track in self.library.tracks if track.netease_id
        }

    def link_allowed(self, event, url: str) -> bool:
        if event.get_platform_name() not in QQ_OFFICIAL:
            return True
        domains = self.config.get("qq_link_domains", [])
        return urlsplit(url).hostname in {domain.strip().lower() for domain in domains}

    async def play_netease(
        self, event, song_id: str, track: Track | None, *, preview: bool = False
    ):
        platform = event.get_platform_name()
        url = song_url(song_id)
        link = self.link_allowed(event, url)
        if track:
            text = track.message(link=link, url=url)
            title = track.title
        else:
            info = await song_info(song_id)
            title = info[0] if info else f"网易云单曲 {song_id}"
            text = " · ".join(part for part in info if part) if info else title
            if link:
                text += f"\n网易云音乐  {url}"
            elif info:
                text += f"\n网易云音乐搜：{' '.join(info).strip()}"
            else:
                text += "\n可打开原分享收听。"

        if preview:
            return await self.send_preview(event, song_id, title, text)
        if platform == "aiocqhttp" and self.config.get("onebot_music_card", True):
            from aiocqhttp.exceptions import ActionFailed

            try:
                await event.send(event.chain_result([NetEaseCard(id=int(song_id))]))
            except ActionFailed as exc:
                logger.info("OneBot 拒绝音乐卡片，改发单曲链接：%s", exc.retcode)
            else:
                return None
        if platform in QQ_OFFICIAL:
            text += f"\n群聊或私聊试听：/东方试听 {song_id}"
        return plain(event, text)

    async def send_preview(self, event, song_id: str, title: str, fallback: str):
        platform = event.get_platform_name()
        official = platform in QQ_OFFICIAL
        raw = getattr(event.message_obj, "raw_message", None)
        if (
            official
            and (getattr(raw, "guild_id", None) or getattr(raw, "channel_id", None))
        ) or platform not in (QQ_OFFICIAL | {"aiocqhttp", "telegram"}):
            return plain(event, f"当前会话请从歌曲页收听。\n{fallback}")
        try:
            async with preview_clip(
                song_id, qq_silk=official, telegram_mp3=platform == "telegram"
            ) as clip:
                if platform == "telegram":
                    record = TelegramPreview(
                        file=clip.as_uri(), path=str(clip), text=title
                    )
                else:
                    record = Record.fromFileSystem(str(clip))
                # QQ 的语音和曲目信息共用一次被动回复。
                chain = (
                    [Plain(f"{fallback}\n约 20 秒试听"), record]
                    if official
                    else [record]
                )
                await event.send(event.chain_result(chain))
        except ValueError as exc:
            return plain(event, f"{exc}\n{fallback}")
        except Exception as exc:
            # 只在网关明确拒绝语音时改发文字。超时可能已实际送达。
            if platform == "aiocqhttp":
                from aiocqhttp.exceptions import ActionFailed

                if isinstance(exc, ActionFailed):
                    return plain(event, f"当前 QQ 协议端未接收语音。\n{fallback}")
            if official and any(code in str(exc) for code in ("304082", "304083")):
                return plain(event, f"QQ 未能处理这段试听。\n{fallback}")
            raise
        return None

    async def selection(
        self,
        event,
        query: str,
        *,
        category: str = "",
        netease_only: bool = False,
        preview: bool = False,
    ):
        event.should_call_llm(False)
        query = query.strip()
        if query == "帮助":
            return plain(event, HELP)
        if query == "曲库":
            tracks = self.library.tracks
            originals = sum(track.kind == "original" for track in tracks)
            circles = sorted(
                {track.artist for track in tracks if track.kind == "arrange"}
            )
            return plain(
                event,
                f"东方曲库 · {len(tracks)} 首，其中 {len(self.by_id)} 首可用网易云点播"
                f"\n原曲 {originals} 首 · 同人 {len(tracks) - originals} 首"
                f"\n社团与作者：{'、'.join(circles)}",
            )
        try:
            if looks_like_share(query) or (
                netease_only and query.isascii() and query.isdigit()
            ):
                song_id = await resolve_share(query)
                track = self.by_id.get(song_id)
                if category and (not track or not matches(track, [category])):
                    return plain(
                        event,
                        f"这首歌未收录在「{category}」分类里。可直接发 /东方播放 {song_id}。",
                    )
                if track:
                    await asyncio.to_thread(
                        self.library.record_request, room_id(event), track
                    )
                return await self.play_netease(event, song_id, track, preview=preview)
            track = await asyncio.to_thread(
                self.library.pick,
                room_id(event),
                f"{category} {query}".strip(),
                netease_only=netease_only,
            )
            if track is None:
                hint = (
                    "当前网易云曲库里没找到。可以粘贴这首歌的网易云分享链接，或发 /来首东方 加曲名。"
                    if netease_only
                    else "曲库里没找到。可以缩短曲名，或发 /来首东方 曲库 查看社团。"
                )
                return plain(event, hint)
            if track.netease_id:
                use_netease = (
                    netease_only
                    or self.link_allowed(event, song_url(track.netease_id))
                    or track.listen_url == song_url(track.netease_id)
                )
                if use_netease:
                    return await self.play_netease(
                        event, track.netease_id, track, preview=preview
                    )
            link = self.link_allowed(event, track.listen_url)
            text = track.message(link=link)
            if track.netease_id and not link:
                text += f"\n群聊或私聊试听：/东方试听 {track.netease_id}"
            return plain(event, text)
        except ValueError as exc:
            return plain(event, str(exc))

    @filter.command("来首东方", alias={"來首東方"})
    async def play(self, event: AstrMessageEvent, query: GreedyStr):
        """推荐一首东方音乐，也可以粘贴网易云分享。"""
        return await self.selection(event, query)

    @filter.command("来首原曲", alias={"來首原曲"})
    async def original(self, event: AstrMessageEvent, query: GreedyStr):
        """从 ZUN 的游戏原曲中推荐一首。"""
        return await self.selection(event, query, category="原曲")

    @filter.command("来首电音", alias={"來首電音"})
    async def electronic(self, event: AstrMessageEvent, query: GreedyStr):
        """从电子编曲中推荐一首。"""
        return await self.selection(event, query, category="电音")

    @filter.command("来首幽闭星光", alias={"來首幽閉星光"})
    async def yuuhei(self, event: AstrMessageEvent, query: GreedyStr):
        """推荐一首幽闭星光的东方同人曲。"""
        return await self.selection(event, query, category="幽闭星光")

    @filter.command("东方播放", alias={"东方点歌", "来首网易云", "東方播放"})
    async def play_song(self, event: AstrMessageEvent, query: GreedyStr):
        """点播网易云单曲，也可按曲库中的曲名或社团选歌。"""
        return await self.selection(event, query, netease_only=True)

    @filter.command("东方试听", alias={"東方試聽"})
    async def preview_song(self, event: AstrMessageEvent, query: GreedyStr):
        """发送所选网易云歌曲约 20 秒的语音试听。"""
        return await self.selection(event, query, netease_only=True, preview=True)
