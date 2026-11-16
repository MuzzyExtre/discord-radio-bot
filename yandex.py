# -*- coding: utf-8 -*-
"""Мини-клиент Яндекс.Музыки: ссылки на треки, альбомы и плейлисты.

Работает на неофициальном API api.music.yandex.net.

Без токена API отдаёт только 30-секундные превью. С токеном (и подпиской Плюс)
— полные треки. Токен задаётся в config.json (`yandex_token`) или командой
/yandextoken.

Схема получения ссылки на файл:
  1. GET /tracks/{id}/download-info  -> downloadInfoUrl (XML)
  2. GET downloadInfoUrl             -> host, path, ts, s
  3. sign = md5(SALT + path[1:] + s); url = https://{host}/get-mp3/{sign}/{ts}{path}
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from typing import Any, Optional
from urllib.parse import urlparse
from xml.etree import ElementTree

import aiohttp

API = "https://api.music.yandex.net"
SIGN_SALT = "XGRlBW9FXlekgbPrRHuSiA"

# music.yandex.ru, music.yandex.com, .by, .kz, .uz, .com.tr
HOST_PREFIX = "music.yandex."

RE_TRACK = re.compile(r"/album/(\d+)/track/(\d+)")
RE_TRACK_SHORT = re.compile(r"/track/(\d+)")
RE_ALBUM = re.compile(r"/album/(\d+)")
RE_ARTIST = re.compile(r"/artist/(\d+)")
RE_PLAYLIST = re.compile(r"/users/([^/?#]+)/playlists/(\d+)")
RE_PLAYLIST_UUID = re.compile(r"/playlists/([0-9a-zA-Z._-]{6,})")

DEFAULT_LIMIT = 50


class YandexError(RuntimeError):
    """Ошибка Яндекс.Музыки."""


def is_yandex_url(url: str) -> bool:
    try:
        host = urlparse(url).netloc.lower()
    except ValueError:
        return False
    return host.startswith(HOST_PREFIX)


def parse_url(url: str) -> tuple[str, tuple[str, ...]]:
    """Возвращает ('track', (id,)) / ('album', (id, track_id|'')) / ('playlist', (user, kind))."""
    path = urlparse(url).path

    match = RE_TRACK.search(path)
    if match:
        return "track", (match.group(2),)

    match = RE_TRACK_SHORT.search(path)
    if match:
        return "track", (match.group(1),)

    match = RE_PLAYLIST.search(path)
    if match:
        return "playlist", (match.group(1), match.group(2))

    match = RE_PLAYLIST_UUID.search(path)
    if match:
        return "playlist", (match.group(1), "")

    match = RE_ALBUM.search(path)
    if match:
        return "album", (match.group(1), "")

    match = RE_ARTIST.search(path)
    if match:
        return "artist", (match.group(1),)

    raise YandexError(
        "не понял ссылку Яндекс.Музыки. Подходят ссылки на трек, альбом, "
        "исполнителя или плейлист: /track/123, /album/123, "
        "/album/123/track/456, /artist/123, /users/логин/playlists/3, "
        "/playlists/<uuid>"
    )


def track_id(url: str) -> str | None:
    """Если это ссылка на один трек — вернуть его id (для волны по треку)."""
    if not is_yandex_url(url):
        return None
    try:
        kind, args = parse_url(url)
    except YandexError:
        return None
    return args[0] if kind == "track" else None


def build_direct_link(xml_data: bytes) -> str:
    """Собирает прямую ссылку на mp3 из XML, который отдаёт downloadInfoUrl."""
    try:
        root = ElementTree.fromstring(xml_data)
    except ElementTree.ParseError as exc:
        raise YandexError(f"Яндекс вернул не XML: {exc}") from exc

    def node(tag: str) -> str:
        element = root.find(tag)
        if element is None or not element.text:
            raise YandexError(f"в ответе Яндекса нет поля {tag}")
        return element.text

    host, path, ts, s = node("host"), node("path"), node("ts"), node("s")
    sign = hashlib.md5((SIGN_SALT + path[1:] + s).encode("utf-8")).hexdigest()
    return f"https://{host}/get-mp3/{sign}/{ts}{path}"


def _normalize(track: dict[str, Any] | None) -> Optional[dict[str, Any]]:
    if not track:
        return None
    track_id = track.get("id") or track.get("realId")
    if not track_id:
        return None

    artists = ", ".join(
        artist.get("name", "") for artist in (track.get("artists") or []) if artist.get("name")
    )
    title = track.get("title") or "Без названия"
    return {
        "id": str(track_id),
        "title": f"{artists} — {title}" if artists else title,
        "available": bool(track.get("available", True)),
        "duration": int(track.get("durationMs") or 0),
    }


class YandexMusic:
    def __init__(self, token: str = "") -> None:
        self._token = token.strip()
        self._session: aiohttp.ClientSession | None = None

    # -- настройки ---------------------------------------------------------

    @property
    def has_token(self) -> bool:
        return bool(self._token)

    def set_token(self, token: str) -> None:
        self._token = token.strip()

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # -- транспорт ---------------------------------------------------------

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=30, connect=10)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    def _headers(self, token: str | None = None) -> dict[str, str]:
        headers = {"User-Agent": "Mozilla/5.0"}
        actual = self._token if token is None else token
        if actual:
            headers["Authorization"] = f"OAuth {actual}"
        return headers

    async def _json(
        self, path: str, token: str | None = None, **params: Any
    ) -> dict[str, Any]:
        session = await self._get_session()
        try:
            async with session.get(
                API + path, params=params or None, headers=self._headers(token)
            ) as response:
                text = await response.text()
                if response.status >= 400:
                    raise YandexError(
                        f"HTTP {response.status} на {path}: {text[:200]}"
                    )
                return json.loads(text)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise YandexError(f"сеть: {exc}") from exc

    async def _raw(self, url: str) -> bytes:
        session = await self._get_session()
        try:
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0"}) as response:
                if response.status >= 400:
                    raise YandexError(f"HTTP {response.status} при получении ссылки")
                return await response.read()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise YandexError(f"сеть: {exc}") from exc

    # -- проверка токена ---------------------------------------------------

    async def validate_token(self, token: str) -> Optional[str]:
        """Возвращает логин владельца токена или None, если токен не подошёл."""
        token = token.strip()
        if token.lower().startswith("oauth "):
            token = token[6:].strip()
        if not token:
            return None
        try:
            data = await self._json("/account/status", token=token)
        except YandexError:
            return None
        login = (data.get("result") or {}).get("account", {}).get("login")
        return login or None

    # -- метаданные --------------------------------------------------------

    async def _track(self, track_id: str) -> list[dict[str, Any]]:
        data = await self._json(f"/tracks/{track_id}")
        result = data.get("result") or []
        if isinstance(result, dict):
            result = [result]
        return [item for item in (_normalize(t) for t in result) if item]

    async def _artist(self, artist_id: str, limit: int) -> list[dict[str, Any]]:
        size = max(1, min(limit or 200, 200))
        data = await self._json(
            f"/artists/{artist_id}/tracks", **{"page": 0, "page-size": size}
        )
        result = data.get("result") or {}
        tracks = [
            entry.get("track") or entry
            for entry in (result.get("tracks") or [])
            if entry
        ]
        return [item for item in (_normalize(t) for t in tracks) if item]

    async def _album(self, album_id: str, only_track: str = "") -> list[dict[str, Any]]:
        data = await self._json(f"/albums/{album_id}/with-tracks")
        result = data.get("result") or {}
        volumes = result.get("volumes") or []
        tracks = [track for volume in volumes for track in volume]
        items = [item for item in (_normalize(t) for t in tracks) if item]
        if only_track:
            items = [item for item in items if item["id"] == only_track] or items
        return items

    async def _playlist(self, user: str, kind: str) -> list[dict[str, Any]]:
        if kind:
            data = await self._json(f"/users/{user}/playlists/{kind}")
            result = data.get("result") or {}
        else:
            # Новый формат ссылок: music.yandex.ru/playlists/<uuid> -> /playlist/<uuid>
            data = await self._json(f"/playlist/{user}")
            result = data.get("result") or {}
            if "playlist" in result and isinstance(result["playlist"], dict):
                result = result["playlist"]

        raw_tracks = result.get("tracks") or []
        tracks = []
        for entry in raw_tracks:
            if not entry:
                continue
            track = entry.get("track") if isinstance(entry, dict) else None
            if track is None and isinstance(entry, dict) and entry.get("title"):
                track = entry
            if track:
                tracks.append(track)
        return [item for item in (_normalize(t) for t in tracks) if item]

    # -- основное ----------------------------------------------------------

    async def resolve(
        self, url: str, limit: int = 0
    ) -> tuple[list[dict[str, Any]], bool]:
        """Ссылка -> список треков (id, title, available). Второе значение — обрезали ли.

        limit=0 — без ограничения.
        """
        kind, args = parse_url(url)
        if kind == "track":
            items = await self._track(args[0])
        elif kind == "artist":
            items = await self._artist(args[0], limit)
        elif kind == "album":
            items = await self._album(args[0], args[1])
        else:
            items = await self._playlist(args[0], args[1])

        if not items:
            raise YandexError("в ссылке не нашлось треков")
        if limit and limit > 0:
            return items[:limit], len(items) > limit
        return items, False

    # -- поиск -------------------------------------------------------------

    async def search(self, text: str, limit: int = 10) -> list[dict[str, Any]]:
        """Поиск треков по названию/исполнителю."""
        text = text.strip()
        if not text:
            return []
        data = await self._json(
            "/search", **{"text": text, "type": "track", "page": 0}
        )
        result = data.get("result") or {}
        tracks = ((result.get("tracks") or {}).get("results")) or []
        return [item for item in (_normalize(t) for t in tracks[:limit]) if item]

    async def wave_tracks(
        self, station: str, batch_id: str | None = None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Очередная порция треков «волны» (rotor).

        station: user:onyourwave, genre:rock, mood:calm, artist:<id>, track:<id>.
        """
        params: dict[str, Any] = {"settings2": "true"}
        if batch_id:
            params["batch-id"] = batch_id
        data = await self._json(f"/rotor/station/{station}/tracks", **params)
        result = data.get("result") or {}
        sequence = result.get("sequence") or []
        tracks = [
            item.get("track")
            for item in sequence
            if isinstance(item, dict) and item.get("track")
        ]
        normalized = [item for item in (_normalize(t) for t in tracks) if item]
        return normalized, result.get("batchId")

    async def direct_url(self, track_id: str) -> str:
        """Свежая прямая ссылка на mp3. Живёт около минуты, поэтому берём перед запуском."""
        data = await self._json(f"/tracks/{track_id}/download-info")
        items = data.get("result") or []
        if not items:
            raise YandexError("Яндекс не отдал вариантов загрузки (нет прав на трек)")

        mp3 = [item for item in items if item.get("codec") == "mp3"] or items
        mp3.sort(
            key=lambda item: (
                not item.get("preview", False),
                item.get("bitrateInKbps", 0),
            ),
            reverse=True,
        )
        best = mp3[0]
        info_url = best.get("downloadInfoUrl")
        if not info_url:
            raise YandexError("в ответе нет downloadInfoUrl")
        return build_direct_link(await self._raw(info_url))
