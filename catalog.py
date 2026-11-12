"""曲库检索与群聊的最近推荐记录。"""

import json
import random
import re
import sqlite3
import unicodedata
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit


def normalize(text: str) -> str:
    return "".join(
        c for c in unicodedata.normalize("NFKC", text).casefold() if c.isalnum()
    )


GENRES = {"电子": "电音", "电子乐": "电音", "edm": "电音", "电子音乐": "电音"}
KINDS = {"原曲": "original", "原作": "original", "同人": "arrange", "改编": "arrange"}
YUUHEI = {
    "幽闭星光",
    "幽閉星光",
    "幽闭卫星",
    "幽閉衛星",
    "幽閉サテライト",
    "幽闭サテライト",
    "yuuheisatellite",
}


@dataclass(frozen=True)
class Track:
    title: str
    artist: str
    kind: str
    genres: tuple[str, ...]
    original: tuple[str, ...]
    work: str
    listen_url: str
    terms: tuple[str, ...]
    netease_id: str | None = None

    def message(self, *, link: bool, url: str | None = None) -> str:
        lines = [f"♪ {self.title}", self.artist]
        if self.kind == "original":
            lines.append(f"原曲 · {self.work}" if self.work else "东方原曲")
        elif self.original:
            lines.append(f"改编自《{'》《'.join(self.original)}》")
        url = url or self.listen_url
        address = urlsplit(url)
        host = address.hostname
        platform = {
            "music.apple.com": "Apple Music",
            "music.163.com": "网易云音乐",
            "www.bilibili.com": "哔哩哔哩",
            "www.youtube.com": "YouTube",
            "youtu.be": "YouTube",
        }.get(host, "Bandcamp" if host and host.endswith(".bandcamp.com") else "播放页")
        if link:
            label = "Apple Music 试听" if platform == "Apple Music" else platform
            lines.append(f"{label}  {url}")
        elif platform == "哔哩哔哩" and (
            video := re.search(r"/(BV[0-9A-Za-z]+)(?:/|$)", address.path)
        ):
            lines.append(f"哔哩哔哩搜：{video[1]}")
        else:
            lines.append(f"{platform} 搜：{self.title} {self.artist}")
        return "\n".join(lines)


def load_catalog(directory: Path) -> list[Track]:
    tracks = []
    seen_urls = set()
    original_names = {}
    for file in sorted(directory.glob("*.json")):
        for row in json.loads(file.read_text(encoding="utf-8-sig")):
            url = row["listen_url"]
            if (
                url in seen_urls
                or urlsplit(url).scheme != "https"
                or not urlsplit(url).hostname
            ):
                raise ValueError(f"{file.name} 中的播放地址重复或无效：{url}")
            seen_urls.add(url)
            address = urlsplit(url)
            netease_id = row.get("netease_id")
            if address.hostname == "music.163.com" and address.path == "/song":
                netease_id = parse_qs(address.query).get("id", [None])[0]
            if netease_id is not None and not re.fullmatch(
                r"[1-9][0-9]{0,18}", netease_id
            ):
                raise ValueError(f"{file.name} 中的网易云单曲 ID 无效：{netease_id}")
            title, artist, kind = row["title"], row["artist"], row["kind"]
            if kind not in KINDS.values() or not (title.strip() and artist.strip()):
                raise ValueError(f"{file.name} 中的曲名、作者或曲目类型无效：{title}")
            work = row.get("work", "")
            original = tuple(row.get("original", []))
            genres = tuple(row["genres"])
            fields = [title, artist, work, *original, *genres, *row["aliases"]]
            fields.extend(GENRES.get(normalize(genre), genre) for genre in genres)
            if kind == "original":
                names = tuple(normalize(name) for name in [title, *row["aliases"]])
                for name in names:
                    original_names[name] = (*names, normalize(work))
            if kind == "original" and artist == "上海爱丽丝幻乐团":
                fields.extend(["ZUN", "上海アリス幻樂団", "Team Shanghai Alice"])
            if any(name in normalize(artist) for name in YUUHEI):
                fields.extend(YUUHEI)
            tracks.append(
                Track(
                    title,
                    artist,
                    kind,
                    genres,
                    original,
                    work,
                    url,
                    tuple(normalize(field) for field in fields if field),
                    netease_id,
                )
            )
    if not tracks:
        raise ValueError("插件目录 data 下缺少曲库")
    # 让同人曲的日文原曲署名也能用游戏原声里的中文译名检索。
    for index, track in enumerate(tracks):
        related = [
            name
            for credit in track.original
            for name in original_names.get(normalize(credit), ())
        ]
        if related:
            tracks[index] = replace(track, terms=(*track.terms, *related))
    return tracks


def matches(track: Track, words: list[str]) -> bool:
    for word in words:
        if word in KINDS:
            if track.kind != KINDS[word]:
                return False
        elif not any(GENRES.get(word, word) in term for term in track.terms):
            return False
    return True


class Jukebox:
    def __init__(self, directory: Path, database: Path):
        self.tracks = load_catalog(directory)
        self.database = database
        with closing(sqlite3.connect(database)) as db, db:
            db.execute("""CREATE TABLE IF NOT EXISTS plays (
                room TEXT NOT NULL, url TEXT NOT NULL, turn INTEGER NOT NULL,
                PRIMARY KEY (room, url)
            )""")

    @staticmethod
    def _record(db: sqlite3.Connection, room: str, track: Track) -> None:
        db.execute(
            """INSERT INTO plays (room, url, turn)
               SELECT ?, ?, COALESCE(MAX(turn), 0) + 1 FROM plays WHERE room = ?
               ON CONFLICT (room, url) DO UPDATE SET turn = excluded.turn""",
            (room, track.listen_url, room),
        )

    def record_request(self, room: str, track: Track) -> None:
        with closing(sqlite3.connect(self.database)) as db, db:
            self._record(db, room, track)

    def pick(
        self, room: str, query: str, *, netease_only: bool = False
    ) -> Track | None:
        words = [normalize(word) for word in query.split()]
        if any(not word for word in words):
            return None
        pool = [
            track
            for track in self.tracks
            if (track.netease_id or not netease_only) and matches(track, words)
        ]
        if not pool:
            return None

        with closing(sqlite3.connect(self.database)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            history = dict(
                db.execute("SELECT url, turn FROM plays WHERE room = ?", (room,))
            )
            choices = [track for track in pool if track.listen_url not in history]
            if not choices:
                pool.sort(key=lambda track: history[track.listen_url], reverse=True)
                choices = pool[min(5, len(pool) - 1) :]
            picked = random.choice(choices)
            self._record(db, room, picked)
        return picked
