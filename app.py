import os
import sys
import warnings
import re
import asyncio
import logging
import tempfile
import shutil
import hashlib
import base64
import html
import time
from datetime import datetime
from typing import Any
import dotenv
from dotenv import load_dotenv
load_dotenv()
import aiohttp
import asyncpg
import yt_dlp
try:
    import browser_cookie3
except ImportError:
    browser_cookie3 = None
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import (
    Message, CallbackQuery, ChatMemberUpdated,
    InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile
)
from aiogram.filters import Command
from aiogram.enums import ChatMemberStatus, ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramConflictError, TelegramNetworkError
from aiogram.client.default import DefaultBotProperties

warnings.filterwarnings("ignore", category=RuntimeWarning, message=".*ffmpeg.*")

try:
    from shazamio import Shazam
except ImportError:
    Shazam = None

try:
    import speech_recognition as sr
except ImportError:
    sr = None


def can_use_shazam() -> bool:
    if Shazam is None:
        return False
    try:
        import audioop  # noqa: F401
        return True
    except ImportError:
        return False


shazam_client = Shazam() if can_use_shazam() else None

# ========================== CONFIG ==========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "8860093565:AAEYUKIC_dNOPeKSeoksM0L9YE9QzFu1mrE")
ADMIN_ID = int(os.getenv("ADMIN_ID", "7961099561"))
DATABASE_URL = (
    "postgresql://postgres:NWthCzkTirkhOLywbKwWlwXrnOfiqjSO"
    "@turntable.proxy.rlwy.net:14314/railway"
)

# YouTube Data API v3 — qidirish uchun (cookies kerak emas, bepul)
# Olish: https://console.cloud.google.com/apis/credentials
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")

# YouTube proxy (ixtiyoriy)
YOUTUBE_PROXY = os.getenv("YOUTUBE_PROXY", "")

AUTO_REFRESH_COOKIES = os.getenv("AUTO_REFRESH_COOKIES", "0").lower() in ("1", "true", "yes")
COOKIE_REFRESH_INTERVAL_HOURS = int(os.getenv("COOKIE_REFRESH_INTERVAL_HOURS", "6"))
BROWSER_COOKIE_SOURCES = [b.strip() for b in os.getenv("BROWSER_COOKIE_SOURCES", "chrome,edge,firefox,chromium").split(",") if b.strip()]

MAX_SIZE_MB = 50
RESULTS_PER_PAGE = 10

SHARE_PROMO_CAPTION = (
    "❤️ @skachatinstavideo_bot orqali istagan musiqangizni tez va oson toping! 🚀"
)
BOT_USERNAME = "skachatinstavideo_bot"
# ========================== UNIFIED COOKIE HELPER ==========================
# Barcha platformalar uchun BIR cookie fayl
COOKIE_FILE = os.path.join(os.getcwd(), "cookies.txt")


def _is_youtube_cookiefile_valid(path: str) -> bool:
    try:
        with open(path, 'r', encoding='utf-8') as f:
            cookie_content = f.read()
        has_youtube = '.youtube.com' in cookie_content
        has_youtube_login = any(token in cookie_content for token in ('LOGIN_INFO', 'SID', 'SAPISID', 'APISID', 'HSID', 'SSID'))
        return has_youtube and has_youtube_login
    except Exception:
        return False


def _write_netscape_cookiejar(jar, path: str) -> bool:
    try:
        with open(path, 'w', encoding='utf-8') as f:
            f.write("# Netscape HTTP Cookie File\n")
            for cookie in jar:
                domain = cookie.domain or ""
                flag = "TRUE" if domain.startswith('.') else "FALSE"
                cookie_path = cookie.path or "/"
                secure = "TRUE" if getattr(cookie, 'secure', False) else "FALSE"
                expires = str(int(cookie.expires)) if getattr(cookie, 'expires', None) else "0"
                f.write("\t".join([
                    domain,
                    flag,
                    cookie_path,
                    secure,
                    expires,
                    cookie.name,
                    cookie.value,
                ]) + "\n")
        return True
    except Exception as e:
        logging.warning(f"[Cookies] Netscape cookie faylini yozishda xatolik: {e}")
        return False


def refresh_youtube_cookiefile() -> bool:
    if browser_cookie3 is None:
        logging.warning("[Cookies] browser_cookie3 mavjud emas, cookie refresh uchun o'rnatish kerak.")
        return False

    for source in BROWSER_COOKIE_SOURCES:
        getter = getattr(browser_cookie3, source, None)
        if not callable(getter):
            continue

        try:
            jar = getter(domain_name='youtube.com')
            if not jar:
                continue

            if any('youtube.com' in getattr(cookie, 'domain', '') for cookie in jar):
                if _write_netscape_cookiejar(jar, COOKIE_FILE) and _is_youtube_cookiefile_valid(COOKIE_FILE):
                    logging.info(f"[Cookies] YouTube cookies browserdan yangilandi: {source}")
                    return True
        except Exception as e:
            logging.warning(f"[Cookies] {source} browser cookie olishda xato: {e}")
            continue

    logging.error("[Cookies] Browserdan YouTube cookies topilmadi yoki yozib bo'lmadi.")
    return False


def get_cookiefile() -> str | None:
    """Agar cookies.txt mavjud bo'lsa, yo'lini qaytaradi, aks holda None.
    Instagram cookies faqat sessionid bo'lsa ishlatiladi."""
    if os.path.exists(COOKIE_FILE) and os.path.getsize(COOKIE_FILE) > 100:
        try:
            with open(COOKIE_FILE, 'r', encoding='utf-8') as f:
                cookie_content = f.read()

            # YouTube cookies borligini tekshirish
            has_youtube = '.youtube.com' in cookie_content
            has_youtube_login = 'LOGIN_INFO' in cookie_content or 'SID' in cookie_content

            # Instagram cookies tekshirish
            has_instagram = '.instagram.com' in cookie_content
            has_instagram_session = 'sessionid' in cookie_content.lower()

            if has_instagram and not has_youtube and not has_instagram_session:
                logging.info("[Cookies] Instagram cookies mavjud lekin sessionid yo'q. Anonim so'rov ishlatiladi.")
                return None

            if has_youtube and not has_youtube_login:
                if AUTO_REFRESH_COOKIES and refresh_youtube_cookiefile():
                    return COOKIE_FILE
                return None

            if AUTO_REFRESH_COOKIES:
                age = time.time() - os.path.getmtime(COOKIE_FILE)
                if age > COOKIE_REFRESH_INTERVAL_HOURS * 3600:
                    if refresh_youtube_cookiefile():
                        return COOKIE_FILE

            return COOKIE_FILE
        except Exception:
            if AUTO_REFRESH_COOKIES and refresh_youtube_cookiefile():
                return COOKIE_FILE
            return COOKIE_FILE if os.path.exists(COOKIE_FILE) else None

    if AUTO_REFRESH_COOKIES and refresh_youtube_cookiefile():
        return COOKIE_FILE

    return None


def normalize_search_query(query: str) -> str:
    """Musiqa qidiruv so'rovini tozalash va fayl nomi bo'yicha noto'g'ri so'rovlarni qisqartirish."""
    query = query or ""
    query = re.sub(r'[_\-\.\+]+', ' ', query)
    query = re.sub(
        r'\b(?:mp4|webm|mkv|mov|m4v|jpg|jpeg|png|webp|video|audio|download|instagram|insta|reel|tv|post)\b',
        ' ',
        query,
        flags=re.I,
    )
    query = re.sub(r'\d+', ' ', query)
    query = re.sub(r'\s+', ' ', query).strip()
    return query


def build_query_from_filename(filename: str) -> str | None:
    base_name = os.path.splitext(os.path.basename(filename))[0]
    base_name = re.sub(r'^(?:ig_embed_|ig_api_|dl_nocook_|dl_|yt_|audio_|video_|\d+_)+', '', base_name, flags=re.I)
    base_name = normalize_search_query(base_name)
    return base_name if len(base_name) >= 3 else None



# ========================== HELPERS ==========================
def build_share_kb() -> InlineKeyboardMarkup:
    """Faqat guruhga qo'shish knopkasi (rasm uchun)"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="👥 Guruhga qo'shish",
                url=f"https://t.me/{BOT_USERNAME}?startgroup=true"
            ),
        ]
    ])


def build_video_kb(user_id: int, lang: str) -> InlineKeyboardMarkup:
    """Video uchun: Qo'shiqni yuklab olish + Guruhga qo'shish"""
    if lang == "uz_kr":
        dl_text = "🎵 Қўшиқни юклаб олиш"
    elif lang == "ru":
        dl_text = "🎵 Скачать музыку"
    else:
        dl_text = "🎵 Qo'shiqni yuklab olish"

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text=dl_text,
                callback_data=f"dl_music:{user_id}"
            ),
        ],
        [
            InlineKeyboardButton(
                text="👥 Guruhga qo'shish",
                url=f"https://t.me/{BOT_USERNAME}?startgroup=true"
            ),
        ]
    ])


def format_duration(seconds) -> str:
    if not seconds:
        return "?"
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def find_ffmpeg_cmd() -> str | None:
    try:
        import static_ffmpeg
        static_ffmpeg.add_paths()
    except ImportError:
        pass
    return shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")


async def check_subscriptions(bot: Bot, user_id: int, channels: list) -> bool:
    if not channels:
        return True
    for ch in channels:
        try:
            member = await bot.get_chat_member(chat_id=f"@{ch['username']}", user_id=user_id)
            if member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.KICKED):
                return False
        except Exception:
            return False
    return True


async def search_youtube_tracks(query: str, max_results: int = 15) -> list:
    """
    Musiqa qidirish (cookies kerak emas):
    1. YouTube Data API v3 (YOUTUBE_API_KEY bo'lsa)
    2. yt-dlp ytsearch (fallback, cookies yo'q)
    3. Piped API (oxirgi fallback)
    """
    # 1. YouTube Data API v3
    if YOUTUBE_API_KEY:
        results = await _search_youtube_api(query, max_results)
        if results:
            logging.info(f"[Search] YouTube API: {len(results)} ta natija")
            return results
        logging.warning("[Search] YouTube API natija qaytarmadi, yt-dlp ga o'tmoqda...")

    # 2. yt-dlp ytsearch (cookies yo'q)
    results = await asyncio.to_thread(_search_ytdlp, query, max_results)
    if results:
        logging.info(f"[Search] yt-dlp ytsearch: {len(results)} ta natija")
        return results
    logging.warning("[Search] yt-dlp ytsearch ishlamadi, Piped ga o'tmoqda...")

    # 3. Piped fallback
    results = await search_piped(query, max_results)
    if results:
        logging.info(f"[Search] Piped: {len(results)} ta natija")
    return results


async def _search_youtube_api(query: str, max_results: int = 15) -> list:
    """YouTube Data API v3 orqali qidirish — COOKIES KERAK EMAS"""
    try:
        params = {
            "part": "snippet",
            "q": query,
            "type": "video",
            "videoCategoryId": "10",   # Music kategoriyasi
            "maxResults": min(max_results, 50),
            "key": YOUTUBE_API_KEY,
            "fields": "items(id/videoId,snippet(title,channelTitle))",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://www.googleapis.com/youtube/v3/search",
                params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status == 403:
                    data = await resp.json()
                    reason = data.get("error", {}).get("errors", [{}])[0].get("reason", "")
                    if reason in ("quotaExceeded", "dailyLimitExceeded"):
                        logging.warning("[YouTube API] Kunlik limit tugadi, yt-dlp ga o'tmoqda")
                    else:
                        logging.warning(f"[YouTube API] 403: {reason}")
                    return []
                if resp.status != 200:
                    logging.warning(f"[YouTube API] Status {resp.status}")
                    return []
                data = await resp.json()

        items = data.get("items", [])
        if not items:
            return []

        # Duration olish uchun videos.list so'rov (batch)
        video_ids = [it["id"]["videoId"] for it in items if it.get("id", {}).get("videoId")]
        durations = {}
        if video_ids:
            dur_params = {
                "part": "contentDetails",
                "id": ",".join(video_ids),
                "key": YOUTUBE_API_KEY,
                "fields": "items(id,contentDetails/duration)",
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    "https://www.googleapis.com/youtube/v3/videos",
                    params=dur_params,
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp2:
                    if resp2.status == 200:
                        dur_data = await resp2.json()
                        for v in dur_data.get("items", []):
                            vid_id = v["id"]
                            iso = v.get("contentDetails", {}).get("duration", "PT0S")
                            durations[vid_id] = _parse_iso_duration(iso)

        results = []
        for it in items:
            vid_id = it.get("id", {}).get("videoId", "")
            if not vid_id:
                continue
            snippet = it.get("snippet", {})
            results.append({
                "id": vid_id,
                "title": snippet.get("title", "Noma'lum"),
                "uploader": snippet.get("channelTitle", "Noma'lum"),
                "duration": durations.get(vid_id, 0),
                "url": f"https://www.youtube.com/watch?v={vid_id}",
                "thumbnail": f"https://i.ytimg.com/vi/{vid_id}/mqdefault.jpg",
            })
        return results
    except Exception as e:
        logging.error(f"[YouTube API] Xatolik: {e}")
        return []


def _parse_iso_duration(iso: str) -> int:
    """PT3M45S → 225 sekund"""
    try:
        m = re.match(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", iso)
        if not m:
            return 0
        h = int(m.group(1) or 0)
        mi = int(m.group(2) or 0)
        s = int(m.group(3) or 0)
        return h * 3600 + mi * 60 + s
    except Exception:
        return 0


def _search_ytdlp(query: str, max_results: int = 15) -> list:
    """yt-dlp orqali YouTube qidirish — cookies kerak emas"""
    try:
        ffmpeg_cmd = find_ffmpeg_cmd()
        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "noplaylist": True,
            "socket_timeout": 20,
            "retries": 2,
            "extractor_args": {
                "youtube": {
                    "player_client": ["android_vr"],
                    "formats": "missing_pot",
                }
            },
        }
        if ffmpeg_cmd:
            opts["ffmpeg_location"] = os.path.dirname(ffmpeg_cmd)
        if YOUTUBE_PROXY:
            opts["proxy"] = YOUTUBE_PROXY

        search_url = f"ytsearch{max_results}:{query}"
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search_url, download=False)
            if not info or "entries" not in info:
                return []
            results = []
            for entry in info["entries"]:
                if not entry:
                    continue
                vid_id = entry.get("id", "")
                results.append({
                    "id": vid_id,
                    "title": entry.get("title", "Noma'lum"),
                    "uploader": entry.get("uploader") or entry.get("channel", "Noma'lum"),
                    "duration": entry.get("duration", 0),
                    "url": entry.get("webpage_url") or (f"https://www.youtube.com/watch?v={vid_id}" if vid_id else ""),
                    "thumbnail": entry.get("thumbnail", ""),
                })
            return results
    except Exception as e:
        logging.error(f"[Search] yt-dlp ytsearch xatolik: {e}")
        return []






# ========================== PIPED API ==========================
PIPED_INSTANCES = [
    "https://api.piped.projectsegfault.com",
    "https://pipedapi.moomoo.me",
    "https://pipedapi.adminforge.de",
    "https://api.piped.privacydev.net",
    "https://pipedapi.mha.fi",
    "https://api.piped.privacy.com.de",
]

async def search_piped(query: str, max_results: int = 15) -> list:
    """
    Piped API orqali YouTube'dan qidirish - cookies kerak emas
    """
    encoded_query = aiohttp.helpers.quote(query)
    
    for instance in PIPED_INSTANCES:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{instance}/search",
                    params={"q": query, "filter": "music_songs"},
                    timeout=aiohttp.ClientTimeout(total=15),
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Accept": "application/json",
                    }
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        items = data.get("items", [])
                        
                        results = []
                        for item in items[:max_results]:
                            results.append({
                                "id": item.get("url", "").split("v=")[-1] if "v=" in item.get("url", "") else item.get("url", "").split("/")[-1],
                                "title": item.get("title", "Noma'lum"),
                                "uploader": item.get("uploaderName", "Noma'lum"),
                                "duration": item.get("duration", 0),
                                "url": item.get("url", ""),
                                "thumbnail": item.get("thumbnail", ""),
                            })
                        
                        if results:
                            logging.info(f"[Piped] {instance} dan {len(results)} ta natija topildi")
                            return results
                        
        except Exception as e:
            logging.warning(f"[Piped] {instance} xatolik: {e}")
            continue
    
    logging.error("[Piped] Barcha instancelar ishlamadi")
    return []


async def get_piped_audio_url(video_id: str) -> tuple[str | None, dict | None]:
    """
    Piped API orqali audio stream URL va metadata olish
    """
    for instance in PIPED_INSTANCES:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{instance}/streams/{video_id}",
                    timeout=aiohttp.ClientTimeout(total=15),
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Accept": "application/json",
                    }
                ) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        
                        # Audio streams dan eng yaxshisini tanlash
                        audio_streams = data.get("audioStreams", [])
                        if not audio_streams:
                            logging.warning(f"[Piped] {video_id} uchun audio stream topilmadi")
                            continue
                        
                        # Eng yuqori sifatli audio stream
                        best_audio = max(audio_streams, key=lambda x: x.get("bitrate", 0))
                        audio_url = best_audio.get("url")
                        
                        if audio_url:
                            metadata = {
                                "title": data.get("title", "Noma'lum"),
                                "uploader": data.get("uploader", "Noma'lum"),
                                "thumbnail": data.get("thumbnailUrl", ""),
                                "duration": data.get("duration", 0),
                            }
                            logging.info(f"[Piped] Audio URL topildi: {instance}")
                            return audio_url, metadata
                        
        except Exception as e:
            logging.warning(f"[Piped] {instance} streams xatolik: {e}")
            continue
    
    logging.error(f"[Piped] {video_id} uchun audio URL topilmadi")
    return None, None


async def download_piped_audio(audio_url: str, filename: str) -> str | None:
    """
    Piped audio stream URL dan fayl yuklash
    """
    def _download():
        safe = re.sub(r'[\\/*?:"<>|]', "_", filename)
        output_path = os.path.join(tempfile.gettempdir(), f"piped_{safe}.mp3")
        
        try:
            import urllib.request
            
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "*/*",
                "Referer": "https://www.youtube.com/",
            }
            
            req = urllib.request.Request(audio_url, headers=headers)
            
            with urllib.request.urlopen(req, timeout=60) as response:
                with open(output_path, 'wb') as f:
                    # Chunklarda yuklash
                    while True:
                        chunk = response.read(8192)
                        if not chunk:
                            break
                        f.write(chunk)
            
            if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                file_size = os.path.getsize(output_path) / (1024 * 1024)
                logging.info(f"[Piped] Audio yuklandi: {output_path} ({file_size:.1f} MB)")
                return output_path
            
            return None
            
        except Exception as e:
            logging.error(f"[Piped] Yuklash xatosi: {e}")
            return None
    
    return await asyncio.to_thread(_download)

async def download_youtube_audio(url: str, filename: str) -> str | None:
    """
    YouTube'dan audio yuklash:
    1. Piped API orqali urinish
    2. Piped ishlamasa — yt-dlp (download_youtube_audio_sync) ga fallback
    """
    # --- 1. Piped orqali urinish ---
    video_id = None
    patterns = [
        r'(?:v=|\/)([0-9A-Za-z_-]{11}).*',
        r'youtu\.be\/([0-9A-Za-z_-]{11})',
        r'youtube\.com\/embed\/([0-9A-Za-z_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            video_id = match.group(1)
            break
    if not video_id and len(url) == 11 and re.match(r'^[0-9A-Za-z_-]+$', url):
        video_id = url

    if video_id:
        try:
            audio_url, metadata = await get_piped_audio_url(video_id)
            if audio_url:
                safe_filename = filename or (metadata.get("title", video_id) if metadata else video_id)
                audio_path = await download_piped_audio(audio_url, safe_filename)
                if audio_path and os.path.exists(audio_path):
                    logging.info(f"[Audio] Piped orqali muvaffaqiyatli yuklandi: {audio_path}")
                    return audio_path
                logging.warning("[Audio] Piped audio fayl yuklashda muvaffaqiyatsiz, yt-dlp ga o'tmoqda...")
            else:
                logging.warning("[Audio] Piped audio URL topilmadi, yt-dlp ga o'tmoqda...")
        except Exception as e:
            logging.warning(f"[Audio] Piped xatolik: {e}, yt-dlp ga o'tmoqda...")
    else:
        logging.warning(f"[Audio] Video ID ajratib olinmadi ({url}), yt-dlp bilan to'g'ridan-to'g'ri urinish...")

    # --- 2. yt-dlp fallback ---
    logging.info(f"[Audio] yt-dlp orqali yuklanmoqda: {url}")
    return await asyncio.to_thread(download_youtube_audio_sync, url, filename)


# Eski nom saqlanadi (boshqa joylarda ishlatilmasin uchun)
_download_youtube_audio_piped_only = download_youtube_audio


def _is_youtube_blocking_response(file_path: str) -> bool:
    """YouTube bot-block yoki login sahifasini aniqlash"""
    if not os.path.exists(file_path):
        return False
    
    try:
        with open(file_path, 'rb') as f:
            header = f.read(2048)  # Birinchi 2KB tekshirish
        
        # HTML/XML sahifasi (bot-block yoki login page)
        if header.startswith(b'<!DOCTYPE') or header.startswith(b'<html') or b'<HTML' in header[:100]:
            logging.warning(f"[Blocking] HTML page qaytdi (YouTube blocking yoki login)")
            return True
        
        # Recaptcha/challenge sahifasi
        if b'recaptcha' in header or b'challenge' in header or b'Sign in' in header:
            logging.warning(f"[Blocking] Recaptcha/Sign-in page detected")
            return True
        
        return False
    except Exception:
        return False


def _detect_youtube_error(error_str: str) -> str:
    """YouTube xatosini turini aniqlash va qayta yangilash kerakligini bilish"""
    error_lower = error_str.lower()
    
    # Bot-block xatolari
    if 'sign in' in error_lower or 'login' in error_lower:
        return "LOGIN_REQUIRED"
    if '403' in error_str or 'forbidden' in error_lower:
        return "FORBIDDEN"
    if '429' in error_str or 'too many requests' in error_lower:
        return "RATE_LIMIT"
    if 'unavailable' in error_lower or 'not available' in error_lower:
        return "UNAVAILABLE"
    if 'blocked' in error_lower or 'captcha' in error_lower:
        return "BOT_BLOCKED"
    
    return "UNKNOWN"


def _should_refresh_cookies_on_error(error_type: str) -> bool:
    """Xatosiga qarab cookies yangilash kerakligini aniqlash"""
    refresh_on = {'LOGIN_REQUIRED', 'FORBIDDEN', 'BOT_BLOCKED', 'RATE_LIMIT'}
    return error_type in refresh_on


def download_youtube_audio_sync(url: str, filename: str) -> str | None:
    """
    YouTube audio yuklash — AUTO-REFRESH COOKIES.
    Xatolar bo'lsa cookies avtomatik yangilash va qayta urinish.
    """
    safe = re.sub(r'[\\/*?:"<>|]', "_", filename)
    ffmpeg_cmd = find_ffmpeg_cmd()

    def _try(prefix: str, player_client: str, extra: dict = None) -> str | None:
        output_path = os.path.join(tempfile.gettempdir(), f"{prefix}_{safe}.%(ext)s")
        opts = {
            "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best[ext=mp4]/best",
            "outtmpl": output_path,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "ignoreerrors": False,
            "socket_timeout": 30,
            "retries": 3,
            "extractor_args": {
                "youtube": {
                    "player_client": [player_client],
                    "formats": "missing_pot",
                }
            },
            # Cookies HECH QACHON ishlatilmaydi
        }
        if ffmpeg_cmd:
            opts["ffmpeg_location"] = os.path.dirname(ffmpeg_cmd)
        if YOUTUBE_PROXY:
            opts["proxy"] = YOUTUBE_PROXY
        if extra:
            opts.update(extra)

        blocked_detected = False
        
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if not info:
                    return None

                downloaded = ydl.prepare_filename(info)
                mp3_path = downloaded.rsplit(".", 1)[0] + ".mp3"
                check_path = mp3_path if os.path.exists(mp3_path) else downloaded

                if os.path.exists(check_path):
                    # HTML page tekshirish (bot check)
                    if _is_youtube_blocking_response(check_path):
                        logging.warning(f"[Audio] {player_client}: YouTube BLOCKING detected (HTML page)")
                        blocked_detected = True
                        os.remove(check_path)
                        # Auto-refresh cookies agar AUTO_REFRESH_COOKIES enabled
                        if AUTO_REFRESH_COOKIES and refresh_youtube_cookiefile():
                            logging.info("[Audio] Cookies avtomatik yangilandi, qayta urinish...")
                            # Recursive urinish cookiefile bilan
                            return _try_with_cookies(prefix, player_client, extra)
                        return None
                    
                    if os.path.getsize(check_path) < 1024:
                        logging.warning(f"[Audio] {player_client}: Fayl juda kichik ({os.path.getsize(check_path)} bytes)")
                        with open(check_path, "rb") as fc:
                            content_sample = fc.read()
                        if b'<!DOCTYPE' in content_sample or b'<html' in content_sample or b'<HTML' in content_sample:
                            logging.warning(f"[Audio] {player_client}: Kichik HTML fayl - YouTube BLOCKING")
                            blocked_detected = True
                            os.remove(check_path)
                            if AUTO_REFRESH_COOKIES and refresh_youtube_cookiefile():
                                logging.info("[Audio] Cookies avtomatik yangilandi, qayta urinish...")
                                return _try_with_cookies(prefix, player_client, extra)
                        os.remove(check_path)
                        return None

                if os.path.exists(mp3_path):
                    logging.info(f"[Audio] {player_client}: muvaffaqiyatli → {mp3_path}")
                    return mp3_path
                if os.path.exists(downloaded):
                    logging.info(f"[Audio] {player_client}: muvaffaqiyatli → {downloaded}")
                    return downloaded

                # Fallback: temp papkada qidirish
                base = os.path.join(tempfile.gettempdir(), f"{prefix}_{safe}")
                for ext in [".mp3", ".m4a", ".webm", ".opus", ".mp4"]:
                    p = base + ext
                    if os.path.exists(p) and os.path.getsize(p) > 1024:
                        with open(p, "rb") as fc:
                            h = fc.read(50)
                        if not (h.startswith(b'<!DOCTYPE') or h.startswith(b'<html')):
                            logging.info(f"[Audio] {player_client}: fallback → {p}")
                            return p

                return None
        except Exception as e:
            err = str(e)
            error_type = _detect_youtube_error(err)
            
            if error_type in ("LOGIN_REQUIRED", "BOT_BLOCKED", "FORBIDDEN", "RATE_LIMIT"):
                logging.warning(f"[Audio] {player_client}: {error_type} — YouTube blocking detected")
                if AUTO_REFRESH_COOKIES and _should_refresh_cookies_on_error(error_type):
                    if refresh_youtube_cookiefile():
                        logging.info("[Audio] Cookies avtomatik yangilandi, qayta urinish...")
                        return _try_with_cookies(prefix, player_client, extra)
            
            if "403" in err:
                logging.warning(f"[Audio] {player_client}: 403 Forbidden")
            elif "Sign in" in err or "login" in err.lower():
                logging.warning(f"[Audio] {player_client}: Login talab qilindi")
            elif "unavailable" in err.lower() or "not available" in err.lower():
                logging.warning(f"[Audio] {player_client}: Video mavjud emas")
            else:
                logging.warning(f"[Audio] {player_client}: {err[:120]}")
            return None

    def _try_with_cookies(prefix: str, player_client: str, extra: dict = None) -> str | None:
        """Cookies bilan qayta urinish (1 marta)"""
        cookiefile = get_cookiefile()
        if not cookiefile:
            logging.warning("[Audio] Cookies refresh qilindi lekin fayl mavjud emas, anonim urinish...")
            return _try(f"{prefix}_retry", player_client, extra)
        
        output_path = os.path.join(tempfile.gettempdir(), f"{prefix}_ck_{safe}.%(ext)s")
        opts = {
            "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best[ext=mp4]/best",
            "outtmpl": output_path,
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "ignoreerrors": False,
            "socket_timeout": 30,
            "retries": 3,
            "cookiefile": cookiefile,
            "extractor_args": {
                "youtube": {
                    "player_client": [player_client],
                    "formats": "missing_pot",
                }
            },
        }
        if ffmpeg_cmd:
            opts["ffmpeg_location"] = os.path.dirname(ffmpeg_cmd)
        if YOUTUBE_PROXY:
            opts["proxy"] = YOUTUBE_PROXY
        if extra:
            opts.update(extra)

        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                if not info:
                    return None

                downloaded = ydl.prepare_filename(info)
                mp3_path = downloaded.rsplit(".", 1)[0] + ".mp3"
                check_path = mp3_path if os.path.exists(mp3_path) else downloaded

                if os.path.exists(check_path) and os.path.getsize(check_path) > 1024:
                    if not _is_youtube_blocking_response(check_path):
                        if os.path.exists(mp3_path):
                            logging.info(f"[Audio] {player_client} + cookies: MUVAFFAQIYATLI → {mp3_path}")
                            return mp3_path
                        if os.path.exists(downloaded):
                            logging.info(f"[Audio] {player_client} + cookies: MUVAFFAQIYATLI → {downloaded}")
                            return downloaded
                        os.remove(check_path) if os.path.exists(check_path) else None

                return None
        except Exception as e:
            logging.warning(f"[Audio] {player_client} + cookies: {str(e)[:100]}")
            return None

    # ============================================================
    # FAQAT COOKIES TALAB QILMAYDIGAN CLIENTLAR
    # ============================================================

    # 1. android_vr — PO Token ham, cookies ham talab qilmaydi
    result = _try("dl_vr", "android_vr")
    if result:
        return result

    # 2. tv_embedded — embed player, cookie-free
    result = _try("dl_tve", "tv_embedded")
    if result:
        return result

    # 3. mweb — mobil web, odatda cookie-free ishlaydi
    result = _try("dl_mweb", "mweb")
    if result:
        return result

    # 4. web_embedded — iframe embed, cookie-free
    result = _try("dl_emb", "web_embedded")
    if result:
        return result

    # 5. android — ko'pincha cookie-free ishlaydi
    result = _try("dl_and", "android")
    if result:
        return result

    cookiefile = get_cookiefile()
    if cookiefile:
        logging.info("[Audio] yt-dlp cookie bilan fallback urinish...")
        result = _download_youtube_audio_with_cookies(url, filename, cookiefile)
        if result:
            return result

    logging.error(f"[Audio] Barcha usullar ishlamadi: {url}")
    return None


def _download_youtube_audio_with_cookies(url: str, filename: str, cookiefile: str) -> str | None:
    safe = re.sub(r'[\\/*?:"<>|]', "_", filename)
    ffmpeg_cmd = find_ffmpeg_cmd()
    output_path = os.path.join(tempfile.gettempdir(), f"dl_ck_{safe}.%(ext)s")
    opts = {
        "format": "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best[ext=mp4]/best",
        "outtmpl": output_path,
        "postprocessors": [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": "mp3",
            "preferredquality": "192",
        }],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "ignoreerrors": False,
        "socket_timeout": 30,
        "retries": 3,
        "cookiefile": cookiefile,
        "extractor_args": {
            "youtube": {
                "player_client": ["android_vr"],
                "formats": "missing_pot",
            }
        },
    }
    if ffmpeg_cmd:
        opts["ffmpeg_location"] = os.path.dirname(ffmpeg_cmd)
    if YOUTUBE_PROXY:
        opts["proxy"] = YOUTUBE_PROXY

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if not info:
                return None

            downloaded = ydl.prepare_filename(info)
            mp3_path = downloaded.rsplit(".", 1)[0] + ".mp3"
            check_path = mp3_path if os.path.exists(mp3_path) else downloaded

            if os.path.exists(check_path):
                # Blocking tekshiruvi
                if _is_youtube_blocking_response(check_path) and os.path.getsize(check_path) < 100000:
                    logging.warning("[Audio] Cookie fallback: YouTube blocking detected")
                    if AUTO_REFRESH_COOKIES:
                        logging.info("[Audio] Cookies qayta yangilash urinish...")
                        if refresh_youtube_cookiefile():
                            logging.info("[Audio] Cookies yangilandi, lekin replay qila olmamiz (cookie fallback bosqichida)")
                    os.remove(check_path)
                    return None
                
                if os.path.getsize(check_path) > 1024:
                    if os.path.exists(mp3_path):
                        logging.info(f"[Audio] Cookie fallback: MUVAFFAQIYATLI → {mp3_path}")
                        return mp3_path
                    if os.path.exists(downloaded):
                        logging.info(f"[Audio] Cookie fallback: MUVAFFAQIYATLI → {downloaded}")
                        return downloaded
                os.remove(check_path) if os.path.exists(check_path) else None

            return None
    except Exception as e:
        err = str(e)
        error_type = _detect_youtube_error(err)
        
        if error_type in ("LOGIN_REQUIRED", "BOT_BLOCKED", "FORBIDDEN"):
            logging.warning(f"[Audio] Cookie fallback: {error_type} — cookies yaroqsiz yoki eskirgan")
            # Cookies yangilash urinish (lekin recursive call yo'q)
            if AUTO_REFRESH_COOKIES:
                logging.info("[Audio] Cookies qayta yangilash urinish...")
                if refresh_youtube_cookiefile():
                    logging.info("[Audio] Cookies yangilandi (replay uchun vaqt yo'q)")
        elif "403" in err:
            logging.warning("[Audio] Cookie fallback: 403 Forbidden")
        elif "Sign in" in err or "login" in err.lower():
            logging.warning("[Audio] Cookie fallback: Login talab qilindi")
        else:
            logging.warning(f"[Audio] Cookie fallback: {err[:120]}")
        return None


async def download_instagram_direct(url: str) -> tuple[str | None, dict[str, Any] | None]:
    """
    FIXED Instagram post yuklash - video va rasmni to'g'ri aniqlash bilan
    """
    def _download():
        import urllib.request
        import ssl
        import json

        # Post ID ajratib olish
        post_id = None
        patterns = [
            r'instagram\.com/(?:p|reel|tv|share)/([^/?#]+)',
            r'instagram\.com/reel/([^/?#]+)',
            r'instagram\.com/p/([^/?#]+)',
            r'instagram\.com/tv/([^/?#]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, url)
            if match:
                post_id = match.group(1)
                break

        if not post_id:
            logging.warning(f"[Instagram] Post ID ajratib olinmadi: {url}")
            return None, None

        logging.info(f"[Instagram] Post ID: {post_id}")

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        # ========== USUL 1: Embed sahifa ==========
        try:
            embed_url = f"https://www.instagram.com/p/{post_id}/embed/captioned/"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Referer': 'https://www.instagram.com/',
            }

            req = urllib.request.Request(embed_url, headers=headers)
            with urllib.request.urlopen(req, timeout=20, context=ctx) as response:
                html = response.read().decode('utf-8')

            logging.info(f"[Instagram] Embed sahifa yuklandi. Uzunlik: {len(html)}")

            media_url = None
            is_video = False
            title = None
            thumbnail_url = None

            # 1. JSON-LD dan olish
            ld_json = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, flags=re.S)
            if ld_json:
                try:
                    ld_data = json.loads(ld_json.group(1))
                    if isinstance(ld_data, dict):
                        if 'video' in ld_data and isinstance(ld_data['video'], dict):
                            media_url = ld_data['video'].get('contentUrl')
                            is_video = True
                            logging.info(f"[Instagram] JSON-LD video URL: {media_url[:80] if media_url else 'None'}...")
                        elif 'image' in ld_data and isinstance(ld_data['image'], str):
                            thumbnail_url = ld_data['image']
                            logging.info(f"[Instagram] JSON-LD image URL: {thumbnail_url[:80] if thumbnail_url else 'None'}...")
                        if 'caption' in ld_data:
                            title = ld_data['caption']
                except Exception as e:
                    logging.warning(f"[Instagram] JSON-LD parse xatolik: {e}")

            # 2. og:video dan olish
            if not media_url:
                og_video = re.search(r'<meta[^>]+property="og:video"[^>]+content="(https://[^"]+)"', html)
                if og_video:
                    media_url = og_video.group(1)
                    is_video = True
                    logging.info(f"[Instagram] og:video topildi: {media_url[:80]}...")

            # 3. og:video:secure_url
            if not media_url:
                og_video_secure = re.search(r'<meta[^>]+property="og:video:secure_url"[^>]+content="(https://[^"]+)"', html)
                if og_video_secure:
                    media_url = og_video_secure.group(1)
                    is_video = True

            # 4. video tag
            if not media_url:
                video_matches = re.findall(r'<video[^>]+(?:src|data-src)="(https://[^"]+)"', html)
                if video_matches:
                    media_url = video_matches[0]
                    is_video = True
                    logging.info(f"[Instagram] video tag dan topildi: {media_url[:80]}...")

            # 5. JS orqali video URL
            if not media_url:
                video_url_patterns = [
                    r'"video_url":"(https://[^"]+\.mp4[^"]*)"',
                    r'"video_url":"([^"]+)"',
                    r'src="(https://[^"]+instagram\.com[^"]+\.mp4[^"]*)"',
                ]
                for pattern in video_url_patterns:
                    match = re.search(pattern, html)
                    if match:
                        media_url = match.group(1).replace('\u0026', '&')
                        is_video = True
                        logging.info(f"[Instagram] JS video URL topildi: {media_url[:80]}...")
                        break

            # 6. Thumbnail (agar video topilmasa)
            if not media_url and not thumbnail_url:
                og_match = re.search(r'<meta[^>]+property="og:image"[^>]+content="(https://[^"]+scontent[^"]+)"', html)
                if og_match:
                    thumbnail_url = og_match.group(1)
                    logging.info(f"[Instagram] og:image topildi (thumbnail): {thumbnail_url[:80]}...")

            # ========== VIDEO YUKLASH ==========
            if media_url and is_video:
                media_url = media_url.replace('\u0026', '&').replace('&amp;', '&')
                output_path = os.path.join(tempfile.gettempdir(), f"ig_embed_{post_id}.mp4")

                req_media = urllib.request.Request(media_url, headers={
                    'User-Agent': headers['User-Agent'],
                    'Referer': 'https://www.instagram.com/',
                    'Accept': '*/*',
                })
                with urllib.request.urlopen(req_media, timeout=30, context=ctx) as response:
                    with open(output_path, 'wb') as f:
                        f.write(response.read())

                if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                    file_size = os.path.getsize(output_path)
                    logging.info(f"[Instagram] Fayl yuklandi: {file_size} bytes ({file_size/1024:.1f} KB)")

                    # ===== MUHIM FIX: Fayl haqiqatan video ekanligini tekshirish =====
                    with open(output_path, 'rb') as f_check:
                        header = f_check.read(12)

                    # MP4 signature tekshirish
                    is_real_video = (
                        header.startswith(b'\x00\x00\x00') and b'ftyp' in header[:20]
                    ) or b'ftypmp42' in header[:20]

                    # JPEG/PNG signature tekshirish (agar rasm bo'lsa)
                    is_image = header.startswith(b'\xff\xd8\xff') or header.startswith(b'\x89PNG')

                    if is_image:
                        logging.warning(f"[Instagram] Fayl rasm ekan (JPEG/PNG), video emas. Thumbnail sifatida ishlatiladi.")
                        new_path = output_path.rsplit('.', 1)[0] + '.jpg'
                        os.rename(output_path, new_path)
                        return new_path, {"title": title or f"Instagram {post_id}", "is_video": False}

                    if not is_real_video and file_size < 1024 * 1024:  # 1MB dan kichik va video emas
                        logging.warning(f"[Instagram] Fayl juda kichik ({file_size/1024:.1f} KB) va video emas. Boshqa usulga o'tish...")
                        os.remove(output_path)
                        media_url = None
                    else:
                        return output_path, {"title": title or f"Instagram {post_id}", "is_video": True}

            # ========== RASM YUKLASH (agar video topilmasa) ==========
            if thumbnail_url and not media_url:
                thumbnail_url = thumbnail_url.replace('\u0026', '&').replace('&amp;', '&')
                output_path = os.path.join(tempfile.gettempdir(), f"ig_embed_{post_id}.jpg")

                req_img = urllib.request.Request(thumbnail_url, headers={
                    'User-Agent': headers['User-Agent'],
                    'Referer': 'https://www.instagram.com/',
                })
                with urllib.request.urlopen(req_img, timeout=30, context=ctx) as response:
                    with open(output_path, 'wb') as f:
                        f.write(response.read())

                if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                    logging.info(f"[Instagram] Rasm yuklandi (video o'rniga)")
                    return output_path, {"title": title or f"Instagram {post_id}", "is_video": False}

        except Exception as e:
            logging.warning(f"[Instagram] Embed usuli xatolik: {e}")

        # ========== USUL 2: Post sahifasi ==========
        try:
            post_url = f"https://www.instagram.com/p/{post_id}/"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Referer': 'https://www.instagram.com/',
            }

            req = urllib.request.Request(post_url, headers=headers)
            with urllib.request.urlopen(req, timeout=20, context=ctx) as response:
                html = response.read().decode('utf-8', errors='ignore')

            media_url = None
            is_video = False

            ld_json = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, flags=re.S)
            if ld_json:
                try:
                    ld_data = json.loads(ld_json.group(1))
                    if isinstance(ld_data, dict):
                        if 'video' in ld_data and isinstance(ld_data['video'], dict):
                            media_url = ld_data['video'].get('contentUrl') or ld_data['video'].get('thumbnailUrl')
                            is_video = True
                        elif 'image' in ld_data and isinstance(ld_data['image'], str):
                            media_url = ld_data['image']
                except Exception:
                    pass

            if not media_url:
                og_match = re.search(r'<meta[^>]+property="og:image"[^>]+content="(https://[^"]+scontent[^"]+)"', html)
                if og_match:
                    media_url = og_match.group(1)

            if not media_url:
                og_video = re.search(r'<meta[^>]+property="og:video"[^>]+content="(https://[^"]+)"', html)
                if og_video:
                    media_url = og_video.group(1)
                    is_video = True

            if media_url:
                media_url = media_url.replace('\u0026', '&').replace('&amp;', '&')
                ext = '.mp4' if is_video else '.jpg'
                output_path = os.path.join(tempfile.gettempdir(), f"ig_post_{post_id}{ext}")

                req_media = urllib.request.Request(media_url, headers=headers)
                with urllib.request.urlopen(req_media, timeout=30, context=ctx) as response:
                    with open(output_path, 'wb') as f:
                        f.write(response.read())

                if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                    return output_path, {"title": f"Instagram {post_id}", "is_video": is_video}
        except Exception as e:
            logging.warning(f"[Instagram] Post page xatolik: {e}")

        logging.error(f"[Instagram] Barcha usullar ishlamadi. Post ID: {post_id}")
        return None, None

    return await asyncio.to_thread(_download)

async def download_instagram_embed(url: str) -> str | None:
    """
    Instagram embed sahifasi orqali video/rasm yuklash (cookies siz).
    Bu usul Instagram rate limit bo'lsa ishlaydi.
    """
    def _download_embed():
        import urllib.request
        import json

        try:
            # Post ID ni olish
            post_id = None
            patterns = [
                r'instagram\.com/(?:p|reel|tv)/([^/?]+)',
                r'instagram\.com/reel/([^/?]+)',
                r'instagram\.com/p/([^/?]+)',
                r'instagram\.com/tv/([^/?]+)',
                r'instagram\.com/share/([^/?]+)',
            ]
            for pattern in patterns:
                match = re.search(pattern, url)
                if match:
                    post_id = match.group(1)
                    break

            if not post_id:
                return None

            # Embed sahifasini ochish
            embed_url = f"https://www.instagram.com/p/{post_id}/embed/"

            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Referer': 'https://www.instagram.com/',
            }

            req = urllib.request.Request(embed_url, headers=headers)

            with urllib.request.urlopen(req, timeout=15) as response:
                html = response.read().decode('utf-8')

            # Video URL ni qidirish
            video_patterns = [
                r'"video_url":"([^"]+)"',
                r'"videoUrl":"([^"]+)"',
                r'<video[^>]+src="([^"]+)"',
                r'property="og:video" content="([^"]+)"',
                r'property="og:video:secure_url" content="([^"]+)"',
            ]

            video_url = None
            for pattern in video_patterns:
                match = re.search(pattern, html)
                if match:
                    video_url = match.group(1).replace('\u0026', '&')
                    break

            if video_url:
                # Video yuklash
                output_path = os.path.join(tempfile.gettempdir(), f"ig_embed_{post_id}.mp4")
                req_video = urllib.request.Request(video_url, headers=headers)
                with urllib.request.urlopen(req_video, timeout=30) as response:
                    with open(output_path, 'wb') as f:
                        f.write(response.read())
                if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                    return output_path

            # Rasm URL ni qidirish
            image_patterns = [
                r'"display_url":"([^"]+)"',
                r'property="og:image" content="([^"]+)"',
                r'<img[^>]+src="([^"]+)"[^>]*class="[^"]*EmbeddedMediaImage',
                r'<img[^>]+src="([^"]+)"[^>]*data-testid="[^"]*photo',
                r'"thumbnail_src":"([^"]+)"',
                r'"media_preview":"([^"]+)"',
                r'data-src="([^"]+)"',
            ]

            image_url = None
            for pattern in image_patterns:
                match = re.search(pattern, html)
                if match:
                    image_url = match.group(1).replace('\u0026', '&')
                    break

            if image_url:
                output_path = os.path.join(tempfile.gettempdir(), f"ig_embed_{post_id}.jpg")
                req_img = urllib.request.Request(image_url, headers=headers)
                with urllib.request.urlopen(req_img, timeout=30) as response:
                    with open(output_path, 'wb') as f:
                        f.write(response.read())
                if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                    return output_path

            return None

        except Exception as e:
            logging.warning(f"Instagram embed yuklashda xatolik: {e}")
            return None

    return await asyncio.to_thread(_download_embed)


async def download_instagram_api(url: str) -> str | None:
    """
    Instagram API orqali yuklash (cookies siz).
    Bu usul public postlar uchun ishlaydi.
    """
    def _download_api():
        import urllib.request
        import json

        try:
            # Post ID ni olish
            post_id = None
            patterns = [
                r'instagram\.com/(?:p|reel|tv)/([^/?]+)',
                r'instagram\.com/reel/([^/?]+)',
                r'instagram\.com/p/([^/?]+)',
                r'instagram\.com/tv/([^/?]+)',
                r'instagram\.com/share/([^/?]+)',
            ]
            for pattern in patterns:
                match = re.search(pattern, url)
                if match:
                    post_id = match.group(1)
                    break

            if not post_id:
                return None

            # Instagram oEmbed API
            oembed_url = f"https://graph.facebook.com/v18.0/instagram_oembed?url=https://www.instagram.com/p/{post_id}/&access_token=APP_ID|APP_SECRET"

            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
            }

            # OEmbed orqali thumbnail olish
            req = urllib.request.Request(oembed_url, headers=headers)

            try:
                with urllib.request.urlopen(req, timeout=10) as response:
                    data = json.loads(response.read().decode('utf-8'))
                    thumbnail_url = data.get('thumbnail_url')
                    if thumbnail_url:
                        output_path = os.path.join(tempfile.gettempdir(), f"ig_api_{post_id}.jpg")
                        req_img = urllib.request.Request(thumbnail_url, headers=headers)
                        with urllib.request.urlopen(req_img, timeout=30) as response:
                            with open(output_path, 'wb') as f:
                                f.write(response.read())
                        if os.path.exists(output_path):
                            return output_path
            except Exception:
                pass

            return None

        except Exception as e:
            logging.warning(f"Instagram API yuklashda xatolik: {e}")
            return None

    result = await asyncio.to_thread(_download_api)
    if result:
        return result
    return await download_instagram_post_page(url)


async def download_instagram_post_page(url: str) -> str | None:
    """
    Instagram post sahifasini yuklab olish va rasm/video URL ini topish.
    Public postlar uchun HTML orqali ishlaydi.
    """
    def _download_post():
        import urllib.request
        import ssl
        import json
        from urllib.parse import urlparse

        try:
            post_id = None
            patterns = [
                r'instagram\.com/(?:p|reel|tv)/([^/?]+)',
                r'instagram\.com/reel/([^/?]+)',
                r'instagram\.com/p/([^/?]+)',
                r'instagram\.com/tv/([^/?]+)',
                r'instagram\.com/share/([^/?]+)',
            ]
            for pattern in patterns:
                match = re.search(pattern, url)
                if match:
                    post_id = match.group(1)
                    break

            if not post_id:
                return None

            post_url = f"https://www.instagram.com/p/{post_id}/"
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Referer': 'https://www.instagram.com/',
            }
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            req = urllib.request.Request(post_url, headers=headers)
            with urllib.request.urlopen(req, timeout=20, context=ctx) as response:
                html = response.read().decode('utf-8', errors='ignore')

            json_text = None
            shared_data = re.search(r'window\._sharedData\s*=\s*(\{.*?\});</script>', html, flags=re.S)
            if shared_data:
                json_text = shared_data.group(1)
            else:
                additional = re.search(r'window\.__additionalDataLoaded\([^,]+,\s*(\{.*?\})\);', html, flags=re.S)
                if additional:
                    json_text = additional.group(1)
                else:
                    ld_json = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, flags=re.S)
                    if ld_json:
                        json_text = ld_json.group(1)

            data = None
            if json_text:
                try:
                    data = json.loads(json_text)
                except Exception:
                    # Ba'zan HTML ichida qatorlar bilan kelgan JSON bor
                    cleaned = re.sub(r'\s+', ' ', json_text)
                    try:
                        data = json.loads(cleaned)
                    except Exception:
                        data = None

            media_url = None
            is_video = False
            if isinstance(data, dict):
                node = data
                if 'graphql' in node and isinstance(node['graphql'], dict):
                    node = node['graphql'].get('shortcode_media', node)
                elif 'shortcode_media' in node:
                    node = node['shortcode_media']
                elif '@graph' in node and isinstance(node['@graph'], list) and node['@graph']:
                    node = node['@graph'][0]

                if isinstance(node, dict):
                    if node.get('is_video'):
                        is_video = True
                        media_url = node.get('video_url') or node.get('videoUrl') or node.get('display_url')
                    else:
                        media_url = node.get('display_url') or node.get('thumbnail_src') or node.get('thumbnailUrl')
                        if not media_url and isinstance(node.get('edge_sidecar_to_children'), dict):
                            for edge in node['edge_sidecar_to_children'].get('edges', []):
                                child = edge.get('node', {})
                                if child.get('display_url'):
                                    media_url = child['display_url']
                                    break

            if not media_url:
                video_match = re.search(r'property="og:video" content="([^"]+)"', html)
                if video_match:
                    media_url = video_match.group(1)
                    is_video = True
                else:
                    image_match = re.search(r'property="og:image" content="([^"]+)"', html)
                    if image_match:
                        media_url = image_match.group(1)

            # Qo'shimcha: JSON-LD dan rasm URL ni olish
            if not media_url:
                ld_json = re.search(r'<script type="application/ld\+json">(.*?)</script>', html, flags=re.S)
                if ld_json:
                    try:
                        import json
                        ld_data = json.loads(ld_json.group(1))
                        if isinstance(ld_data, dict):
                            if 'video' in ld_data and 'contentUrl' in ld_data['video']:
                                media_url = ld_data['video']['contentUrl']
                                is_video = True
                            elif 'image' in ld_data and isinstance(ld_data['image'], str):
                                media_url = ld_data['image']
                            elif 'thumbnailUrl' in ld_data:
                                media_url = ld_data['thumbnailUrl']
                    except Exception:
                        pass

            # HTML dan to'g'ridan-to'g'ri rasm URL qidirish (eng keng qamrovli)
            if not media_url:
                img_matches = re.findall(r'src="(https://[^"]+instagram\.com[^"]+\.(?:jpg|jpeg|png|webp))"', html)
                if img_matches:
                    media_url = img_matches[0]

            if not media_url:
                return None

            media_url = media_url.replace('\\u0026', '&')
            ext = os.path.splitext(urlparse(media_url).path)[1].split('?')[0].lower()
            if ext not in ('.mp4', '.jpg', '.jpeg', '.png', '.webp', '.gif'):
                ext = '.mp4' if is_video else '.jpg'

            output_path = os.path.join(tempfile.gettempdir(), f"ig_post_{post_id}{ext}")
            req_media = urllib.request.Request(media_url, headers=headers)
            with urllib.request.urlopen(req_media, timeout=30, context=ctx) as response:
                with open(output_path, 'wb') as f:
                    f.write(response.read())

            if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                return output_path
            return None
        except Exception as e:
            logging.warning(f"Instagram post page yuklashda xatolik: {e}")
            return None

    return await asyncio.to_thread(_download_post)


# ========================== I18N TEXTS ==========================
TEXTS = {
    "uz": {
        "choose_lang": "🌐 Tilni tanlang:",
        "welcome": (
            "✨ @skachatinstavideo_bot ga xush kelibsiz!\n\n"
            "🎵 Nom, ijrochi, matn yoki link orqali qidiruv.\n"
            "🎙 Ovozli xabar va video orqali musiqani aniqlash.\n"
            "📎 Instagram, YouTube va boshqa ijtimoiy tarmoqlardan media yuboring.\n\n"
            "🔗 Link, 🎙 Voice yoki 📝 Matn yuboring!"
        ),
        "force_sub": "❌ <b>Botdan foydalanish uchun quyidagi kanallarga a'zo bo'ling:</b>",
        "check_sub": "✅ A'zolikni tekshirish",
        "not_subscribed": "❌ Siz hali barcha kanallarga a'zo bo'lmagansiz!",
        "sub_ok_group": "✅ A'zolik tasdiqlandi! Endi musiqa qidirishingiz mumkin.",
        "searching": "🔍 Qidirilmoqda...",
        "search_results": "🔎 <b>Topilgan musiqalar:</b>\n\n",
        "no_results": "❌ Hech narsa topilmadi.",
        "music_selected": "🎵 {artist} - {title}\n\n🔎 Yuklanmoqda...",
        "recognizing": "🔎 Aniqlanmoqda...",
        "not_recognized": "❌ Musiqa aniqlanmadi. Iltimos, sifatli audio yuboring.",
        "downloading": "🔎 Yuklanmoqda...",
        "download_done": "✅ Yuklandi!",
        "download_error": (
            "Afsuski ushbu media faylni yuklay olmadim.\n\n"
            "Media faylni yuklash uchun, botni guruhga qo'shing"
        ),
        "download_failed_group": (
            "Afsuski ushbu media faylni yuklay olmadim.\n\n"
            "Media faylni yuklash uchun, botni guruhga qo'shing"
        ),
        "music_not_found_group": (
            "⚠️ Afsuski musiqa topilmadi\n\n"
            "Musiqani topish uchun guruhga qo'shing👇"
        ),
        "file_too_large": (
            "❌ Fayl hajmi juda katta ({size_mb} MB).\n"
            "Telegram bot orqali maksimal yuborish hajmi: {limit_mb} MB.\n"
            "Iltimos, kichikroq fayl yuboring yoki to'g'ridan-to'g'ri link orqali yuklab oling."
        ),
        "photo_downloaded": (
            "📸 <b>Rasm qabul qilindi!</b>\n\n"
            "❤️ @skachatinstavideo_bot"
        ),
        "video_downloaded": (
            "🎬 <b>Video qabul qilindi!</b>\n\n"
            "🎵 Musiqasini topish uchun tugmani bosing!"
        ),
        "extracting_music": "🎵 Musiqa aniqlanmoqda...",
        "music_found_from_video": (
            "🎵 <b>Musiqa topildi!</b>\n\n"
            "🎤 Ijrochi: {artist}\n"
            "🎼 Qo'shiq: {title}\n\n"
            "🔎 Qidirilmoqda..."
        ),
        "admin_welcome": (
            "🔧 <b>Admin Panel</b>\n\n"
            "Kerakli bo'limni tanlang:"
        ),
        "stats": (
            "📊 <b>Analitika</b>\n\n"
            "👤 Jami foydalanuvchilar: <b>{total}</b>\n"
            "⚡ Faol (24 soat): <b>{active}</b>\n"
            "🆕 Yangi (7 kun): <b>{week}</b>\n"
            "🌐 Tillar bo'yicha:\n{langs}\n\n"
            "🔥 Top 10 qidiruvlar:\n{top}"
        ),
        "broadcast_ask_text": "📨 Broadcast matnini yuboring:",
        "broadcast_ask_media": "📎 Broadcast uchun media yuboring (rasm, video, audio):",
        "broadcast_ask_caption": (
            "📎 Media qabul qilindi.\n\n"
            "📝 Yozuv (caption) qo'shish uchun matn yuboring:\n"
            "⏭️ O'tkazib yuborish uchun /skip deb yozing:"
        ),
        "broadcast_done": "✅ Xabar <b>{count}</b> ta foydalanuvchi va guruhga yuborildi.",
        "blocked": "🚫 Siz botdan bloklangansiz.",
        "blacklist_add": "🚫 Foydalanuvchi blocklandi.",
        "blacklist_remove": "✅ Foydalanuvchi blockdan chiqarildi.",
        "channel_add": "📢 Kanal qo'shildi.",
        "channel_remove": "❌ Kanal o'chirildi.",
        "profile": (
            "👤 <b>Profil</b>\n\n"
            "🆔 ID: <code>{telegram_id}</code>\n"
            "👤 Ism: {fullname}\n"
            "🔤 Username: {username}\n"
            "🌐 Til: {language}"
        ),
        "help_text": (
            "🎵 @skachatinstavideo_bot yordam\n\n"
            "🔹 <b>Qo'shiq nomi</b> — <i>Eminem Mockingbird</i>\n"
            "🔹 <b>Ijrochi</b> — <i>Eminem</i>\n"
            "🔹 <b>Matn</b> — <i>yuragim yonadi sensiz</i>\n"
            "🔹 <b>Ovozli xabar</b> — musiqani aniqlash\n"
            "🔹 <b>Video/Audio</b> — fayldan musiqani topish\n"
            "🔹 <b>Link</b> — Instagram, YouTube, Pinterest, Snapchat dan video/rasm yuklash\n\n"
            "🎵 Natijalar tez topiladi!\n"
            "📢 Admin: @admin"
        ),
        "no_blocked": "🚫 Bloklangan foydalanuvchilar yo'q.",
        "enter_channel": (
            "📢 Kanal qo'shish:\n\n"
            "Format: <code>@username</code> yoki <code>username</code>\n"
            "Kanal nomini yuboring:"
        ),
        "enter_block_id": (
            "🚫 Block qilish uchun foydalanuvchi ID sini yuboring:\n\n"
            "Misol: <code>123456789</code>"
        ),
        "invalid_id": "❌ Noto'g'ri ID formati.",
        "groups_list": "👥 <b>Bot qo'shilgan guruhlar:</b>\n\n{groups}",
        "no_groups": "Guruhlar yo'q.",
        "lang_stats": "🌐 <b>Tillar bo'yicha foydalanuvchilar:</b>\n\n{stats}",
        "ffmpeg_missing": "❌ FFmpeg topilmadi! Iltimos, ffmpeg.exe ni o'rnating va PATH ga qo'shing.",
        "file_not_found": "❌ Fayl topilmadi: {path}",
        "unknown_message": (
            "❌ Bu xabar turini tushunmadim.\n\n"
            "Menga quyidagilarni yuboring:\n"
            "🎵 Qo'shiq nomi yoki ijrochi\n"
            "🎙 Ovozli xabar\n"
            "📎 Audio/Video fayl\n"
            "🔗 Ijtimoiy tarmoq linki (Instagram, YouTube,Pinterest, Snapchat)"
        ),
        "document_received": (
            "📄 <b>Hujjat qabul qilindi</b>\n\n"
            "Fayl: <code>{filename}</code>\n"
            "Hajmi: {size_mb} MB\n\n"
            "⚠️ Men faqat musiqa, video va linklarni qayta ishlayman. "
            "Hujjatlar bilan ishlash imkoniyati mavjud emas."
        ),
        "quick_search_hint": (
            "💡 <b>Tez qidiruv:</b>\n"
            "Qo'shiq nomi yoki ijrochi yozing — 1-2 sekundda topaman!"
        ),
        "link_downloaded": (
            "📎 <b>Link qabul qilindi!</b>\n\n"
            "🔎 Yuklanmoqda..."
        ),
        "video_from_link": (
            "🎬 <b>Video yuklandi!</b>\n\n"
            "🎵 Musiqani aniqlash uchun tugmani bosing:"
        ),
        "photo_from_link": (
            "📸 <b>Rasm yuklandi!</b>\n\n"
            "❤️ @skachatinstavideo_bot"
        ),
    },
    "uz_kr": {
        "choose_lang": "🌐 Тилни танланг:",
        "welcome": (
            "✨ @skachatinstavideo_bot га хуш келибсиз!\n\n"
            "🎵 Ном, ижрочи, матн ёки линк орқали қидириш.\n"
            "🎙 Овозли хабар ва видео орқали мусиқани аниқлаш.\n"
            "📎 Instagram, YouTube ва бошқа ижтимоий тармоқлардан медиа юборинг.\n\n"
            "🔗 Линк, 🎙 Voice ёки 📝 Матн юборинг!"
        ),
        "force_sub": "❌ <b>Ботдан фойдаланиш учун қуйидаги каналларга аъзо бўлинг:</b>",
        "check_sub": "✅ Аъзоликни текшириш",
        "not_subscribed": "❌ Сиз ҳали барча каналларга аъзо бўлмагансиз!",
        "sub_ok_group": "✅ Аъзолик тасдиқланди! Энди мусиқа қидиришингиз мумкин.",
        "searching": "🔍 Қидирилмоқда...",
        "search_results": "🔎 <b>Топилган мусиқалар:</b>\n\n",
        "no_results": "❌ Ҳеч нарса топилмади.",
        "music_selected": "🎵 {artist} - {title}\n\n🔎 Юкланмоқда...",
        "recognizing": "🔎 Аниқланмоқда...",
        "not_recognized": "❌ Мусиқа аниқланмади. Илтимос, сифатли аудио юборинг.",
        "downloading": "🔎 Юкланмоқда...",
        "download_done": "✅ Юкланди!",
        "download_error": (
            "Afsuski ушбу медиа файлни юклай олмадим.\n\n"
            "Медиа файлни юклаш учун, ботни гуруҳга қўшинг"
        ),
        "download_failed_group": (
            "Afsuski ушбу медиа файлни юклай олмадим.\n\n"
            "Медиа файлни юклаш учун, ботни гуруҳга қўшинг"
        ),
        "music_not_found_group": (
            "⚠️ Afsuski мусиқа топилмади\n\n"
            "Мусиқани топиш учун гуруҳга қўшинг👇"
        ),
        "file_too_large": (
            "❌ Файл ҳажми жуда катта ({size_mb} MB).\n"
            "Telegram бот орқали максимал юбориш ҳажми: {limit_mb} MB.\n"
            "Илтимос, кичикроқ файл юборинг ёки тўғридан-тўғри линк орқали юклаб олинг."
        ),
        "photo_downloaded": (
            "📸 <b>Расм қабул қилинди!</b>\n\n"
            "❤️ @skachatinstavideo_bot"
        ),
        "video_downloaded": (
            "🎬 <b>Видео қабул қилинди!</b>\n\n"
            "🎵 Мусиқасини топиш учун тугмани босинг!"
        ),
        "extracting_music": "🎵 Мусиқа аниқланмоқда...",
        "music_found_from_video": (
            "🎵 <b>Мусиқа топилди!</b>\n\n"
            "🎤 Ижрочи: {artist}\n"
            "🎼 Қўшиқ: {title}\n\n"
            "🔎 Қидирилмоқда..."
        ),
        "admin_welcome": (
            "🔧 <b>Админ Панел</b>\n\n"
            "Керакли бўлимни танланг:"
        ),
        "stats": (
            "📊 <b>Статистика</b>\n\n"
            "👤 Жами фойдаланувчилар: <b>{total}</b>\n"
            "⚡ Фаол (24 соат): <b>{active}</b>\n"
            "🆕 Янги (7 кун): <b>{week}</b>\n"
            "🌐 Тиллар бўйича:\n{langs}\n\n"
            "🔥 Топ 10 қидирувлар:\n{top}"
        ),
        "broadcast_ask_text": "📨 Broadcast матнини юборинг:",
        "broadcast_ask_media": "📎 Broadcast учун медиа юборинг (расм, видео, аудио):",
        "broadcast_ask_caption": (
            "📎 Медиа қабул қилинди.\n\n"
            "📝 Ёзув (caption) қўшиш учун матн юборинг:\n"
            "⏭️ Ўтказиб юбориш учун /skip деб ёзинг:"
        ),
        "broadcast_done": "✅ Хабар <b>{count}</b> та фойдаланувчи ва гуруҳга юборилди.",
        "blocked": "🚫 Сиз ботдан блоклангансиз.",
        "blacklist_add": "🚫 Фойдаланувчи блокланди.",
        "blacklist_remove": "✅ Фойдаланувчи блокдан чиқарилди.",
        "channel_add": "📢 Канал қўшилди.",
        "channel_remove": "❌ Канал ўчирилди.",
        "profile": (
            "👤 <b>Профил</b>\n\n"
            "🆔 ID: <code>{telegram_id}</code>\n"
            "👤 Исм: {fullname}\n"
            "🔤 Username: {username}\n"
            "🌐 Тил: {language}"
        ),
        "help_text": (
            "🎵 @skachatinstavideo_bot ёрдам\n\n"
            "🔹 <b>Қўшиқ номи</b> — <i>Eminem Mockingbird</i>\n"
            "🔹 <b>Ижрочи</b> — <i>Eminem</i>\n"
            "🔹 <b>Матн</b> — <i>yuragim yonadi sensiz</i>\n"
            "🔹 <b>Овозли хабар</b> — мусиқани аниқлаш\n"
            "🔹 <b>Видео/Аудио</b> — файлдан мусиқани топиш\n"
            "🔹 <b>Link</b> — Instagram, YouTube, Pinterest, Snapchat дан видео/расм юклаш\n\n"
            "🎵 Натижалар тез топилади!\n"
            "📢 Админ: @admin"
        ),
        "no_blocked": "🚫 Блокланган фойдаланувчилар йўқ.",
        "enter_channel": (
            "📢 Канал қўшиш:\n\n"
            "Формат: <code>@username</code> ёки <code>username</code>\n"
            "Канал номини юборинг:"
        ),
        "enter_block_id": (
            "🚫 Блок қилиш учун фойдаланувчи ID сини юборинг:\n\n"
            "Мисол: <code>123456789</code>"
        ),
        "invalid_id": "❌ Нотўғри ID формати.",
        "groups_list": "👥 <b>Бот қўшилган гуруҳлар:</b>\n\n{groups}",
        "no_groups": "Гуруҳлар йўқ.",
        "lang_stats": "🌐 <b>Тиллар бўйича фойдаланувчилар:</b>\n\n{stats}",
        "ffmpeg_missing": "❌ FFmpeg топилмади! Илтимос, ffmpeg.exe ни ўрнating ва PATH га қўшинг.",
        "file_not_found": "❌ Файл топилмади: {path}",
        "unknown_message": (
            "❌ Бу хабар турини тушунмадим.\n\n"
            "Манга қуйидагиларни юборинг:\n"
            "🎵 Қўшиқ номи ёки ижрочи\n"
            "🎙 Овозли хабар\n"
            "📎 Аудио/Видео файл\n"
            "🔗 Ижтимоий тармоқ линки (Instagram, YouTube, Pinterest, Snapchat)"
        ),
        "document_received": (
            "📄 <b>Ҳужжат қабул қилинди</b>\n\n"
            "Файл: <code>{filename}</code>\n"
            "Ҳажми: {size_mb} MB\n\n"
            "⚠️ Мен фақат мусиқа, видео ва линкларни қайта ишлайман. "
            "Ҳужжатлар билан ишлаш имконияти мавжуд эмас."
        ),
        "quick_search_hint": (
            "💡 <b>Тез қидирув:</b>\n"
            "Қўшиқ номи ёки ижрочи ёзинг — 1-2 секундда топаман!"
        ),
        "link_downloaded": (
            "📎 <b>Link қабул қилинди!</b>\n\n"
            "🔎 Юкланмоқда..."
        ),
        "video_from_link": (
            "🎬 <b>Видео юкланди!</b>\n\n"
            "🎵 Мусиқани аниқлаш учун тугмани босинг:"
        ),
        "photo_from_link": (
            "📸 <b>Расм юкланди!</b>\n\n"
            "❤️ @skachatinstavideo_bot"
        ),
    },
    "ru": {
        "choose_lang": "🌐 Выберите язык:",
        "welcome": (
            "✨ Добро пожаловать в @skachatinstavideo_bot!\n\n"
            "🎵 Поиск по названию, исполнителю, тексту или ссылке.\n"
            "🎙 Голосовое сообщение и видео для распознавания музыки.\n"
            "📎 Загружайте медиа из Instagram, YouTube и других сетей.\n\n"
            "Отправьте 🔗 Ссылку, 🎙 Голос или 📝 Текст!"
        ),
        "force_sub": "❌ <b>Для использования бота подпишитесь на каналы:</b>",
        "check_sub": "✅ Проверить подписку",
        "not_subscribed": "❌ Вы еще не подписаны на все каналы!",
        "sub_ok_group": "✅ Подписка подтверждена! Теперь можно искать музыку.",
        "searching": "🔍 Поиск...",
        "search_results": "🔎 <b>Найденные треки:</b>\n\n",
        "no_results": "❌ Ничего не найдено.",
        "music_selected": "🎵 {artist} - {title}\n\n🔎 Загрузка...",
        "recognizing": "🔎 Распознавание...",
        "not_recognized": "❌ Музыка не распознана. Отправьте качественное аудио.",
        "downloading": "🔎 Загрузка...",
        "download_done": "✅ Загружено!",
        "download_error": (
            "К сожалению, я не смог загрузить этот медиафайл.\n\n"
            "Чтобы загрузить медиафайл, добавьте бота в группу"
        ),
        "download_failed_group": (
            "К сожалению, я не смог загрузить этот медиафайл.\n\n"
            "Чтобы загрузить медиафайл, добавьте бота в группу"
        ),
        "music_not_found_group": (
            "⚠️ К сожалению, музыка не найдена\n\n"
            "Чтобы найти музыку, добавьте бота в группу👇"
        ),
        "file_too_large": (
            "❌ Размер файла слишком большой ({size_mb} МБ).\n"
            "Максимальный размер отправки через Telegram бот: {limit_mb} МБ.\n"
            "Пожалуйста, отправьте файл меньшего размера или скачайте по прямой ссылке."
        ),
        "photo_downloaded": (
            "📸 <b>Фото получено!</b>\n\n"
            "❤️ @skachatinstavideo_bot"
        ),
        "video_downloaded": (
            "🎬 <b>Видео получено!</b>\n\n"
            "🎵 Нажмите, чтобы найти музыку!"
        ),
        "extracting_music": "🎵 Извлечение музыки...",
        "music_found_from_video": (
            "🎵 <b>Музыка найдена!</b>\n\n"
            "🎤 Исполнитель: {artist}\n"
            "🎼 Трек: {title}\n\n"
            "🔎 Поиск..."
        ),
        "admin_welcome": (
            "🔧 <b>Панель администратора</b>\n\n"
            "Выберите раздел:"
        ),
        "stats": (
            "📊 <b>Аналитика</b>\n\n"
            "👤 Всего пользователей: <b>{total}</b>\n"
            "⚡ Активные (24 ч): <b>{active}</b>\n"
            "🆕 Новые (7 дней): <b>{week}</b>\n"
            "🌐 По языкам:\n{langs}\n\n"
            "🔥 Топ 10 запросов:\n{top}"
        ),
        "broadcast_ask_text": "📨 Отправьте текст для рассылки:",
        "broadcast_ask_media": "📎 Отправьте медиа для рассылки (фото, видео, аудио):",
        "broadcast_ask_caption": (
            "📎 Медиа получено.\n\n"
            "📝 Отправьте подпись (caption):\n"
            "⏭️ Или напишите /skip, чтобы пропустить:"
        ),
        "broadcast_done": "✅ Сообщение отправлено <b>{count}</b> пользователям и группам.",
        "blocked": "🚫 Вы заблокированы в боте.",
        "blacklist_add": "🚫 Пользователь заблокирован.",
        "blacklist_remove": "✅ Пользователь разблокирован.",
        "channel_add": "📢 Канал добавлен.",
        "channel_remove": "❌ Канал удалён.",
        "profile": (
            "👤 <b>Профиль</b>\n\n"
            "🆔 ID: <code>{telegram_id}</code>\n"
            "👤 Имя: {fullname}\n"
            "🔤 Username: {username}\n"
            "🌐 Язык: {language}"
        ),
        "help_text": (
            "🎵 @skachatinstavideo_bot — помощь\n\n"
            "🔹 <b>Название трека</b> — <i>Eminem Mockingbird</i>\n"
            "🔹 <b>Исполнитель</b> — <i>Eminem</i>\n"
            "🔹 <b>Текст песни</b> — <i>yuragim yonadi sensiz</i>\n"
            "🔹 <b>Голосовое сообщение</b> — распознавание музыки\n"
            "🔹 <b>Видео/Аудио</b> — поиск музыки из файла\n"
            "🔹 <b>Ссылка</b> — загрузка видео/фото из Instagram, YouTube, Pinterest, Snapchat\n\n"
            "🎵 Результаты найдутся быстро!\n"
            "📢 Админ: @admin"
        ),
        "no_blocked": "🚫 Заблокированных пользователей нет.",
        "enter_channel": (
            "📢 Добавить канал:\n\n"
            "Формат: <code>@username</code> или <code>username</code>\n"
            "Отправьте название канала:"
        ),
        "enter_block_id": (
            "🚫 Отправьте ID пользователя для блокировки:\n\n"
            "Пример: <code>123456789</code>"
        ),
        "invalid_id": "❌ Неверный формат ID.",
        "groups_list": "👥 <b>Группы бота:</b>\n\n{groups}",
        "no_groups": "Групп нет.",
        "lang_stats": "🌐 <b>Пользователи по языкам:</b>\n\n{stats}",
        "ffmpeg_missing": "❌ FFmpeg не найден! Установите ffmpeg и добавьте в PATH.",
        "file_not_found": "❌ Файл не найден: {path}",
        "unknown_message": (
            "❌ Не понял этот тип сообщения.\n\n"
            "Отправьте мне:\n"
            "🎵 Название или исполнитель\n"
            "🎙 Голосовое сообщение\n"
            "📎 Аудио/Видео файл\n"
            "🔗 Ссылка на соцсеть (Instagram, YouTube, Pinterest, Snapchat)"
        ),
        "document_received": (
            "📄 <b>Документ получен</b>\n\n"
            "Файл: <code>{filename}</code>\n"
            "Размер: {size_mb} МБ\n\n"
            "⚠️ Я работаю только с музыкой, видео и ссылками. "
            "Работа с документами недоступна."
        ),
        "quick_search_hint": (
            "💡 <b>Быстрый поиск:</b>\n"
            "Введите название или исполнителя — найду за 1-2 секунды!"
        ),
        "link_downloaded": (
            "📎 <b>Ссылка получена!</b>\n\n"
            "🔎 Загрузка..."
        ),
        "video_from_link": (
            "🎬 <b>Видео загружено!</b>\n\n"
            "🎵 Нажмите кнопку для распознавания музыки:"
        ),
        "photo_from_link": (
            "📸 <b>Фото загружено!</b>\n\n"
            "❤️ @skachatinstavideo_bot"
        ),
    },
    "en": {
        "choose_lang": "🌐 Choose language:",
        "welcome": (
            "✨ Welcome to @skachatinstavideo_bot!\n\n"
            "🎵 <b>Music search</b> — by name, artist, lyrics\n"
            "🎙 <b>Voice message</b> — identify music\n"
            "📎 <b>Link</b> — download video/photo from Instagram, YouTube and other social networks\n\n"
            "Send a 🔗 <b>Link</b>, 🎙 <b>Voice</b>, 📝 <b>Text</b>!"
        ),
        "force_sub": "❌ <b>Please subscribe to these channels to use the bot:</b>",
        "check_sub": "✅ Check subscription",
        "not_subscribed": "❌ You haven't subscribed to all channels yet!",
        "sub_ok_group": "✅ Subscription confirmed! You can now search for music.",
        "searching": "🔍 Searching...",
        "search_results": "🔎 <b>Found tracks:</b>\n\n",
        "no_results": "❌ Nothing found.",
        "music_selected": "🎵 {artist} - {title}\n\n🔎 Downloading...",
        "recognizing": "🔎 Identifying...",
        "not_recognized": "❌ Music not recognized. Please send quality audio.",
        "downloading": "🔎 Downloading...",
        "download_done": "✅ Done!",
        "download_error": (
            "Sorry, I couldn't download this media file.\n\n"
            "To download media, add the bot to a group"
        ),
        "download_failed_group": (
            "Sorry, I couldn't download this media file.\n\n"
            "To download media, add the bot to a group"
        ),
        "music_not_found_group": (
            "⚠️ Sorry, music not found\n\n"
            "To find music, add the bot to a group👇"
        ),
        "file_too_large": (
            "❌ File is too large ({size_mb} MB).\n"
            "Max file size via Telegram bot: {limit_mb} MB.\n"
            "Please send a smaller file or download via direct link."
        ),
        "photo_downloaded": (
            "📸 <b>Photo received!</b>\n\n"
            "❤️ @skachatinstavideo_bot"
        ),
        "video_downloaded": (
            "🎬 <b>Video received!</b>\n\n"
            "🎵 Press the button to find the music!"
        ),
        "extracting_music": "🎵 Extracting music...",
        "music_found_from_video": (
            "🎵 <b>Music found!</b>\n\n"
            "🎤 Artist: {artist}\n"
            "🎼 Track: {title}\n\n"
            "🔎 Searching..."
        ),
        "admin_welcome": (
            "🔧 <b>Admin Panel</b>\n\n"
            "Select a section:"
        ),
        "stats": (
            "📊 <b>Analytics</b>\n\n"
            "👤 Total users: <b>{total}</b>\n"
            "⚡ Active (24h): <b>{active}</b>\n"
            "🆕 New (7 days): <b>{week}</b>\n"
            "🌐 By language:\n{langs}\n\n"
            "🔥 Top 10 searches:\n{top}"
        ),
        "broadcast_ask_text": "📨 Send broadcast text:",
        "broadcast_ask_media": "📎 Send broadcast media (photo, video, audio):",
        "broadcast_ask_caption": (
            "📎 Media received.\n\n"
            "📝 Send a caption:\n"
            "⏭️ Write /skip to skip:"
        ),
        "broadcast_done": "✅ Message sent to <b>{count}</b> users and groups.",
        "blocked": "🚫 You are blocked from this bot.",
        "blacklist_add": "🚫 User blocked.",
        "blacklist_remove": "✅ User unblocked.",
        "channel_add": "📢 Channel added.",
        "channel_remove": "❌ Channel removed.",
        "profile": (
            "👤 <b>Profile</b>\n\n"
            "🆔 ID: <code>{telegram_id}</code>\n"
            "👤 Name: {fullname}\n"
            "🔤 Username: {username}\n"
            "🌐 Language: {language}"
        ),
        "help_text": (
            "🎵 @skachatinstavideo_bot help\n\n"
            "🔹 <b>Track name</b> — <i>Eminem Mockingbird</i>\n"
            "🔹 <b>Artist</b> — <i>Eminem</i>\n"
            "🔹 <b>Lyrics</b> — <i>yuragim yonadi sensiz</i>\n"
            "🔹 <b>Voice message</b> — identify music from audio\n"
            "🔹 <b>Video/Audio</b> — find music from file\n"
            "🔹 <b>Link</b> — download video/photo from Instagram, YouTube, Pinterest, Snapchat\n\n"
            "🎵 All music is found via <b>YouTube</b>!\n"
            "📢 Admin: @admin"
        ),
        "no_blocked": "🚫 No blocked users.",
        "enter_channel": (
            "📢 Add channel:\n\n"
            "Format: <code>@username</code> or <code>username</code>\n"
            "Send channel name:"
        ),
        "enter_block_id": (
            "🚫 Send user ID to block:\n\n"
            "Example: <code>123456789</code>"
        ),
        "invalid_id": "❌ Invalid ID format.",
        "groups_list": "👥 <b>Bot groups:</b>\n\n{groups}",
        "no_groups": "No groups.",
        "lang_stats": "🌐 <b>Users by language:</b>\n\n{stats}",
        "ffmpeg_missing": "❌ FFmpeg not found! Please install ffmpeg and add to PATH.",
        "file_not_found": "❌ File not found: {path}",
        "unknown_message": (
            "❌ I don't understand this message type.\n\n"
            "Send me:\n"
            "🎵 Track name or artist\n"
            "🎙 Voice message\n"
            "📎 Audio/Video file\n"
            "🔗 Social media link (Instagram, YouTube, Pinterest, Snapchat)"
        ),
        "document_received": (
            "📄 <b>Document received</b>\n\n"
            "File: <code>{filename}</code>\n"
            "Size: {size_mb} MB\n\n"
            "⚠️ I only process music, video, and links. "
            "Document handling is not available."
        ),
        "quick_search_hint": (
            "💡 <b>Quick search:</b>\n"
            "Type a track name or artist — I'll find it in 1-2 seconds!"
        ),
        "link_downloaded": (
            "📎 <b>Link received!</b>\n\n"
            "🔎 Downloading..."
        ),
        "video_from_link": (
            "🎬 <b>Video downloaded!</b>\n\n"
            "🎵 Press the button to recognize music:"
        ),
        "photo_from_link": (
            "📸 <b>Photo downloaded!</b>\n\n"
            "❤️ @skachatinstavideo_bot"
        ),
    },
}

# ========================== DATABASE ==========================
class Database:
    def __init__(self, url: str):
        self.url = url
        self.pool: asyncpg.Pool | None = None

    async def connect(self):
        self.pool = await asyncpg.create_pool(self.url, min_size=2, max_size=10)
        await self._create_tables()

    async def _create_tables(self):
        async with self.pool.acquire() as conn:
            await conn.execute(
                r"""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id BIGINT PRIMARY KEY,
                    fullname VARCHAR(255),
                    username VARCHAR(255),
                    language VARCHAR(10) DEFAULT 'uz',
                    created_at TIMESTAMP DEFAULT NOW(),
                    last_active TIMESTAMP DEFAULT NOW(),
                    is_blocked BOOLEAN DEFAULT FALSE
                );
                CREATE TABLE IF NOT EXISTS groups_bot (
                    group_id BIGINT PRIMARY KEY,
                    title VARCHAR(255),
                    added_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS channels (
                    id SERIAL PRIMARY KEY,
                    username VARCHAR(100) UNIQUE,
                    title VARCHAR(255),
                    active BOOLEAN DEFAULT TRUE,
                    added_by BIGINT,
                    added_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS searches (
                    id SERIAL PRIMARY KEY,
                    query TEXT,
                    type VARCHAR(50),
                    user_id BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
                    created_at TIMESTAMP DEFAULT NOW()
                );
                CREATE TABLE IF NOT EXISTS musics (
                    id SERIAL PRIMARY KEY,
                    title VARCHAR(255),
                    artist VARCHAR(255),
                    file_id VARCHAR(500),
                    created_at TIMESTAMP DEFAULT NOW(),
                    UNIQUE(title, artist)
                );
                """
            )

    async def get_user(self, telegram_id: int):
        async with self.pool.acquire() as conn:
            return await conn.fetchrow(
                "SELECT * FROM users WHERE telegram_id = $1", telegram_id
            )

    async def add_user(self, telegram_id: int, fullname: str, username: str | None):
        async with self.pool.acquire() as conn:
            await conn.execute(
                r"""
                INSERT INTO users (telegram_id, fullname, username)
                VALUES ($1, $2, $3)
                ON CONFLICT (telegram_id) DO UPDATE
                SET fullname = $2, username = $3, last_active = NOW()
                """,
                telegram_id, fullname, username,
            )

    async def set_language(self, telegram_id: int, language: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET language = $1 WHERE telegram_id = $2",
                language, telegram_id,
            )

    async def update_activity(self, telegram_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET last_active = NOW() WHERE telegram_id = $1",
                telegram_id,
            )

    async def add_search(self, query: str, type_: str, user_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO searches (query, type, user_id) VALUES ($1, $2, $3)",
                query, type_, user_id,
            )

    async def get_stats(self):
        async with self.pool.acquire() as conn:
            total = await conn.fetchval("SELECT COUNT(*) FROM users")
            active = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE last_active > NOW() - INTERVAL '24 hours'"
            )
            week = await conn.fetchval(
                "SELECT COUNT(*) FROM users WHERE created_at > NOW() - INTERVAL '7 days'"
            )
            top = await conn.fetch(
                r"""
                SELECT query, COUNT(*) as c
                FROM searches
                GROUP BY query
                ORDER BY c DESC
                LIMIT 10
                """
            )
            return total, active, week, top

    async def get_language_stats(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT language, COUNT(*) as c FROM users GROUP BY language ORDER BY c DESC"
            )

    async def get_channels(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT * FROM channels WHERE active = TRUE ORDER BY id"
            )

    async def add_channel(self, username: str, title: str, added_by: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                r"""
                INSERT INTO channels (username, title, added_by)
                VALUES ($1, $2, $3)
                ON CONFLICT (username) DO UPDATE
                SET active = TRUE, title = $2, added_by = $3
                """,
                username, title, added_by,
            )

    async def remove_channel(self, username: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE channels SET active = FALSE WHERE username = $1",
                username,
            )

    async def add_group(self, group_id: int, title: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                r"""
                INSERT INTO groups_bot (group_id, title) VALUES ($1, $2)
                ON CONFLICT (group_id) DO UPDATE SET title = $2
                """,
                group_id, title,
            )

    async def get_groups(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch("SELECT * FROM groups_bot ORDER BY added_at DESC")

    async def get_all_users(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT telegram_id FROM users WHERE is_blocked = FALSE"
            )

    async def block_user(self, telegram_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET is_blocked = TRUE WHERE telegram_id = $1",
                telegram_id,
            )

    async def unblock_user(self, telegram_id: int):
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE users SET is_blocked = FALSE WHERE telegram_id = $1",
                telegram_id,
            )

    async def is_blocked(self, telegram_id: int):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT is_blocked FROM users WHERE telegram_id = $1", telegram_id
            )
            return row["is_blocked"] if row else False

    async def get_blocked_users(self):
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT telegram_id, fullname, username FROM users WHERE is_blocked = TRUE ORDER BY telegram_id"
            )

    async def get_music_file_id(self, title: str, artist: str):
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT file_id FROM musics WHERE title = $1 AND artist = $2",
                title, artist,
            )
            return row["file_id"] if row else None

    async def save_music_file_id(self, title: str, artist: str, file_id: str):
        async with self.pool.acquire() as conn:
            await conn.execute(
                r"""
                INSERT INTO musics (title, artist, file_id) VALUES ($1, $2, $3)
                ON CONFLICT (title, artist) DO UPDATE SET file_id = $3
                """,
                title, artist, file_id,
            )

# ========================== GLOBAL STATE ==========================
user_search_state: dict[int, dict] = {}
admin_state: dict[int, dict] = {}
video_recognition_state: dict[int, dict] = {}
link_download_state: dict[int, dict[str, Any]] = {}

db: Database | None = None

# ========================== KEYBOARDS ==========================
def lang_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🇺🇿 O'zbekcha", callback_data="lang:uz"),
                InlineKeyboardButton(text="🇺🇿 Ўзбекча", callback_data="lang:uz_kr"),
            ],
            [
                InlineKeyboardButton(text="🇷🇺 Русский", callback_data="lang:ru"),
                InlineKeyboardButton(text="🇺🇸 English", callback_data="lang:en"),
            ],
        ]
    )


def main_menu_kb(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🌐 " + (
                        "Tilni o'zgartirish" if lang == "uz"
                        else "Тилни ўзгартириш" if lang == "uz_kr"
                        else "Сменить язык" if lang == "ru"
                        else "Change language"
                    ),
                    callback_data="lang_menu",
                )
            ],
            [
                InlineKeyboardButton(
                    text="👤 " + ("Profil" if lang == "uz" else "Профил" if lang == "uz_kr" else "Профиль" if lang == "ru" else "Profile"),
                    callback_data="profile",
                )
            ],
        ]
    )


# ========================== ADMIN KEYBOARDS ==========================
def admin_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📢 Majburiy obuna", callback_data="admin:channels"),
                InlineKeyboardButton(text="🌐 Tillar", callback_data="admin:langs"),
            ],
            [
                InlineKeyboardButton(text="📊 Analitika", callback_data="admin:stats"),
                InlineKeyboardButton(text="👥 Guruhlar", callback_data="admin:groups"),
            ],
            [
                InlineKeyboardButton(text="📨 Reklama", callback_data="admin:broadcast"),
                InlineKeyboardButton(text="🚫 Blacklist", callback_data="admin:blacklist"),
            ],
            [
                InlineKeyboardButton(text="❌ Panelni yopish", callback_data="admin:close"),
            ],
        ]
    )


def admin_back_kb(to: str = "admin:back") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Orqaga", callback_data=to)]
        ]
    )


def channels_kb(channels: list) -> InlineKeyboardMarkup:
    kb = []
    for ch in channels:
        kb.append([
            InlineKeyboardButton(
                text=f"❌ {ch['title'] or ch['username']}",
                callback_data=f"admin:ch_del:{ch['username']}"
            )
        ])
    kb.append([InlineKeyboardButton(text="➕ Kanal qo'shish", callback_data="admin:ch_add")])
    kb.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin:back")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def blacklist_kb(users: list) -> InlineKeyboardMarkup:
    kb = []
    for u in users:
        name = u["fullname"] or f"ID:{u['telegram_id']}"
        kb.append([
            InlineKeyboardButton(
                text=f"✅ {name}",
                callback_data=f"admin:unblock:{u['telegram_id']}"
            )
        ])
    kb.append([InlineKeyboardButton(text="➕ Block qilish", callback_data="admin:block_add")])
    kb.append([InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin:back")])
    return InlineKeyboardMarkup(inline_keyboard=kb)


def broadcast_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📝 Matn yuborish", callback_data="admin:bc_text"),
                InlineKeyboardButton(text="📎 Media yuborish", callback_data="admin:bc_media"),
            ],
            [InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin:back")],
        ]
    )


def broadcast_skip_caption_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏭️ O'tkazib yuborish", callback_data="admin:bc_skip")],
        ]
    )

# ========================== LINK HELPERS ==========================
def is_social_media_url(text: str) -> bool:
    patterns = [
        r'https?://(?:www\.)?instagram\.com/\S+',
        r'https?://(?:www\.)?youtube\.com/\S+',
        r'https?://youtu\.be/\S+',
        r'https?://(?:www\.)?tiktok\.com/\S+',
        r'https?://(?:www\.)?vt\.tiktok\.com/\S+',
        r'https?://(?:www\.)?vm\.tiktok\.com/\S+',
        r'https?://(?:www\.)?facebook\.com/\S+',
        r'https?://(?:www\.)?twitter\.com/\S+',
        r'https?://(?:www\.)?x\.com/\S+',
        r'https?://(?:www\.)?soundcloud\.com/\S+',
        r'https?://(?:www\.)?likee\.video/\S+',
        r'https?://l\.likee\.video/\S+',
        r'https?://(?:www\.)?threads\.net/\S+',
        r'https?://(?:www\.)?threads\.com/\S+',
        r'https?://(?:www\.)?pinterest\.com/\S+',
        r'https?://(?:www\.)?pin\.it/\S+',
        r'https?://(?:www\.)?snapchat\.com/\S+',
        r'https?://(?:www\.)?reddit\.com/\S+',
        r'https?://(?:www\.)?vimeo\.com/\S+',
        r'https?://(?:www\.)?dailymotion\.com/\S+',
        r'https?://(?:www\.)?twitch\.tv/\S+',
        r'https?://(?:www\.)?vk\.com/\S+',
        r'https?://(?:www\.)?ok\.ru/\S+',
    ]
    return any(re.search(p, text) for p in patterns)

async def resolve_short_url(url: str) -> str:
    """Qisqa URL'larni to'liq URL'ga o'zgartirish"""
    
    # TikTok short URL - vm.tiktok.com, vt.tiktok.com
    # MUHIM: TikTok qisqa linklarni resolve qilmaymiz!
    # yt-dlp o'zining redirect handler'ini ishlatadi
    if re.search(r'https?://(?:vt|vm)\.tiktok\.com/\S+', url):
        logging.info(f"[TikTok] Short URL o'z holida qoldirildi: {url}")
        return url  # O'zgarishsiz qaytarish
    
    # Pinterest short URL - pin.it
    if re.search(r'https?://(?:www\.)?pin\.it/\S+', url):
        try:
            import urllib.request
            import ssl
            
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            
            req = urllib.request.Request(url, headers={
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Referer': 'https://www.pinterest.com/',
            })
            with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
                final_url = resp.geturl()
                if final_url != url and 'pinterest.com' in final_url:
                    logging.info(f"[Pinterest] Short URL resolved: {url} -> {final_url}")
                    return final_url
        except Exception as e:
            logging.warning(f"[Pinterest] Short URL resolve error: {e}")
    
    # Likee short URL - l.likee.video/v/XXXX
    if re.search(r'https?://l\.likee\.video/v/\S+', url):
        match = re.search(r'/v/([A-Za-z0-9]+)', url)
        if match:
            post_id = match.group(1)
            try:
                import urllib.request
                import ssl
                ctx = ssl.create_default_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                
                api_url = f"https://likee.video/v/{post_id}"
                req = urllib.request.Request(api_url, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.5',
                })
                with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
                    final_url = resp.geturl()
                    if 'trending' in final_url or final_url == 'https://likee.video/':
                        logging.error(f"[Likee] Post ID topilmadi, redirect: {final_url}")
                        return url
                    return final_url
            except Exception as e:
                logging.warning(f"[Likee] URL resolve error: {e}")
    
    return url

async def download_video(url: str) -> tuple[str | None, dict[str, Any] | None]:
    """
    Instagram, YouTube va boshqa platformalardan video/rasm yuklash.
    Pinterest'da video topilmasa rasm yuklaydi (fallback).
    """

    # ==================== PROXY SOZLAMALARI ====================
    # Proxy ni yoqish/o'chirish - True/False
    USE_PROXY = True

    # HTTP Proxy (agar kerak bo'lsa) - o'zingizning proxyingizni kiriting
    # Format: "http://user:pass@host:port" yoki "http://host:port"
    HTTP_PROXY = "http://user:pass@proxy_host:port"
    HTTPS_PROXY = "http://user:pass@proxy_host:port"
    SOCKS_PROXY = "socks5://user:pass@proxy_host:port"  # SOCKS5 proxy

    # Tekshirilgan bepul proxy ro'yxati (ishlaydiganini tanlang)
    # Eslatma: Bepul proxy tez-tez o'zgaradi, o'zingizning proxyingizni ishlating
    # Webshare.io - 10 ta bepul proxy (ro'yxatdan o'tish kerak)
    # Bright Data - 15 ta bepul proxy + 2GB/oy
    FREE_PROXIES = [
        None,  # Proxy siz (birinchi urinish)
        # Quyidagilarni o'zingizning proxyingiz bilan almashtiring:
        # "http://proxy1.example.com:8080",
        # "http://proxy2.example.com:3128",
        # "socks5://127.0.0.1:1080",  # Local SOCKS5 (masalan, Tor)
    ]
    # ==========================================================

    def _download():
        cookie_path = get_cookiefile()

        def _try_video_download(prefix: str, use_cookies: bool = True, extra_opts: dict = None, proxy_url: str = None) -> tuple[str | None, dict] | None:
            output_template = os.path.join(tempfile.gettempdir(), f"{prefix}_{datetime.now().timestamp():.0f}_%(ext)s")

            ydl_opts = {
                'format': 'best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best',
                'outtmpl': output_template,
                'quiet': True,
                'no_warnings': True,
                'ignoreerrors': True,
                'socket_timeout': 60,
                'retries': 10,
                'fragment_retries': 10,
                'file_access_retries': 5,
                'nocheckcertificate': True,
                'geo_bypass': True,
                'geo_bypass_country': 'US',
                'headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
                    'Accept-Language': 'en-US,en;q=0.5',
                },
            }

            # Proxy sozlamalari
            if USE_PROXY and proxy_url:
                ydl_opts['proxy'] = proxy_url
                logging.info(f"[Proxy] Proxy ishlatilmoqda: {proxy_url[:30]}...")

            # Threads uchun maxsus sozlamalar
            if "threads" in url.lower():
                ydl_opts.update({
                    'extractor_args': {
                        'generic': {
                            'referer': 'https://www.threads.net/',
                        }
                    },
                })
            
            # TikTok uchun maxsus sozlamalar (qisqa linklar uchun MUHIM)
            if "tiktok" in url.lower() or "vt.tiktok" in url.lower() or "vm.tiktok" in url.lower():
                ydl_opts.update({
                    'extractor_args': {
                        'tiktok': {
                            'webpage_download': True,
                            'app_info': '1180',
                        }
                    },
                    'headers': {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                        'Accept-Language': 'en-US,en;q=0.5',
                        'Referer': 'https://www.tiktok.com/',
                    },
                    'geo_bypass': True,
                    'geo_bypass_country': 'US',
                })
            
            # Instagram uchun maxsus sozlamalar
            if "instagram" in url.lower():
                ydl_opts.update({
                    'extract_flat': False,
                    'playlist_items': '1',
                    'format': 'best[ext=mp4]/bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best',
                })

            # Likee uchun maxsus sozlamalar
            if "likee" in url.lower():
                ydl_opts.update({
                    'extractor_args': {
                        'likee': {
                            'app_id': 'likee-2311',
                        }
                    },
                })

            
            # Pinterest uchun maxsus sozlamalar
            if "pinterest" in url.lower():
                ydl_opts.update({
                    'extractor_args': {
                        'pinterest': {
                            'no_check_certificate': True,
                        }
                    },
                    'format': 'best[ext=mp4]/bestvideo[ext=mp4]+bestaudio/best/bestimage/best',  # Rasm ham qidirish
                })

            # Cookies faqat so'ralganda qo'shiladi
            # FIXED: Instagram uchun cookies ishlatma (rate limitni tezlashtiradi)
            if cookie_path and "instagram" not in url.lower():
                ydl_opts['cookiefile'] = cookie_path

            if extra_opts:
                ydl_opts.update(extra_opts)

            def extract_metadata(entry, info):
                title = None
                if isinstance(entry, dict):
                    title = entry.get('title')
                if not title and isinstance(info, dict):
                    title = info.get('title')
                metadata = {"title": title} if title else {}
                return metadata

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    if info:
                        entries = info.get('entries', [info])
                        for entry in entries:
                            if not entry:
                                continue
                            downloaded_path = ydl.prepare_filename(entry)
                            metadata = extract_metadata(entry, info)
                            if os.path.exists(downloaded_path):
                                return downloaded_path, metadata
                            base = downloaded_path.rsplit(".", 1)[0]
                            for ext in ['.mp4', '.webm', '.mkv', '.mov', '.m4v', '.jpg', '.jpeg', '.png', '.webp']:
                                candidate = base + ext
                                if os.path.exists(candidate):
                                    return candidate, metadata
            except Exception as e:
                logging.warning(f"Video yuklashda xatolik ({prefix}, cookies={'ha' if use_cookies else 'yo\'q'}): {e}")
            return None

        # Proxy bilan yuklash urinishlari
        proxies_to_try = FREE_PROXIES if USE_PROXY else [None]

        for proxy in proxies_to_try:
            proxy_str = proxy[:30] + "..." if proxy and len(proxy) > 30 else (proxy or "Yo'q")
            logging.info(f"[TikTok] Proxy: {proxy_str}")

            # 1-urinish: Cookies SIZ, proxy bilan/yo'q
            result = _try_video_download("dl", use_cookies=False, proxy_url=proxy)
            if result and result[0]:
                return result

            # 2-urinish: Cookies BILAN
            result = _try_video_download("dl_cook", use_cookies=True, proxy_url=proxy)
            if result and result[0]:
                return result

            # 3-urinish: Android client
            result = _try_video_download("dl_and", use_cookies=False, proxy_url=proxy, extra_opts={
                'extractor_args': {
                    'tiktok': {
                        'app_info': '1180',
                    }
                },
                'headers': {
                    'User-Agent': 'Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36',
                },
            })
            if result and result[0]:
                return result

            # 4-urinish: Worst quality
            result = _try_video_download("dl_worst", use_cookies=False, proxy_url=proxy, extra_opts={
                'format': 'worst',
            })
            if result and result[0]:
                return result

        return None, None

    result = await asyncio.to_thread(_download)
    
    # Pinterest fallback - agar video topilmasa rasm yuklashga urinish
    if "pinterest" in url.lower() and (result is None or result[0] is None):
        logging.info("[Pinterest] Video topilmadi, rasm yuklanmoqda (fallback)...")
        fallback_result = await download_pinterest_fallback(url)
        if fallback_result and fallback_result[0]:
            return fallback_result
    
    return result

async def download_pinterest_fallback(url: str) -> tuple[str | None, dict[str, Any] | None]:
    """Pinterest'dan rasm yuklash (yt-dlp ishlamaganda fallback)"""
    def _download():
        import urllib.request
        import ssl
        
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
                'Accept-Language': 'en-US,en;q=0.5',
                'Referer': 'https://www.pinterest.com/',
            }
            
            req = urllib.request.Request(url, headers=headers)
            
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            
            with urllib.request.urlopen(req, timeout=15, context=ctx) as response:
                html = response.read().decode('utf-8', errors='ignore')
            
            image_url = None
            
            # 1. JSON ichidan "images" -> "orig" -> "url" qidirish
            try:
                import json
                json_pattern = r'<script[^>]*type="application/json"[^>]*>(.*?)</script>'
                for match in re.finditer(json_pattern, html, re.DOTALL):
                    try:
                        data = json.loads(match.group(1))
                        def find_image_url(obj):
                            if isinstance(obj, dict):
                                if 'images' in obj and isinstance(obj['images'], dict):
                                    orig = obj['images'].get('orig', {})
                                    if isinstance(orig, dict) and 'url' in orig:
                                        return orig['url']
                                if 'image' in obj and isinstance(obj, dict):
                                    if 'url' in obj['image']:
                                        return obj['image']['url']
                                    if 'images' in obj['image']:
                                        images = obj['image']['images']
                                        if isinstance(images, dict):
                                            orig = images.get('orig', {})
                                            if isinstance(orig, dict) and 'url' in orig:
                                                return orig['url']
                                for v in obj.values():
                                    result = find_image_url(v)
                                    if result:
                                        return result
                            elif isinstance(obj, list):
                                for item in obj:
                                    result = find_image_url(item)
                                    if result:
                                        return result
                            return None
                        
                        img_url = find_image_url(data)
                        if img_url and 'pinimg.com' in img_url:
                            image_url = img_url
                            break
                    except (json.JSONDecodeError, Exception):
                        continue
            except Exception as e:
                logging.warning(f"[Pinterest] JSON parsing error: {e}")
            
            # 2. Agar JSON'dan topilmasa, HTML patternlar bilan qidirish
            if not image_url:
                image_patterns = [
                    r'<img[^>]+data-test-id="pinCloseupImage"[^>]+src="(https://i\.pinimg\.com/[^"]+)"',
                    r'<img[^>]+data-test-id="pinCloseupImage"[^>]+srcset="(https://i\.pinimg\.com/[^ ]+)',
                    r'<img[^>]+src="(https://i\.pinimg\.com/originals/[^"]+)"',
                    r'<img[^>]+src="(https://i\.pinimg\.com/[^"]+\.(?:jpg|jpeg|png|webp))"',
                    r'property="og:image" content="(https://[^"]+pinimg\.com[^"]+)"',
                    r'name="og:image" content="(https://[^"]+pinimg\.com[^"]+)"',
                    r'<meta[^>]+property="twitter:image"[^>]+content="(https://[^"]+pinimg\.com[^"]+)"',
                    r'"image":"(https://[^"]+pinimg\.com[^"]+)"',
                    r'"url":"(https://i\.pinimg\.com/[^"]+)"',
                    r'data-src="(https://i\.pinimg\.com/[^"]+)"',
                    r'src="(https://s\.pinimg\.com/[^"]+)"',
                ]
                
                for pattern in image_patterns:
                    match = re.search(pattern, html)
                    if match:
                        image_url = match.group(1).replace('\\/', '/').split(' ')[0]
                        break
            
            # 3. i.pinimg.com bilan boshlanadigan har qanday src ni qidirish (eng keng qamrovli)
            if not image_url:
                all_pins = re.findall(r'src="(https://i\.pinimg\.com/[^"]+)"', html)
                all_pins += re.findall(r'data-src="(https://i\.pinimg\.com/[^"]+)"', html)
                all_pins += re.findall(r'content="(https://i\.pinimg\.com/[^"]+)"', html)
                all_pins += re.findall(r'url\((https://i\.pinimg\.com/[^)]+)\)', html)
                
                if all_pins:
                    # Eng katta (yuqori sifatli) rasmni tanlash
                    # "originals" yoki eng uzun URL
                    for pin in all_pins:
                        if 'originals' in pin:
                            image_url = pin
                            break
                    if not image_url:
                        # Eng uzun URL (yuqori sifatli odatda)
                        image_url = max(all_pins, key=len)
            
            # 4. Agar hali ham topilmasa, og:image ni qidirish
            if not image_url:
                og_match = re.search(r'<meta[^>]+property="og:image"[^>]+content="(https?://[^"]+)"', html)
                if og_match:
                    image_url = og_match.group(1)
            
            if not image_url:
                logging.warning(f"[Pinterest] Rasm URL topilmadi. HTML uzunligi: {len(html)}")
                # Debug: HTML'dan i.pinimg.com bilan bog'liq barcha narsalarni chiqarish
                debug_pins = re.findall(r'i\.pinimg\.com/[^"\s<>]+', html)
                if debug_pins:
                    logging.info(f"[Pinterest] Debug - topilgan pinimg URL'lar: {debug_pins[:5]}")
                return None, None
            
            # Rasm yuklash
            output_path = os.path.join(tempfile.gettempdir(), f"pinterest_{datetime.now().timestamp():.0f}.jpg")
            req_img = urllib.request.Request(image_url, headers=headers)
            with urllib.request.urlopen(req_img, timeout=30, context=ctx) as response:
                with open(output_path, 'wb') as f:
                    f.write(response.read())
            
            if os.path.exists(output_path) and os.path.getsize(output_path) > 1024:
                # Title ni ham olishga urinish
                title_match = re.search(r'<title>([^<<]+)</title>', html)
                title = title_match.group(1).strip() if title_match else "Pinterest"
                logging.info(f"[Pinterest] Rasm yuklandi: {output_path} ({os.path.getsize(output_path)} bytes)")
                return output_path, {"title": title}
            else:
                logging.warning(f"[Pinterest] Yuklangan fayl juda kichik yoki topilmadi")
                
        except Exception as e:
            logging.warning(f"[Pinterest] Fallback yuklashda xatolik: {e}")
        return None, None
    
    return await asyncio.to_thread(_download)

async def download_audio_by_query(query: str) -> str | None:
    """
    YouTube'dan audio qidirish va yuklash.
    query: "artist - title" formatida
    """
    def _download():
        cookie_path = get_cookiefile()

        def _try_audio(use_cookies: bool = True) -> str | None:
            output_path = os.path.join(tempfile.gettempdir(), f"audio_{datetime.now().timestamp():.0f}.%(ext)s")
            ydl_opts = {
                'format': 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio/best[ext=mp4]/best',
                'outtmpl': output_path,
                'quiet': True,
                'no_warnings': True,
                'default_search': 'ytsearch1',
                'ignoreerrors': True,
                'postprocessors': [{
                    'key': 'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'headers': {
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                    'Accept-Language': 'en-US,en;q=0.9',
                },
            }

            # Cookies faqat so'ralganda
            if use_cookies and cookie_path:
                ydl_opts['cookiefile'] = cookie_path

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    search_query = f"ytsearch1:{query}"
                    info = ydl.extract_info(search_query, download=True)
                    if not info:
                        return None

                    if 'entries' in info and info['entries']:
                        entry = info['entries'][0]
                        downloaded = ydl.prepare_filename(entry)
                    elif 'id' in info:
                        downloaded = ydl.prepare_filename(info)
                    else:
                        return None

                    mp3_path = downloaded.rsplit(".", 1)[0] + ".mp3"
                    if os.path.exists(mp3_path):
                        return mp3_path
                    if os.path.exists(downloaded):
                        return downloaded

                    # Fallback: faylni qidirish
                    base = os.path.join(tempfile.gettempdir(), f"audio_{datetime.now().timestamp():.0f}")
                    for ext in ['.mp3', '.m4a', '.webm', '.opus', '.mp4']:
                        p = base + ext
                        if os.path.exists(p):
                            return p
            except Exception as e:
                logging.warning(f"Audio yuklash xatoligi (cookies={'ha' if use_cookies else 'yo\'q'}): {e}")
            return None

        # Avval cookies SIZ, keyin cookies BILAN
        result = _try_audio(use_cookies=False)
        if result:
            return result
        return _try_audio(use_cookies=True)

    return await asyncio.to_thread(_download)


# ========================== FILE UTILS ==========================
def _cleanup_file(path: str):
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except Exception:
            pass

# ========================== SHAZAM RECOGNITION ==========================
async def extract_audio_ffmpeg(input_path: str) -> str:
    input_path = os.path.normpath(input_path)
    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Video fayl topilmadi: {input_path}")

    output_path = input_path.rsplit(".", 1)[0] + "_audio.mp3"
    output_path = os.path.normpath(output_path)

    ffmpeg_cmd = find_ffmpeg_cmd()
    if not ffmpeg_cmd:
        raise RuntimeError("FFmpeg topilmadi!")

    try:
        proc = await asyncio.create_subprocess_exec(
            ffmpeg_cmd, "-version",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.communicate(), timeout=5)
    except (FileNotFoundError, asyncio.TimeoutError):
        raise RuntimeError("FFmpeg topilmadi!")

    proc = await asyncio.create_subprocess_exec(
        ffmpeg_cmd, "-y", "-i", input_path,
        "-vn", "-ar", "44100", "-ac", "2", "-b:a", "192k",
        output_path,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if not os.path.exists(output_path):
        err_msg = stderr.decode(errors="ignore") if stderr else "Noma'lum xatolik"
        raise RuntimeError(f"FFmpeg audio ajratishda xatolik: {err_msg}")

    return output_path


async def transcribe_audio_to_text(audio_path: str) -> str | None:
    if sr is None:
        logging.warning("[Speech] SpeechRecognition o'rnatilmagan")
        return None
    if not os.path.exists(audio_path):
        return None

    wav_path = audio_path.rsplit(".", 1)[0] + "_speech.wav"
    if not os.path.exists(wav_path):
        ffmpeg_cmd = find_ffmpeg_cmd()
        if not ffmpeg_cmd:
            logging.warning("[Speech] FFmpeg topilmadi, transkripsiya mumkin emas")
            return None

        proc = await asyncio.create_subprocess_exec(
            ffmpeg_cmd, "-y", "-i", audio_path,
            "-ar", "16000", "-ac", "1", wav_path,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode != 0 or not os.path.exists(wav_path):
            logging.warning(f"[Speech] FFmpeg audio konvertatsiya xatolik: {stderr.decode(errors='ignore')[:150]}")
            return None

    try:
        recognizer = sr.Recognizer()
        with sr.AudioFile(wav_path) as source:
            audio = recognizer.record(source)

        for language in ("uz-UZ", "ru-RU", "en-US"):
            try:
                text = recognizer.recognize_google(audio, language=language)
                if text:
                    logging.info(f"[Speech] Transcription ({language}): {text}")
                    return text
            except sr.UnknownValueError:
                continue
            except sr.RequestError as e:
                logging.warning(f"[Speech] recognize_google xatolik: {e}")
                break
    except Exception as e:
        logging.warning(f"[Speech] transkripsiya xatolik: {e}")

    return None


async def recognize_with_shazam(file_path: str):
    """Shazam orqali musiqa aniqlash"""
    if shazam_client is None:
        logging.warning("ShazamIO o'rnatilmagan, aniqlash o'tkazib yuboriladi")
        return None
    try:
        result = await shazam_client.recognize(file_path)
        if result and "track" in result:
            track = result["track"]
            title = track.get("title", "Noma'lum")
            artist = track.get("subtitle", "Noma'lum")
            return {"title": title, "artist": artist}
    except Exception as e:
        logging.warning(f"Shazam recognition error: {e}")
    return None


# ========================== FORCE SUB GROUP ==========================
async def check_and_force_sub_group(message: Message, lang: str) -> bool:
    if message.chat.type not in ("group", "supergroup"):
        return True
    channels = await db.get_channels()
    if not channels:
        return True
    is_subbed = await check_subscriptions(message.bot, message.from_user.id, channels)
    if is_subbed:
        return True
    kb = InlineKeyboardMarkup(inline_keyboard=[])
    for ch in channels:
        kb.inline_keyboard.append([
            InlineKeyboardButton(
                text=f"📢 {ch['title'] or ch['username']}",
                url=f"https://t.me/{ch['username']}",
            )
        ])
    kb.inline_keyboard.append([
        InlineKeyboardButton(
            text=TEXTS[lang]["check_sub"],
            callback_data=f"check_sub_group:{message.from_user.id}:{message.message_id}"
        )
    ])
    await message.reply(TEXTS[lang]["force_sub"], reply_markup=kb)
    return False


# ========================== BROADCAST ==========================
async def broadcast_to_all(bot: Bot, msg: Message, caption: str | None = None):
    users = await db.get_all_users()
    groups = await db.get_groups()
    count = 0
    targets = [u["telegram_id"] for u in users] + [g["group_id"] for g in groups]
    for target_id in targets:
        try:
            await msg.copy_to(target_id, caption=caption)
            count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    return count

# ========================== ROUTER & HANDLERS ==========================
router = Router()


async def get_lang(user_id: int) -> str:
    user = await db.get_user(user_id)
    return user["language"] if user else "uz"


# ========================== SEARCH & PAGINATION ==========================
async def build_search_page(user_id: int, page: int):
    state = user_search_state.get(user_id)
    if not state:
        return None, None

    results = state["results"]
    query = state.get("query", "")
    per_page = RESULTS_PER_PAGE
    total_pages = (len(results) + per_page - 1) // per_page
    start = page * per_page
    end = min(start + per_page, len(results))
    page_results = results[start:end]

    lang = await get_lang(user_id)
    text = f"🔍 <b>{query}</b>\n\n" if query else TEXTS[lang]["search_results"]
    kb: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for i, item in enumerate(page_results, start=start + 1):
        title = item.get("title", "Noma'lum")
        uploader = item.get("uploader") or item.get("channel", "—")
        duration = format_duration(item.get("duration"))

        if len(title) > 45:
            title = title[:43] + "…"

        text += f"{i}. 🎵 <b>{title}</b> — {uploader} <i>[{duration}]</i>\n"

        btn = InlineKeyboardButton(
            text=f"{i}",
            callback_data=f"music:{user_id}:{start + i - 1}"
        )
        row.append(btn)
        if len(row) == 5:
            kb.append(row)
            row = []

    if row:
        kb.append(row)

    # Navigatsiya tugmalari
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"page:{user_id}:{page - 1}"))
    else:
        nav_row.append(InlineKeyboardButton(text="◀️", callback_data="noop"))

    nav_row.append(InlineKeyboardButton(text="❌", callback_data="close"))

    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"page:{user_id}:{page + 1}"))
    else:
        nav_row.append(InlineKeyboardButton(text="▶️", callback_data="noop"))

    kb.append(nav_row)

    text += f"\n<i>@{BOT_USERNAME} | Sahifa {page + 1}/{total_pages}</i>"

    return text, InlineKeyboardMarkup(inline_keyboard=kb)


@router.callback_query(F.data == "noop")
async def noop_callback(call: CallbackQuery):
    await call.answer()


@router.callback_query(F.data.startswith("page:"))
async def page_callback(call: CallbackQuery):
    _, user_id_str, page_str = call.data.split(":")
    user_id = int(user_id_str)
    page = int(page_str)

    if user_id != call.from_user.id:
        await call.answer("❌ Bu sizning qidiruvingiz emas!", show_alert=True)
        return

    state = user_search_state.get(user_id)
    if not state:
        await call.answer("❌ Qidiruv topilmadi", show_alert=True)
        return

    state["page"] = page
    text, markup = await build_search_page(user_id, page)
    if text:
        await call.message.edit_text(text, reply_markup=markup)
    await call.answer()


@router.callback_query(F.data == "close")
async def close_callback(call: CallbackQuery):
    await call.message.delete()
    await call.answer()

@router.callback_query(F.data.startswith("music:"))
async def select_music(call: CallbackQuery):
    _, user_id_str, idx_str = call.data.split(":")
    user_id = int(user_id_str)
    idx = int(idx_str)

    if user_id != call.from_user.id:
        await call.answer("❌ Bu sizning qidiruvingiz emas!", show_alert=True)
        return

    state = user_search_state.get(user_id)
    if not state or idx >= len(state["results"]):
        await call.answer("❌ Amal qilmadi", show_alert=True)
        return

    item = state["results"][idx]
    title = item.get("title", "Noma'lum")
    uploader = item.get("uploader") or item.get("channel", "—")
    url = item.get("url") or item.get("webpage_url", "")
    if not url:
        url = f"https://www.youtube.com/watch?v={item.get('id', '')}"

    lang = await get_lang(user_id)

    await call.answer()

    # Audio yuklash (Piped → yt-dlp fallback)
    audio_path = await download_youtube_audio(url, f"{uploader} - {title}")

    if not audio_path or not os.path.exists(audio_path):
        await call.message.answer(TEXTS[lang]["download_failed_group"], reply_markup=build_share_kb())
        return

    # Fayl hajmi tekshirish
    size_mb = os.path.getsize(audio_path) / (1024 * 1024)
    if size_mb > MAX_SIZE_MB:
        await call.message.answer(
            TEXTS[lang]["file_too_large"].format(size_mb=f"{size_mb:.1f}", limit_mb=MAX_SIZE_MB)
        )
        _cleanup_file(audio_path)
        return

    # Audio yuborish - varyantlar xabari o'chirilmaydi, faqat audio yuboriladi
    await call.message.answer_audio(
        audio=FSInputFile(audio_path),
        title=title,
        performer=uploader,
        caption=SHARE_PROMO_CAPTION,
        reply_markup=build_share_kb(),
    )
    _cleanup_file(audio_path)
    # State saqlanadi - foydalanuvchi boshqa qo'shiqlarni ham tanlashi mumkin

@router.callback_query(F.data.startswith("dl_music:"))
async def dl_music_callback(call: CallbackQuery):
    """Qo'shiqni yuklab olish — variantlar ro'yxatini chiqaradi"""
    parts = call.data.split(":")
    if len(parts) < 2:
        await call.answer("❌", show_alert=True)
        return

    user_id = int(parts[1])
    if call.from_user.id != user_id:
        await call.answer("❌ Bu sizning videongiz emas!", show_alert=True)
        return

    state = link_download_state.get(user_id)
    if not state:
        await call.answer("❌ Fayl topilmadi. Qayta yuklang.", show_alert=True)
        return

    lang = await get_lang(user_id)
    path = state.get("path")
    metadata_title = state.get("title")

    await call.answer()

    try:
        # Shazam orqali aniqlash
        query = None
        if path and os.path.exists(path):
            try:
                recognize_path = path
                if path.lower().endswith(('.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv')):
                    recognize_path = await extract_audio_ffmpeg(path)

                shazam_result = await recognize_with_shazam(recognize_path)

                if recognize_path != path:
                    _cleanup_file(recognize_path)

                if shazam_result:
                    title = shazam_result.get("title", "")
                    artist = shazam_result.get("artist", "")
                    query = f"{artist} {title}".strip()
            except Exception as e:
                logging.warning(f"Shazam xatolik: {e}")

        if not query and metadata_title:
            query = normalize_search_query(metadata_title)
        if not query and path:
            query = build_query_from_filename(path)

        if not query:
            await call.message.answer(TEXTS[lang]["not_recognized"])
            return

        # YouTube qidirish - loading xabarisiz
        results = await search_youtube_tracks(query, max_results=15)

        if not results:
            await call.message.answer(TEXTS[lang]["music_not_found_group"], reply_markup=build_share_kb())
            return

        # Natijalarni saqlash
        user_search_state[user_id] = {
            "results": results,
            "page": 0,
            "query": query,
            "source": "youtube",
        }

        text, markup = await build_search_page(user_id, 0)
        if text:
            await call.message.answer(text, reply_markup=markup)

    except Exception as e:
        logging.error(f"dl_music error: {e}")
        await call.message.answer(TEXTS[lang]["download_error"], reply_markup=build_share_kb())


@router.callback_query(F.data.startswith("find_music:"))
async def find_music_callback(call: CallbackQuery):
    parts = call.data.split(":")
    if len(parts) < 2:
        await call.answer("❌", show_alert=True)
        return

    user_id = int(parts[1])
    if call.from_user.id != user_id:
        await call.answer("❌ Bu sizning videongiz emas!", show_alert=True)
        return

    state = link_download_state.pop(user_id, None)
    path = state.get("path") if isinstance(state, dict) else None
    metadata_title = state.get("title") if isinstance(state, dict) else None
    if not path or not os.path.exists(path):
        await call.answer("❌ Fayl topilmadi. Qayta yuklang.", show_alert=True)
        return

    await call.answer()

    lang = await get_lang(user_id)
    tmp_path = None
    recognize_path = None
    shazam_result = None

    try:
        tmp_path = path
        ext = path.split(".")[-1] if "." in path else "mp4"

        # Video bo'lsa audio ajratish
        if tmp_path.lower().endswith(('.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv')):
            recognize_path = await extract_audio_ffmpeg(tmp_path)
        else:
            recognize_path = tmp_path

        # Shazam orqali aniqlash
        shazam_result = await recognize_with_shazam(recognize_path)

    except Exception as e:
        logging.warning(f"Shazam aniqlashda xatolik: {e}")
        shazam_result = None
    finally:
        # Vaqtinchalik fayllarni tozalash
        if recognize_path and recognize_path != tmp_path and recognize_path != path:
            _cleanup_file(recognize_path)

    # Shazam topgan bo'lsa, uning natijalari bilan qidirish
    if shazam_result:
        title = shazam_result.get("title", "Noma'lum")
        artist = shazam_result.get("artist", "Noma'lum")
        query = f"{artist} {title}".strip()
    else:
        query = None
        if metadata_title:
            query = normalize_search_query(metadata_title)

        if not query:
            transcript = await transcribe_audio_to_text(path)
            if transcript:
                query = normalize_search_query(transcript) or transcript
                logging.info(f"[Speech] qidiruv so'rovi: {query}")

        if not query:
            query = build_query_from_filename(path)

        if not query:
            await call.message.answer(TEXTS[lang]["not_recognized"])
            _cleanup_file(path)
            return

    # YouTube orqali qidirish - loading xabarisiz
    try:
        youtube_results = await search_youtube_tracks(query, max_results=15)

        if youtube_results:
            user_search_state[user_id] = {
                "results": youtube_results,
                "page": 0,
                "query": f"🎵 {query}" if shazam_result else query,
                "source": "youtube",
            }

            text, markup = await build_search_page(user_id, 0)
            if text:
                await call.message.answer(text, reply_markup=markup)
        else:
            await call.message.answer(TEXTS[lang]["music_not_found_group"], reply_markup=build_share_kb())
    except Exception as e:
        logging.error(f"YouTube qidiruvda xatolik: {e}")
        await call.message.answer(TEXTS[lang]["download_error"], reply_markup=build_share_kb())
    finally:
        _cleanup_file(path)

# ========================== START / HELP / PROFILE ==========================
@router.message(Command("start"))
async def cmd_start(message: Message):
    if message.from_user.id == ADMIN_ID:
        await message.answer(TEXTS["uz"]["admin_welcome"], reply_markup=admin_main_kb())
        return

    user = await db.get_user(message.from_user.id)
    if not user or not user.get("language"):
        await message.answer(TEXTS["uz"]["choose_lang"], reply_markup=lang_kb())
        return

    lang = user["language"]
    channels = await db.get_channels()
    is_subbed = await check_subscriptions(message.bot, message.from_user.id, channels)

    if not is_subbed:
        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for ch in channels:
            kb.inline_keyboard.append([
                InlineKeyboardButton(text=f"📢 {ch['title']}", url=f"https://t.me/{ch['username']}")
            ])
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=TEXTS[lang]["check_sub"], callback_data="check_sub")
        ])
        await message.answer(TEXTS[lang]["force_sub"], reply_markup=kb)
        return

    await message.answer(TEXTS[lang]["welcome"], reply_markup=main_menu_kb(lang))
    await db.add_user(message.from_user.id, message.from_user.full_name, message.from_user.username)


@router.callback_query(F.data.startswith("lang:"))
async def set_language(call: CallbackQuery):
    lang = call.data.split(":")[1]
    await db.add_user(call.from_user.id, call.from_user.full_name, call.from_user.username)
    await db.set_language(call.from_user.id, lang)
    await call.message.delete()

    if call.from_user.id == ADMIN_ID:
        await call.message.answer(TEXTS["uz"]["admin_welcome"], reply_markup=admin_main_kb())
        return

    channels = await db.get_channels()
    is_subbed = await check_subscriptions(call.bot, call.from_user.id, channels)
    if not is_subbed:
        kb = InlineKeyboardMarkup(inline_keyboard=[])
        for ch in channels:
            kb.inline_keyboard.append([
                InlineKeyboardButton(text=f"📢 {ch['title']}", url=f"https://t.me/{ch['username']}")
            ])
        kb.inline_keyboard.append([
            InlineKeyboardButton(text=TEXTS[lang]["check_sub"], callback_data="check_sub")
        ])
        await call.message.answer(TEXTS[lang]["force_sub"], reply_markup=kb)
        return

    await call.message.answer(TEXTS[lang]["welcome"], reply_markup=main_menu_kb(lang))


@router.callback_query(F.data == "check_sub")
async def check_sub(call: CallbackQuery):
    user = await db.get_user(call.from_user.id)
    lang = user["language"] if user else "uz"
    channels = await db.get_channels()
    is_subbed = await check_subscriptions(call.bot, call.from_user.id, channels)
    if not is_subbed:
        await call.answer(TEXTS[lang]["not_subscribed"], show_alert=True)
        return
    await call.message.delete()
    await call.message.answer(TEXTS[lang]["welcome"], reply_markup=main_menu_kb(lang))


@router.callback_query(F.data.startswith("check_sub_group:"))
async def check_sub_group_callback(call: CallbackQuery):
    parts = call.data.split(":")
    if len(parts) < 3:
        await call.answer("❌", show_alert=True)
        return
    target_user_id = int(parts[1])
    if call.from_user.id != target_user_id:
        await call.answer("❌ Bu sizning xabaringiz emas!", show_alert=True)
        return
    lang = await get_lang(call.from_user.id)
    channels = await db.get_channels()
    is_subbed = await check_subscriptions(call.bot, call.from_user.id, channels)
    if not is_subbed:
        await call.answer(TEXTS[lang]["not_subscribed"], show_alert=True)
        return
    await call.answer(TEXTS[lang]["sub_ok_group"], show_alert=True)
    try:
        await call.message.delete()
    except Exception:
        pass


@router.message(Command("help"))
async def help_cmd(message: Message):
    lang = await get_lang(message.from_user.id)
    await message.answer(TEXTS[lang]["help_text"])

# ========================== TEXT HANDLER (LINK + MUSIC SEARCH) ==========================
@router.message(F.text)
async def text_handler(message: Message):
    await db.add_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if await db.is_blocked(message.from_user.id):
        return
    await db.update_activity(message.from_user.id)
    lang = await get_lang(message.from_user.id)

    if not await check_and_force_sub_group(message, lang):
        return

    # Admin state handlers
    if message.from_user.id == ADMIN_ID and message.from_user.id in admin_state:
        state_info = admin_state[message.from_user.id]
        state = state_info.get("state")

        if state == "waiting_broadcast_text":
            admin_state.pop(message.from_user.id, None)
            count = await broadcast_to_all(message.bot, message)
            await message.answer(TEXTS["uz"]["broadcast_done"].format(count=count))
            await message.answer(TEXTS["uz"]["admin_welcome"], reply_markup=admin_main_kb())
            return
        elif state == "waiting_broadcast_media":
            admin_state[message.from_user.id] = {"state": "waiting_broadcast_caption", "media_msg": message}
            await message.answer(TEXTS["uz"]["broadcast_ask_caption"], reply_markup=broadcast_skip_caption_kb())
            return
        elif state == "waiting_broadcast_caption":
            media_msg = admin_state[message.from_user.id].get("media_msg")
            caption_text = message.text.strip() if message.text else ""
            admin_state.pop(message.from_user.id, None)
            if caption_text == "/skip":
                caption_text = None
            if media_msg:
                count = await broadcast_to_all(message.bot, media_msg, caption=caption_text)
                await message.answer(TEXTS["uz"]["broadcast_done"].format(count=count))
            else:
                await message.answer("❌ Media topilmadi.")
            await message.answer(TEXTS["uz"]["admin_welcome"], reply_markup=admin_main_kb())
            return
        elif state == "waiting_channel":
            admin_state.pop(message.from_user.id, None)
            username = message.text.strip().replace("@", "")
            try:
                await db.add_channel(username, username, ADMIN_ID)
                await message.answer(TEXTS["uz"]["channel_add"])
            except Exception as e:
                await message.answer(f"❌ Xatolik: {e}")
            await message.answer(TEXTS["uz"]["admin_welcome"], reply_markup=admin_main_kb())
            return
        elif state == "waiting_block_id":
            admin_state.pop(message.from_user.id, None)
            try:
                target_id = int(message.text.strip())
                await db.block_user(target_id)
                await message.answer(TEXTS["uz"]["blacklist_add"])
            except ValueError:
                await message.answer(TEXTS["uz"]["invalid_id"])
            except Exception as e:
                await message.answer(f"❌ Xatolik: {e}")
            await message.answer(TEXTS["uz"]["admin_welcome"], reply_markup=admin_main_kb())
            return

    # LINK CHECK: Instagram, YouTube, TikTok, Likee, Threads, Pinterest, Snapchat
    if is_social_media_url(message.text):
        try:
            # URL'ni normalize qilish
            url_text = message.text.strip()
            
            # Threads: threads.com -> threads.net
            if re.search(r'https?://(?:www\.)?threads\.com/', url_text, flags=re.I):
                url_text = re.sub(r'^(https?://)(?:www\.)?threads\.com/', r"\1www.threads.net/", url_text, flags=re.I)
            
            # MUHIM: Qisqa URL'larni to'liq URL'ga o'zgartirish
            url_text = await resolve_short_url(url_text)

            downloaded_path, downloaded_meta = await download_video(url_text)
            if not downloaded_path or not os.path.exists(downloaded_path):
                # Instagram uchun fallback usullar
                if "instagram" in message.text.lower():
                    logging.info(f"[Instagram] Fallback boshlanmoqda: {message.text}")

                    # 1. ENG ISHONCHLI: download_instagram_direct (API + Embed + Page)
                    direct_path, direct_meta = await download_instagram_direct(message.text)
                    if direct_path and os.path.exists(direct_path):
                        file_size_mb = os.path.getsize(direct_path) / (1024 * 1024)
                        ext = direct_path.split(".")[-1].lower()

                        # FIXED: is_video metadata dan foydalanish
                        is_actually_video = direct_meta.get("is_video", False) if direct_meta else False

                        # Agar metadata yo'q bo'lsa, ext orqali tekshirish
                        if not is_actually_video:
                            is_actually_video = ext in ('mp4', 'mov', 'avi', 'mkv', 'webm')

                        if ext in ('jpg', 'jpeg', 'png', 'webp', 'gif', 'bmp') or not is_actually_video:
                            # Rasm
                            if file_size_mb <= 10:
                                await message.answer_photo(
                                    photo=FSInputFile(direct_path),
                                    caption=SHARE_PROMO_CAPTION,
                                    reply_markup=build_share_kb(),
                                )
                            else:
                                await message.answer(SHARE_PROMO_CAPTION, reply_markup=build_share_kb())
                            _cleanup_file(direct_path)
                            return
                        else:
                            # Video
                            link_download_state[message.from_user.id] = {
                                "path": direct_path,
                                "title": direct_meta.get("title") if direct_meta else None,
                            }
                            kb = build_video_kb(message.from_user.id, lang)
                            if file_size_mb <= 50:
                                await message.answer_video(
                                    video=FSInputFile(direct_path),
                                    caption=SHARE_PROMO_CAPTION,
                                    reply_markup=kb,
                                )
                            else:
                                await message.answer(SHARE_PROMO_CAPTION, reply_markup=kb)
                            return

                    # 2. Eski embed usuli (agar yangisi ishlamasa)
                    embed_path = await download_instagram_embed(message.text)
                    if embed_path and os.path.exists(embed_path):
                        file_size_mb = os.path.getsize(embed_path) / (1024 * 1024)
                        ext = embed_path.split(".")[-1].lower()

                        if ext in ('jpg', 'jpeg', 'png', 'webp'):
                            await message.answer_photo(
                                photo=FSInputFile(embed_path),
                                caption=SHARE_PROMO_CAPTION,
                                reply_markup=build_share_kb(),
                            )
                            _cleanup_file(embed_path)
                            return
                        else:
                            link_download_state[message.from_user.id] = {
                                "path": embed_path,
                                "title": None,
                            }
                            kb = build_video_kb(message.from_user.id, lang)
                            if file_size_mb <= 50:
                                await message.answer_video(
                                    video=FSInputFile(embed_path),
                                    caption=SHARE_PROMO_CAPTION,
                                    reply_markup=kb,
                                )
                            else:
                                await message.answer(SHARE_PROMO_CAPTION, reply_markup=kb)
                            return

                    # 3. API usuli
                    api_path = await download_instagram_api(message.text)
                    if api_path and os.path.exists(api_path):
                        await message.answer_photo(
                            photo=FSInputFile(api_path),
                            caption=TEXTS[lang]["photo_from_link"],
                            reply_markup=build_share_kb(),
                        )
                        _cleanup_file(api_path)
                        return

                    # Hamma usul ishlamasa
                    logging.error(f"[Instagram] Barcha usullar ishlamadi: {message.text}")
                    # Rate limiting oldini olish uchun 2 soniya kutish
                    await asyncio.sleep(2)
                    await message.answer(TEXTS[lang]["download_failed_group"], reply_markup=build_share_kb())
                    return

                await message.answer(TEXTS[lang]["download_failed_group"], reply_markup=build_share_kb())
                return

            file_size_mb = os.path.getsize(downloaded_path) / (1024 * 1024)
            ext = downloaded_path.split(".")[-1].lower() if "." in downloaded_path else ""

            # Fayl turini aniqlash (ext emas, MIME type bilan)
            import mimetypes
            mime_type, _ = mimetypes.guess_type(downloaded_path)
            is_video_file = mime_type and mime_type.startswith('video/')
            is_image_file = mime_type and mime_type.startswith('image/')

            # Agar ext noto'g'ri bo'lsa, MIME type'ga qarab aniqlash
            if not is_video_file and not is_image_file:
                # Fallback: fayl boshidagi signature'ni tekshirish
                with open(downloaded_path, 'rb') as f_check:
                    header = f_check.read(12)
                    # MP4: b'\x00\x00\x00 ftyp' yoki b'\x00\x00\x00\x18ftyp'
                    # JPEG: b'\xff\xd8\xff'
                    # PNG: b'\x89PNG\r\n\x1a\n'
                    if header.startswith(b'\xff\xd8\xff'):
                        is_image_file = True
                    elif header.startswith(b'\x89PNG'):
                        is_image_file = True
                    elif header.startswith(b'\x00\x00\x00') and b'ftyp' in header[:20]:
                        is_video_file = True
                    elif header.startswith(b'RIFF') or header.startswith(b'WEBP'):
                        is_video_file = True  # yoki rasm

            # Rasm bo'lsa
            if is_image_file or ext in ('jpg', 'jpeg', 'png', 'webp', 'gif', 'bmp'):
                if file_size_mb <= 10:
                    await message.answer_photo(
                        photo=FSInputFile(downloaded_path),
                        caption=SHARE_PROMO_CAPTION,
                        reply_markup=build_share_kb(),
                    )
                else:
                    await message.answer(SHARE_PROMO_CAPTION, reply_markup=build_share_kb())
                _cleanup_file(downloaded_path)
                return

            # Video bo'lsa (yoki video deb topilgan)
            link_download_state[message.from_user.id] = {
                "path": downloaded_path,
                "title": downloaded_meta.get("title") if downloaded_meta else None,
            }

            kb = build_video_kb(message.from_user.id, lang)

            # Video sifatida yuborish (hatto ext noto'g'ri bo'lsa ham)
            if file_size_mb <= 50:
                try:
                    await message.answer_video(
                        video=FSInputFile(downloaded_path),
                        caption=SHARE_PROMO_CAPTION,
                        reply_markup=kb,
                    )
                except Exception as e:
                    # Agar video sifatida yuborish xato bersa, rasm sifatida urinib ko'rish
                    logging.warning(f"Video sifatida yuborish xato: {e}, rasm sifatida urinilmoqda")
                    await message.answer_photo(
                        photo=FSInputFile(downloaded_path),
                        caption=SHARE_PROMO_CAPTION,
                        reply_markup=kb,
                    )
            else:
                await message.answer(SHARE_PROMO_CAPTION, reply_markup=kb)
            return

        except Exception as e:
            logging.error(f"Link download error: {e}")
            await message.answer(TEXTS[lang]["download_error"], reply_markup=build_share_kb())
            return

    # MUSIQA QIDIRUVI (YouTube orqali)
    query = message.text.strip()
    if len(query) < 2:
        await message.answer("⚠️ Kamida 2 ta harf kiriting.")
        return

    msg = await message.answer(TEXTS[lang]["searching"])

    # YouTube qidiruv
    results = await search_youtube_tracks(query, max_results=20)

    if not results:
        await msg.edit_text(TEXTS[lang]["music_not_found_group"], reply_markup=build_share_kb())
        return

    await db.add_search(query, "youtube", message.from_user.id)

    user_search_state[message.from_user.id] = {
        "results": results,
        "page": 0,
        "query": query,
        "source": "youtube",
    }

    text, markup = await build_search_page(message.from_user.id, 0)
    if text:
        await msg.edit_text(text, reply_markup=markup)


# ========================== PHOTO HANDLER ==========================
@router.message(F.photo)
async def photo_handler(message: Message):
    await db.add_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if await db.is_blocked(message.from_user.id):
        return
    await db.update_activity(message.from_user.id)
    lang = await get_lang(message.from_user.id)
    if not await check_and_force_sub_group(message, lang):
        return
    await message.answer(
        "🖼 <b>Rasm qabul qilindi!</b>\n\n"
        "Rasmdan musiqa aniqlash hozircha mavjud emas.\n"
        "🎙 Ovozli xabar yuboring yoki 🎬 video link yuboring."
    )


# ========================== DOCUMENT HANDLER ==========================
@router.message(F.document)
async def document_handler(message: Message):
    await db.add_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if await db.is_blocked(message.from_user.id):
        return
    await db.update_activity(message.from_user.id)
    lang = await get_lang(message.from_user.id)
    if not await check_and_force_sub_group(message, lang):
        return
    doc = message.document
    filename = doc.file_name or "Noma'lum fayl"
    size_mb = doc.file_size / (1024 * 1024) if doc.file_size else 0
    await message.answer(
        TEXTS[lang]["document_received"].format(filename=filename, size_mb=f"{size_mb:.1f}")
    )

# ========================== AUDIO RECOGNITION (Shazam → YouTube) ==========================
async def process_audio_recognition(bot: Bot, file_id: str | None, user_id: int, message: Message, is_video: bool = False, local_path: str | None = None):
    """
    Ovozli xabar / audio / video fayldan yoki local fayldan musiqa aniqlash:
    1. Shazam orqali aniqlash
    2. YouTube orqali qidiruv (natijalarni ko'rsatish)
    """
    lang = await get_lang(user_id)
    tmp_path = None
    recognize_path = None

    loading_msg = await message.answer(TEXTS[lang]["recognizing"])

    try:
        if local_path and os.path.exists(local_path):
            tmp_path = local_path
            ext = local_path.split(".")[-1] if "." in local_path else "mp4"
        elif file_id:
            file = await bot.get_file(file_id)
            ext = file.file_path.split(".")[-1] if "." in file.file_path else "mp3"
            tmp_path = os.path.join(tempfile.gettempdir(), f"{user_id}_{datetime.now().timestamp():.0f}.{ext}")
            tmp_path = os.path.normpath(tmp_path)
            await bot.download_file(file.file_path, tmp_path)
        else:
            raise ValueError("Hech qanday fayl ko'rsatilmagan")

        recognize_path = tmp_path
        if is_video or tmp_path.lower().endswith(('.mp4', '.avi', '.mov', '.mkv', '.webm', '.flv')):
            recognize_path = await extract_audio_ffmpeg(tmp_path)

        result = await recognize_with_shazam(recognize_path)

        # Cleanup
        if tmp_path and tmp_path != local_path:
            _cleanup_file(tmp_path)
        if recognize_path and recognize_path != tmp_path and recognize_path != local_path:
            _cleanup_file(recognize_path)

        if not result:
            await loading_msg.edit_text(TEXTS[lang]["not_recognized"])
            return

        title = result.get("title", "Noma'lum")
        artist = result.get("artist", "Noma'lum")
        query = f"{artist} {title}".strip()

        # YouTube orqali qidirish
        youtube_results = await search_youtube_tracks(query, max_results=15)

        await loading_msg.delete()

        if youtube_results:
            user_search_state[user_id] = {
                "results": youtube_results,
                "page": 0,
                "query": f"🎵 {artist} — {title}",
                "source": "youtube",
            }
            text, markup = await build_search_page(user_id, 0)
            if text:
                await message.answer(text, reply_markup=markup)
            return

        # YouTube topilmasa — musiqa nomini ko'rsatish
        if lang == "uz":
            text = f"🎵 <b>{artist}</b> — <b>{title}</b>\n\n❌ YouTube'da topilmadi.\n🎵 Qo'shiq nomini yozib qidiring."
        elif lang == "uz_kr":
            text = f"🎵 <b>{artist}</b> — <b>{title}</b>\n\n❌ YouTube'да топилмади.\n🎵 Қўшиқ номини ёзиб қидиринг."
        elif lang == "ru":
            text = f"🎵 <b>{artist}</b> — <b>{title}</b>\n\n❌ Не найдено на YouTube.\n🎵 Напишите название трека для поиска."
        else:
            text = f"🎵 <b>{artist}</b> — <b>{title}</b>\n\n❌ Not found on YouTube.\n🎵 Type the track name to search."

        await message.answer(text)

    except Exception as e:
        if tmp_path and tmp_path != local_path:
            _cleanup_file(tmp_path)
        if recognize_path and recognize_path != tmp_path and recognize_path != local_path:
            _cleanup_file(recognize_path)
        try:
            await loading_msg.edit_text(TEXTS[lang]["download_error"], reply_markup=build_share_kb())
        except Exception:
            await message.answer(TEXTS[lang]["download_error"], reply_markup=build_share_kb())


# ========================== VOICE HANDLER ==========================
@router.message(F.voice)
async def voice_handler(message: Message):
    await db.add_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if await db.is_blocked(message.from_user.id):
        return
    await db.update_activity(message.from_user.id)
    lang = await get_lang(message.from_user.id)
    if not await check_and_force_sub_group(message, lang):
        return

    try:
        file = await message.bot.get_file(message.voice.file_id)
        ext = file.file_path.split(".")[-1] if "." in file.file_path else "ogg"
        tmp_path = os.path.join(tempfile.gettempdir(), f"{message.from_user.id}_{datetime.now().timestamp():.0f}.{ext}")
        tmp_path = os.path.normpath(tmp_path)
        await message.bot.download_file(file.file_path, tmp_path)

        link_download_state[message.from_user.id] = {
            "path": tmp_path,
            "title": None,
        }

        if lang == "uz":
            caption = "🎙 <b>Ovozli xabar qabul qilindi!</b>\n\n🎵 Musiqani aniqlash uchun tugmani bosing:"
        elif lang == "uz_kr":
            caption = "🎙 <b>Овозли хабар қабул қилинди!</b>\n\n🎵 Мусиқани аниқлаш учун тугмани босинг:"
        elif lang == "ru":
            caption = "🎙 <b>Голосовое сообщение получено!</b>\n\n🎵 Нажмите кнопку для распознавания музыки:"
        else:
            caption = "🎙 <b>Voice message received!</b>\n\n🎵 Press the button to recognize music:"

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🎵 " + ("Musiqani topish" if lang == "uz" else "Мусиқани топиш" if lang == "uz_kr" else "Найти музыку" if lang == "ru" else "Find Music"),
                callback_data=f"find_music:{message.from_user.id}"
            )]
        ])

        await message.answer(caption, reply_markup=kb)
    except Exception as e:
        logging.error(f"Voice handler error: {e}")
        await message.answer(TEXTS[lang]["download_error"], reply_markup=build_share_kb())


# ========================== AUDIO HANDLER ==========================
@router.message(F.audio)
async def audio_handler(message: Message):
    await db.add_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if await db.is_blocked(message.from_user.id):
        return
    await db.update_activity(message.from_user.id)
    lang = await get_lang(message.from_user.id)
    if not await check_and_force_sub_group(message, lang):
        return

    try:
        file = await message.bot.get_file(message.audio.file_id)
        ext = file.file_path.split(".")[-1] if "." in file.file_path else "mp3"
        tmp_path = os.path.join(tempfile.gettempdir(), f"{message.from_user.id}_{datetime.now().timestamp():.0f}.{ext}")
        tmp_path = os.path.normpath(tmp_path)
        await message.bot.download_file(file.file_path, tmp_path)

        link_download_state[message.from_user.id] = {
            "path": tmp_path,
            "title": None,
        }

        if lang == "uz":
            caption = "🎵 <b>Audio qabul qilindi!</b>\n\n🎵 Musiqani aniqlash uchun tugmani bosing:"
        elif lang == "uz_kr":
            caption = "🎵 <b>Аудио қабул қилинди!</b>\n\n🎵 Мусиқани аниқлаш учун тугмани босинг:"
        elif lang == "ru":
            caption = "🎵 <b>Аудио получено!</b>\n\n🎵 Нажмите кнопку для распознавания музыки:"
        else:
            caption = "🎵 <b>Audio received!</b>\n\n🎵 Press the button to recognize music:"

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🎵 " + ("Musiqani topish" if lang == "uz" else "Мусиқани топиш" if lang == "uz_kr" else "Найти музыку" if lang == "ru" else "Find Music"),
                callback_data=f"find_music:{message.from_user.id}"
            )]
        ])

        await message.answer(caption, reply_markup=kb)
    except Exception as e:
        logging.error(f"Audio handler error: {e}")
        await message.answer(TEXTS[lang]["download_error"], reply_markup=build_share_kb())

# ========================== VIDEO HANDLER ==========================
@router.message(F.video)
async def video_handler(message: Message):
    await db.add_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if await db.is_blocked(message.from_user.id):
        return
    await db.update_activity(message.from_user.id)
    lang = await get_lang(message.from_user.id)
    if not await check_and_force_sub_group(message, lang):
        return

    try:
        file = await message.bot.get_file(message.video.file_id)
        ext = file.file_path.split(".")[-1] if "." in file.file_path else "mp4"
        tmp_path = os.path.join(tempfile.gettempdir(), f"{message.from_user.id}_{datetime.now().timestamp():.0f}.{ext}")
        tmp_path = os.path.normpath(tmp_path)
        await message.bot.download_file(file.file_path, tmp_path)

        link_download_state[message.from_user.id] = {
            "path": tmp_path,
            "title": None,
        }

        if lang == "uz":
            caption = "🎬 <b>Video qabul qilindi!</b>\n\n🎵 Musiqani aniqlash uchun tugmani bosing:"
        elif lang == "uz_kr":
            caption = "🎬 <b>Видео қабул қилинди!</b>\n\n🎵 Мусиқани аниқлаш учун тугмани босинг:"
        elif lang == "ru":
            caption = "🎬 <b>Видео получено!</b>\n\n🎵 Нажмите кнопку для распознавания музыки:"
        else:
            caption = "🎬 <b>Video received!</b>\n\n🎵 Press the button to recognize music:"

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🎵 " + ("Musiqani topish" if lang == "uz" else "Мусиқани топиш" if lang == "uz_kr" else "Найти музыку" if lang == "ru" else "Find Music"),
                callback_data=f"find_music:{message.from_user.id}"
            )]
        ])

        await message.answer(caption, reply_markup=kb)
    except Exception as e:
        logging.error(f"Video handler error: {e}")
        await message.answer(TEXTS[lang]["download_error"], reply_markup=build_share_kb())


# ========================== VIDEO NOTE HANDLER ==========================
@router.message(F.video_note)
async def video_note_handler(message: Message):
    await db.add_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if await db.is_blocked(message.from_user.id):
        return
    await db.update_activity(message.from_user.id)
    lang = await get_lang(message.from_user.id)
    if not await check_and_force_sub_group(message, lang):
        return

    try:
        file = await message.bot.get_file(message.video_note.file_id)
        ext = file.file_path.split(".")[-1] if "." in file.file_path else "mp4"
        tmp_path = os.path.join(tempfile.gettempdir(), f"{message.from_user.id}_{datetime.now().timestamp():.0f}.{ext}")
        tmp_path = os.path.normpath(tmp_path)
        await message.bot.download_file(file.file_path, tmp_path)

        link_download_state[message.from_user.id] = {
            "path": tmp_path,
            "title": None,
        }

        if lang == "uz":
            caption = "🎬 <b>Video note qabul qilindi!</b>\n\n🎵 Musiqani aniqlash uchun tugmani bosing:"
        elif lang == "uz_kr":
            caption = "🎬 <b>Видео ноте қабул қилинди!</b>\n\n🎵 Мусиқани аниқлаш учун тугмани босинг:"
        elif lang == "ru":
            caption = "🎬 <b>Видео-кружок получен!</b>\n\n🎵 Нажмите кнопку для распознавания музыки:"
        else:
            caption = "🎬 <b>Video note received!</b>\n\n🎵 Press the button to recognize music:"

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(
                text="🎵 " + ("Musiqani topish" if lang == "uz" else "Мусиқани топиш" if lang == "uz_kr" else "Найти музыку" if lang == "ru" else "Find Music"),
                callback_data=f"find_music:{message.from_user.id}"
            )]
        ])

        await message.answer(caption, reply_markup=kb)
    except Exception as e:
        logging.error(f"Video note handler error: {e}")
        await message.answer(TEXTS[lang]["download_error"], reply_markup=build_share_kb())

# ========================== PROFILE ==========================
@router.callback_query(F.data == "profile")
async def profile_callback(call: CallbackQuery):
    user = await db.get_user(call.from_user.id)
    if not user:
        return
    lang = user["language"] or "uz"
    text = TEXTS[lang]["profile"].format(
        telegram_id=user["telegram_id"],
        fullname=user["fullname"] or "Noma'lum",
        username=f"@{user['username']}" if user["username"] else "yo'q",
        language=user["language"],
    )
    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="🔙 Orqaga", callback_data="back_to_main")]]
        ),
    )
    await call.answer()


@router.callback_query(F.data == "lang_menu")
async def lang_menu_callback(call: CallbackQuery):
    lang = await get_lang(call.from_user.id)
    await call.message.edit_text(TEXTS[lang]["choose_lang"], reply_markup=lang_kb())
    await call.answer()


@router.callback_query(F.data == "back_to_main")
async def back_to_main(call: CallbackQuery):
    lang = await get_lang(call.from_user.id)
    await call.message.edit_text(TEXTS[lang]["welcome"], reply_markup=main_menu_kb(lang))
    await call.answer()

# ========================== ADMIN PANEL ==========================
@router.message(Command("admin"))
async def admin_cmd(message: Message):
    if message.from_user.id != ADMIN_ID:
        return
    await message.answer(TEXTS["uz"]["admin_welcome"], reply_markup=admin_main_kb())


@router.callback_query(F.data.startswith("admin:"))
async def admin_callbacks(call: CallbackQuery):
    if call.from_user.id != ADMIN_ID:
        await call.answer("❌", show_alert=True)
        return

    parts = call.data.split(":")
    action = parts[1]

    if action == "back":
        await call.message.edit_text(TEXTS["uz"]["admin_welcome"], reply_markup=admin_main_kb())

    elif action == "close":
        await call.message.delete()

    elif action == "stats":
        total, active, week, top = await db.get_stats()
        lang_stats = await db.get_language_stats()
        langs_text = "\n".join([f"• {row['language']}: {row['c']}" for row in lang_stats]) or "Ma'lumot yo'q"
        top_text = "\n".join([f"{i+1}. {row['query']} ({row['c']} marta)" for i, row in enumerate(top)]) or "Ma'lumot yo'q"
        text = TEXTS["uz"]["stats"].format(total=total, active=active, week=week, langs=langs_text, top=top_text)
        await call.message.edit_text(text, reply_markup=admin_back_kb())

    elif action == "channels":
        channels = await db.get_channels()
        if not channels:
            text = "📢 <b>Majburiy obuna kanallari:</b>\n\nKanallar yo'q."
        else:
            text = "📢 <b>Majburiy obuna kanallari:</b>\n\nBirini o'chirish uchun bosing:"
        await call.message.edit_text(text, reply_markup=channels_kb(channels))

    elif action == "ch_add":
        await call.message.edit_text(TEXTS["uz"]["enter_channel"])
        admin_state[call.from_user.id] = {"state": "waiting_channel"}

    elif action == "ch_del" and len(parts) == 3:
        username = parts[2]
        await db.remove_channel(username)
        await call.answer("❌ O'chirildi", show_alert=True)
        channels = await db.get_channels()
        text = "📢 <b>Majburiy obuna kanallari:</b>\n\nBirini o'chirish uchun bosing:"
        await call.message.edit_text(text, reply_markup=channels_kb(channels))

    elif action == "broadcast":
        await call.message.edit_text(
            "📨 <b>Reklama (Broadcast)</b>\n\nQanday xabar yuborishni tanlang:",
            reply_markup=broadcast_kb(),
        )

    elif action == "bc_text":
        await call.message.edit_text(TEXTS["uz"]["broadcast_ask_text"])
        admin_state[call.from_user.id] = {"state": "waiting_broadcast_text"}

    elif action == "bc_media":
        await call.message.edit_text(TEXTS["uz"]["broadcast_ask_media"])
        admin_state[call.from_user.id] = {"state": "waiting_broadcast_media"}

    elif action == "bc_skip":
        media_msg = admin_state.get(call.from_user.id, {}).get("media_msg")
        admin_state.pop(call.from_user.id, None)
        if media_msg:
            count = await broadcast_to_all(call.bot, media_msg)
            await call.message.answer(TEXTS["uz"]["broadcast_done"].format(count=count))
        else:
            await call.message.answer("❌ Media topilmadi.")
        await call.message.answer(TEXTS["uz"]["admin_welcome"], reply_markup=admin_main_kb())

    elif action == "blacklist":
        blocked = await db.get_blocked_users()
        if not blocked:
            text = "🚫 <b>Blacklist</b>\n\n" + TEXTS["uz"]["no_blocked"]
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Block qilish", callback_data="admin:block_add")],
                [InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin:back")],
            ])
            await call.message.edit_text(text, reply_markup=kb)
        else:
            text = "🚫 <b>Bloklangan foydalanuvchilar:</b>\n\nBlokdan ochish uchun bosing:"
            await call.message.edit_text(text, reply_markup=blacklist_kb(blocked))

    elif action == "block_add":
        await call.message.edit_text(TEXTS["uz"]["enter_block_id"])
        admin_state[call.from_user.id] = {"state": "waiting_block_id"}

    elif action == "unblock" and len(parts) == 3:
        target_id = int(parts[2])
        await db.unblock_user(target_id)
        await call.answer("✅ Blokdan ochirildi", show_alert=True)
        blocked = await db.get_blocked_users()
        if not blocked:
            text = "🚫 <b>Blacklist</b>\n\n" + TEXTS["uz"]["no_blocked"]
            kb = InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Block qilish", callback_data="admin:block_add")],
                [InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin:back")],
            ])
            await call.message.edit_text(text, reply_markup=kb)
        else:
            text = "🚫 <b>Bloklangan foydalanuvchilar:</b>\n\nBlokdan ochish uchun bosing:"
            await call.message.edit_text(text, reply_markup=blacklist_kb(blocked))

    elif action == "groups":
        groups = await db.get_groups()
        if groups:
            groups_text = "\n".join([f"• {g['title']} (<code>{g['group_id']}</code>)" for g in groups])
        else:
            groups_text = TEXTS["uz"]["no_groups"]
        text = TEXTS["uz"]["groups_list"].format(groups=groups_text)
        await call.message.edit_text(text, reply_markup=admin_back_kb())

    elif action == "langs":
        lang_stats = await db.get_language_stats()
        if lang_stats:
            stats_text = "\n".join([f"• {row['language']}: {row['c']} ta" for row in lang_stats])
        else:
            stats_text = "Ma'lumot yo'q"
        text = TEXTS["uz"]["lang_stats"].format(stats=stats_text)
        await call.message.edit_text(text, reply_markup=admin_back_kb())

    await call.answer()

# ========================== GROUP TRACKING ==========================
@router.my_chat_member()
async def my_chat_member_handler(update: ChatMemberUpdated):
    if update.new_chat_member and update.new_chat_member.status in (
        ChatMemberStatus.MEMBER,
        ChatMemberStatus.ADMINISTRATOR,
    ):
        await db.add_group(update.chat.id, update.chat.title or "Noma'lum")


# ========================== UNKNOWN MESSAGE HANDLER ==========================
@router.message()
async def unknown_message_handler(message: Message):
    await db.add_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if await db.is_blocked(message.from_user.id):
        return
    await db.update_activity(message.from_user.id)
    lang = await get_lang(message.from_user.id)
    if not await check_and_force_sub_group(message, lang):
        return
    await message.answer(TEXTS[lang]["unknown_message"])


# ========================== GLOBAL ERROR HANDLER ==========================
@router.errors()
async def global_error_handler(event):
    logging.error(f"Global error: {event.exception}", exc_info=True)
    if hasattr(event, 'update') and event.update and hasattr(event.update, 'message') and event.update.message:
        try:
            lang = await get_lang(event.update.message.from_user.id)
            await event.update.message.answer(
                TEXTS[lang]["download_error"].format(error="Kutilmagan xatolik yuz berdi. Iltimos, keyinroq urinib ko'ring.")
            )
        except Exception:
            pass


# ========================== MAIN ==========================
async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    if BOT_TOKEN in ("", "YOUR_BOT_TOKEN_HERE"):
        print("[XATOLIK] BOT_TOKEN sozlanmagan!")
        return

    global db
    db = Database(DATABASE_URL)
    await db.connect()
    print("[OK] PostgreSQL bazaga ulanish o'rnatildi.")

    ffmpeg_cmd = find_ffmpeg_cmd()
    if not ffmpeg_cmd:
        print("[WARN] FFmpeg topilmadi! Videodan musiqa ajratish ishlamaydi.")
    else:
        print(f"[OK] FFmpeg topildi: {ffmpeg_cmd}")

    # yt-dlp versiyasini tekshirish
    try:
        import subprocess
        result = subprocess.run(['yt-dlp', '--version'], capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            print(f"[OK] yt-dlp versiyasi: {result.stdout.strip()}")
        else:
            print("[WARN] yt-dlp versiyasini tekshirishda xatolik")
    except Exception:
        print("[WARN] yt-dlp versiyasini tekshirishda xatolik")

    # Cookies tekshiruvi
    cookie_path = get_cookiefile()
    if cookie_path:
        print(f"[OK] Cookies fayli topildi: {cookie_path}")
    else:
        print("[INFO] Cookies fayli yo'q yoki Premium cookies o'chirildi (GitHub #15330)")

    if shazam_client is None:
        print("[WARN] ShazamIO o'rnatilmagan. Ovozli xabar aniqlash ishlamaydi.")
        if sys.version_info >= (3, 13):
            print("[WARN] Python 3.13+. ShazamIO ishlamasligi mumkin. Python 3.12 ishlatishni tavsiya qilamiz.")
    else:
        print("[OK] ShazamIO tayyor.")

    bot = Bot(token=BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_router(router)

    print("[OK] Bot ishga tushmoqda...")

    for attempt in range(3):
        try:
            info = await bot.get_webhook_info()
            if info.url:
                print(f"[INFO] Webhook o'chirilmoqda (attempt {attempt + 1})...")
                await bot.delete_webhook(drop_pending_updates=True)
            break
        except Exception as e:
            print(f"[WARN] Webhook cleanup attempt {attempt + 1} failed: {e}")
            if attempt < 2:
                await asyncio.sleep(1)

    await asyncio.sleep(2)

    try:
        print("[INFO] Polling boshlanyapti...")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    except TelegramConflictError as e:
        print(f"[ERROR] Telegram conflict: {e}. Bitta bot instansiyasi ishlayotganini tekshiring.")
    except TelegramNetworkError as e:
        print(f"[ERROR] Telegram network error: {e}")
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
