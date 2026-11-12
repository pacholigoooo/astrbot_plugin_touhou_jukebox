"""从网易云对外开放的 MP3 音源截取一段短试听。"""

import asyncio
import importlib
import re
import shutil
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

_BYTE_LIMIT = 1024 * 1024
_SLOTS = asyncio.Semaphore(2)


def _audio_redirect(current: str, location: str) -> str:
    try:
        address = urlsplit(urljoin(current, location))
        port = address.port
    except ValueError as exc:
        raise ValueError("网易云的试听跳转地址有误。") from exc
    host = address.hostname or ""
    if (
        address.scheme not in {"http", "https"}
        or address.username is not None
        or address.password is not None
        or port not in {None, 443}
        or not (
            host == "music.163.com"
            or re.fullmatch(r"(?:[a-z0-9-]+\.)+music\.126\.net", host)
        )
    ):
        raise ValueError("网易云未提供可用的官方试听音源。")
    # 外链服务有时给出 http CDN 地址；只通过 HTTPS 请求。
    return address._replace(scheme="https", fragment="").geturl()


async def _download_intro(song_id: str, destination: Path) -> None:
    url = f"https://music.163.com/song/media/outer/url?id={song_id}.mp3"
    headers = {
        "Referer": "https://music.163.com/",
        "Accept-Encoding": "identity",
        "Range": f"bytes=0-{_BYTE_LIMIT - 1}",
    }
    async with httpx.AsyncClient(headers=headers, timeout=6) as client:
        for _ in range(5):
            async with client.stream("GET", url, follow_redirects=False) as response:
                if response.is_redirect and (
                    location := response.headers.get("location")
                ):
                    url = _audio_redirect(url, location)
                    continue
                mime = (
                    response.headers.get("content-type", "").partition(";")[0].lower()
                )
                if response.status_code not in {200, 206} or mime not in {
                    "audio/mpeg",
                    "audio/mp3",
                    "application/octet-stream",
                }:
                    raise ValueError("这首歌暂未开放外链试听。")
                remaining = _BYTE_LIMIT
                with destination.open("wb") as output:
                    async for chunk in response.aiter_raw(chunk_size=32768):
                        written = output.write(chunk[:remaining])
                        remaining -= written
                        if remaining == 0:
                            break
                return
    raise ValueError("网易云的试听跳转次数过多，请稍后再试。")


def _find_ffmpeg() -> str:
    if binary := shutil.which("ffmpeg"):
        return binary
    try:
        from imageio_ffmpeg import get_ffmpeg_exe

        return get_ffmpeg_exe()
    except (ImportError, RuntimeError, OSError) as exc:
        raise ValueError("机器人尚未安装 FFmpeg，暂时无法生成试听。") from exc


async def _make_clip(binary: str, source: Path, clip: Path) -> None:
    # 强制 MP3 解码，只读取本地有界文件，不让媒体探测器自行打开网络。
    if clip.suffix == ".mp3":
        encoding = ["-c:a", "copy", "-f", "mp3"]
    else:
        encoding = ["-ac", "1", "-ar", "24000", "-c:a", "pcm_s16le", "-f", "wav"]
    process = await asyncio.create_subprocess_exec(
        binary,
        "-nostdin",
        "-loglevel",
        "error",
        "-protocol_whitelist",
        "file",
        "-f",
        "mp3",
        "-i",
        str(source),
        "-t",
        "20",
        "-map",
        "0:a:0",
        "-map_metadata",
        "-1",
        *encoding,
        str(clip),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        code = await process.wait()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if code or not clip.exists() or clip.stat().st_size < 4800:
        raise ValueError("网易云返回的音频无法制作试听。")


@asynccontextmanager
async def preview_clip(
    song_id: str, *, qq_silk: bool = False, telegram_mp3: bool = False
) -> AsyncIterator[Path]:
    """生成最多 20 秒的试听。调用方应在退出上下文前完成发送。"""
    if not re.fullmatch(r"[1-9][0-9]{0,18}", song_id):
        raise ValueError("网易云歌曲编号有误。")
    if _SLOTS.locked():
        raise ValueError("已有两首试听正在处理，请稍后再点。")
    async with _SLOTS:
        binary = _find_ffmpeg()
        if qq_silk:
            # 官方 QQ 适配器会把 WAV 转成 SILK；缺少编码器时它只记录日志。
            try:
                importlib.import_module("pysilk")
            except (ImportError, OSError) as exc:
                raise ValueError(
                    "QQ 语音需要安装 AstrBot 的 silk-python 依赖。"
                ) from exc
        try:
            temporary = tempfile.TemporaryDirectory(prefix="touhou-preview-")
        except OSError as exc:
            raise ValueError("机器人暂时无法创建试听文件。") from exc
        with temporary as directory:
            source = Path(directory, "intro.mp3")
            clip = Path(directory, "preview.mp3" if telegram_mp3 else "preview.wav")
            try:
                async with asyncio.timeout(18):
                    await _download_intro(song_id, source)
                    await _make_clip(binary, source, clip)
            except (TimeoutError, httpx.TimeoutException) as exc:
                raise ValueError("获取试听超时，请稍后再试。") from exc
            except (httpx.HTTPError, OSError) as exc:
                raise ValueError("暂时无法读取或生成网易云试听。") from exc
            yield clip
