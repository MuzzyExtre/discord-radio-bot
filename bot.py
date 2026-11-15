#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Радио-бот для Discord на слэш-командах.

Команды:
  /radio      — включить радио (Soundpark Deep, Свое FM или своя ссылка)
  /play       — поставить трек по ссылке в очередь
  /stop       — остановить и выйти из канала
  /skip       — пропустить трек / перезапустить поток радио
  /status     — что играет сейчас
  /volume     — громкость
  /setchannel — выбрать текстовый канал, из которого бот берёт музыку
  /stations   — список встроенных станций

Плюс бот слушает один текстовый канал: любое сообщение со ссылкой
(или аудиофайлом) автоматически попадает в очередь.

Запуск:  python bot.py
Проверка без токена:  python bot.py --selftest
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import random
import re
import shlex
import shutil
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import unquote, urlparse

import discord
from discord import app_commands

import yandex as yandex_mod

# --------------------------------------------------------------------------
# Настройки / константы
# --------------------------------------------------------------------------

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "config.json"
ENV_PATH = BASE_DIR / ".env"
LOG_PATH = BASE_DIR / "bot.log"
FFMPEG_LOG_PATH = BASE_DIR / "ffmpeg.log"
PANEL_PATH = BASE_DIR / "panel.json"

log = logging.getLogger("radiobot")

STATIONS: dict[str, tuple[str, str]] = {
    "soundpark": ("Soundpark Deep", "https://h.getradio.me/spdeep/hls.m3u8"),
    "svoefm": ("Свое FM", "https://h.getradio.me/svoefm/hls.m3u8"),
}

# «Волны» Яндекс.Музыки (rotor): ключ -> (название, station)
WAVES: dict[str, tuple[str, str]] = {
    "my": ("Моя волна", "user:onyourwave"),
    "rock": ("Рок", "genre:rock"),
    "pop": ("Поп", "genre:pop"),
    "rap": ("Рэп", "genre:rap"),
    "electronic": ("Электроника", "genre:electronic"),
    "metal": ("Метал", "genre:metal"),
    "indie": ("Инди", "genre:indie"),
    "jazz": ("Джаз", "genre:jazz"),
    "classical": ("Классика", "genre:classical"),
    "calm": ("Спокойное", "mood:calm"),
    "happy": ("Весёлое", "mood:happy"),
    "energetic": ("Энергичное", "mood:energetic"),
    "romantic": ("Романтичное", "mood:romantic"),
}

DEFAULT_CONFIG: dict = {
    "token": "",
    "guild_id": 0,            # ID сервера — команды появятся сразу (0 = все серверы)
    "music_channel_id": 0,    # ID текстового канала, откуда брать музыку (0 = выключено)
    "auto_join": True,        # заходить в канал автора сообщения автоматически
    "volume": 60,             # 0..150, проценты
    "ffmpeg_path": "",        # пусто = искать ffmpeg в PATH
    "yandex_token": "",       # токен Яндекс.Музыки (без него только превью по 30 с)
    "max_tracks_per_link": 0,  # 0 = без ограничения; иначе сколько треков брать с одной ссылки
    "panel_repost_after": 0,  # через сколько сообщений переносить панель вниз (0 = не переносить)
    "panel_channel_id": 0,    # канал для панели (0 = тот, где вызвали /panel)
}

# FFmpeg: живой поток. Важно: без -reconnect_at_eof — на HLS он зацикливает
# чтение и ffmpeg зависает, не отдавая звук.
LIVE_BEFORE = (
    "-nostdin -hide_banner -loglevel error "
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
    "-rw_timeout 20000000 -user_agent Mozilla/5.0"
)
# FFmpeg: обычный файл/трек.
FILE_BEFORE = (
    "-nostdin -hide_banner -loglevel error "
    "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 "
    "-rw_timeout 20000000 -user_agent Mozilla/5.0"
)

AUDIO_EXTS = {
    ".mp3", ".m4a", ".aac", ".ogg", ".oga", ".opus",
    ".flac", ".wav", ".webm", ".mp4", ".m3u8", ".ts",
}

URL_RE = re.compile(r"https?://[^\s<>\"'`]+")
TRAILING_JUNK = ").,;:!?»\"'"

RETRY_DELAY = 3.0          # пауза перед переподключением радио, сек
WATCHDOG_INTERVAL = 10.0   # как часто проверять, что звук реально идёт
STATUS_COLORS = {
    "radio": 0x2ECC71,
    "track": 0x3498DB,
    "wave": 0x1ABC9C,
    "idle": 0x95A5A6,
}


# --------------------------------------------------------------------------
# Конфиг
# --------------------------------------------------------------------------

def setup_logging() -> None:
    # В консоли Windows Python сам пишет Unicode; при перенаправлении вывода
    # (файл, пайп) кодировка становится cp1251 и русский текст падает.
    for stream in (sys.stdout, sys.stderr):
        try:
            if not stream.isatty():
                stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    log.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(message)s", "%H:%M:%S")

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    log.addHandler(console)

    try:
        file_handler = RotatingFileHandler(
            LOG_PATH, maxBytes=1_000_000, backupCount=2, encoding="utf-8"
        )
        file_handler.setFormatter(fmt)
        log.addHandler(file_handler)
    except OSError:
        pass

    # discord.py пишет в root — приглушаем лишнее, но шлюз оставляем видимым:
    # так видно переподключения (из-за них теряются нажатия кнопок).
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.INFO)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict):
                cfg.update({k: v for k, v in data.items() if k in cfg})
        except (OSError, json.JSONDecodeError) as exc:
            log.error("Не смог прочитать config.json: %s", exc)

    # .env (простой парсер, чтобы не тащить зависимость)
    if ENV_PATH.exists():
        try:
            for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                if key.strip() in ("DISCORD_TOKEN", "TOKEN") and not cfg.get("token"):
                    cfg["token"] = value.strip().strip("\"'")
        except OSError:
            pass

    env_token = os.environ.get("DISCORD_TOKEN")
    if env_token:
        cfg["token"] = env_token.strip()

    return cfg


def save_config(cfg: dict) -> None:
    clean = {k: cfg.get(k, v) for k, v in DEFAULT_CONFIG.items()}
    try:
        CONFIG_PATH.write_text(
            json.dumps(clean, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        log.error("Не смог сохранить config.json: %s", exc)


def open_ffmpeg_log():
    """FFmpeg пишет диагностику в файл: консоль не засоряется, а ошибки видны."""
    try:
        if FFMPEG_LOG_PATH.exists() and FFMPEG_LOG_PATH.stat().st_size > 2_000_000:
            FFMPEG_LOG_PATH.write_bytes(b"")
        return FFMPEG_LOG_PATH.open("ab", buffering=0)
    except OSError:
        return None


# --------------------------------------------------------------------------
# Разбор ссылок
# --------------------------------------------------------------------------

@dataclass
class Track:
    title: str
    url: str
    live: bool = False
    requester: str = ""
    yandex_id: str | None = None  # если задан — ссылку получаем лениво, перед запуском


def is_live_url(url: str) -> bool:
    low = url.lower().split("?")[0]
    return low.endswith(".m3u8") or low.endswith(".ts") or "/hls" in low


def title_from_url(url: str) -> str:
    try:
        name = unquote(Path(urlparse(url).path).name) or urlparse(url).netloc
    except ValueError:
        name = url
    stem = Path(name).stem if "." in name else name
    return stem or "Поток"


def looks_direct(url: str) -> bool:
    low = url.lower().split("?")[0]
    return any(low.endswith(ext) for ext in AUDIO_EXTS)


YT_HOSTS = (
    "youtube.com", "youtu.be", "music.youtube.com",
    "soundcloud.com", "vimeo.com", "tiktok.com", "twitch.tv",
)

# Единый клиент Яндекс.Музыки на весь процесс.
YANDEX = yandex_mod.YandexMusic()
MAX_TRACKS_PER_LINK = 0  # 0 = без ограничения (можно задать max_tracks_per_link в config.json)


def _ytdlp_extract(url: str) -> tuple[str, str]:
    try:
        import yt_dlp  # необязательная зависимость
    except ImportError as exc:
        raise RuntimeError(
            "Ссылки YouTube/SoundCloud требуют yt-dlp: pip install yt-dlp"
        ) from exc

    opts = {
        "format": "bestaudio/best",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "skip_download": True,
        "socket_timeout": 20,
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    if info and info.get("entries"):
        info = info["entries"][0]
    if not info:
        raise RuntimeError("Не удалось получить аудио по ссылке")

    direct = info.get("url")
    if not direct:
        for fmt in reversed(info.get("formats") or []):
            if fmt.get("acodec") not in (None, "none") and fmt.get("url"):
                direct = fmt["url"]
                break
    if not direct:
        raise RuntimeError("У ссылки нет аудиодорожки")
    return info.get("title") or "Трек", direct


async def resolve_track(url: str, requester: str = "") -> Track:
    url = url.strip().strip("<>").rstrip(TRAILING_JUNK)
    if not url.startswith(("http://", "https://")):
        raise RuntimeError("Нужна ссылка, начинающаяся с http:// или https://")

    if looks_direct(url):
        return Track(title_from_url(url), url, is_live_url(url), requester)

    if any(host in url.lower() for host in YT_HOSTS):
        title, direct = await asyncio.to_thread(_ytdlp_extract, url)
        return Track(title, direct, False, requester)

    return Track(title_from_url(url), url, is_live_url(url), requester)


async def resolve_tracks(
    url: str, requester: str = "", limit: int = 0
) -> tuple[list[Track], bool, bool]:
    """Ссылка -> список треков. Возвращает (треки, обрезано_ли, это_яндекс)."""
    if yandex_mod.is_yandex_url(url):
        items, truncated = await YANDEX.resolve(url, limit=limit)
        tracks = [
            Track(
                title=item["title"],
                url="",
                live=False,
                requester=requester,
                yandex_id=item["id"],
            )
            for item in items
        ]
        return tracks, truncated, True
    return [await resolve_track(url, requester)], False, False


# --------------------------------------------------------------------------
# Плеер на один сервер
# --------------------------------------------------------------------------

class WatchedVolume(discord.PCMVolumeTransformer):
    """Громкость + отметка времени последнего прочитанного куска звука."""

    def __init__(self, original: discord.AudioSource, volume: float):
        super().__init__(original, volume=volume)
        self.last_read = time.monotonic()

    def read(self) -> bytes:
        data = super().read()
        if data:
            self.last_read = time.monotonic()
        return data


class GuildPlayer:
    def __init__(self, bot: "RadioBot", guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue: deque[Track] = deque()
        self.radio: Track | None = None
        self._current: Track | None = None
        self._seq = 0
        self._stopped = True
        self._loop = asyncio.get_event_loop()
        self.started_at: float | None = None
        self._failures = 0
        self._reconnect_task: asyncio.Task | None = None
        self._watchdog: asyncio.Task | None = None
        self._source: WatchedVolume | None = None
        self._paused_live = False
        self.on_change = None  # callback: обновить панель
        self.wave_station: str | None = None
        self.wave_name = ""
        self._wave_batch: str | None = None
        self._wave_task: asyncio.Task | None = None
        self.volume = max(0.0, min(1.5, float(bot.config.get("volume", 60)) / 100))

    def _notify(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change()
            except Exception:
                log.debug("on_change упал", exc_info=True)

    # -- голос -------------------------------------------------------------

    @property
    def vc(self) -> discord.VoiceClient | None:
        return self.guild.voice_client

    @property
    def current(self) -> Track | None:
        return self._current

    @property
    def paused(self) -> bool:
        if self._paused_live:
            return True
        vc = self.vc
        return bool(vc and vc.is_paused())

    async def ensure_voice(self, channel: discord.abc.Connectable) -> discord.VoiceClient:
        vc = self.guild.voice_client
        if vc and vc.is_connected():
            if vc.channel != channel:
                await vc.move_to(channel)
            return vc
        if vc:
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
        return await channel.connect(self_deaf=True, timeout=30)

    # -- управление --------------------------------------------------------

    def play_radio(self, track: Track) -> None:
        self.stop_wave()
        self.radio = track
        self.queue.clear()
        self._failures = 0
        self._stopped = False
        self._paused_live = False
        self._restart()
        self._notify()

    def play_wave(self, name: str, station: str) -> None:
        """Бесконечная волна Яндекс.Музыки: очередь всё время пополняется."""
        self.radio = None
        self.queue.clear()
        self._wave_name = name
        self.wave_name = name
        self.wave_station = station
        self._wave_batch = None
        self._failures = 0
        self._stopped = False
        self._paused_live = False
        self._start_wave_loop()
        self._restart()
        self._notify()

    def stop_wave(self) -> bool:
        was = self.wave_station is not None
        self.wave_station = None
        self.wave_name = ""
        self._wave_batch = None
        task = self._wave_task
        if task is not None and not task.done():
            task.cancel()
        self._wave_task = None
        return was

    def _start_wave_loop(self) -> None:
        if self._wave_task is None or self._wave_task.done():
            self._wave_task = asyncio.create_task(self._wave_loop())

    async def _wave_loop(self) -> None:
        while not self._stopped and self.wave_station:
            station = self.wave_station
            if len(self.queue) <= 3:
                try:
                    tracks, batch = await self.bot.yandex.wave_tracks(
                        station, self._wave_batch
                    )
                except Exception as exc:
                    log.warning("Волна: не смог получить треки (%s)", exc)
                    await asyncio.sleep(10)
                    continue

                if self.wave_station != station or self._stopped:
                    return

                self._wave_batch = batch
                added = 0
                for item in tracks:
                    self.queue.append(
                        Track(
                            title=item["title"],
                            url="",
                            live=False,
                            requester=self.wave_name or "волна",
                            yandex_id=item["id"],
                        )
                    )
                    added += 1
                if added:
                    log.info("Волна «%s»: +%d треков", self.wave_name, added)
                    self._notify()
                    if self._current is None:
                        self._advance()
            await asyncio.sleep(4)

    def enqueue(self, track: Track) -> None:
        self.queue.append(track)
        if self._current is None:
            self._stopped = False
            self._advance()
        elif self._current.live:
            # Радио уступает место треку, а после трека включится снова.
            self._restart()
        self._notify()

    def skip(self) -> None:
        self._stopped = False
        self._paused_live = False
        self._restart()

    def clear_queue(self) -> int:
        """Убирает все треки из очереди, не трогая текущий."""
        count = len(self.queue)
        self.queue.clear()
        self._notify()
        return count

    def shuffle(self) -> int:
        items = list(self.queue)
        random.shuffle(items)
        self.queue.clear()
        self.queue.extend(items)
        self._notify()
        return len(items)

    def toggle_pause(self) -> str:
        """Возвращает 'paused', 'resumed' или 'idle'."""
        vc = self.vc
        if vc is None or self._current is None:
            return "idle"

        if self._current.live:
            # Живой поток нельзя «сдвинуть» на паузу: останавливаем ffmpeg,
            # а при продолжении подключаемся к эфиру заново.
            if self._paused_live:
                self._paused_live = False
                self._restart()
                self._notify()
                return "resumed"
            self._paused_live = True
            self._seq += 1
            if vc.is_playing() or vc.is_paused():
                try:
                    vc.stop()
                except Exception:
                    pass
            self._notify()
            return "paused"

        if vc.is_paused():
            vc.resume()
            self._notify()
            return "resumed"
        if vc.is_playing():
            vc.pause()
            self._notify()
            return "paused"
        return "idle"

    def on_listener_join(self) -> None:
        """В канал зашёл человек: если E2EE ещё не готов, перезапускаем поток."""
        if self._stopped or self._current is None:
            return
        vc = self.vc
        conn = getattr(vc, "_connection", None) if vc else None
        if conn is None:
            return
        if getattr(conn, "dave_protocol_version", 0) and not getattr(
            conn, "can_encrypt", False
        ):
            log.info("Слушатель зашёл, E2EE ещё не готов — перезапускаю поток")
            self._restart()

    def _restart(self) -> None:
        vc = self.vc
        self._seq += 1
        self._current = None
        if vc and (vc.is_playing() or vc.is_paused()):
            try:
                vc.stop()
            except Exception:
                pass
        self._advance()

    def _advance(self) -> None:
        if self._stopped:
            return
        vc = self.vc
        if vc is None or not vc.is_connected():
            return

        self._seq += 1
        seq = self._seq

        if self.queue:
            track = self.queue.popleft()
        elif self.radio:
            track = self.radio
        elif self.wave_station:
            # Волна: треки ещё не подгрузились — подождём и попробуем снова.
            self._current = None
            self._schedule_advance(1.0, seq)
            return
        else:
            self._current = None
            if vc.is_playing():
                vc.stop()
            self._notify()
            return

        self._current = track
        self.started_at = time.monotonic()
        if track.yandex_id:
            # Ссылка на файл живёт около минуты — берём её прямо перед запуском.
            asyncio.create_task(self._resolve_yandex(track, seq))
        else:
            self._play(vc, track, seq)

    async def _resolve_yandex(self, track: Track, seq: int) -> None:
        try:
            url = await self.bot.yandex.direct_url(track.yandex_id or "")
        except Exception as exc:
            log.warning(
                "Яндекс.Музыка: «%s» не открылся (%s) — пропускаю", track.title, exc
            )
            self._schedule_advance(1.5, seq)
            return
        if seq != self._seq or self._stopped:
            return
        vc = self.vc
        if vc is None or not vc.is_connected():
            return
        track.url = url
        self._play(vc, track, seq)

    def _play(
        self,
        vc: discord.VoiceClient,
        track: Track,
        seq: int,
        attempt: int = 0,
        dave_wait: int = 0,
    ) -> None:
        if seq != self._seq or self._stopped:
            return

        # DAVE (E2EE): ключ выдаётся, только когда в канале есть кто-то ещё.
        # Если слушатели есть, а ключ ещё не готов — ждём до ~8 секунд.
        conn = getattr(vc, "_connection", None)
        dave_version = getattr(conn, "dave_protocol_version", 0) or 0
        channel = getattr(vc, "channel", None)
        listeners = [
            m for m in getattr(channel, "members", []) if not m.bot
        ]
        if dave_version and not getattr(conn, "can_encrypt", False) and listeners:
            if dave_wait < 32:
                self._loop.call_later(
                    0.25, self._play, vc, track, seq, attempt, dave_wait + 1
                )
                return
            log.warning("DAVE-ключ так и не появился — играю без E2EE")

        before = LIVE_BEFORE if track.live else FILE_BEFORE
        try:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            raw = discord.FFmpegPCMAudio(
                track.url,
                before_options=before,
                options="-vn",
                executable=self.bot.ffmpeg,
                stderr=getattr(self.bot, "ffmpeg_log", None),
            )
            source = WatchedVolume(raw, volume=self.volume)
            self._source = source
        except Exception:
            log.exception("Не смог открыть поток: %s", track.url)
            self._schedule_advance(RETRY_DELAY if track.live else 0.5, seq)
            return

        def after(err: Exception | None, _seq: int = seq) -> None:
            try:
                self._loop.call_soon_threadsafe(self._after, _seq, err)
            except RuntimeError:
                pass  # цикл уже закрыт

        try:
            vc.play(source, after=after)
            self._ensure_watchdog()
            where = getattr(vc, "channel", None) or "канал"
            conn = getattr(vc, "_connection", None)
            e2ee = "да" if getattr(conn, "can_encrypt", False) else "НЕТ"
            log.info(
                "[%s] играю: %s → %s (E2EE: %s)", self.guild.name, track.title, where, e2ee
            )
            self._notify()
        except Exception as exc:
            if attempt < 2:
                log.warning("vc.play не сработал (%s), пробую ещё раз", exc)
                self._loop.call_later(0.5, self._play, vc, track, seq, attempt + 1)
            else:
                log.exception("Не удалось запустить воспроизведение")
                self._schedule_advance(RETRY_DELAY, seq)

    def _after(self, seq: int, err: Exception | None) -> None:
        if seq != self._seq or self._stopped:
            return
        cur = self._current
        if err:
            log.warning(
                "Поток прервался (%s): %s", cur.title if cur else "?", err
            )
        if cur and cur.live:
            lived = time.monotonic() - (self.started_at or time.monotonic())
            if lived < 10:
                self._failures += 1
            else:
                self._failures = 0
            if self._failures:
                delay = min(RETRY_DELAY * (2 ** min(self._failures - 1, 3)), 30.0)
            else:
                delay = RETRY_DELAY
            if self._failures and self._failures % 5 == 0:
                log.error(
                    "Поток не оживает (%d попыток подряд), следующая через %.0f с",
                    self._failures,
                    delay,
                )
            self._schedule_advance(delay, seq)
            return
        self._advance()

    def _schedule_advance(self, delay: float, seq: int) -> None:
        def run() -> None:
            if seq != self._seq or self._stopped:
                return
            self._advance()

        self._loop.call_later(delay, run)

    def play_tone(self) -> None:
        """8 секунд тона 440 Гц — проверить, что звук доходит до слушателей."""
        vc = self.vc
        if vc is None:
            return
        self._stopped = False
        self._seq += 1
        seq = self._seq
        try:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
            raw = discord.FFmpegPCMAudio(
                "sine=frequency=440:duration=8",
                before_options="-f lavfi",
                options="-vn",
                executable=self.bot.ffmpeg,
                stderr=getattr(self.bot, "ffmpeg_log", None),
            )
            source = WatchedVolume(raw, self.volume)
        except Exception:
            log.exception("Не смог создать тестовый тон")
            return
        self._source = source
        self._current = Track("Тестовый тон 440 Гц", "tone", live=False)

        def after(err: Exception | None, _seq: int = seq) -> None:
            try:
                self._loop.call_soon_threadsafe(self._after, _seq, err)
            except RuntimeError:
                pass

        vc.play(source, after=after)
        self._ensure_watchdog()
        log.info("[%s] играю тестовый тон", self.guild.name)

    def _ensure_watchdog(self) -> None:
        if self._watchdog is None or self._watchdog.done():
            self._watchdog = asyncio.create_task(self._watchdog_loop())

    async def _watchdog_loop(self) -> None:
        """Следит, чтобы звук реально шёл, а не «висел» без ошибок."""
        while not self._stopped:
            await asyncio.sleep(WATCHDOG_INTERVAL)
            if self._stopped or self._current is None:
                continue
            vc = self.vc
            if vc is None or not vc.is_connected():
                continue
            if vc.is_paused() or self._paused_live:
                continue
            if not vc.is_playing():
                log.warning("Плеер остановился без ошибки — перезапускаю поток")
                self._restart()
                continue
            src = self._source
            if src is not None and time.monotonic() - src.last_read > WATCHDOG_INTERVAL * 2:
                log.warning(
                    "Звук не идёт %.0f с — перезапускаю поток", WATCHDOG_INTERVAL * 2
                )
                self._restart()

    async def shutdown(self) -> None:
        self._stopped = True
        self._seq += 1
        self._paused_live = False
        self.stop_wave()
        if self._watchdog and not self._watchdog.done():
            self._watchdog.cancel()
        self.radio = None
        self.queue.clear()
        self._current = None
        self.started_at = None
        vc = self.vc
        if vc is None:
            self._notify()
            return
        try:
            if vc.is_playing() or vc.is_paused():
                vc.stop()
        except Exception:
            pass
        try:
            await vc.disconnect(force=True)
        except Exception:
            pass
        self._notify()

    async def reconnect(self, channel: discord.abc.Connectable) -> None:
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = asyncio.create_task(self._reconnect_loop(channel))

    async def _reconnect_loop(self, channel: discord.abc.Connectable) -> None:
        for attempt in range(1, 6):
            await asyncio.sleep(min(2 * attempt, 10))
            if self._stopped or not (self.radio or self.queue):
                return
            try:
                vc = self.vc
                if vc:
                    try:
                        await vc.disconnect(force=True)
                    except Exception:
                        pass
                await channel.connect(self_deaf=True, timeout=30)
                self._current = None
                self._advance()
                log.info("Переподключился к каналу «%s»", channel)
                return
            except Exception as exc:
                log.warning("Переподключение %d/5 не удалось: %s", attempt, exc)

    # -- статус ------------------------------------------------------------

    def status_text(self) -> str:
        if self._current is None:
            return "Ничего не играет"
        if self._current is self.radio:
            return f"📻 Радио: **{self._current.title}**"
        extra = f" (в очереди: {len(self.queue)})" if self.queue else ""
        return f"🎵 Трек: **{self._current.title}**{extra}"


# --------------------------------------------------------------------------
# Панель плеера
# --------------------------------------------------------------------------

def build_panel_embed(player: "GuildPlayer | None") -> discord.Embed:
    if player is None or player.current is None:
        return discord.Embed(
            title="🎵 Плеер",
            description=(
                "Сейчас ничего не играет.\n"
                "Включи радио: `/radio`, или кинь ссылку: `/play` / `#music`."
            ),
            colour=STATUS_COLORS["idle"],
        )

    cur = player.current
    if player.wave_station:
        title = f"🌊 Волна · {player.wave_name}"
        colour = STATUS_COLORS["wave"]
    elif cur.live:
        title = "📻 Радио"
        colour = STATUS_COLORS["radio"]
    else:
        title = "🎵 Сейчас играет"
        colour = STATUS_COLORS["track"]

    embed = discord.Embed(
        title=title,
        description=f"**{cur.title[:180]}**",
        colour=colour,
    )
    if player.paused:
        embed.description += "\n⏸ **Пауза**"

    queue = list(player.queue)
    if queue:
        shown = queue[:5]
        lines = [
            f"`{i}.` {track.title[:60]}" for i, track in enumerate(shown, 1)
        ]
        if len(queue) > len(shown):
            lines.append(f"…и ещё **{len(queue) - len(shown)}**")
        embed.add_field(
            name=f"Очередь — {len(queue)}", value="\n".join(lines)[:1024], inline=False
        )
    else:
        embed.add_field(name="Очередь", value="пусто", inline=False)

    footer = []
    channel = getattr(player.vc, "channel", None) if player.vc else None
    if channel is not None:
        footer.append(f"🔊 {channel.name}")
    footer.append(f"громкость {round(player.volume * 100)}%")
    embed.set_footer(text=" · ".join(footer))
    return embed


def build_queue_embed(player: "GuildPlayer | None") -> discord.Embed:
    embed = discord.Embed(title="Очередь", colour=STATUS_COLORS["track"])
    if player is None or player.current is None:
        embed.description = "Сейчас ничего не играет."
        return embed

    embed.add_field(
        name="Сейчас" + (" (пауза)" if player.paused else ""),
        value=player.current.title[:200],
        inline=False,
    )
    items = list(player.queue)[:20]
    if items:
        text = "\n".join(f"`{i}.` {track.title[:70]}" for i, track in enumerate(items, 1))
        if len(player.queue) > len(items):
            text += f"\n…и ещё {len(player.queue) - len(items)}"
        embed.add_field(
            name=f"Дальше — {len(player.queue)}", value=text[:1024], inline=False
        )
    else:
        embed.add_field(name="Дальше", value="пусто", inline=False)
    return embed


class SearchModal(discord.ui.Modal, title="Поиск в Яндекс.Музыке"):
    query: discord.ui.TextInput = discord.ui.TextInput(
        label="Название или исполнитель", max_length=120
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        bot = interaction.client
        try:
            items = await bot.yandex.search(str(self.query), limit=10)
        except Exception as exc:
            await interaction.followup.send(f"Поиск не удался: {exc}", ephemeral=True)
            return
        if not items:
            await interaction.followup.send("Ничего не нашёл.", ephemeral=True)
            return
        await interaction.edit_original_response(
            content="Выбери трек — он встанет в очередь:",
            view=TrackSelect(items),
        )


class UrlModal(discord.ui.Modal, title="Добавить по ссылке"):
    url: discord.ui.TextInput = discord.ui.TextInput(
        label="Ссылка на трек, альбом или плейлист", max_length=400
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        bot = interaction.client
        voice = getattr(interaction.user, "voice", None)
        if voice is None or voice.channel is None:
            await interaction.followup.send(
                "Сначала зайди в голосовой канал.", ephemeral=True
            )
            return
        try:
            tracks, truncated, is_yandex = await resolve_tracks(
                str(self.url),
                interaction.user.display_name,
                int(bot.config.get("max_tracks_per_link") or 0),
            )
        except Exception as exc:
            await interaction.followup.send(
                f"Не смог разобрать ссылку: {exc}", ephemeral=True
            )
            return

        player = bot.get_player(interaction.guild)
        try:
            await player.ensure_voice(voice.channel)
        except Exception as exc:
            await interaction.followup.send(
                f"Не смог зайти в канал: {exc}", ephemeral=True
            )
            return

        for track in tracks:
            player.enqueue(track)

        text = f"➕ Добавил треков: {len(tracks)}"
        if truncated:
            text += " (сработал лимит)"
        if is_yandex and not bot.yandex.has_token:
            text += "\n⚠️ Без токена играют 30-секундные превью"
        await interaction.edit_original_response(content=text)


class PlayerPanel(discord.ui.View):
    """Одна панель на все функции. custom_id фиксированные, живёт после рестарта."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        data = interaction.data or {}
        log.info("panel check: custom_id=%s", data.get("custom_id"))
        return True

    async def on_error(
        self, interaction: discord.Interaction, error: Exception, item
    ) -> None:
        log.exception("Ошибка панели", exc_info=error)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(f"Ошибка: {error}", ephemeral=True)
            else:
                await interaction.response.send_message(
                    f"Ошибка: {error}", ephemeral=True
                )
        except Exception:
            pass

    # -- ряд 0: управление ------------------------------------------------

    @discord.ui.button(
        label="Пауза",
        emoji="⏯",
        style=discord.ButtonStyle.primary,
        custom_id="panel:toggle",
        row=0,
    )
    async def toggle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.handle_panel_action(interaction, "toggle")

    @discord.ui.button(
        label="Пропустить",
        emoji="⏭",
        style=discord.ButtonStyle.secondary,
        custom_id="panel:skip",
        row=0,
    )
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.handle_panel_action(interaction, "skip")

    @discord.ui.button(
        label="Стоп",
        emoji="⏹",
        style=discord.ButtonStyle.danger,
        custom_id="panel:stop",
        row=0,
    )
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.handle_panel_action(interaction, "stop")

    @discord.ui.button(
        label="Очистить",
        emoji="🧹",
        style=discord.ButtonStyle.secondary,
        custom_id="panel:clear",
        row=0,
    )
    async def clear(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.handle_panel_action(interaction, "clear")

    @discord.ui.button(
        label="Перемешать",
        emoji="🔀",
        style=discord.ButtonStyle.secondary,
        custom_id="panel:shuffle",
        row=0,
    )
    async def shuffle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.handle_panel_action(interaction, "shuffle")

    # -- ряд 1: радио ------------------------------------------------------

    @discord.ui.select(
        placeholder="Радиостанция",
        custom_id="panel:radio",
        row=1,
        options=[
            discord.SelectOption(label="Soundpark Deep", value="soundpark", emoji="📻"),
            discord.SelectOption(label="Свое FM", value="svoefm", emoji="📻"),
            discord.SelectOption(label="Выключить радио", value="off", emoji="⏹"),
        ],
    )
    async def radio_select(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ) -> None:
        await interaction.client.handle_panel_action(interaction, "radio")

    # -- ряд 2: громкость --------------------------------------------------

    @discord.ui.select(
        placeholder="Громкость",
        custom_id="panel:volume",
        row=2,
        options=[
            discord.SelectOption(label=f"{value}%", value=str(value))
            for value in (0, 25, 50, 75, 100, 125, 150)
        ],
    )
    async def volume_select(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ) -> None:
        await interaction.client.handle_panel_action(interaction, "volume")

    # -- ряд 3: поиск, ссылка, очередь -------------------------------------

    @discord.ui.button(
        label="Поиск",
        emoji="🔎",
        style=discord.ButtonStyle.secondary,
        custom_id="panel:search",
        row=3,
    )
    async def search_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.handle_panel_action(interaction, "search")

    @discord.ui.button(
        label="Ссылка",
        emoji="🔗",
        style=discord.ButtonStyle.secondary,
        custom_id="panel:url",
        row=3,
    )
    async def url_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.handle_panel_action(interaction, "url")

    @discord.ui.button(
        label="Очередь",
        emoji="📋",
        style=discord.ButtonStyle.secondary,
        custom_id="panel:queue",
        row=3,
    )
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.client.handle_panel_action(interaction, "queue")

    # -- ряд 4: волны Яндекс.Музыки ---------------------------------------

    @discord.ui.select(
        placeholder="Волна Яндекс.Музыки",
        custom_id="panel:wave",
        row=4,
        options=[
            discord.SelectOption(label="Моя волна", value="my", emoji="🌊"),
            discord.SelectOption(label="Рок", value="rock"),
            discord.SelectOption(label="Поп", value="pop"),
            discord.SelectOption(label="Рэп", value="rap"),
            discord.SelectOption(label="Электроника", value="electronic"),
            discord.SelectOption(label="Джаз", value="jazz"),
            discord.SelectOption(label="Спокойное", value="calm"),
            discord.SelectOption(label="Весёлое", value="happy"),
            discord.SelectOption(label="Выключить волну", value="off", emoji="⏹"),
        ],
    )
    async def wave_select(
        self, interaction: discord.Interaction, select: discord.ui.Select
    ) -> None:
        await interaction.client.handle_panel_action(interaction, "wave")


class TrackSelect(discord.ui.View):
    """Выпадающий список результатов поиска."""

    def __init__(self, items: list[dict]) -> None:
        super().__init__(timeout=180)
        self.items = items
        options = []
        for item in items[:25]:
            duration = item.get("duration") or 0
            description = None
            if duration:
                total = duration // 1000
                description = f"{total // 60}:{total % 60:02d}"
            options.append(
                discord.SelectOption(
                    label=item["title"][:100], value=item["id"], description=description
                )
            )
        select = discord.ui.Select(placeholder="Выбери трек", options=options)
        select.callback = self._on_pick
        self.add_item(select)

    async def _on_pick(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer()
        values = getattr(interaction, "data", {}).get("values") or []
        track_id = values[0] if values else None
        item = next((i for i in self.items if i["id"] == track_id), None)
        if item is None:
            await interaction.edit_original_response(content="Трек не найден.")
            return

        voice = getattr(interaction.user, "voice", None)
        if voice is None or voice.channel is None:
            await interaction.edit_original_response(
                content="Сначала зайди в голосовой канал."
            )
            return

        bot = interaction.client
        player = bot.get_player(interaction.guild)
        try:
            await player.ensure_voice(voice.channel)
        except Exception as exc:
            await interaction.edit_original_response(content=f"Не смог зайти в канал: {exc}")
            return

        player.enqueue(
            Track(
                title=item["title"],
                url="",
                live=False,
                requester=interaction.user.display_name,
                yandex_id=item["id"],
            )
        )
        await interaction.edit_original_response(
            content=f"➕ Добавил: **{item['title']}**"
        )
        await bot.ensure_panel(interaction.guild, interaction.channel)


# --------------------------------------------------------------------------
# Бот
# --------------------------------------------------------------------------

class RadioBot(discord.Client):
    def __init__(self, config: dict, message_content: bool = True):
        intents = discord.Intents.default()
        intents.message_content = message_content
        intents.voice_states = True
        intents.guilds = True
        super().__init__(intents=intents)

        self.config = config
        self.tree = app_commands.CommandTree(self)
        self.players: dict[int, GuildPlayer] = {}
        self.ffmpeg = config.get("ffmpeg_path") or shutil.which("ffmpeg") or "ffmpeg"
        self.ffmpeg_log = open_ffmpeg_log()
        self.yandex = YANDEX
        self.yandex.set_token(str(config.get("yandex_token") or ""))
        self.panel_view = PlayerPanel()
        self.panels: dict[int, dict] = self._load_panels()
        self._panel_tasks: dict[int, asyncio.Task] = {}
        self._panel_handled: set[int] = set()
        self._panel_pending: dict[int, int] = {}
        self._panel_move_tasks: dict[int, asyncio.Task] = {}
        self._synced = False
        self.tree.on_error = self.on_tree_error
        setup_commands(self)

    # -- панель ------------------------------------------------------------

    def _load_panels(self) -> dict[int, dict]:
        try:
            if PANEL_PATH.exists():
                data = json.loads(PANEL_PATH.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return {int(k): v for k, v in data.items() if isinstance(v, dict)}
        except (OSError, ValueError):
            pass
        return {}

    def _save_panels(self) -> None:
        try:
            PANEL_PATH.write_text(
                json.dumps(
                    {str(k): v for k, v in self.panels.items()},
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass

    async def ensure_panel(self, guild: discord.Guild, channel) -> None:
        """Создаёт панель в канале, если её ещё нет."""
        target_id = int(self.config.get("panel_channel_id") or 0)
        if target_id:
            resolved = guild.get_channel(target_id)
            if resolved is not None:
                channel = resolved
        if channel is None or not hasattr(channel, "send"):
            return
        if guild.id in self.panels:
            await self.refresh_panel(guild, force=True)
            return
        player = self.player_or_none(guild)
        try:
            message = await channel.send(
                embed=build_panel_embed(player), view=self.panel_view
            )
        except Exception:
            log.debug("Не смог отправить панель", exc_info=True)
            return
        await self._pin_panel(message)
        self.panels[guild.id] = {
            "channel_id": channel.id,
            "message_id": message.id,
        }
        self._save_panels()

    async def _pin_panel(self, message: discord.Message) -> None:
        try:
            await message.pin(reason="Панель плеера")
            log.info("Панель закреплена в канале %s", message.channel)
        except discord.Forbidden:
            log.warning(
                "Не смог закрепить панель: нужно право «Управление сообщениями»"
            )
        except Exception:
            log.debug("Не смог закрепить панель", exc_info=True)

    async def refresh_panel(self, guild: discord.Guild, force: bool = False) -> None:
        ref = self.panels.get(guild.id)
        if not ref:
            return
        channel = guild.get_channel(ref.get("channel_id", 0))
        if channel is None:
            try:
                channel = await self.fetch_channel(ref.get("channel_id", 0))
            except Exception:
                self.panels.pop(guild.id, None)
                self._save_panels()
                return
        player = self.player_or_none(guild)
        try:
            message = await channel.fetch_message(ref["message_id"])
            await message.edit(embed=build_panel_embed(player), view=self.panel_view)
        except (discord.NotFound, discord.Forbidden):
            current = self.panels.get(guild.id)
            if current and current.get("message_id") == ref.get("message_id"):
                self.panels.pop(guild.id, None)
                self._save_panels()
        except Exception:
            log.debug("Не смог обновить панель", exc_info=True)

    def schedule_panel_update(self, guild: discord.Guild) -> None:
        if guild.id not in self.panels:
            return
        task = self._panel_tasks.get(guild.id)
        if task is not None and not task.done():
            return
        self._panel_tasks[guild.id] = asyncio.create_task(self._panel_later(guild))

    async def _panel_later(self, guild: discord.Guild) -> None:
        await asyncio.sleep(0.5)
        await self.refresh_panel(guild)

    async def move_panel_to_bottom(self, guild: discord.Guild) -> None:
        """Переотправляет панель, чтобы она снова оказалась внизу канала."""
        ref = self.panels.get(guild.id)
        if not ref:
            return
        channel = guild.get_channel(ref.get("channel_id", 0))
        if channel is None:
            return

        # Уже последнее сообщение — двигать некуда.
        if channel.last_message_id == ref.get("message_id"):
            return

        player = self.player_or_none(guild)
        try:
            message = await channel.send(
                embed=build_panel_embed(player), view=self.panel_view
            )
        except Exception:
            log.debug("Не смог переотправить панель", exc_info=True)
            return

        old_id = ref.get("message_id")
        self.panels[guild.id] = {"channel_id": channel.id, "message_id": message.id}
        self._save_panels()
        await self._pin_panel(message)

        if old_id:
            try:
                old = await channel.fetch_message(old_id)
                await old.delete()
            except Exception:
                pass

    def schedule_panel_move(self, guild: discord.Guild) -> None:
        """Перенос панели вниз с задержкой: пачка сообщений = один перенос."""
        task = self._panel_move_tasks.get(guild.id)
        if task is not None and not task.done():
            return
        self._panel_move_tasks[guild.id] = asyncio.create_task(
            self._panel_move_later(guild)
        )

    async def _panel_move_later(self, guild: discord.Guild) -> None:
        await asyncio.sleep(1.2)
        await self.move_panel_to_bottom(guild)

    # -- служебное ---------------------------------------------------------

    def get_player(self, guild: discord.Guild) -> GuildPlayer:
        player = self.players.get(guild.id)
        if player is None:
            player = GuildPlayer(self, guild)
            player.on_change = lambda: self.schedule_panel_update(guild)
            self.players[guild.id] = player
        return player

    def player_or_none(self, guild: discord.Guild | None) -> GuildPlayer | None:
        if guild is None:
            return None
        return self.players.get(guild.id)

    async def sync_commands(self) -> None:
        gid = int(self.config.get("guild_id") or 0)
        try:
            if gid:
                target = discord.Object(id=gid)
                self.tree.copy_global_to(guild=target)
                await self.tree.sync(guild=target)
                log.info("Слэш-команды синхронизированы для сервера %s", gid)
            else:
                for guild in self.guilds:
                    self.tree.copy_global_to(guild=guild)
                    await self.tree.sync(guild=guild)
                log.info(
                    "Слэш-команды синхронизированы для %d сервер(ов)",
                    len(self.guilds),
                )
        except Exception:
            log.exception("Не удалось синхронизировать слэш-команды")

    async def on_tree_error(
        self, interaction: discord.Interaction, error: Exception
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            text = f"Слишком часто, подожди {error.retry_after:.0f} с."
        else:
            log.exception("Ошибка команды", exc_info=error)
            text = f"Ошибка: {error}"
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except Exception:
            pass

    # -- события -----------------------------------------------------------

    async def setup_hook(self) -> None:
        # Постоянные кнопки панели работают и после перезапуска.
        self.add_view(self.panel_view)
        store = getattr(self._connection, "_view_store", None)
        views = getattr(store, "_views", {})
        log.info(
            "Панель: кнопок %d, зарегистрировано в store: %s",
            len(self.panel_view.children),
            {str(k): len(v) for k, v in views.items()},
        )

    async def close(self) -> None:
        try:
            await self.yandex.close()
        except Exception:
            pass
        await super().close()

    async def on_interaction(self, interaction: discord.Interaction) -> None:
        try:
            data = interaction.data or {}
            log.info(
                "interaction: type=%s custom_id=%s name=%s user=%s lat=%.2f",
                interaction.type,
                data.get("custom_id"),
                data.get("name"),
                getattr(interaction.user, "id", "?"),
                self.latency,
            )
        except Exception:
            log.debug("не смог залогировать interaction", exc_info=True)

        if interaction.type is discord.InteractionType.application_command:
            # После команды панель тоже должна оказаться ниже её ответа.
            threshold = int(self.config.get("panel_repost_after") or 0)
            if threshold > 0 and interaction.guild is not None:
                ref = self.panels.get(interaction.guild.id)
                if ref and interaction.channel_id == ref.get("channel_id"):
                    self.schedule_panel_move(interaction.guild)

        if interaction.type is discord.InteractionType.component:
            custom_id = (interaction.data or {}).get("custom_id") or ""
            if custom_id.startswith("panel:"):
                await self.handle_panel_action(interaction, custom_id.split(":", 1)[1])
                return
            asyncio.create_task(self._ensure_ack(interaction))

    async def handle_panel_action(
        self, interaction: discord.Interaction, action: str
    ) -> None:
        if interaction.id in self._panel_handled:
            return
        self._panel_handled.add(interaction.id)
        if len(self._panel_handled) > 500:
            self._panel_handled.clear()
            self._panel_handled.add(interaction.id)

        # Модалка должна быть первым ответом — до defer.
        if action == "search":
            await interaction.response.send_modal(SearchModal())
            return
        if action == "url":
            await interaction.response.send_modal(UrlModal())
            return

        try:
            await interaction.response.defer(ephemeral=True)
        except Exception:
            pass

        guild = interaction.guild
        player = self.player_or_none(guild)
        values = (interaction.data or {}).get("values") or []
        value = values[0] if values else ""

        if action == "radio":
            await self._panel_radio(interaction, value)
            return

        if action == "wave":
            await self._panel_wave(interaction, value)
            return

        if guild is None or player is None or player.vc is None:
            await self._panel_say(interaction, "Я не в голосовом канале. Включи `/radio`.")
            return

        if action == "toggle":
            state = player.toggle_pause()
            await self._panel_say(
                interaction,
                {
                    "paused": "⏸ Пауза",
                    "resumed": "▶️ Продолжаю",
                    "idle": "Сейчас нечего ставить на паузу",
                }.get(state, state),
            )
        elif action == "skip":
            player.skip()
            await self._panel_say(interaction, "⏭ Пропустил")
        elif action == "stop":
            await player.shutdown()
            await self._panel_say(interaction, "⏹ Остановил и вышел из канала")
        elif action == "clear":
            await self._panel_say(
                interaction, f"🧹 Убрал из очереди: {player.clear_queue()}"
            )
        elif action == "shuffle":
            await self._panel_say(
                interaction, f"🔀 Перемешал: {player.shuffle()} трек(ов)"
            )
        elif action == "volume":
            try:
                level = max(0, min(150, int(value)))
            except (TypeError, ValueError):
                level = 60
            player.volume = level / 100
            source = getattr(player.vc, "source", None)
            if isinstance(source, discord.PCMVolumeTransformer):
                source.volume = player.volume
            self.config["volume"] = level
            save_config(self.config)
            await self._panel_say(interaction, f"🔊 Громкость: {level}%")
        elif action == "queue":
            await interaction.followup.send(
                embed=build_queue_embed(player), ephemeral=True
            )
            return
        else:
            await self._panel_say(interaction, "Неизвестная кнопка")
            return

        await self.refresh_panel(guild, force=True)

    async def _panel_wave(self, interaction: discord.Interaction, value: str) -> None:
        guild = interaction.guild
        player = self.get_player(guild)

        if value == "off":
            player.stop_wave()
            player.queue.clear()
            player.skip()
            await self._panel_say(interaction, "🌊 Волна выключена")
            await self.refresh_panel(guild, force=True)
            return

        wave = WAVES.get(value)
        if wave is None:
            await self._panel_say(interaction, "Неизвестная волна")
            return

        voice = getattr(interaction.user, "voice", None)
        if voice is None or voice.channel is None:
            await self._panel_say(interaction, "Сначала зайди в голосовой канал.")
            return

        try:
            await player.ensure_voice(voice.channel)
        except Exception as exc:
            await self._panel_say(interaction, f"Не смог зайти в канал: {exc}")
            return

        name, station = wave
        player.play_wave(name, station)
        await self._panel_say(interaction, f"🌊 Волна: {name}")
        await self.refresh_panel(guild, force=True)

    async def _panel_radio(self, interaction: discord.Interaction, value: str) -> None:
        guild = interaction.guild
        player = self.get_player(guild)

        if value == "off":
            player.radio = None
            if player.current is not None and player.current.live:
                player.skip()
            await self._panel_say(interaction, "📻 Радио выключено")
            await self.refresh_panel(guild, force=True)
            return

        station = STATIONS.get(value)
        if station is None:
            await self._panel_say(interaction, "Неизвестная станция")
            return

        voice = getattr(interaction.user, "voice", None)
        if voice is None or voice.channel is None:
            await self._panel_say(interaction, "Сначала зайди в голосовой канал.")
            return

        try:
            await player.ensure_voice(voice.channel)
        except Exception as exc:
            await self._panel_say(interaction, f"Не смог зайти в канал: {exc}")
            return

        name, url = station
        player.play_radio(
            Track(
                title=name,
                url=url,
                live=True,
                requester=interaction.user.display_name,
            )
        )
        await self._panel_say(interaction, f"📻 Включил {name}")
        await self.refresh_panel(guild, force=True)

    async def _panel_say(self, interaction: discord.Interaction, text: str) -> None:
        try:
            await interaction.followup.send(text, ephemeral=True)
        except Exception:
            pass

    async def _ensure_ack(self, interaction: discord.Interaction) -> None:
        """Страховка: если обработчик не подтвердил нажатие за 2 с, делаем это сами."""
        await asyncio.sleep(2.0)
        try:
            if interaction.response.is_done():
                return
            await interaction.response.defer()
            log.warning(
                "interaction %s не был подтверждён обработчиком — подтвердил принудительно",
                (interaction.data or {}).get("custom_id"),
            )
        except Exception:
            pass

    async def on_ready(self) -> None:
        log.info("Бот вошёл как %s (id %s)", self.user, self.user.id)
        log.info(
            "Message Content Intent: %s",
            "включён" if self.intents.message_content else "ВЫКЛЮЧЕН (музыка из чата не работает)",
        )
        log.info(
            "Яндекс.Музыка: %s",
            "токен загружен, полные треки"
            if self.yandex.has_token
            else "без токена — только превью 30 с",
        )
        if not self._synced:
            self._synced = True
            await self.sync_commands()

        perms = discord.Permissions(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            add_reactions=True,
            connect=True,
            speak=True,
        )
        invite = discord.utils.oauth_url(
            self.user.id, permissions=perms, scopes=("bot", "applications.commands")
        )
        log.info("Ссылка для приглашения бота:\n%s", invite)

        channel_id = int(self.config.get("music_channel_id") or 0)
        if channel_id:
            log.info("Слушаю текстовый канал для музыки: %s", channel_id)
        else:
            log.info("Канал для музыки не задан (/setchannel или music_channel_id)")

    async def on_guild_join(self, guild: discord.Guild) -> None:
        try:
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        except Exception:
            log.exception("Не смог синхронизировать команды на новом сервере")

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        # Держим панель внизу канала: считаем сообщения после неё.
        ref = self.panels.get(message.guild.id)
        if ref and message.channel.id == ref.get("channel_id"):
            threshold = int(self.config.get("panel_repost_after") or 0)
            if threshold > 0:
                pending = self._panel_pending.get(message.guild.id, 0) + 1
                if pending >= threshold:
                    self._panel_pending[message.guild.id] = 0
                    self.schedule_panel_move(message.guild)
                else:
                    self._panel_pending[message.guild.id] = pending

        channel_id = int(self.config.get("music_channel_id") or 0)
        if not channel_id or message.channel.id != channel_id:
            return

        urls = [u.rstrip(TRAILING_JUNK) for u in URL_RE.findall(message.content)]
        for att in message.attachments:
            ctype = (att.content_type or "").lower()
            fname = att.filename.lower()
            if ctype.startswith("audio/") or ctype.startswith("video/") or any(
                fname.endswith(ext) for ext in AUDIO_EXTS
            ):
                urls.append(att.url)

        if not urls:
            return

        player = self.get_player(message.guild)

        # Если бот нигде не сидит — по желанию заходим к автору сообщения.
        if player.vc is None and self.config.get("auto_join", True):
            voice = getattr(message.author, "voice", None)
            if voice and voice.channel:
                try:
                    await player.ensure_voice(voice.channel)
                except Exception as exc:
                    log.warning("Не смог зайти в канал: %s", exc)

        if player.vc is None:
            notice = await message.reply(
                "Я не в голосовом канале. Зайди в канал и включи `/radio`.",
                mention_author=False,
            )
            asyncio.create_task(self._delete_later(notice))
            return

        added = 0
        need_token = False
        truncated = False
        limit = int(self.config.get("max_tracks_per_link") or 0)
        for url in urls[:5]:
            try:
                tracks, was_truncated, is_yandex = await resolve_tracks(
                    url, message.author.display_name, limit
                )
                for track in tracks:
                    player.enqueue(track)
                added += len(tracks)
                truncated = truncated or was_truncated
                if is_yandex and not self.yandex.has_token:
                    need_token = True
            except Exception as exc:
                log.warning("Не смог разобрать %s: %s", url, exc)
                try:
                    await message.add_reaction("⚠️")
                except Exception:
                    pass

        if added:
            log.info("Из чата добавлено треков: %d", added)
            try:
                await message.add_reaction("✅")
            except Exception:
                pass

            notes = []
            if truncated:
                notes.append(
                    f"сработал лимит {limit} треков на ссылку (max_tracks_per_link)"
                )
            if need_token:
                notes.append(
                    "токена Яндекс.Музыки нет — играют 30-секундные превью, "
                    "полные треки: /yandextoken"
                )
            if notes:
                try:
                    notice = await message.reply(
                        "ℹ️ " + "; ".join(notes) + ".", mention_author=False
                    )
                    asyncio.create_task(self._delete_later(notice))
                except Exception:
                    pass

    async def _delete_later(
        self, message: discord.Message, seconds: float = 25.0
    ) -> None:
        await asyncio.sleep(seconds)
        try:
            await message.delete()
        except Exception:
            pass

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if self.user is None:
            return

        if member.id != self.user.id:
            # В канал зашёл человек — возможно, пора включить E2EE.
            if member.bot or after.channel is None:
                return
            player = self.player_or_none(member.guild)
            if player is None or player.vc is None:
                return
            if player.vc.channel != after.channel:
                return
            player.on_listener_join()
            return

        if before.channel is None or after.channel is not None:
            return  # это не отключение бота

        player = self.player_or_none(member.guild)
        if player is None or player._stopped:
            return
        if not (player.radio or player.queue):
            return
        log.warning("Бота выкинуло из канала — пробую вернуться")
        await player.reconnect(before.channel)


# --------------------------------------------------------------------------
# Слэш-команды
# --------------------------------------------------------------------------

def _station_choices() -> list[app_commands.Choice[str]]:
    return [app_commands.Choice(name=name, value=key) for key, (name, _) in STATIONS.items()]


def setup_commands(bot: RadioBot) -> None:
    tree = bot.tree

    async def _require_voice(
        interaction: discord.Interaction,
    ) -> discord.VoiceChannel | None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Эта команда работает только на сервере.", ephemeral=True)
            return None
        voice = interaction.user.voice
        if voice is None or voice.channel is None:
            await interaction.followup.send(
                "Сначала зайди в голосовой канал, потом повтори команду.", ephemeral=True)
            return None
        perms = voice.channel.permissions_for(interaction.guild.me)
        if not (perms.connect and perms.speak):
            await interaction.followup.send(
                "У меня нет прав `Connect`/`Speak` в этом канале. Выдай права и повтори.", ephemeral=True)
            return None
        return voice.channel

    # ------------------------------------------------------------------ /radio
    @tree.command(name="radio", description="Включить радио в голосовом канале")
    @app_commands.describe(
        station="Радиостанция",
        url="Своя ссылка на поток (если указана — важнее станции)",
    )
    @app_commands.choices(station=_station_choices())
    async def radio(
        interaction: discord.Interaction,
        station: app_commands.Choice[str] | None = None,
        url: str | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        channel = await _require_voice(interaction)
        if channel is None:
            return

        key = station.value if station else next(iter(STATIONS))
        name, station_url = STATIONS[key]
        if url:
            stream_url, title = url.strip(), "Своя ссылка"
        else:
            stream_url, title = station_url, name

        if not stream_url.startswith(("http://", "https://")):
            await interaction.followup.send(
                "Ссылка должна начинаться с `http://` или `https://`.", ephemeral=True)
            return

        player = bot.get_player(interaction.guild)
        try:
            await player.ensure_voice(channel)
        except Exception as exc:
            log.exception("Не смог подключиться к голосовому каналу")
            await interaction.followup.send(f"Не смог зайти в канал: {exc}", ephemeral=True)
            return

        player.play_radio(
            Track(
                title=title,
                url=stream_url,
                live=True,  # радио всегда живой поток: переподключаем при обрыве
                requester=interaction.user.display_name,
            )
        )

        embed = discord.Embed(
            title="📻 Радио включено",
            description=f"**{title}**\n`{stream_url}`",
            colour=STATUS_COLORS["radio"],
        )
        embed.add_field(name="Канал", value=channel.mention, inline=True)
        embed.add_field(name="Громкость", value=f"{round(player.volume * 100)}%", inline=True)
        embed.set_footer(text="Остановить: /stop · Перезапустить поток: /skip")
        await interaction.followup.send(embed=embed, ephemeral=True)

        await bot.ensure_panel(interaction.guild, interaction.channel)

    # ------------------------------------------------------------------- /play
    @tree.command(name="play", description="Поставить трек по ссылке в очередь")
    @app_commands.describe(url="Прямая ссылка на аудио, YouTube или SoundCloud")
    async def play(interaction: discord.Interaction, url: str) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        channel = await _require_voice(interaction)
        if channel is None:
            return

        limit = int(bot.config.get("max_tracks_per_link") or 0)
        try:
            tracks, truncated, is_yandex = await resolve_tracks(
                url, interaction.user.display_name, limit
            )
        except Exception as exc:
            log.warning("Не смог разобрать %s: %s", url, exc)
            await interaction.followup.send(f"Не смог разобрать ссылку: {exc}", ephemeral=True)
            return

        player = bot.get_player(interaction.guild)
        try:
            await player.ensure_voice(channel)
        except Exception as exc:
            await interaction.followup.send(f"Не смог зайти в канал: {exc}", ephemeral=True)
            return

        for track in tracks:
            player.enqueue(track)

        if len(tracks) == 1:
            text = f"➕ **{tracks[0].title}**"
        else:
            text = f"➕ Добавил треков: **{len(tracks)}**"
        if truncated:
            text += f" (сработал лимит {limit})"
        if player.radio:
            text += "\nРадио вернётся после очереди."
        else:
            text += f"\nВ очереди: {len(player.queue)}."
        if is_yandex and not bot.yandex.has_token:
            text += (
                "\n⚠️ Без токена Яндекс.Музыки играют 30-секундные превью: `/yandextoken`."
            )
        await interaction.followup.send(text, ephemeral=True)

        await bot.ensure_panel(interaction.guild, interaction.channel)

    # ------------------------------------------------------------------- /stop
    @tree.command(name="stop", description="Остановить музыку и выйти из канала")
    async def stop(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        player = bot.player_or_none(interaction.guild)
        if player is None or player.vc is None:
            await interaction.followup.send("Я и так не в голосовом канале.", ephemeral=True)
            return
        await player.shutdown()
        await interaction.followup.send("⏹ Остановлено, вышел из канала.", ephemeral=True)

    # ------------------------------------------------------------------ /panel
    @tree.command(name="panel", description="Панель плеера с кнопками")
    async def panel(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send("Только на сервере.", ephemeral=True)
            return

        old = bot.panels.pop(interaction.guild.id, None)
        if old:
            channel = interaction.guild.get_channel(old.get("channel_id", 0))
            if channel is not None:
                try:
                    message = await channel.fetch_message(old.get("message_id", 0))
                    await message.delete()
                except Exception:
                    pass
            bot._save_panels()

        await interaction.followup.send("🎛 Панель плеера готова.", ephemeral=True)
        await bot.ensure_panel(interaction.guild, interaction.channel)
        bot._panel_pending[interaction.guild.id] = 0

    # ------------------------------------------------------------------ /queue
    @tree.command(name="queue", description="Показать очередь")
    async def queue_cmd(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        player = bot.player_or_none(interaction.guild)
        if player is None or (player.current is None and not player.queue):
            await interaction.followup.send("Очередь пуста.", ephemeral=True)
            return

        embed = discord.Embed(title="Очередь", colour=STATUS_COLORS["track"])
        if player.current is not None:
            embed.add_field(
                name="Сейчас" + (" (пауза)" if player.paused else ""),
                value=player.current.title[:200],
                inline=False,
            )
        items = list(player.queue)[:20]
        if items:
            text = "\n".join(f"`{i}.` {t.title[:70]}" for i, t in enumerate(items, 1))
            if len(player.queue) > len(items):
                text += f"\n…и ещё {len(player.queue) - len(items)}"
            embed.add_field(
                name=f"Дальше — {len(player.queue)}", value=text[:1024], inline=False
            )
        else:
            embed.add_field(name="Дальше", value="пусто", inline=False)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ----------------------------------------------------------------- /search
    @tree.command(name="search", description="Найти трек в Яндекс.Музыке")
    @app_commands.describe(query="Название или исполнитель")
    async def search_cmd(interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            items = await bot.yandex.search(query, limit=10)
        except Exception as exc:
            await interaction.followup.send(f"Поиск не удался: {exc}", ephemeral=True)
            return
        if not items:
            await interaction.followup.send("Ничего не нашёл.", ephemeral=True)
            return
        await interaction.followup.send(
            "Выбери трек — он встанет в очередь:",
            view=TrackSelect(items),
            ephemeral=True,
        )

    # -------------------------------------------------------------------- /wave
    @tree.command(name="wave", description="Включить волну Яндекс.Музыки")
    @app_commands.describe(kind="Какая волна (по умолчанию — Моя волна)")
    @app_commands.choices(
        kind=[
            app_commands.Choice(name=name, value=key)
            for key, (name, _) in WAVES.items()
        ]
    )
    async def wave_cmd(
        interaction: discord.Interaction,
        kind: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        channel = await _require_voice(interaction)
        if channel is None:
            return

        key = kind.value if kind else "my"
        name, station = WAVES[key]

        player = bot.get_player(interaction.guild)
        try:
            await player.ensure_voice(channel)
        except Exception as exc:
            await interaction.followup.send(
                f"Не смог зайти в канал: {exc}", ephemeral=True
            )
            return

        player.play_wave(name, station)
        await interaction.followup.send(f"🌊 Волна: **{name}**", ephemeral=True)
        await bot.ensure_panel(interaction.guild, interaction.channel)

    # ------------------------------------------------------------------ /clear
    @tree.command(
        name="clear",
        description="Очистить очередь, не останавливая текущий трек",
    )
    async def clear(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        player = bot.player_or_none(interaction.guild)
        if player is None or not player.queue:
            await interaction.followup.send("Очередь уже пуста.", ephemeral=True)
            return
        removed = player.clear_queue()
        await interaction.followup.send(
            f"🧹 Очистил очередь: убрал {removed} трек(ов). Текущий играет дальше.", ephemeral=True)

    # ------------------------------------------------------------------- /skip
    @tree.command(name="skip", description="Пропустить трек или перезапустить поток")
    async def skip(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        player = bot.player_or_none(interaction.guild)
        if player is None or player.vc is None or player.current is None:
            await interaction.followup.send("Сейчас ничего не играет.", ephemeral=True)
            return
        was_radio = player.current.live
        player.skip()
        if was_radio:
            await interaction.followup.send("🔄 Поток перезапущен.", ephemeral=True)
        else:
            await interaction.followup.send("⏭ Пропустил трек.", ephemeral=True)

    # ------------------------------------------------------------------ /test
    @tree.command(name="test", description="Проверка звука: 8 секунд тона 440 Гц")
    async def test(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        channel = await _require_voice(interaction)
        if channel is None:
            return
        player = bot.get_player(interaction.guild)
        try:
            await player.ensure_voice(channel)
        except Exception as exc:
            await interaction.followup.send(f"Не смог зайти в канал: {exc}", ephemeral=True)
            return
        player.play_tone()
        await interaction.followup.send(
            "🔔 Играю тон 440 Гц 8 секунд. Если слышно — звук от бота доходит.", ephemeral=True)

    # ----------------------------------------------------------------- /status
    @tree.command(name="status", description="Что играет сейчас")
    async def status(interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        player = bot.player_or_none(interaction.guild)
        if player is None or player.current is None:
            await interaction.followup.send("Сейчас ничего не играет.", ephemeral=True)
            return

        embed = discord.Embed(
            title="Сейчас в эфире",
            description=player.status_text(),
            colour=STATUS_COLORS["radio"] if player.current.live else STATUS_COLORS["track"],
        )
        if player.vc and player.vc.channel:
            embed.add_field(name="Канал", value=player.vc.channel.mention)
        if player.started_at:
            minutes, seconds = divmod(int(time.monotonic() - player.started_at), 60)
            hours, minutes = divmod(minutes, 60)
            embed.add_field(name="Играет", value=f"{hours:02d}:{minutes:02d}:{seconds:02d}")
        embed.add_field(name="Громкость", value=f"{round(player.volume * 100)}%")
        embed.add_field(
            name="Яндекс.Музыка",
            value="токен есть" if bot.yandex.has_token else "только превью 30 с",
        )
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ----------------------------------------------------------------- /volume
    @tree.command(name="volume", description="Громкость бота в процентах")
    @app_commands.describe(level="0–150, по умолчанию 60")
    async def volume(
        interaction: discord.Interaction,
        level: app_commands.Range[int, 0, 150],
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send("Только на сервере.", ephemeral=True)
            return
        player = bot.get_player(interaction.guild)
        player.volume = level / 100
        if player.vc and player.vc.source and isinstance(
            player.vc.source, discord.PCMVolumeTransformer
        ):
            player.vc.source.volume = player.volume
        bot.config["volume"] = level
        save_config(bot.config)
        await interaction.followup.send(f"🔊 Громкость: {level}%", ephemeral=True)

    # ------------------------------------------------------------- /setchannel
    @tree.command(
        name="setchannel",
        description="Из какого текстового канала брать музыку",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(channel="Канал (пусто = текущий, без канала = выключить)")
    async def setchannel(
        interaction: discord.Interaction,
        channel: discord.TextChannel | None = None,
    ) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        if interaction.guild is None:
            await interaction.followup.send("Только на сервере.", ephemeral=True)
            return

        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await interaction.followup.send("Выбери обычный текстовый канал.", ephemeral=True)
            return

        perms = target.permissions_for(interaction.guild.me)
        if not (perms.view_channel and perms.read_message_history):
            await interaction.followup.send(
                f"У меня нет доступа к {target.mention}: нужны `View Channel` и `Read Message History`.", ephemeral=True)
            return

        bot.config["music_channel_id"] = target.id
        save_config(bot.config)
        await interaction.followup.send(
            f"✅ Беру музыку из {target.mention}.\n"
            "Кидай туда ссылку — она попадёт в очередь. Для HLS-ссылок (`m3u8`) трек считается живым потоком.", ephemeral=True)

    # ------------------------------------------------------------ /yandextoken
    @tree.command(
        name="yandextoken",
        description="Токен Яндекс.Музыки — включает полные треки вместо превью",
    )
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.describe(
        token="OAuth-токен из music.yandex.ru (можно вместе со словом OAuth)"
    )
    async def yandextoken(interaction: discord.Interaction, token: str) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        login = await bot.yandex.validate_token(token)
        if not login:
            await interaction.followup.send(
                "❌ Токен не подошёл. Скопируй его целиком из заголовка "
                "`Authorization: OAuth ...` (или из ссылки после `access_token=`).",
                ephemeral=True,
            )
            return

        clean = token.strip()
        if clean.lower().startswith("oauth "):
            clean = clean[6:].strip()
        bot.yandex.set_token(clean)
        bot.config["yandex_token"] = clean
        save_config(bot.config)
        await interaction.followup.send(
            f"✅ Токен принят, аккаунт **{login}**. Полные треки включены, "
            "перезапуск не нужен.",
            ephemeral=True,
        )

    # --------------------------------------------------------------- /stations
    @tree.command(name="stations", description="Список встроенных радиостанций")
    async def stations(interaction: discord.Interaction) -> None:
        lines = [
            f"**{name}** — `{key}`\n{url}"
            for key, (name, url) in STATIONS.items()
        ]
        embed = discord.Embed(
            title="Встроенные станции",
            description="\n\n".join(lines),
            colour=STATUS_COLORS["radio"],
        )
        embed.set_footer(text="/radio station:<станция> · своя ссылка: /radio url:<ссылка>")
        await interaction.response.send_message(embed=embed, ephemeral=True)


# --------------------------------------------------------------------------
# Самопроверка и запуск
# --------------------------------------------------------------------------

def run_selftest(ffmpeg: str) -> None:
    print(f"Python:      {sys.version.split()[0]}")
    print(f"discord.py:  {discord.__version__}")
    print(f"FFmpeg:      {ffmpeg}")
    try:
        import nacl  # noqa: F401

        print("PyNaCl:      ok")
    except ImportError:
        print("PyNaCl:      НЕ УСТАНОВЛЕН → pip install \"discord.py[voice]\"")

    ok = True
    for _key, (name, url) in STATIONS.items():
        print(f"\nПроверяю {name}\n  {url}", flush=True)
        cmd = (
            [ffmpeg]
            + shlex.split(LIVE_BEFORE)
            + [
                "-i", url,
                "-t", "2",
                "-vn",
                "-f", "s16le",
                "-ar", "48000",
                "-ac", "2",
                "pipe:1",
            ]
        )
        try:
            proc = subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=40
            )
            if proc.returncode != 0:
                err = proc.stderr.decode("utf-8", errors="replace").strip()
                raise RuntimeError(err[-400:] or f"код возврата {proc.returncode}")
            if len(proc.stdout) < 48_000:
                raise RuntimeError("FFmpeg не отдал звук")
            print(f"  ok, {len(proc.stdout)} байт PCM за 2 секунды", flush=True)
        except Exception as exc:
            ok = False
            print(f"  ОШИБКА: {exc}", flush=True)

    print("\nИтог:", "всё готово" if ok else "есть проблемы (см. выше)", flush=True)
    if not ok:
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description="Discord radio bot")
    parser.add_argument(
        "--selftest", action="store_true", help="проверить ffmpeg и потоки, не подключаясь к Discord"
    )
    args = parser.parse_args()

    setup_logging()
    config = load_config()

    ffmpeg = config.get("ffmpeg_path") or shutil.which("ffmpeg")
    if not ffmpeg:
        log.error(
            "FFmpeg не найден. Установи его и добавь в PATH, либо пропиши ffmpeg_path в config.json."
        )
        sys.exit(1)
    config["ffmpeg_path"] = ffmpeg

    if args.selftest:
        run_selftest(ffmpeg)
        return

    if not CONFIG_PATH.exists():
        save_config(config)
        log.info("Создал config.json — впиши туда токен бота.")

    token = (config.get("token") or "").strip()
    if not token or token in ("PASTE_TOKEN_HERE", "YOUR_TOKEN"):
        log.error(
            "Токен не задан. Открой config.json и вставь токен в поле \"token\" "
            "(или задай переменную окружения DISCORD_TOKEN)."
        )
        sys.exit(1)

    message_content = True
    while True:
        bot = RadioBot(config, message_content=message_content)
        try:
            bot.run(token, log_handler=None)
            break
        except discord.LoginFailure:
            log.error("Discord не принял токен. Скопируй его заново в Bot → Reset Token.")
            sys.exit(1)
        except discord.PrivilegedIntentsRequired:
            if not message_content:
                log.error(
                    "Discord отклонил подключение из-за интентов. Проверь настройки приложения."
                )
                sys.exit(1)
            message_content = False
            log.error(
                "MESSAGE CONTENT INTENT выключен в Developer Portal — запускаю без чтения чата.\n"
                "  Чтобы бот брал музыку из канала: Developer Portal → приложение → Bot →\n"
                "  Privileged Gateway Intents → Message Content Intent → Save, затем перезапуск.\n"
                "  Слэш-команды и радио работают уже сейчас."
            )
            try:
                if bot.ffmpeg_log:
                    bot.ffmpeg_log.close()
            except Exception:
                pass
        except KeyboardInterrupt:
            break


if __name__ == "__main__":
    main()
