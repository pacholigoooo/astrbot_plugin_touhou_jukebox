"""网易云单曲分享链接与公开曲目信息。"""

import asyncio
import html
import re
from urllib.parse import parse_qs, urljoin, urlsplit

import aiohttp

_LINK = re.compile(
    r"""(?:https?://|(?<![a-z0-9_./@:%-])(?=(?:(?:y\.)?music\.163\.com|163cn\.tv)/))"""
    r"""[^\s<>"'《》【】「」。，、；！？（）()\[\]]+""",
    re.IGNORECASE,
)
_MUSIC_HOSTS = {"music.163.com", "y.music.163.com"}
_SONG_PATH = re.compile(r"/(?:(?:m|f)/)?song(?:/([1-9][0-9]{0,18}))?/?")
_ID = re.compile(r"[1-9][0-9]{0,18}")


def looks_like_share(text: str) -> bool:
    return _LINK.search(text) is not None


def song_url(song_id: str) -> str:
    if not _ID.fullmatch(song_id):
        raise ValueError("请输入网易云音乐的数字单曲 ID。")
    return f"https://music.163.com/song?id={song_id}"


def _destination(url: str) -> tuple[str, str]:
    """返回单曲 ID 或可安全请求的规范短链。"""
    try:
        parts = urlsplit(url)
    except ValueError:
        raise ValueError("请复制网易云音乐的单曲分享链接。") from None
    host = parts.netloc.lower()
    if parts.scheme not in {"https", "http"} or host not in _MUSIC_HOSTS | {"163cn.tv"}:
        raise ValueError("请使用网易云音乐的单曲分享链接。")
    if host == "163cn.tv":
        if not re.fullmatch(r"/[A-Za-z0-9_-]{1,64}/?", parts.path):
            raise ValueError("网易云短链缺少有效分享码，请重新复制。")
        return "short", "https://163cn.tv" + parts.path.rstrip("/")
    if parts.path in {"", "/"} and re.match(r"^/[^/]", parts.fragment):
        parts = urlsplit(parts.fragment)
    match = _SONG_PATH.fullmatch(parts.path)
    if not match:
        raise ValueError("请在网易云打开一首歌，点「分享 → 复制链接」。")
    ids = parse_qs(parts.query, keep_blank_values=True).get("id", [])
    if match[1]:
        ids.append(match[1])
    if not ids or len(set(ids)) != 1 or not _ID.fullmatch(ids[0]):
        raise ValueError("分享链接里的单曲 ID 有误，请重新复制。")
    return "song", ids[0]


async def resolve_share(text: str) -> str:
    """从数字 ID、单曲链接或整段分享文案中取得单曲 ID。"""
    text = text.strip()
    if _ID.fullmatch(text):
        return text
    link = _LINK.search(text)
    if not link:
        raise ValueError("请粘贴网易云音乐单曲分享链接或数字 ID。")
    url = html.unescape(link[0]).rstrip(".,;!")
    if not url.lower().startswith(("http://", "https://")):
        url = "https://" + url
    kind, value = _destination(url)
    if kind == "song":
        return value

    try:
        async with asyncio.timeout(6), aiohttp.ClientSession() as session:
            for _ in range(3):
                async with session.get(value, allow_redirects=False) as response:
                    location = response.headers.get("Location")
                    if response.status not in {301, 302, 303, 307, 308} or not location:
                        break
                    kind, value = _destination(urljoin(value, location))
                    if kind == "song":
                        return value
    except (aiohttp.ClientError, TimeoutError):
        raise ValueError(
            "暂时连不上网易云短链服务。请稍后重试，或粘贴单曲长链接。"
        ) from None
    raise ValueError("网易云短链已失效或跳转异常，请重新分享这首歌。")


def _single_line(value: object) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(_LINK.sub("", value).split())[:150]


async def song_info(song_id: str) -> tuple[str, str] | None:
    """读取网易云的曲名和歌手；临时网络故障交由播放端处理。"""
    if not _ID.fullmatch(song_id):
        raise ValueError("请输入网易云音乐的数字单曲 ID。")
    try:
        async with (
            asyncio.timeout(4),
            aiohttp.ClientSession() as session,
            session.get(
                "https://music.163.com/api/song/detail",
                params={"ids": f"[{song_id}]"},
                allow_redirects=False,
            ) as response,
        ):
            if response.status != 200:
                return None
            data = await response.json(content_type=None)
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("code") != 200:
        return None
    songs = data.get("songs")
    if songs == []:
        raise ValueError("网易云未找到这首歌，请检查单曲链接或 ID。")
    if not isinstance(songs, list):
        return None
    for song in songs:
        if not isinstance(song, dict) or str(song.get("id")) != song_id:
            continue
        title = _single_line(song.get("name"))
        artists = song.get("artists")
        if title and isinstance(artists, list):
            names = [
                _single_line(artist.get("name"))
                for artist in artists
                if isinstance(artist, dict)
            ]
            return title, " / ".join(filter(None, names))[:150]
    return None
