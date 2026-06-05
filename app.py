import os
import sys
import warnings
import re
import asyncio
import logging
import subprocess
import tempfile
import shutil
import hashlib
import base64
import html
import io
import csv
import time
import urllib.request
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
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramConflictError, TelegramNetworkError
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


def _is_git_repo(path: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except Exception:
        return False


def _self_update() -> bool:
    repo_path = os.getcwd()
    if not _is_git_repo(repo_path):
        logging.info("[AutoDeploy] Git repository emas, yangilash o'tkazib yuborildi.")
        return False

    try:
        branch = AUTO_DEPLOY_BRANCH
        remote = AUTO_DEPLOY_REMOTE
        logging.info(f"[AutoDeploy] Yangilash: git fetch {remote}")
        subprocess.run(["git", "fetch", remote], cwd=repo_path, check=True, capture_output=True, text=True, timeout=120)

        logging.info(f"[AutoDeploy] Yangilash: git reset --hard {remote}/{branch}")
        subprocess.run(["git", "reset", "--hard", f"{remote}/{branch}"], cwd=repo_path, check=True, capture_output=True, text=True, timeout=120)

        req_path = os.path.join(repo_path, "requirements.txt")
        if os.path.exists(req_path):
            logging.info("[AutoDeploy] requirements.txt o'rnatilmoqda...")
            subprocess.run([sys.executable, "-m", "pip", "install", "--no-cache-dir", "-r", req_path], cwd=repo_path, check=True, capture_output=True, text=True, timeout=300)

        logging.info("[AutoDeploy] Yangilanish muvaffaqiyatli. Jarayon qayta ishga tushiriladi.")
        return True
    except subprocess.CalledProcessError as e:
        logging.error(f"[AutoDeploy] Git update xatosi: {e.stderr or e.stdout}")
    except Exception as e:
        logging.error(f"[AutoDeploy] Yangilanish xatosi: {e}")
    return False


def _get_restart_command() -> list[str] | None:
    # Agar container yoki oddiy Python ishlayotgan bo'lsa, hozirgi faylni qayta ishga tushuradi.
    current_file = os.path.abspath(__file__)
    python_exec = sys.executable
    if current_file.endswith("app.py"):
        return [python_exec, current_file]
    return None


def _schedule_auto_deploy():
    async def _worker():
        if not AUTO_DEPLOY:
            return
        interval = max(1, AUTO_DEPLOY_INTERVAL_MINUTES) * 60
        while True:
            await asyncio.sleep(interval)
            logging.info("[AutoDeploy] 15 minutdan keyin yangilanish tekshirilmoqda...")
            if _self_update():
                restart_cmd = _get_restart_command()
                if restart_cmd:
                    logging.info("[AutoDeploy] Jarayon qayta ishga tushirilmoqda...")
                    os.execv(restart_cmd[0], restart_cmd)
                else:
                    logging.warning("[AutoDeploy] Qayta ishga tushirish buyrug'i aniqlanmadi.")
                    break
    return asyncio.create_task(_worker())


def _schedule_auto_cookie_refresh():
    """Cookies har COOKIE_REFRESH_INTERVAL_HOURS da avtomatik refresh qilish"""
    async def _worker():
        if not AUTO_REFRESH_COOKIES:
            logging.info("[Cookies] AUTO_REFRESH_COOKIES o'chirilgan")
            return
        
        interval = max(1, COOKIE_REFRESH_INTERVAL_HOURS) * 3600  # Soatlarga o'tkazish
        logging.info(f"[Cookies] Auto-refresh scheduled: har {COOKIE_REFRESH_INTERVAL_HOURS} soatda")
        
        while True:
            await asyncio.sleep(interval)
            logging.info("[Cookies] Avtomatik refresh tekshirilmoqda...")
            
            try:
                # Cookies tekshirish va yangilash
                if refresh_youtube_cookiefile(force=False):  # force=False = faqat eskirgan bo'lsa yangilash
                    logging.info("[Cookies] ✓ Avtomatik refresh muvaffaqiyatli")
                else:
                    logging.warning("[Cookies] Avtomatik refresh amalga oshmadi yoki browserdan topilmadi")
            except Exception as e:
                logging.warning(f"[Cookies] Avtomatik refresh xatosi: {e}")
    
    return asyncio.create_task(_worker())


shazam_client = Shazam() if can_use_shazam() else None

# ========================== CONFIG ==========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "7961099561"))
DATABASE_URL = (
    "postgresql://postgres:NWthCzkTirkhOLywbKwWlwXrnOfiqjSO"
    "@turntable.proxy.rlwy.net:14314/railway"
)

# YouTube Data API v3 — qidirish uchun (cookies kerak emas, bepul)
# Olish: https://console.cloud.google.com/apis/credentials
YOUTUBE_API_KEY = os.getenv("YOUTUBE_API_KEY", "")

# YouTube proxy (ixtiyoriy)
YOUTUBE_PROXY_RAW = os.getenv("YOUTUBE_PROXY", "").strip()
PROXY_API_KEY = os.getenv("PROXY_API_KEY", "").strip()
# Optional format string that can contain `{api_key}` to produce a proxy URL.
# Example: PROXY_API_FORMAT=http://{api_key}@proxy.provider.com:8000
PROXY_API_FORMAT = os.getenv("PROXY_API_FORMAT", "").strip()


def _resolve_youtube_proxy(raw: str, api_key: str, api_format: str) -> str:
    """Resolve effective YOUTUBE_PROXY using either raw value, a formatted
    proxy built from an API key, or simple heuristics.

    Priority:
    1. If `raw` is a valid proxy URL or host:port value, use it.
    2. If `api_format` and `api_key` are set, return api_format.format(api_key=api_key).
    3. If only `api_key` is set and it already looks like a proxy URL/host:port, use it.
    4. Otherwise return empty string so GitHub proxy fallback can be used.
    """
    def _normalize_proxy_candidate(value: str) -> str | None:
        if not value:
            return None
        candidate = value.strip().split()[0]
        if not candidate or candidate.startswith("#"):
            return None
        if candidate.startswith(("http://", "https://", "socks5://", "socks5h://", "socks4://", "socks4a://")):
            return candidate
        if re.match(r"^(?:[^:@\s]+(?::[^:@\s]*)?@)?[^:@\s]+:\d+$", candidate):
            return f"http://{candidate}"
        return None

    if raw:
        normalized_raw = _normalize_proxy_candidate(raw)
        if normalized_raw:
            return normalized_raw
        logging.debug("[Proxy] YOUTUBE_PROXY is set but not a valid proxy URL; ignoring it and falling back.")

    if api_format and api_key:
        try:
            formatted = api_format.format(api_key=api_key)
            normalized_formatted = _normalize_proxy_candidate(formatted)
            if normalized_formatted:
                return normalized_formatted
            logging.debug("[Proxy] PROXY_API_FORMAT produced invalid proxy URL.")
        except Exception:
            logging.debug("[Proxy] PROXY_API_FORMAT formatting failed")

    if api_key:
        normalized_key = _normalize_proxy_candidate(api_key)
        if normalized_key:
            return normalized_key
        logging.debug("[Proxy] PROXY_API_KEY mavjud, lekin uning formatini aniqlashning iloji yo'q. PROXY_API_FORMAT kerak.")

    return ""


YOUTUBE_PROXY = _resolve_youtube_proxy(YOUTUBE_PROXY_RAW, PROXY_API_KEY, PROXY_API_FORMAT)
if YOUTUBE_PROXY:
    logging.info(f"[Proxy] YOUTUBE_PROXY o'rnatildi: {YOUTUBE_PROXY[:60]}")
else:
    logging.warning("[Proxy] YOUTUBE_PROXY aniqlanmadi yoki noto'g'ri format; proxy ishlatilmaydi.")

# Proxy list from GitHub / dynamic rotation
PROXY_LIST_ENABLED = os.getenv("PROXY_LIST_ENABLED", "1").lower() in ("1", "true", "yes")
PROXY_LIST_URLS = [
    url.strip() for url in os.getenv(
        "PROXY_LIST_URLS",
        "https://raw.githubusercontent.com/proxifly/free-proxy-list/main/proxies/all/data.txt"
    ).split(",") if url.strip()
]
PROXY_LIST = os.getenv("PROXY_LIST", "").strip()
PROXY_LIST_FILE = os.getenv("PROXY_LIST_FILE", "").strip()
PROXY_LIST_REFRESH_INTERVAL_MINUTES = int(os.getenv("PROXY_LIST_REFRESH_INTERVAL_MINUTES", "60"))
PROXY_LIST_MAX_ENTRIES = int(os.getenv("PROXY_LIST_MAX_ENTRIES", "150"))


def _normalize_proxy_url(value: str) -> str | None:
    if not value:
        return None
    proxy = value.strip().split()[0]
    if not proxy or proxy.startswith("#"):
        return None
    if proxy.startswith(("http://", "https://", "socks5://", "socks5h://", "socks4://", "socks4a://")):
        return proxy
    # ip:port or user:pass@host:port
    if re.match(r"^(?:[^:@\s]+(?::[^:@\s]*)?@)?[^:@\s]+:\d+$", proxy):
        return f"http://{proxy}"
    return None


def _extract_proxies_from_json(data: Any) -> list[str]:
    proxies: list[str] = []
    if isinstance(data, str):
        normalized = _normalize_proxy_url(data)
        if normalized:
            proxies.append(normalized)
        return proxies
    if isinstance(data, dict):
        for value in data.values():
            proxies.extend(_extract_proxies_from_json(value))
        return proxies
    if isinstance(data, list):
        for item in data:
            proxies.extend(_extract_proxies_from_json(item))
        return proxies
    return proxies


def _extract_proxies_from_csv(text: str) -> list[str]:
    proxies: list[str] = []
    try:
        import csv as _csv
        import io as _io
        reader = _csv.reader(_io.StringIO(text))
        for row in reader:
            for cell in row:
                normalized = _normalize_proxy_url(cell)
                if normalized:
                    proxies.append(normalized)
    except Exception:
        pass
    return proxies


def _extract_proxies_from_text(text: str) -> list[str]:
    proxies: list[str] = []
    for line in text.splitlines():
        normalized = _normalize_proxy_url(line)
        if normalized:
            proxies.append(normalized)
    return proxies


class ProxyRotationManager:
    def __init__(self, urls: list[str], refresh_interval_minutes: int, max_entries: int):
        self.urls = urls
        self.refresh_interval = max(1, refresh_interval_minutes)
        self.max_entries = max(10, max_entries)
        self.proxies: list[str] = []
        self.blocked: set[str] = set()
        self.current_index = 0
        self.last_refresh = 0.0

    def _fetch_url(self, url: str) -> str | None:
        try:
            import urllib.request as _urllib
            with _urllib.urlopen(url, timeout=20) as response:
                if response.status != 200:
                    return None
                raw = response.read().decode('utf-8', errors='ignore')
                return raw
        except Exception as e:
            logging.debug(f"[ProxyList] URL fetch failed: {url} -> {e}")
            return None

    def _parse_url(self, url: str, content: str) -> list[str]:
        proxies: list[str] = []
        lower_url = url.lower()
        parsed: list[str] = []
        if lower_url.endswith('.json') or content.strip().startswith('{') or content.strip().startswith('['):
            try:
                parsed = _extract_proxies_from_json(json.loads(content))
            except Exception:
                parsed = []
        elif lower_url.endswith('.csv') or ',' in content.splitlines()[0] if content else False:
            parsed = _extract_proxies_from_csv(content)
        else:
            parsed = _extract_proxies_from_text(content)
        for proxy in parsed:
            if proxy not in proxies:
                proxies.append(proxy)
        return proxies

    def _load_local_file(self, path: str) -> str | None:
        if not path:
            return None
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return f.read()
        except FileNotFoundError:
            logging.warning(f"[ProxyList] Local proxy file topilmadi: {path}")
        except Exception as e:
            logging.warning(f"[ProxyList] Local proxy file o'qishda xato: {path} -> {e}")
        return None

    def refresh_proxies(self, force: bool = False) -> None:
        if not PROXY_LIST_ENABLED:
            return
        now = time.time()
        if not force and self.proxies and (now - self.last_refresh) < self.refresh_interval * 60:
            return

        candidate: list[str] = []
        for url in self.urls:
            content = self._fetch_url(url)
            if not content:
                continue
            candidate.extend(self._parse_url(url, content))

        if PROXY_LIST:
            candidate.extend(_extract_proxies_from_text(PROXY_LIST))

        if PROXY_LIST_FILE:
            file_content = self._load_local_file(PROXY_LIST_FILE)
            if file_content:
                if PROXY_LIST_FILE.lower().endswith('.json'):
                    candidate.extend(_extract_proxies_from_json(json.loads(file_content)))
                elif PROXY_LIST_FILE.lower().endswith('.csv'):
                    candidate.extend(_extract_proxies_from_csv(file_content))
                else:
                    candidate.extend(_extract_proxies_from_text(file_content))

        candidate = [p for p in dict.fromkeys(candidate)]
        if candidate:
            self.proxies = candidate[:self.max_entries]
            self.current_index = 0
            self.last_refresh = now
            logging.info(f"[ProxyList] {len(self.proxies)} proxy yuklandi (remote+local)")
        else:
            logging.warning("[ProxyList] Proxy ro'yxati yuklanmadi; mavjud ro'yxat saqlanadi")

    def get_next_proxy(self) -> str | None:
        if not PROXY_LIST_ENABLED:
            return None
        self.refresh_proxies()
        if not self.proxies:
            return None
        total = len(self.proxies)
        for _ in range(total):
            proxy = self.proxies[self.current_index]
            self.current_index = (self.current_index + 1) % total
            if proxy not in self.blocked:
                return proxy
        return None

    def block_proxy(self, proxy: str) -> None:
        if proxy:
            self.blocked.add(proxy)
            logging.warning(f"[ProxyList] Blocked proxy: {proxy}")

    def reset_blocked(self) -> None:
        self.blocked.clear()


proxy_list_manager = ProxyRotationManager(PROXY_LIST_URLS, PROXY_LIST_REFRESH_INTERVAL_MINUTES, PROXY_LIST_MAX_ENTRIES)


def get_current_proxy() -> str | None:
    if YOUTUBE_PROXY and YOUTUBE_PROXY not in proxy_list_manager.blocked:
        return YOUTUBE_PROXY
    proxy = proxy_list_manager.get_next_proxy()
    if proxy:
        logging.info(f"[Proxy] GitHub proxy fallback ishlatilmoqda: {proxy[:60]}...")
    return proxy


def mark_proxy_blocked(proxy: str | None) -> None:
    if not proxy:
        return
    if proxy == YOUTUBE_PROXY:
        proxy_list_manager.blocked.add(proxy)
        logging.warning(f"[Proxy] YOUTUBE_PROXY bloklandi: {proxy}")
        return
    proxy_list_manager.block_proxy(proxy)
    logging.debug(f"[Proxy] Fallback proxy bloklandi: {proxy}")

# YouTube cookies fayli uchun maxsus yo‘l (agar qo‘lda eksport qilingan bo‘lsa)
YOUTUBE_COOKIE_FILE = os.getenv("YOUTUBE_COOKIE_FILE", "")

# Piped instance URLlarini @piped-video dan avtomatik topish va ishlaydiganlarni keshlash
PIPED_API_INSTANCES = [
    inst.strip() for inst in os.getenv(
        "PIPED_API_INSTANCES",
        ",".join([
            "https://api.piped.projectsegfault.com",
            "https://pipedapi.kavin.rocks",
            "https://pipedapi.leptons.xyz",
            "https://pipedapi.nosebs.ru",
            "https://pipedapi-libre.kavin.rocks",
            "https://piped-api.privacy.com.de",
            "https://pipedapi.adminforge.de",
            "https://api.piped.yt",
            "https://pipedapi.drgns.space",
            "https://pipedapi.owo.si",
            "https://pipedapi.ducks.party",
            "https://piped-api.codespace.cz",
            "https://pipedapi.reallyaweso.me",
            "https://api.piped.private.coffee",
            "https://pipedapi.darkness.services",
            "https://pipedapi.orangenet.cc",
            "https://pipedapi.moomoo.me",
            "https://pipedapi.mha.fi",
            "https://api.piped.privacydev.net",
        ]),
    ).split(",")
    if inst.strip()
]
PIPED_INSTANCE_REFRESH_INTERVAL_SECONDS = int(os.getenv("PIPED_INSTANCE_REFRESH_INTERVAL_SECONDS", "600"))
PIPED_PROBE_VIDEO_ID = os.getenv("PIPED_PROBE_VIDEO_ID", "dQw4w9WgXcQ").strip()

# Optional per-cookie proxies (comma separated list). When creating fresh cookies,
# rotation will assign proxies round-robin to cookies so yt-dlp can use per-cookie proxies.
COOKIE_PROXIES = [p.strip() for p in os.getenv("COOKIE_PROXIES", "").split(",") if p.strip()]
_COOKIE_PROXY_INDEX = 0

# Optional Playwright persistent profile directory (if you export a logged-in profile)
PLAYWRIGHT_PROFILE_DIR = os.getenv("PLAYWRIGHT_PROFILE_DIR", "").strip()

# Cobalt o'chirildi - shunchaki yt-dlp + Piped + Invidious ishlatamiz

# Avtomatik o'zini yangilash (auto deploy)
AUTO_DEPLOY = os.getenv("AUTO_DEPLOY", "0").lower() in ("1", "true", "yes")
AUTO_DEPLOY_INTERVAL_MINUTES = int(os.getenv("AUTO_DEPLOY_INTERVAL_MINUTES", "15"))
AUTO_DEPLOY_BRANCH = os.getenv("AUTO_DEPLOY_BRANCH", "main")
AUTO_DEPLOY_REMOTE = os.getenv("AUTO_DEPLOY_REMOTE", "origin")

AUTO_REFRESH_COOKIES = os.getenv("AUTO_REFRESH_COOKIES", "0").lower() in ("1", "true", "yes")
COOKIE_REFRESH_INTERVAL_HOURS = int(os.getenv("COOKIE_REFRESH_INTERVAL_HOURS", "6"))
BROWSER_COOKIE_SOURCES = [b.strip() for b in os.getenv("BROWSER_COOKIE_SOURCES", "chrome,edge,firefox,chromium").split(",") if b.strip()]

MAX_SIZE_MB = 50
RESULTS_PER_PAGE = 10

SHARE_PROMO_CAPTION = (
    "❤️ @videoSkachatbbot orqali istagan musiqangizni yuklang hamda tez va oson toping! 🚀"
)
BOT_USERNAME = "videoSkachatbbot"
# ========================== COOKIE ROTATION MANAGER ==========================
# Yangi sistema: Har operatsiyadan oldin yangi cookies yaratish
import json
from pathlib import Path

COOKIES_POOL_DIR = os.path.join(os.getcwd(), "cookies_pool")
BLOCKED_COOKIES_FILE = os.path.join(COOKIES_POOL_DIR, ".blocked_cookies.json")
ACTIVE_COOKIES_FILE = os.path.join(COOKIES_POOL_DIR, ".active_cookies.json")

# Create pool directory if not exists
os.makedirs(COOKIES_POOL_DIR, exist_ok=True)

class CookieRotationManager:
    """
    Avtomatik cookie rotation - har operatsiyadan oldin yangi cookies yaratish.
    
    Features:
    - Har search/download dan oldin yangi cookies fayl yaratish
    - Bloklanib qolgan cookiesni blacklist-ga qo'shish
    - Eski cookiesni avtomatik o'chirish
    - Muvaffaqiyatli cookiesni prioritize qilish
    """
    
    def __init__(self):
        self.pool_dir = COOKIES_POOL_DIR
        self.blocked_file = BLOCKED_COOKIES_FILE
        self.active_file = ACTIVE_COOKIES_FILE
        self.blocked_cookies = set()
        self.active_cookies = {}
        self.load_cookie_status()
        
    def load_cookie_status(self):
        """Bloklangan va aktiv cookieslarni yuklash"""
        try:
            if os.path.exists(self.blocked_file):
                with open(self.blocked_file, 'r') as f:
                    data = json.load(f)
                    self.blocked_cookies = set(data.get('blocked', []))
        except Exception as e:
            logging.debug(f"[CookieRotation] Blocked cookies file load xatosi: {e}")
            
        try:
            if os.path.exists(self.active_file):
                with open(self.active_file, 'r') as f:
                    self.active_cookies = json.load(f)
        except Exception as e:
            logging.debug(f"[CookieRotation] Active cookies file load xatosi: {e}")
    
    def save_cookie_status(self):
        """Cookieslarni saqlash"""
        try:
            with open(self.blocked_file, 'w') as f:
                json.dump({'blocked': list(self.blocked_cookies)}, f)
            with open(self.active_file, 'w') as f:
                json.dump(self.active_cookies, f)
        except Exception as e:
            logging.warning(f"[CookieRotation] Cookie status save xatosi: {e}")
    
    def create_fresh_cookie_file(self) -> str | None:
        """Yangi cookie fayl yaratish"""
        try:
            global _COOKIE_PROXY_INDEX
            # 1) If Playwright persistent profile provided, try to extract cookies from it
            try:
                if PLAYWRIGHT_PROFILE_DIR:
                    try:
                        from playwright.sync_api import sync_playwright
                        with sync_playwright() as pw:
                            browser = pw.chromium.launch_persistent_context(user_data_dir=PLAYWRIGHT_PROFILE_DIR, headless=True)
                            page = browser.new_page()
                            page.goto("https://www.youtube.com", timeout=15000)
                            # collect cookies from context
                            cookies = browser.cookies()
                            browser.close()
                            if cookies:
                                timestamp = int(time.time() * 1000)
                                cookie_file = os.path.join(self.pool_dir, f"fresh_{timestamp}.txt")
                                try:
                                    with open(cookie_file, 'w', encoding='utf-8') as f:
                                        f.write("# Netscape HTTP Cookie File\n")
                                        for c in cookies:
                                            domain = c.get('domain', '')
                                            flag = "TRUE" if domain.startswith('.') else "FALSE"
                                            cookie_path = c.get('path', '/')
                                            secure = "TRUE" if c.get('secure', False) else "FALSE"
                                            expires = str(int(c.get('expires', 0))) if c.get('expires') else '0'
                                            name = c.get('name', '')
                                            value = c.get('value', '')
                                            f.write("\t".join([domain, flag, cookie_path, secure, expires, name, value]) + "\n")

                                    # assign optional proxy
                                    proxy = None
                                    if COOKIE_PROXIES:
                                        proxy = COOKIE_PROXIES[_COOKIE_PROXY_INDEX % len(COOKIE_PROXIES)]
                                        _COOKIE_PROXY_INDEX += 1

                                    logging.info(f"[CookieRotation] Yangi cookies yaratildi: playwright profile -> {os.path.basename(cookie_file)}")
                                    self.active_cookies[cookie_file] = {
                                        'created': time.time(),
                                        'source': 'playwright',
                                        'successes': 0,
                                        'failures': 0,
                                        'blocked': False,
                                        'proxy': proxy,
                                    }
                                    self.save_cookie_status()
                                    return cookie_file
                                except Exception as e:
                                    logging.debug(f"[CookieRotation] Playwright cookie yozishda xato: {e}")
                    except Exception as e:
                        logging.debug(f"[CookieRotation] Playwright ishlamadi: {e}")
            except Exception:
                pass

            # 2) Fallback to browser_cookie3 extraction (existing approach)
            if browser_cookie3 is None:
                logging.debug("[CookieRotation] browser_cookie3 mavjud emas")
                return None

            for source in BROWSER_COOKIE_SOURCES:
                getter = getattr(browser_cookie3, source, None)
                if not callable(getter):
                    continue

                try:
                    jar = getter(domain_name='youtube.com')
                    if not jar:
                        continue

                    has_youtube = any('youtube.com' in getattr(cookie, 'domain', '') for cookie in jar)
                    if has_youtube:
                        # Yangi fayl yaratish
                        timestamp = int(time.time() * 1000)
                        cookie_file = os.path.join(self.pool_dir, f"fresh_{timestamp}.txt")

                        if _write_netscape_cookiejar(jar, cookie_file):
                            # assign optional proxy
                            proxy = None
                            if COOKIE_PROXIES:
                                proxy = COOKIE_PROXIES[_COOKIE_PROXY_INDEX % len(COOKIE_PROXIES)]
                                _COOKIE_PROXY_INDEX += 1

                            logging.info(f"[CookieRotation] Yangi cookies yaratildi: {source} -> {os.path.basename(cookie_file)}")
                            self.active_cookies[cookie_file] = {
                                'created': time.time(),
                                'source': source,
                                'successes': 0,
                                'failures': 0,
                                'blocked': False,
                                'proxy': proxy,
                            }
                            self.save_cookie_status()
                            return cookie_file
                except Exception as e:
                    logging.debug(f"[CookieRotation] {source} xatosi: {str(e)[:100]}")
                    continue

            logging.warning("[CookieRotation] Browserdan fresh cookies olalmagani, backup dan ishlatish...")
            return self.get_best_cached_cookie()
            
        except Exception as e:
            logging.error(f"[CookieRotation] Yangi cookie yaratishda xatolik: {e}")
            return None
    
    def get_best_cached_cookie(self) -> str | None:
        """Eng yaxshi keshirlangan cookiedan olish (block bo'lmagani)"""
        try:
            best_cookie = None
            best_score = -999
            
            for cookie_file, status in self.active_cookies.items():
                if status.get('blocked', False):
                    continue
                    
                successes = status.get('successes', 0)
                failures = status.get('failures', 0)
                
                # Score hisoblash
                total = successes + failures + 1
                score = successes / total
                
                if score > best_score and os.path.exists(cookie_file):
                    best_score = score
                    best_cookie = cookie_file
            
            return best_cookie
        except Exception as e:
            logging.debug(f"[CookieRotation] Best cookie topishda xatolik: {e}")
            return None
    
    def mark_cookie_success(self, cookie_file: str):
        """Cookiedan muvaffaqiyatli foydalanish"""
        try:
            if cookie_file in self.active_cookies:
                self.active_cookies[cookie_file]['successes'] = \
                    self.active_cookies[cookie_file].get('successes', 0) + 1
                self.save_cookie_status()
                logging.info(f"[CookieRotation] ✓ Muvaffaqiyatli: {os.path.basename(cookie_file)}")
        except Exception as e:
            logging.debug(f"[CookieRotation] Mark success xatosi: {e}")
    
    def mark_cookie_blocked(self, cookie_file: str):
        """Cookieni bloklanganlar ro'yxatiga qo'shish"""
        try:
            self.blocked_cookies.add(cookie_file)
            
            if cookie_file in self.active_cookies:
                self.active_cookies[cookie_file]['blocked'] = True
                self.active_cookies[cookie_file]['failures'] = \
                    self.active_cookies[cookie_file].get('failures', 0) + 1
            
            self.save_cookie_status()
            logging.warning(f"[CookieRotation] ⚠ Blocked: {os.path.basename(cookie_file)}")
            
            # Eski fayl o'chirish
            try:
                if os.path.exists(cookie_file):
                    os.remove(cookie_file)
                    logging.info(f"[CookieRotation] O'chirildi: {os.path.basename(cookie_file)}")
            except Exception:
                pass
                
        except Exception as e:
            logging.debug(f"[CookieRotation] Mark blocked xatosi: {e}")
    
    def cleanup_old_cookies(self, max_age_hours: int = 2):
        """Eski cookiesni o'chirish"""
        try:
            now = time.time()
            removed_count = 0
            
            for cookie_file in list(self.active_cookies.keys()):
                created = self.active_cookies[cookie_file].get('created', 0)
                age_hours = (now - created) / 3600
                
                if age_hours > max_age_hours:
                    try:
                        if os.path.exists(cookie_file):
                            os.remove(cookie_file)
                            del self.active_cookies[cookie_file]
                            removed_count += 1
                    except Exception:
                        pass
            
            if removed_count > 0:
                self.save_cookie_status()
                logging.info(f"[CookieRotation] {removed_count} ta eski cookies o'chirildi")
        except Exception as e:
            logging.debug(f"[CookieRotation] Cleanup xatosi: {e}")

# Global instance
cookie_rotation_manager = CookieRotationManager()

# ========================== FRESH COOKIE HELPERS ==========================

def get_fresh_cookie_for_operation() -> str | None:
    """
    Har operatsiya uchun yangi cookies fayl olish.
    Agar browserdan cookies olalmaas - best cached cookie qaytaradi.
    """
    fresh = cookie_rotation_manager.create_fresh_cookie_file()
    if fresh:
        return fresh
    return cookie_rotation_manager.get_best_cached_cookie()

def mark_operation_success(cookie_file: str | None):
    """Operatsiyadan muvaffaqiyatli foydalanish"""
    if cookie_file:
        cookie_rotation_manager.mark_cookie_success(cookie_file)

def mark_operation_blocked(cookie_file: str | None):
    """Operatsiya bloklandi"""
    if cookie_file:
        cookie_rotation_manager.mark_cookie_blocked(cookie_file)

def mark_operation_failure(cookie_file: str | None):
    """Operatsiya xatosi bilan yakunlandi"""
    if cookie_file:
        if cookie_file in cookie_rotation_manager.active_cookies:
            cookie_rotation_manager.active_cookies[cookie_file]['failures'] = \
                cookie_rotation_manager.active_cookies[cookie_file].get('failures', 0) + 1
            cookie_rotation_manager.save_cookie_status()


# ========================== UNIFIED COOKIE HELPER ==========================
# Barcha platformalar uchun BIR cookie fayl
DEFAULT_COOKIE_FILE = os.path.join(os.getcwd(), "cookies.txt")
COOKIE_FILE = os.path.abspath(YOUTUBE_COOKIE_FILE) if YOUTUBE_COOKIE_FILE else DEFAULT_COOKIE_FILE


def _is_youtube_cookiefile_valid(path: str, check_expiry: bool = True) -> bool:
    """Cookies fayli tekshirish (expiry ham tekshiriladi)"""
    try:
        if not os.path.exists(path):
            return False
        
        with open(path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        has_youtube = False
        has_youtube_login = False
        now = int(time.time())
        expired_count = 0
        total_cookies = 0
        
        for line in lines:
            if line.startswith('#') or not line.strip():
                continue
            
            parts = line.strip().split('\t')
            if len(parts) < 7:
                continue
            
            total_cookies += 1
            domain, _, _, _, expiry_str, name, value = parts[:7]
            
            # YouTube domain tekshirish
            if 'youtube.com' in domain:
                has_youtube = True
            
            # YouTube login tokens
            if any(token in name for token in ('LOGIN_INFO', 'SID', 'SAPISID', 'APISID', 'HSID', 'SSID')):
                has_youtube_login = True
            
            # Expiry tekshirish
            if check_expiry and expiry_str != '0':
                try:
                    expiry = int(expiry_str)
                    if expiry > 0 and expiry < now:
                        expired_count += 1
                except (ValueError, TypeError):
                    pass
        
        # Agar cookies 50% dan ko'proq eskirgan bo'lsa - invalid deb bilish
        if check_expiry and total_cookies > 0 and (expired_count / total_cookies) > 0.5:
            logging.info(f"[Cookies] {expired_count}/{total_cookies} cookies eskirgan, yangilash kerak")
            return False
        
        return has_youtube and has_youtube_login
    except Exception as e:
        logging.debug(f"[Cookies] Validation xatosi: {e}")
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


def refresh_youtube_cookiefile(force: bool = False) -> bool:
    """Avtomatik YouTube cookies refresh - browserdan olish"""
    # Agar force=False va cookies fresh bo'lsa - qaytarish
    if not force and os.path.exists(COOKIE_FILE) and _is_youtube_cookiefile_valid(COOKIE_FILE, check_expiry=True):
        return True

    if browser_cookie3 is None:
        logging.debug("[Cookies] browser_cookie3 mavjud emas")
        return False

    logging.info("[Cookies] YouTube cookies browserdan yangilash...")
    
    for source in BROWSER_COOKIE_SOURCES:
        getter = getattr(browser_cookie3, source, None)
        if not callable(getter):
            continue

        try:
            jar = getter(domain_name='youtube.com')
            if not jar:
                continue

            has_youtube = any('youtube.com' in getattr(cookie, 'domain', '') for cookie in jar)
            if has_youtube:
                if _write_netscape_cookiejar(jar, COOKIE_FILE) and _is_youtube_cookiefile_valid(COOKIE_FILE, check_expiry=False):
                    logging.info(f"[Cookies] ✓ YouTube cookies muvaffaqiyatli yangilandi: {source}")
                    return True
        except Exception as e:
            logging.debug(f"[Cookies] {source} xatosi: {str(e)[:100]}")
            continue

    logging.warning("[Cookies] Browserdan cookies topilmadi, existing file ishlatiladi")
    return os.path.exists(COOKIE_FILE) and _is_youtube_cookiefile_valid(COOKIE_FILE, check_expiry=False)


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
    """Faqat guruhga qo'shish knopkasi"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(
                text="👥 Guruhga qo'shish",
                url=f"https://t.me/{BOT_USERNAME}?startgroup=true"
            ),
        ]
    ])


def build_media_kb(user_id: int, lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="🔁 Dumaloq video qilish", callback_data=f"round_media:{user_id}"),
            InlineKeyboardButton(text="💾 Saqlash", callback_data=f"save_media:{user_id}"),
        ],
        [
            InlineKeyboardButton(
                text="👥 Guruhga qo'shish",
                url=f"https://t.me/{BOT_USERNAME}?startgroup=true",
            )
        ],
    ])


def build_video_kb(user_id: int, lang: str) -> InlineKeyboardMarkup:
    return build_media_kb(user_id, lang)


def convert_to_video_note(input_path: str, output_path: str) -> bool:
    """240x240 video note (past sifat - Telegram limit)"""
    ffmpeg_cmd = find_ffmpeg_cmd()
    if not ffmpeg_cmd:
        logging.error("[RoundVideo] FFmpeg topilmadi!")
        return False

    try:
        w, h = _get_video_dimensions(input_path, ffmpeg_cmd)

        if w > 0 and h > 0:
            size = min(w, h)
            x = (w - size) // 2
            y = (h - size) // 2
            crop_filter = f"crop={size}:{size}:{x}:{y},scale=240:240"
            logging.info(f"[RoundVideo] Video {w}x{h}, crop={size}:{size}:{x}:{y}")
        else:
            crop_filter = "crop='min(iw,ih)':'min(iw,ih)',scale=240:240"
            logging.warning(f"[RoundVideo] O'lcham aniqlanmadi, fallback ishlatilmoqda")

        cmd = [
            ffmpeg_cmd, "-y", "-i", input_path,
            "-vf", crop_filter,
            "-c:v", "libx264", "-profile:v", "baseline", "-level", "3.0",
            "-pix_fmt", "yuv420p", "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
            "-t", "60",
            output_path,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            logging.error(f"[RoundVideo] FFmpeg xatosi: {result.stderr[:500]}")
            return False
        if not os.path.exists(output_path) or os.path.getsize(output_path) < 1024:
            logging.error("[RoundVideo] Output fayl yaratilmadi yoki juda kichik")
            return False
        logging.info(f"[RoundVideo] Video note tayyor: {output_path}")
        return True

    except Exception as e:
        logging.error(f"[RoundVideo] FFmpeg xatosi: {e}")
        return False


def convert_to_video_note_hq(input_path: str, output_path: str) -> bool:
    """Telegram video note (240x240) - 12MB limitini hisobga olib"""
    ffmpeg_cmd = find_ffmpeg_cmd()
    if not ffmpeg_cmd:
        logging.error("[VideoNoteHQ] FFmpeg topilmadi!")
        return False

    try:
        w, h = _get_video_dimensions(input_path, ffmpeg_cmd)

        if w > 0 and h > 0:
            size = min(w, h)
            x = (w - size) // 2
            y = (h - size) // 2
            crop_filter = f"crop={size}:{size}:{x}:{y},scale=240:240:flags=lanczos,unsharp=3:3:0.5"
            logging.info(f"[VideoNoteHQ] Video {w}x{h} -> 240x240 (lanczos+unsharp)")
        else:
            crop_filter = "crop='min(iw,ih)':'min(iw,ih)',scale=240:240:flags=lanczos"
            logging.warning(f"[VideoNoteHQ] O'lcham aniqlanmadi, fallback ishlatilmoqda")

        # Telegram limit: 12MB = 12,582,912 bytes
        MAX_VIDEO_NOTE_BYTES = 12_582_912

        # Video uzunligini aniqlash (60s limit)
        duration = 0
        try:
            ffprobe_path = ffmpeg_cmd.replace('ffmpeg', 'ffprobe')
            probe = subprocess.run(
                [ffprobe_path, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "format=duration", "-of", "csv=p=0", input_path],
                capture_output=True, text=True, timeout=10
            )
            if probe.returncode == 0:
                duration = float(probe.stdout.strip())
        except Exception:
            pass

        # 60 soniyadan uzun bo'lsa kesish
        duration_limit = 60
        if duration > duration_limit:
            duration = duration_limit

        # Bitrate hisoblash: target_size = bitrate * duration / 8
        # bitrate (bits/sec) = target_size * 8 / duration
        # 80% hajm zaxirasi (audio + container overhead uchun)
        target_size_bits = (MAX_VIDEO_NOTE_BYTES * 0.8) * 8
        if duration > 0:
            calculated_bitrate = int(target_size_bits / duration)
            # Min/Max chegaralari
            video_bitrate = max(300_000, min(calculated_bitrate, 1_500_000))  # 300K - 1.5M
        else:
            video_bitrate = 800_000  # default

        audio_bitrate = min(128_000, max(64_000, video_bitrate // 4))  # video_bitrate ning 1/4 qismi

        logging.info(f"[VideoNoteHQ] Target: {video_bitrate/1000:.0f}k video, {audio_bitrate/1000:.0f}k audio, max {duration}s")

        cmd = [
            ffmpeg_cmd, "-y", "-i", input_path,
            "-vf", crop_filter,
            "-c:v", "libx264",
            "-b:v", f"{video_bitrate}",
            "-maxrate", f"{int(video_bitrate * 1.2)}",
            "-bufsize", f"{video_bitrate * 2}",
            "-preset", "fast",  # "slow" o'rniga "fast" - tezroq va kichikroq
            "-profile:v", "baseline",  # "high" o'rniga "baseline" - kichikroq
            "-level", "3.0",
            "-refs", "2",
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-c:a", "aac",
            "-b:a", f"{audio_bitrate}",
            "-ar", "44100",
            "-t", str(duration_limit),
            output_path,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            logging.error(f"[VideoNoteHQ] FFmpeg xatosi: {result.stderr[:500]}")
            return False

        if not os.path.exists(output_path) or os.path.getsize(output_path) < 1024:
            logging.error("[VideoNoteHQ] Output fayl yaratilmadi yoki juda kichik")
            return False

        file_size = os.path.getsize(output_path)
        logging.info(f"[VideoNoteHQ] Tayyor: 240x240, {file_size/(1024*1024):.1f}MB, bitrate={video_bitrate/1000:.0f}k")

        # 12MB dan katta bo'lsa - qayta kompress qilish
        if file_size > MAX_VIDEO_NOTE_BYTES:
            logging.warning(f"[VideoNoteHQ] {file_size/(1024*1024):.1f}MB > 12MB limit, qayta kompress...")
            # Yana kichikroq bitrate bilan
            smaller_bitrate = int(video_bitrate * 0.6)
            cmd[cmd.index("-b:v") + 1] = f"{smaller_bitrate}"
            cmd[cmd.index("-maxrate") + 1] = f"{int(smaller_bitrate * 1.2)}"
            cmd[cmd.index("-bufsize") + 1] = f"{smaller_bitrate * 2}"

            result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            if result.returncode == 0 and os.path.exists(output_path):
                new_size = os.path.getsize(output_path)
                logging.info(f"[VideoNoteHQ] Qayta kompress: {new_size/(1024*1024):.1f}MB")
                if new_size <= MAX_VIDEO_NOTE_BYTES:
                    return True

        return file_size <= MAX_VIDEO_NOTE_BYTES

    except Exception as e:
        logging.error(f"[VideoNoteHQ] FFmpeg xatosi: {e}")
        return False


def convert_to_square_video(input_path: str, output_path: str) -> bool:
    """ASL SIFATDA kvadrat video (1080p, 2K, 4K - asl sifatni saqlaydi)"""
    ffmpeg_cmd = find_ffmpeg_cmd()
    if not ffmpeg_cmd:
        logging.error("[SquareVideo] FFmpeg topilmadi!")
        return False

    try:
        w, h = _get_video_dimensions(input_path, ffmpeg_cmd)

        if w > 0 and h > 0:
            size = min(w, h)
            x = (w - size) // 2
            y = (h - size) // 2
            # Asl sifatni saqlash - scale YO'Q!
            crop_filter = f"crop={size}:{size}:{x}:{y}"
            logging.info(f"[SquareVideo] Video {w}x{h}, crop={size}:{size}:{x}:{y} (asl sifat)")
        else:
            crop_filter = "crop='min(iw,ih)':'min(iw,ih)'"
            logging.warning(f"[SquareVideo] O'lcham aniqlanmadi, fallback ishlatilmoqda")

        # Asl sifatni saqlash: -crf 18 (yuqori sifat), preset slow (eng yaxshi sifat)
        cmd = [
            ffmpeg_cmd, "-y", "-i", input_path,
            "-vf", crop_filter,
            "-c:v", "libx264",
            "-crf", "18",           # Yuqori sifat (0=lossless, 18=visually lossless, 23=default)
            "-preset", "medium",    # Sifat va tezlik balansi
            "-profile:v", "high",  # Yuqori profil (high quality)
            "-level", "5.1",       # 4K qo'llab-quvvatlash
            "-pix_fmt", "yuv420p",
            "-movflags", "+faststart",
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            output_path,
        ]

        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            logging.error(f"[SquareVideo] FFmpeg xatosi: {result.stderr[:500]}")
            return False
        if not os.path.exists(output_path) or os.path.getsize(output_path) < 1024:
            logging.error("[SquareVideo] Output fayl yaratilmadi yoki juda kichik")
            return False

        # Natija o'lchamlarini tekshirish
        out_w, out_h = _get_video_dimensions(output_path, ffmpeg_cmd)
        file_size = os.path.getsize(output_path)
        logging.info(f"[SquareVideo] Tayyor: {out_w}x{out_h}, {file_size/(1024*1024):.1f}MB")
        return True

    except Exception as e:
        logging.error(f"[SquareVideo] FFmpeg xatosi: {e}")
        return False


def _get_video_dimensions(input_path: str, ffmpeg_cmd: str) -> tuple[int, int]:
    """Video o'lchamlarini aniqlash (ffprobe yoki ffmpeg)"""
    w, h = 0, 0

    # ffprobe bilan - to'g'ri yo'lni topish
    ffprobe_candidates = [
        ffmpeg_cmd.replace('ffmpeg', 'ffprobe'),
        ffmpeg_cmd.replace('ffmpeg.exe', 'ffprobe.exe'),
        shutil.which("ffprobe"),
        shutil.which("ffprobe.exe"),
    ]

    ffprobe_path = None
    for candidate in ffprobe_candidates:
        if candidate and os.path.exists(candidate):
            ffprobe_path = candidate
            break

    if ffprobe_path:
        try:
            probe = subprocess.run(
                [ffprobe_path, "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "csv=p=0", input_path],
                capture_output=True, text=True, timeout=10
            )
            if probe.returncode == 0:
                dims = probe.stdout.strip().split(",")
                if len(dims) == 2:
                    try:
                        w, h = int(dims[0]), int(dims[1])
                        if w > 0 and h > 0:
                            return w, h
                    except ValueError:
                        pass
        except Exception as e:
            logging.debug(f"[Dimensions] ffprobe xatosi: {e}")

    # ffmpeg fallback - stderr dan o'qish
    try:
        probe = subprocess.run(
            [ffmpeg_cmd, "-i", input_path],
            capture_output=True, text=True, timeout=10
        )
        # Ko'proq patternlar
        patterns = [
            r'Stream.*Video.*\s(\d+)x(\d+)',
            r'(\d+)x(\d+)',
            r'Video:.*\s(\d+)x(\d+)',
        ]
        for pattern in patterns:
            m = re.search(pattern, probe.stderr)
            if m:
                try:
                    w, h = int(m.group(1)), int(m.group(2))
                    if w > 0 and h > 0:
                        return w, h
                except ValueError:
                    pass
    except Exception as e:
        logging.debug(f"[Dimensions] ffmpeg fallback xatosi: {e}")

    return w, h


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
    """
    yt-dlp orqali YouTube qidirish.
    Har qidirishdan oldin YANGI cookies yaratish (agar kerak bo'lsa)
    """
    def _try_search(use_fresh_cookies: bool = False, attempt: int = 1):
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
            proxy = get_current_proxy()
            if proxy:
                opts["proxy"] = proxy

            # Fresh cookies qo'shish (agar kerak bo'lsa)
            fresh_cookie_file = None
            if use_fresh_cookies:
                fresh_cookie_file = cookie_rotation_manager.create_fresh_cookie_file()
                if fresh_cookie_file:
                    opts["cookiefile"] = fresh_cookie_file
                    logging.info(f"[Search] Attempt {attempt}: Fresh cookies bilan | {os.path.basename(fresh_cookie_file)}")

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
                
                # Muvaffaqiyatli! Mark cookie success
                if use_fresh_cookies and fresh_cookie_file:
                    cookie_rotation_manager.mark_cookie_success(fresh_cookie_file)
                
                return results
                
        except Exception as e:
            error_msg = str(e).lower()
            
            # Agar YouTube blocking bo'lsa va cookies ishlatilmadi - qayta urinish
            if not use_fresh_cookies and any(blocker in error_msg for blocker in [
                'sign in to confirm',
                'bot',
                'login_required',
                '403',
                'forbidden'
            ]):
                logging.warning(f"[Search] YouTube blocking aniqlandı, fresh cookies bilan qayta urinish...")
                return _try_search(use_fresh_cookies=True, attempt=2)
            
            # Agar cookies bilan ham bloklansa - block mark qilish
            if use_fresh_cookies and fresh_cookie_file and any(blocker in error_msg for blocker in [
                'sign in to confirm',
                'bot',
                'login_required',
                '403'
            ]):
                cookie_rotation_manager.mark_cookie_blocked(fresh_cookie_file)
                logging.warning(f"[Search] Cookie bloklanib qoldi, keyingi urinishda o'tkazib yuboriladi")
            
            logging.debug(f"[Search] yt-dlp ytsearch xatolik (attempt {attempt}): {str(e)[:100]}")
            return []
    
    # Birinchi urinish: Cookies SIZ
    results = _try_search(use_fresh_cookies=False, attempt=1)
    if results:
        return results
    
    # Ikkinchi urinish: Fresh cookies BILAN (agar needed)
    return _try_search(use_fresh_cookies=True, attempt=2)


# ========================== YOUTUBE VIDEO ID EXTRACTOR ==========================
def extract_youtube_video_id(url: str) -> str | None:
    """YouTube URL dan video ID ajratib olish"""
    patterns = [
        r'(?:v=|\/)([0-9A-Za-z_-]{11})(?:[?&]|$)',
        r'youtu\.be\/([0-9A-Za-z_-]{11})',
        r'youtube\.com\/embed\/([0-9A-Za-z_-]{11})',
        r'youtube\.com\/shorts\/([0-9A-Za-z_-]{11})',
        r'youtube\.com\/live\/([0-9A-Za-z_-]{11})',
    ]
    for pattern in patterns:
        match = re.search(pattern, url)
        if match:
            return match.group(1)
    # Agar to'g'ridan-to'g'ri video ID yuborilgan bo'lsa
    if len(url) == 11 and re.match(r'^[0-9A-Za-z_-]+$', url):
        return url
    return None


# ========================== PIPED INSTANCES ==========================
WORKING_PIPED_INSTANCES: list[str] = []
PIPED_INSTANCE_LOCK = asyncio.Lock()
PIPED_LAST_REFRESH = 0.0


async def probe_piped_instance(instance: str) -> bool:
    probe_video_id = PIPED_PROBE_VIDEO_ID
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json",
    }
    connector = aiohttp.TCPConnector(ssl=False)
    try:
        proxy = get_current_proxy()
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                f"{instance}/streams/{probe_video_id}",
                timeout=aiohttp.ClientTimeout(total=12),
                headers=headers,
                proxy=proxy,
            ) as resp:
                if resp.status != 200:
                    return False
                data = await resp.json()
                if not isinstance(data, dict):
                    return False
                audio_streams = data.get("audioStreams", [])
                if audio_streams and any(s.get("url") for s in audio_streams):
                    return True
                video_streams = data.get("videoStreams", [])
                return bool(video_streams and any(s.get("url") for s in video_streams if not s.get("videoOnly", False)))
    except Exception:
        return False


async def refresh_piped_instances(force: bool = False) -> list[str]:
    global WORKING_PIPED_INSTANCES, PIPED_LAST_REFRESH
    now = time.time()
    if not force and WORKING_PIPED_INSTANCES and now - PIPED_LAST_REFRESH < PIPED_INSTANCE_REFRESH_INTERVAL_SECONDS:
        return WORKING_PIPED_INSTANCES

    async with PIPED_INSTANCE_LOCK:
        if not force and WORKING_PIPED_INSTANCES and now - PIPED_LAST_REFRESH < PIPED_INSTANCE_REFRESH_INTERVAL_SECONDS:
            return WORKING_PIPED_INSTANCES

        tasks = {
            asyncio.create_task(probe_piped_instance(instance)): instance
            for instance in PIPED_API_INSTANCES
        }
        if not tasks:
            return []

        done, pending = await asyncio.wait(tasks.keys(), timeout=18)
        working = []
        for task in done:
            instance = tasks[task]
            try:
                if task.result():
                    working.append(instance)
            except Exception as e:
                logging.debug(f"[Piped] probe xatolik {instance}: {e}")
        for task in pending:
            task.cancel()

        WORKING_PIPED_INSTANCES = working
        PIPED_LAST_REFRESH = time.time()
        logging.info(f"[Piped] ishlaydigan instancelar: {WORKING_PIPED_INSTANCES}")
        return WORKING_PIPED_INSTANCES


# ========================== PIPED AUDIO URL ==========================
async def _get_piped_audio_url_instance(instance: str, video_id: str) -> tuple[str | None, dict | None]:
    connector = aiohttp.TCPConnector(ssl=False)
    try:
        proxy = get_current_proxy()
        async with aiohttp.ClientSession(connector=connector) as session:
            async with session.get(
                f"{instance}/streams/{video_id}",
                timeout=aiohttp.ClientTimeout(total=12),
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json",
                },
                proxy=proxy,
            ) as resp:
                if resp.status != 200:
                    body = await resp.text()
                    logging.debug(
                        f"[Piped] {instance} status {resp.status} for {video_id}: {body[:180]}"
                    )
                    return None, None

                try:
                    data = await resp.json()
                except Exception as e:
                    text = await resp.text()
                    logging.debug(
                        f"[Piped] {instance} invalid JSON for {video_id}: {e} / {text[:180]}"
                    )
                    return None, None

                audio_streams = data.get("audioStreams", [])
                if not audio_streams:
                    logging.debug(f"[Piped] {instance} no audioStreams for {video_id}")
                    video_streams = data.get("videoStreams", [])
                    if not video_streams:
                        return None, None
                    best_audio = max(
                        (s for s in video_streams if s.get("url") and not s.get("videoOnly", False)),
                        key=lambda x: x.get("bitrate", 0),
                        default=None,
                    )
                    if not best_audio:
                        return None, None
                    audio_url = best_audio.get("url")
                    if not audio_url:
                        return None, None
                    metadata = {
                        "title": data.get("title", "Noma'lum"),
                        "uploader": data.get("uploader", "Noma'lum"),
                        "thumbnail": data.get("thumbnailUrl", ""),
                        "duration": data.get("duration", 0),
                    }
                    logging.info(f"[Piped] Audio URL topildi (video stream fallback): {instance}")
                    return audio_url, metadata

                best_audio = max(
                    (s for s in audio_streams if s.get("url")),
                    key=lambda x: x.get("bitrate", 0),
                    default=None,
                )
                if not best_audio:
                    logging.debug(f"[Piped] {instance} no valid audio stream for {video_id}")
                    return None, None

                audio_url = best_audio.get("url")
                if not audio_url:
                    logging.debug(f"[Piped] {instance} best_audio missing URL for {video_id}")
                    return None, None

                metadata = {
                    "title": data.get("title", "Noma'lum"),
                    "uploader": data.get("uploader", "Noma'lum"),
                    "thumbnail": data.get("thumbnailUrl", ""),
                    "duration": data.get("duration", 0),
                }
                logging.info(f"[Piped] Audio URL topildi: {instance}")
                return audio_url, metadata

    except asyncio.TimeoutError:
        logging.debug(f"[Piped] {instance} timeout")
    except aiohttp.ClientError as e:
        logging.debug(f"[Piped] {instance} client error: {e}")
    except Exception as e:
        logging.debug(f"[Piped] {instance} xatolik: {str(e)[:180]}")
    return None, None


async def get_piped_audio_url(video_id: str) -> tuple[str | None, dict | None]:
    instances = await refresh_piped_instances()
    if not instances:
        logging.warning("[Piped] Hoziroq ishlaydigan instancelar topilmadi, barcha kandidatlardan qayta tekshirilyapti...")
        instances = await refresh_piped_instances(force=True)

    if not instances:
        logging.warning("[Piped] Hech qanday probe orqali ishlaydigan instancelar topilmadi, barcha kandidatlarni qayta sinayapmiz...")
        instances = list(PIPED_API_INSTANCES)

    if not instances:
        logging.error(f"[Piped] {video_id} uchun hech qanday ishlaydigan instancelar topilmadi")
        return None, None
    logging.info(f"[Piped] Urinish qilinmoqda — instancelar: {instances}")
    tasks = {
        asyncio.create_task(_get_piped_audio_url_instance(instance, video_id)): instance
        for instance in instances
    }
    try:
        while tasks:
            done, pending = await asyncio.wait(
                tasks.keys(),
                return_when=asyncio.FIRST_COMPLETED,
                timeout=12,
            )
            if not done:
                break

            for task in done:
                instance = tasks.pop(task)
                try:
                    audio_url, metadata = task.result()
                except Exception as e:
                    logging.debug(f"[Piped] {instance} audio task xatolik: {e}")
                    continue

                if audio_url:
                    for pending_task in pending:
                        pending_task.cancel()
                    logging.info(f"[Piped] {instance} dan birinchi javob olindi")
                    return audio_url, metadata

            tasks = {task: inst for task, inst in tasks.items() if task in pending}
    finally:
        for task in tasks:
            task.cancel()

    logging.error(f"[Piped] {video_id} uchun barcha instancelar ishlamadi")
    # On-demand sweep: try every candidate sequentially (useful when probes didn't catch per-video variance)
    logging.info(f"[Piped] On-demand sweep boshlanmoqda for {video_id}")
    for candidate in PIPED_API_INSTANCES:
        try:
            logging.info(f"[Piped] On-demand sinov: {candidate} -> {video_id}")
            audio_url, metadata = await _get_piped_audio_url_instance(candidate, video_id)
            if audio_url:
                # promote this candidate to working list
                async with PIPED_INSTANCE_LOCK:
                    if candidate not in WORKING_PIPED_INSTANCES:
                        WORKING_PIPED_INSTANCES.insert(0, candidate)
                logging.info(f"[Piped] On-demand topildi: {candidate} for {video_id}")
                return audio_url, metadata
        except Exception as e:
            logging.debug(f"[Piped] On-demand xato {candidate}: {e}")

    logging.error(f"[Piped] {video_id} uchun barcha instancelar ishlamadi (on-demand ham) ")
    return None, None


# ========================== DOWNLOAD PIPED AUDIO ==========================
async def download_piped_audio(audio_url: str, filename: str) -> str |None:
    """
    Piped audio stream URL dan fayl yuklash.
    """
    safe = re.sub(r'[\\/*?:"<>|]', "_", filename)
    output_path = os.path.join(tempfile.gettempdir(), f"piped_{safe}.mp3")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*",
        "Referer": "https://www.youtube.com/",
    }

    try:
        proxy = get_current_proxy()
        async with aiohttp.ClientSession() as session:
            async with session.get(
                audio_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=120),
                proxy=proxy,
            ) as response:
                if response.status != 200:
                    logging.warning(f"[Piped] Yuklash xatosi: status {response.status}")
                    if proxy and response.status in (403, 429, 500, 502, 503, 504):
                        mark_proxy_blocked(proxy)
                    return None

                content_type = response.headers.get("Content-Type", "").lower()
                if "audio/mpeg" in content_type or "mp3" in content_type:
                    ext = ".mp3"
                elif "audio/mp4" in content_type or "video/mp4" in content_type:
                    ext = ".m4a"
                elif "audio/webm" in content_type or "video/webm" in content_type:
                    ext = ".webm"
                elif "audio/opus" in content_type:
                    ext = ".opus"
                else:
                    ext = ".mp3"

                output_path = os.path.join(tempfile.gettempdir(), f"piped_{safe}{ext}")
                with open(output_path, "wb") as f:
                    async for chunk in response.content.iter_chunked(65536):
                        if not chunk:
                            break
                        f.write(chunk)

        if not os.path.exists(output_path) or os.path.getsize(output_path) < 2048:
            logging.warning("[Piped] Fayl juda kichik yoki yuklanmadi")
            return None

        file_size = os.path.getsize(output_path) / (1024 * 1024)
        logging.info(f"[Piped] Yuklandi: {os.path.basename(output_path)} ({file_size:.1f} MB)")
        return output_path
    except Exception as e:
        logging.error(f"[Piped] Yuklash xatosi: {e}")
        return None


# ========================== INVIDIOUS INSTANCES ==========================
INVIDIOUS_INSTANCES = [
    "https://vid.puffyan.us",
    "https://inv.riverside.rocks",
    "https://invidious.snopyta.org",
    "https://y.com.sb",
    "https://invidious.kavin.rocks",
    "https://iv.nboeck.de",
    "https://iv.datura.network",
    "https://yt.artemislena.eu",
    "https://invidious.perennialte.ch",
    "https://iv.melmac.space",
]

async def get_invidious_audio_url(video_id: str) -> tuple[str | None, dict | None]:
    """Invidious API orqali audio stream URL olish - cookies kerak emas"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json",
    }
    connector = aiohttp.TCPConnector(ssl=False)

    async with aiohttp.ClientSession(connector=connector) as session:
        for instance in INVIDIOUS_INSTANCES:
            try:
                api_url = f"{instance}/api/v1/videos/{video_id}"
                proxy = get_current_proxy()
                async with session.get(
                    api_url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=10),
                    proxy=proxy,
                ) as resp:
                    if resp.status != 200:
                        logging.debug(f"[Invidious] {instance} status {resp.status}")
                        continue

                    try:
                        data = await resp.json()
                    except Exception:
                        continue

                    formats = data.get("adaptiveFormats")
                    if formats is None:
                        formats = data.get("formatStreams")
                    if formats is None and isinstance(data.get("streamingData"), dict):
                        formats = data["streamingData"].get("adaptiveFormats") or data["streamingData"].get("formats")
                    if formats is None:
                        formats = data.get("formats", [])
                    if formats is None:
                        formats = []

                    audio_formats = [
                        f for f in formats
                        if f.get("type", "").lower().startswith("audio/") or "audio" in f.get("type", "").lower()
                    ]

                    if not audio_formats:
                        continue

                    best_audio = max(
                        audio_formats,
                        key=lambda x: int(x.get("bitrate", 0) or 0)
                    )

                    audio_url = best_audio.get("url")
                    if not audio_url:
                        continue

                    metadata = {
                        "title": data.get("title", "Noma'lum"),
                        "uploader": data.get("author", "Noma'lum"),
                        "thumbnail": (data.get("videoThumbnails", [{}])[0].get("url", "")
                                     if data.get("videoThumbnails") else ""),
                        "duration": int(data.get("lengthSeconds", 0) or 0),
                    }
                    logging.info(f"[Invidious] Audio URL topildi: {instance}")
                    return audio_url, metadata

            except asyncio.TimeoutError:
                logging.debug(f"[Invidious] {instance} timeout")
            except Exception as e:
                logging.debug(f"[Invidious] {instance} xato: {str(e)[:100]}")

    return None, None


async def download_invidious_audio(audio_url: str, filename: str) -> str | None:
    """Invidious audio stream dan fayl yuklash"""
    safe = re.sub(r'[\\/*?:"<>|]', "_", filename)
    output_path = os.path.join(tempfile.gettempdir(), f"invidious_{safe}.mp3")
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "*/*",
        "Referer": "https://www.youtube.com/",
    }

    try:
        proxy = get_current_proxy()
        async with aiohttp.ClientSession() as session:
            async with session.get(
                audio_url,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=120),
                proxy=proxy,
            ) as response:
                if response.status != 200:
                    logging.warning(f"[Invidious] Yuklash xatosi: status {response.status}")
                    if proxy and response.status in (403, 429, 500, 502, 503, 504):
                        mark_proxy_blocked(proxy)
                    return None

                content_type = response.headers.get("Content-Type", "").lower()
                if "audio/mpeg" in content_type:
                    ext = ".mp3"
                elif "audio/mp4" in content_type:
                    ext = ".m4a"
                elif "audio/webm" in content_type:
                    ext = ".webm"
                else:
                    ext = ".mp3"

                output_path = os.path.join(tempfile.gettempdir(), f"invidious_{safe}{ext}")
                with open(output_path, "wb") as f:
                    async for chunk in response.content.iter_chunked(65536):
                        if not chunk:
                            break
                        f.write(chunk)

        if os.path.exists(output_path) and os.path.getsize(output_path) > 2048:
            file_size = os.path.getsize(output_path) / (1024 * 1024)
            logging.info(f"[Invidious] Yuklandi: {os.path.basename(output_path)} ({file_size:.1f} MB)")
            return output_path
        return None
    except Exception as e:
        logging.error(f"[Invidious] Yuklash xatosi: {e}")
        return None


# Cobalt API o'chirildi — faqat Piped, Invidious va yt-dlp bilan proxy dan foydalanadi


# ========================== YT-DLP FALLBACK (COOKIE-FREE) ==========================
def download_youtube_audio_sync(url: str, filename: str) -> str | None:
    """
    YouTube audio yuklash — yt-dlp cookie-free clients bilan urinish (PROXY BILAN OPTIMIZED).
    Bu funksiya YOUTUBE_PROXY dan foydalanib anonim clientlar orqali ishlaydi.
    Har video uchun bir necha proxy bilan urinadi (dynamic rotation).
    
    OPTIMIZED FEATURES:
    - Multi-proxy rotation: 10+ proxies per download attempt
    - 7 ta cookie-free client: android_vr (best), tv_embedded, web_creator, mweb, ios, android, web_safari
    - Enhanced timeouts: 40s socket, 7x retries
    - Fragment retries: 7x for robust streaming
    - Dynamic proxy from GitHub list (3000+ proxies, refreshes hourly)
    """
    safe = re.sub(r'[\\/*?:"<>|]', "_", filename)
    ffmpeg_cmd = find_ffmpeg_cmd()

    # Priority: best performers first
    clients = [
        ("android_vr", "android_vr"),        # ← Eng yaxshi success rate
        ("tv_embedded", "tv_embedded"),      # ← Yaxshi
        ("web_creator", "web_creator"),      # ← Yaxshi
        ("mweb", "mweb"),                   # ← O'rtacha
        ("ios", "ios"),                     # ← O'rtacha
        ("android", "android"),             # ← O'rtacha
        ("web_safari", "web_safari"),       # ← Backup
    ]

    # Har video uchun 10-15 xil proxy bilan urinish
    max_proxy_attempts = 12
    tried_proxies: set[str | None] = set()
    
    for proxy_attempt_num in range(max_proxy_attempts):
        proxy = get_current_proxy()
        if proxy in tried_proxies:
            logging.debug(f"[Audio] Proxy takrorlanayapti, tugating: {proxy[:40] if proxy else 'NONE'}")
            break
        tried_proxies.add(proxy)
        logging.info(f"[Audio] Proxy attempt {proxy_attempt_num + 1}/{max_proxy_attempts}: {proxy[:40] if proxy else 'NONE'}...")

        for client_name, client_val in clients:
            output_path = os.path.join(tempfile.gettempdir(), f"dl_{client_name}_{safe}.%(ext)s")
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
                "socket_timeout": 40,
                "retries": 7,
                "fragment_retries": 7,
                "file_access_retries": 5,
                "extractor_args": {
                    "youtube": {
                        "player_client": [client_val],
                        "player_skip": ["webpage", "js"],
                        "formats": "missing_pot",
                    }
                },
                "headers": {
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                    "Accept-Language": "en-US,en;q=0.5",
                    "Accept-Encoding": "gzip, deflate, br",
                    "DNT": "1",
                    "Connection": "keep-alive",
                    "Upgrade-Insecure-Requests": "1",
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Priority": "u=1",
                },
                "geo_bypass": True,
                "geo_bypass_country": "US",
                "nocheckcertificate": True,
                "cookiesfrombrowser": None,
            }

            if ffmpeg_cmd:
                opts["ffmpeg_location"] = os.path.dirname(ffmpeg_cmd)
            if proxy:
                opts["proxy"] = proxy

            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    if not info:
                        logging.debug(f"[Audio] yt-dlp {client_name}: info bo'sh")
                        continue

                    downloaded = ydl.prepare_filename(info)
                    mp3_path = downloaded.rsplit(".", 1)[0] + ".mp3"

                    if os.path.exists(mp3_path) and os.path.getsize(mp3_path) > 2048:
                        logging.info(f"[Audio] ✓ yt-dlp {client_name}: MUVAFFAQIYATLI (MP3)")
                        return mp3_path

                    if os.path.exists(downloaded) and os.path.getsize(downloaded) > 2048:
                        logging.info(f"[Audio] ✓ yt-dlp {client_name}: MUVAFFAQIYATLI (format)")
                        return downloaded

            except Exception as e:
                err = str(e).lower()
                error_type = _detect_youtube_error(str(e))
                if proxy and any(token in err for token in (
                    "proxy", "timed out", "connection refused", "cannot connect", "failed to connect",
                    "connection reset", "remote host", "proxy error"
                )) or proxy and error_type in ("BOT_BLOCKED", "FORBIDDEN", "RATE_LIMIT", "BLOCKED", "UNAVAILABLE"):
                    logging.warning(f"[Audio] yt-dlp proxy {proxy[:40]} ishlamadi: {error_type or err[:40]}. Next proxy... ")
                    mark_proxy_blocked(proxy)
                    break

                if error_type in ("LOGIN_REQUIRED", "BOT_BLOCKED", "FORBIDDEN"):
                    logging.debug(f"[Audio] yt-dlp {client_name}: YouTube blocking ({error_type}), keyingiga o'tish...")
                else:
                    logging.debug(f"[Audio] yt-dlp {client_name}: {str(e)[:80]}")
                continue

        else:
            # All clients failed for this proxy
            if proxy:
                logging.warning(f"[Audio] yt-dlp proxy {proxy[:40]} barcha clientlar uchun ishlamadi")
                mark_proxy_blocked(proxy)
            continue

    logging.warning(f"[Audio] yt-dlp cookie-free clients ({max_proxy_attempts} ta proxy sinandi) natija bermadi, fresh cookies qo'llaniladi...")
    return None


# ========================== MAIN DOWNLOAD FUNCTION ==========================
async def download_youtube_audio(url: str, filename: str) -> str | None:
    """
    YouTube'dan audio yuklash - OPTIMIZED (Proxy + Fresh Cookies):
    1️⃣ Piped API (cookies yo'q - ASOSIY)
    2️⃣ Invidious API (cookies yo'q - YouTube mirror)
    3️⃣ yt-dlp COOKIE-FREE CLIENTS (proxy bilan)
    4️⃣ yt-dlp FRESH COOKIES (har urinish yangi cookies + proxy)
    5️⃣ yt-dlp + Saved Cookie File (last resort + proxy)
    
    Proxy: YOUTUBE_PROXY dan ishlatiladi (.env ichida)
    """
    video_id = extract_youtube_video_id(url)

    # ========== 1. PIPED API (ASOSIY) ==========
    if video_id:
        try:
            logging.info("[Audio] Stage 1: Piped API (proxy bilan)...")
            audio_url, metadata = await asyncio.wait_for(
                get_piped_audio_url(video_id),
                timeout=15.0
            )
            if audio_url:
                safe_filename = filename or (metadata.get("title", video_id) if metadata else video_id)
                logging.info(f"[Audio] → Piped URL topildi, yuklash...")
                audio_path = await download_piped_audio(audio_url, safe_filename)

                if audio_path and os.path.exists(audio_path) and os.path.getsize(audio_path) > 2048:
                    logging.info(f"[Audio] ✓ MUVAFFAQIYATLI: Piped → {os.path.basename(audio_path)}")
                    return audio_path
                else:
                    logging.warning("[Audio] Piped: fayl juda kichik yoki yaroqsiz")
            else:
                logging.debug("[Audio] Piped: URL qaytarilmadi")
        except asyncio.TimeoutError:
            logging.warning("[Audio] Piped timeout → Invidious'ga o'tish...")
        except Exception as e:
            logging.warning(f"[Audio] Piped xatolik: {type(e).__name__}: {str(e)[:80]}")
    else:
        logging.error(f"[Audio] Video ID ajratib olinmadi")
        return None

    # ========== 2. INVIDIOUS API (FALLBACK) ==========
    if video_id:
        try:
            logging.info("[Audio] Stage 2: Invidious API (YouTube zerkali)...")
            audio_url, metadata = await asyncio.wait_for(
                get_invidious_audio_url(video_id),
                timeout=15.0
            )
            if audio_url:
                safe_filename = filename or (metadata.get("title", video_id) if metadata else video_id)
                logging.info(f"[Audio] → Invidious URL topildi, yuklash...")
                audio_path = await download_invidious_audio(audio_url, safe_filename)
                if audio_path and os.path.exists(audio_path) and os.path.getsize(audio_path) > 2048:
                    logging.info("[Audio] ✓ MUVAFFAQIYATLI: Invidious")
                    return audio_path
                else:
                    logging.warning("[Audio] Invidious: fayl juda kichik yoki yaroqsiz")
            else:
                logging.debug("[Audio] Invidious: URL qaytarilmadi")
        except asyncio.TimeoutError:
            logging.warning("[Audio] Invidious timeout → yt-dlp'ga o'tish...")
        except Exception as e:
            logging.warning(f"[Audio] Invidious xatolik: {type(e).__name__}: {str(e)[:80]}")

    # ========== 3. YT-DLP COOKIE-FREE CLIENTS (PROXY BILAN) ==========
    logging.warning(f"[Audio] Stage 3: yt-dlp cookie-free (7 clients, proxy bilan)...")
    audio_path = await asyncio.to_thread(download_youtube_audio_sync, url, filename)
    if audio_path:
        logging.info(f"[Audio] ✓ MUVAFFAQIYATLI: yt-dlp cookie-free")
        return audio_path
    logging.warning("[Audio] yt-dlp cookie-free ishlamadi → Fresh cookies'ga o'tish...")

    # ========== 4. YT-DLP + FRESH COOKIES (PROXY BILAN ROTATION) ==========
    logging.warning(f"[Audio] Stage 4: Fresh cookies x3 (proxy rotation, container deployment)...")
    fresh_ok = 0
    for attempt in range(1, 4):
        try:
            fresh_cookie = await asyncio.to_thread(cookie_rotation_manager.create_fresh_cookie_file)
            if fresh_cookie and os.path.exists(fresh_cookie):
                fresh_ok += 1
                cookie_name = os.path.basename(fresh_cookie)
                logging.info(f"[Audio] Fresh attempt {attempt}/3: {cookie_name} (proxy: {YOUTUBE_PROXY[:30] if YOUTUBE_PROXY else 'NONE'}...)")
                audio_path = await asyncio.to_thread(
                    _download_youtube_audio_with_cookies,
                    url,
                    filename,
                    fresh_cookie,
                    retry=False
                )
                if audio_path and os.path.exists(audio_path) and os.path.getsize(audio_path) > 2048:
                    logging.info(f"[Audio] ✓ MUVAFFAQIYATLI: Fresh cookies attempt {attempt}")
                    cookie_rotation_manager.mark_cookie_success(fresh_cookie)
                    return audio_path
                else:
                    logging.debug(f"[Audio] Fresh {attempt}: Muvaffaqiyatsiz yoki fayl kichik")
                    cookie_rotation_manager.mark_cookie_blocked(fresh_cookie)
        except Exception as e:
            logging.debug(f"[Audio] Fresh attempt {attempt} xatolik: {str(e)[:60]}")
    
    if fresh_ok == 0:
        logging.warning("[Audio] Fresh cookies: Browserdan yaratilmadi (container muhiti)")

    # ========== 5. SAVED COOKIES (LAST RESORT + PROXY) ==========
    logging.warning(f"[Audio] Stage 5: Saqlangan cookies (last resort, proxy: {YOUTUBE_PROXY[:30] if YOUTUBE_PROXY else 'NONE'}...)...")
    cookie_file = get_cookiefile()
    if cookie_file and os.path.exists(cookie_file) and os.path.getsize(cookie_file) > 100:
        try:
            logging.info(f"[Audio] Saqlangan cookie faylidan foydalanish: {os.path.basename(cookie_file)}")
            cookie_path = await asyncio.to_thread(
                _download_youtube_audio_with_cookies,
                url,
                filename,
                cookie_file,
                retry=False
            )
            if cookie_path and os.path.exists(cookie_path) and os.path.getsize(cookie_path) > 2048:
                logging.info("[Audio] ✓ MUVAFFAQIYATLI: Saved cookies")
                return cookie_path
        except Exception as e:
            logging.debug(f"[Audio] Saved cookies error: {str(e)[:60]}")

    # ===== ALL FAILED =====
    logging.error(f"[Audio] ❌ BARCHA 5 STAGE ISHLAMADI: {url}")
    logging.error("[Audio] 📋 PROXY KONFIGURATSIYA:")
    if YOUTUBE_PROXY:
        logging.error("[Audio]    YOUTUBE_PROXY qiymati: %s", YOUTUBE_PROXY)
    else:
        logging.error("[Audio]    YOUTUBE_PROXY qiymati: ❌ BO'SH (ENV ichida O'RNATISH KERAK)")
    logging.error("[Audio]    Format: http://proxy-server:port YOKI socks5://server:port")
    logging.error("[Audio] 💡 .env fayliga qo'shish:")
    logging.error("[Audio]    YOUTUBE_PROXY=http://your-proxy-ip:port")
    logging.error("[Audio] 📝 COOKIES BILAN HAM URINING:")
    logging.error("[Audio]    YouTube cookies'ni chrome ichidan export qiling")
    logging.error("[Audio]    Keyin: python manage_cookies.py convert cookies.json")
    logging.error(f"[Audio]    Keyin: python manage_cookies.py convert cookies.json")
    return None


# ========================== DOWNLOAD BY QUERY ==========================
async def download_audio_by_query(query: str) -> str | None:
    """
    YouTube'dan audio qidirish va yuklash (Piped API asosiy).
    query: "artist - title" formatida

    Ishlash tartibi:
    1. YouTube API v3 orqali qidirish (bepul, cookie kerak emas)
    2. Birinchi natija URL'sini olish
    3. Piped/yt-dlp pipeline orqali yuklash (Cobalt o'chirildi)
    """
    # 1. YouTube'da qidirish (API yoki yt-dlp search)
    results = await search_youtube_tracks(query, max_results=1)
    if not results:
        logging.error(f"[AudioDownload] Qidiruv natija yo'q: {query}")
        return None

    item = results[0]
    url = item.get("url") or f"https://www.youtube.com/watch?v={item.get('id', '')}"
    title = item.get("title", "audio")
    uploader = item.get("uploader", "unknown")

    logging.info(f"[AudioDownload] Qidiruv natija: {title} - {uploader}")
    logging.info(f"[AudioDownload] URL: {url}")

    # 2. Piped/yt-dlp pipeline orqali yuklash (Cobalt o'chirildi)
    return await download_youtube_audio(url, f"{uploader} - {title}")


async def search_piped(query: str, max_results: int = 15) -> list:
    """
    Piped API orqali YouTube'dan qidirish - cookies kerak emas va PROXY BILAN ishlaydi.
    
    Proxy: dynamic proxy pool / YOUTUBE_PROXY dan foydalanadi
    Timeouts: 15 sekund API request uchun
    Instances: Multiple Piped instances bilan fallback
    """
    encoded_query = aiohttp.helpers.quote(query)
    instances = WORKING_PIPED_INSTANCES or PIPED_API_INSTANCES
    connector = aiohttp.TCPConnector(ssl=False)
    
    proxy = get_current_proxy()
    async with aiohttp.ClientSession(connector=connector) as session:
        for instance in instances:
            try:
                async with session.get(
                    f"{instance}/search",
                    params={"q": query},
                    timeout=aiohttp.ClientTimeout(total=15),
                    headers={
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                        "Accept": "application/json",
                    },
                    proxy=proxy,
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












def _is_youtube_blocking_response(file_path: str) -> bool:
    """
    YouTube bot-block yoki login sahifasini aniqlash (IMPROVED).
    Qaytaradi: True agar YouTube blocking/login talab qilsa, False agar fayl to'g'ri audio bo'lsa.
    """
    if not os.path.exists(file_path):
        return False
    
    try:
        file_size = os.path.getsize(file_path)
        
        # Audio fayl hajmi 100KB dan kam bo'lsa, ehtimol HTML page
        if file_size < 100 * 1024:
            with open(file_path, 'rb') as f:
                header = f.read(min(4096, file_size))
            
            # HTML/XML sahifasi (bot-block yoki login page)
            blocking_patterns = [
                b'<!DOCTYPE', b'<html', b'<HTML', b'<?xml',
                b'recaptcha', b'challenge', b'Sign in', b'sign in to confirm',
                b'login_required', b'LOGIN_REQUIRED', b'bot_check',
                b'Try again later', b'Sorry', b'blocked'
            ]
            
            for pattern in blocking_patterns:
                if pattern in header:
                    logging.warning(f"[Blocking] Aniqland: {pattern.decode('utf-8', errors='ignore')}")
                    return True
            
            # HTTP error codes in content
            if b'403' in header or b'429' in header or b'401' in header:
                logging.warning("[Blocking] HTTP xato kodi topildi")
                return True
        
        return False
    except Exception as e:
        logging.debug(f"[Blocking] Check xatosi: {e}")
        return False


def _detect_youtube_error(error_str: str) -> str:
    """
    YouTube xatosini turini aniqlash va qayta yangilash kerakligini bilish (IMPROVED).
    """
    error_lower = error_str.lower()
    
    # Blocking-ga olib keladigan xatolar
    blocking_keywords = [
        'sign in', 'login', 'login_required', 'bot', 'captcha',
        'challenge', 'restricted', 'forbidden', 'not available',
        'unavailable', 'blocked', 'please try again'
    ]
    
    for keyword in blocking_keywords:
        if keyword in error_lower:
            if 'sign in' in error_lower or 'login' in error_lower:
                return "LOGIN_REQUIRED"
            elif 'bot' in error_lower or 'captcha' in error_lower:
                return "BOT_BLOCKED"
            elif 'forbidden' in error_lower or '403' in error_str:
                return "FORBIDDEN"
            else:
                return "BLOCKED"
    
    # Rate limiting
    if 'rate' in error_lower or '429' in error_str or 'too many' in error_lower:
        return "RATE_LIMIT"
    
    # Not available
    if 'not available' in error_lower or 'unavailable' in error_lower:
        return "UNAVAILABLE"
    
    # HTTP codes
    if '403' in error_str:
        return "FORBIDDEN"
    if '401' in error_str:
        return "UNAUTHORIZED"
    if '429' in error_str:
        return "RATE_LIMIT"
    
    return "UNKNOWN"


def _should_refresh_cookies_on_error(error_type: str) -> bool:
    """
    Xatosiga qarab cookies yangilash kerakligini aniqlash (IMPROVED).
    """
    refresh_on = {'LOGIN_REQUIRED', 'FORBIDDEN', 'BOT_BLOCKED', 'RATE_LIMIT', 'UNAUTHORIZED', 'BLOCKED'}
    return error_type in refresh_on





def _download_youtube_audio_with_cookies(url: str, filename: str, cookiefile: str, retry: bool = True) -> str | None:
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
    proxy = get_current_proxy()
    if proxy:
        opts["proxy"] = proxy

    try:
        # Diagnostic: log cookie file usage
        try:
            if cookiefile and os.path.exists(cookiefile):
                age = int(time.time() - os.path.getmtime(cookiefile))
                with open(cookiefile, 'r', encoding='utf-8') as cf:
                    content = cf.read()
                has_login = any(tok in content for tok in ("LOGIN_INFO", "SID", "SAPISID", "APISID"))
                logging.info(f"[Audio] Cookie fallback using: {os.path.basename(cookiefile)} (age={age}s, has_login={has_login})")
        except Exception:
            pass

        # Per-cookie proxy support
        cookie_meta = cookie_rotation_manager.active_cookies.get(cookiefile, {}) if cookiefile else {}
        cookie_proxy = cookie_meta.get('proxy')
        if cookie_proxy:
            opts['proxy'] = cookie_proxy
            logging.info(f"[Audio] Using proxy for cookie {os.path.basename(cookiefile)} -> {cookie_proxy}")

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
                    os.remove(check_path)
                    # Mark this cookie as blocked and try refreshing
                    mark_operation_blocked(cookiefile)
                    if AUTO_REFRESH_COOKIES and retry and refresh_youtube_cookiefile():
                        logging.info("[Audio] Cookie fayl yangilandi, qayta urinish...")
                        return _download_youtube_audio_with_cookies(url, filename, cookiefile, retry=False)
                    return None

                if os.path.getsize(check_path) > 1024:
                    if os.path.exists(mp3_path):
                        logging.info(f"[Audio] Cookie fallback: MUVAFFAQIYATLI → {mp3_path}")
                        mark_operation_success(cookiefile)
                        return mp3_path
                    if os.path.exists(downloaded):
                        logging.info(f"[Audio] Cookie fallback: MUVAFFAQIYATLI → {downloaded}")
                        mark_operation_success(cookiefile)
                        return downloaded
                os.remove(check_path) if os.path.exists(check_path) else None

            return None
    except Exception as e:
        err = str(e)
        error_type = _detect_youtube_error(err)
        
        if error_type in ("LOGIN_REQUIRED", "BOT_BLOCKED", "FORBIDDEN", "RATE_LIMIT"):
            logging.warning(f"[Audio] Cookie fallback: {error_type} — cookies yaroqsiz yoki eskirgan")
            # Mark this cookie as blocked so it won't be reused
            try:
                mark_operation_blocked(cookiefile)
            except Exception:
                pass
            if AUTO_REFRESH_COOKIES and retry:
                logging.info("[Audio] Cookies qayta yangilash urinish...")
                if refresh_youtube_cookiefile():
                    logging.info("[Audio] Cookie fayl yangilandi, qayta urinish...")
                    return _download_youtube_audio_with_cookies(url, filename, cookiefile, retry=False)
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
            "✨ @videoSkachatbbot ga xush kelibsiz!\n\n"
            "📎 Instagram, YouTube, TikTok, Pinterest yoki Snapchat linkini yuboring.\n"
            "🎬 Video yoki rasmni to'g'ridan-to'g'ri yuklab olaman.\n\n"
            "🔗 Faol ijtimoiy tarmoq linkini yuboring!"
        ),
        "force_sub": "❌ <b>Botdan foydalanish uchun quyidagi kanallarga a'zo bo'ling:</b>",
        "check_sub": "✅ A'zolikni tekshirish",
        "not_subscribed": "❌ Siz hali barcha kanallarga a'zo bo'lmagansiz!",
        "sub_ok_group": "✅ A'zolik tasdiqlandi! Endi musiqa qidirishingiz mumkin.",
        "searching": "⌛",
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
            "❤️ @videoSkachatbbot"
        ),
        "video_downloaded": (
            "🎬 <b>Video qabul qilindi!</b>\n\n"
            "🔎 Ijtimoiy tarmoq linki ishlanmoqda..."
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
        "broadcast_ask_photo": "🖼 Broadcast uchun rasm yuboring:",
        "broadcast_ask_video": "🎬 Broadcast uchun video yuboring:",
        "broadcast_ask_caption": (
            "📎 Media qabul qilindi.\n\n"
            "📝 Yozuv (caption) qo'shish uchun matn yuboring:\n"
            "⏭️ Matnsiz yuborish uchun tugmani bosing:"
        ),
        "broadcast_done": "✅ Reklama <b>{count}</b> ta foydalanuvchi/guruhga yuborildi.",
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
        "back": "🔙 Orqaga",
        "save_media_success": "✅ Media profilga saqlandi.",
        "save_media_no_media": "❌ Saqlash uchun media topilmadi.",
        "round_media_only_video": "❌ Faqat video uchun dumaloq video yaratish mumkin.",
        "round_media_preparing": "🔄 Dumaloq videoni tayyorlash...",
        "round_media_ready": "✅ Dumaloq video tayyor!",
        "round_media_failed": "❌ Dumaloq video yaratib bo'lmadi.",
        "saved_media_none": "📁 Saqlangan media topilmadi.",
        "saved_media_title": "📁 <b>Saqlangan media</b>",
        "saved_media_shown": "✅ Saqlangan media ro'yxati yangilandi.",
        "saved_media_preview_photo": "📁 Saqlangan rasm",
        "saved_media_preview_video": "📁 Saqlangan video",
        "help_text": (
            "🎵 @videoSkachatbbot yordam\n\n"
            "🔹 <b>Link</b> — Instagram, YouTube, TikTok, Pinterest yoki Snapchat dan video/rasm yuklash\n"
            "🔹 <b>Faqat ijtimoiy tarmoq linklari</b> qo'llab-quvvatlanadi\n\n"
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
            "📎 Ijtimoiy tarmoq linki (Instagram, YouTube, TikTok, Pinterest, Snapchat)\n"
            "🎬 Video yoki rasm linkini yuboring"
        ),
        "document_received": (
            "📄 <b>Hujjat qabul qilindi</b>\n\n"
            "Fayl: <code>{filename}</code>\n"
            "Hajmi: {size_mb} MB\n\n"
            "⚠️ Men faqat musiqa, video va linklarni qayta ishlayman. "
            "Hujjatlar bilan ishlash imkoniyati mavjud emas."
        ),
        "quick_search_hint": (
            "💡 <b>Tez maslahat:</b>\n"
            "Video yoki rasm olish uchun ijtimoiy tarmoq linkini yuboring."
        ),
        "link_downloaded": (
            "📎 <b>Link qabul qilindi!</b>\n\n"
            "🔎 Yuklanmoqda..."
        ),
        "video_from_link": (
            "🎬 <b>Video yuklandi!</b>\n\n"
            "🔎 Ijtimoiy tarmoq linki ishlanmoqda..."
        ),
        "photo_from_link": (
            "📸 <b>Rasm yuklandi!</b>\n\n"
            "❤️ @videoSkachatbbot"
        ),
        # Admin panel keys
        "admin_channels": "📢 Majburiy obuna",
        "admin_langs": "🌐 Tillar",
        "admin_stats": "📊 Analitika",
        "admin_groups": "👥 Guruhlar",
        "admin_broadcast": "📨 Reklama",
        "admin_blacklist": "🚫 Blacklist",
        "admin_close": "❌ Panelni yopish",
        "admin_add_channel": "➕ Kanal qo'shish",
        "admin_block_add": "➕ Block qilish",
        "admin_broadcast_text": "📝 Matn",
        "admin_broadcast_photo": "🖼 Rasm",
        "admin_broadcast_video": "🎬 Video",
        "admin_broadcast_skip": "⏭️ Matnsiz yuborish",
        "admin_broadcast_cancel": "🔙 Bekor qilish",
        "admin_channels_title": "📢 <b>Majburiy obuna kanallari:</b>\n\nBirini o'chirish uchun bosing:",
        "admin_no_channels": "📢 <b>Majburiy obuna kanallari:</b>\n\nKanallar yo'q.",
        "admin_blacklist_title": "🚫 <b>Bloklangan foydalanuvchilar:</b>\n\nBlokdan ochish uchun bosing:",
        "admin_broadcast_title": "📨 <b>Reklama (Broadcast)</b>\n\nQanday xabar yuborishni tanlang:",
        "saved_media_btn": "📁 Saqlangan media",
        "callback_not_for_you": "❌ Bu sizning videongiz emas!",
    },
    "uz_kr": {
        "choose_lang": "🌐 Тилни танланг:",
        "welcome": (
            "✨ @videoSkachatbbot га хуш келибсиз!\n\n"
            "📎 Instagram, YouTube, TikTok, Pinterest ёки Snapchat ҳаволасини юборинг.\n"
            "🎬 Видео ёки расмни тўғридан-тўғри юклаб оламан.\n\n"
            "🔗 Фаол ижтимоий тармоқ ҳаволасини юборинг!"
        ),
        "force_sub": "❌ <b>Ботдан фойдаланиш учун қуйидаги каналларга аъзо бўлинг:</b>",
        "check_sub": "✅ Аъзоликни текшириш",
        "not_subscribed": "❌ Сиз ҳали барча каналларга аъзо бўлмагансиз!",
        "sub_ok_group": "✅ Аъзолик тасдиқланди! Энди мусиқа қидиришингиз мумкин.",
        "searching": "⌛",
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
            "❤️ @videoSkachatbbot"
        ),
        "video_downloaded": (
            "🎬 <b>Видео қабул қилинди!</b>\n\n"
            "🔎 Ссылка ишланмоқда..."
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
        "broadcast_ask_photo": "🖼 Broadcast учун расм юборинг:",
        "broadcast_ask_video": "🎬 Broadcast учун видео юборинг:",
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
        "back": "🔙 Орқага",
        "save_media_success": "✅ Media профильга сақланди.",
        "save_media_no_media": "❌ Сақлаш учун медиа топилмади.",
        "round_media_only_video": "❌ Фақат видео учун доималоқ видео яратиш мумкин.",
        "round_media_preparing": "🔄 Доималоқ видеони тайёрлаш...",
        "round_media_ready": "✅ Доималоқ видео тайёр!",
        "round_media_failed": "❌ Доималоқ видео яратиб бўлмади.",
        "saved_media_none": "📁 Сақланган медиа топилмади.",
        "saved_media_title": "📁 <b>Сақланган медиа</b>",
        "saved_media_shown": "✅ Сақланган медиа рўйхати янгиланди.",
        "saved_media_preview_photo": "📁 Сақланган расм",
        "saved_media_preview_video": "📁 Сақланган видео",
        "help_text": (
            "🎵 @videoSkachatbbot ёрдам\n\n"
            "🔹 <b>Link</b> — Instagram, YouTube, TikTok, Pinterest ёки Snapchat дан видео/расм юклаш\n"
            "🔹 <b>Фақат ижтимоий тармоқ ҳаволалари</b> қўллаб-қувватланади\n\n"
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
            "📎 Ижтимоий тармоқ линки (Instagram, YouTube, TikTok, Pinterest, Snapchat)\n"
            "🎬 Видео ёки расм линк юборинг"
        ),
        "document_received": (
            "📄 <b>Ҳужжат қабул қилинди</b>\n\n"
            "Файл: <code>{filename}</code>\n"
            "Ҳажми: {size_mb} MB\n\n"
            "⚠️ Мен фақат мусиқа, видео ва линкларни қайта ишлайман. "
            "Ҳужжатлар билан ишлаш имконияти мавжуд эмас."
        ),
        "quick_search_hint": (
            "💡 <b>Тез маслаҳат:</b>\n"
            "Видео ёки расмни юклаб олиш учун ижтимоий тармоқ ҳаволасини юборинг."
        ),
        "link_downloaded": (
            "📎 <b>Link қабул қилинди!</b>\n\n"
            "🔎 Юкланмоқда..."
        ),
        "video_from_link": (
            "🎬 <b>Видео юкланди!</b>\n\n"
            "🔎 Ссылка ишланмоқда..."
        ),
        "photo_from_link": (
            "📸 <b>Расм юкланди!</b>\n\n"
            "❤️ @videoSkachatbbot"
        ),
        # Admin panel keys
        "admin_channels": "📢 Мажбурий обуна",
        "admin_langs": "🌐 Тиллар",
        "admin_stats": "📊 Статистика",
        "admin_groups": "👥 Гуруҳлар",
        "admin_broadcast": "📨 Реклама",
        "admin_blacklist": "🚫 Blacklist",
        "admin_close": "❌ Панелни ёпиш",
        "admin_add_channel": "➕ Канал қўшиш",
        "admin_block_add": "➕ Блок қилиш",
        "admin_broadcast_text": "📝 Матн",
        "admin_broadcast_photo": "🖼 Расм",
        "admin_broadcast_video": "🎬 Видео",
        "admin_broadcast_skip": "⏭️ Матнсиз юбориш",
        "admin_broadcast_cancel": "🔙 Бекор қилиш",
        "admin_channels_title": "📢 <b>Мажбурий обуна каналлари:</b>\n\nБирини ўчириш учун босинг:",
        "admin_no_channels": "📢 <b>Мажбурий обуна каналлари:</b>\n\nКаналлар йўқ.",
        "admin_blacklist_title": "🚫 <b>Блокланган фойдаланувчилар:</b>\n\nБлокдан очиш учун босинг:",
        "admin_broadcast_title": "📨 <b>Реклама (Broadcast)</b>\n\nҚандай хабар юборишни танланг:",
        "saved_media_btn": "📁 Сақланган медиа",
        "callback_not_for_you": "❌ Бу сизнинг видеонгиз эмас!",
    },
    "ru": {
        "choose_lang": "🌐 Выберите язык:",
        "welcome": (
            "✨ Добро пожаловать в @videoSkachatbbot!\n\n"
            "📎 Отправьте ссылку из Instagram, YouTube, TikTok, Pinterest или Snapchat.\n"
            "🎬 Я загружу видео или фото из ссылки.\n\n"
            "🔗 Отправьте ссылку на социальную сеть!"
        ),
        "force_sub": "❌ <b>Для использования бота подпишитесь на каналы:</b>",
        "check_sub": "✅ Проверить подписку",
        "not_subscribed": "❌ Вы еще не подписаны на все каналы!",
        "sub_ok_group": "✅ Подписка подтверждена! Теперь можно искать музыку.",
        "searching": "⌛",
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
            "❤️ @videoSkachatbbot"
        ),
        "video_downloaded": (
            "🎬 <b>Видео получено!</b>\n\n"
            "🔎 Ссылка обрабатывается..."
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
        "broadcast_ask_photo": "🖼 Отправьте фото для рассылки:",
        "broadcast_ask_video": "🎬 Отправьте видео для рассылки:",
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
        "back": "🔙 Назад",
        "save_media_success": "✅ Медиа сохранено в профиле.",
        "save_media_no_media": "❌ Медиа для сохранения не найдено.",
        "round_media_only_video": "❌ Круглое видео можно создать только из видео.",
        "round_media_preparing": "🔄 Подготавливаю круглое видео...",
        "round_media_ready": "✅ Круглое видео готово!",
        "round_media_failed": "❌ Не удалось создать круглое видео.",
        "saved_media_none": "📁 Сохраненное медиа не найдено.",
        "saved_media_title": "📁 <b>Сохраненное медиа</b>",
        "saved_media_shown": "✅ Список сохраненного медиа обновлен.",
        "saved_media_preview_photo": "📁 Сохраненное фото",
        "saved_media_preview_video": "📁 Сохраненное видео",
        "help_text": (
            "🎵 @videoSkachatbbot — помощь\n\n"
            "🔹 <b>Link</b> — загрузка видео/фото из Instagram, YouTube, TikTok, Pinterest, Snapchat\n"
            "🔹 <b>Поддерживаются только ссылки из соцсетей</b>\n\n"
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
            "📎 Ссылку на соцсеть (Instagram, YouTube, TikTok, Pinterest, Snapchat)\n"
            "🎬 Видео или фото по ссылке"
        ),
        "document_received": (
            "📄 <b>Документ получен</b>\n\n"
            "Файл: <code>{filename}</code>\n"
            "Размер: {size_mb} МБ\n\n"
            "⚠️ Я работаю только с музыкой, видео и ссылками. "
            "Работа с документами недоступна."
        ),
        "quick_search_hint": (
            "💡 <b>Быстрый совет:</b>\n"
            "Отправьте ссылку на соцсеть, чтобы скачать видео или фото."
        ),
        "link_downloaded": (
            "📎 <b>Ссылка получена!</b>\n\n"
            "🔎 Загрузка..."
        ),
        "video_from_link": (
            "🎬 <b>Видео загружено!</b>\n\n"
            "🔎 Ссылка обрабатывается..."
        ),
        "photo_from_link": (
            "📸 <b>Фото загружено!</b>\n\n"
            "❤️ @videoSkachatbbot"
        ),
        # Admin panel keys
        "admin_channels": "📢 Обязательная подписка",
        "admin_langs": "🌐 Языки",
        "admin_stats": "📊 Аналитика",
        "admin_groups": "👥 Группы",
        "admin_broadcast": "📨 Рассылка",
        "admin_blacklist": "🚫 Чёрный список",
        "admin_close": "❌ Закрыть панель",
        "admin_add_channel": "➕ Добавить канал",
        "admin_block_add": "➕ Заблокировать",
        "admin_broadcast_text": "📝 Текст",
        "admin_broadcast_photo": "🖼 Фото",
        "admin_broadcast_video": "🎬 Видео",
        "admin_broadcast_skip": "⏭️ Отправить без текста",
        "admin_broadcast_cancel": "🔙 Отмена",
        "admin_channels_title": "📢 <b>Каналы обязательной подписки:</b>\n\nНажмите для удаления:",
        "admin_no_channels": "📢 <b>Каналы обязательной подписки:</b>\n\nКаналов нет.",
        "admin_blacklist_title": "🚫 <b>Заблокированные пользователи:</b>\n\nНажмите для разблокировки:",
        "admin_broadcast_title": "📨 <b>Рассылка (Broadcast)</b>\n\nВыберите тип сообщения:",
        "saved_media_btn": "📁 Сохранённые медиа",
        "callback_not_for_you": "❌ Это не ваше видео!",
    },
    "en": {
        "choose_lang": "🌐 Choose language:",
        "welcome": (
            "✨ Welcome to @videoSkachatbbot!\n\n"
            "📎 Send a link from Instagram, YouTube, TikTok, Pinterest, or Snapchat.\n"
            "🎬 I'll download the video or photo from the link.\n\n"
            "🔗 Send a social media link!"
        ),
        "force_sub": "❌ <b>Please subscribe to these channels to use the bot:</b>",
        "check_sub": "✅ Check subscription",
        "not_subscribed": "❌ You haven't subscribed to all channels yet!",
        "sub_ok_group": "✅ Subscription confirmed! You can now search for music.",
        "searching": "⌛",
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
            "❤️ @videoSkachatbbot"
        ),
        "video_downloaded": (
            "🎬 <b>Video received!</b>\n\n"
            "🔎 Processing your social media link..."
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
        "broadcast_ask_photo": "🖼 Send broadcast photo:",
        "broadcast_ask_video": "🎬 Send broadcast video:",
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
        "back": "🔙 Back",
        "save_media_success": "✅ Media saved to profile.",
        "save_media_no_media": "❌ No media found to save.",
        "round_media_only_video": "❌ Round video can only be created from a video.",
        "round_media_preparing": "🔄 Preparing round video...",
        "round_media_ready": "✅ Round video is ready!",
        "round_media_failed": "❌ Could not create round video.",
        "saved_media_none": "📁 No saved media found.",
        "saved_media_title": "📁 <b>Saved media</b>",
        "saved_media_shown": "✅ Saved media list updated.",
        "saved_media_preview_photo": "📁 Saved photo",
        "saved_media_preview_video": "📁 Saved video",
        "help_text": (
            "🎵 @videoSkachatbbot help\n\n"
            "🔹 <b>Link</b> — download video/photo from Instagram, YouTube, TikTok, Pinterest, Snapchat\n"
            "🔹 <b>Only social media links</b> are supported\n\n"
            "🎵 Results are downloaded directly from the link!\n"
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
            "📎 A social media link (Instagram, YouTube, TikTok, Pinterest, Snapchat)\n"
            "🎬 A link to video or photo"
        ),
        "document_received": (
            "📄 <b>Document received</b>\n\n"
            "File: <code>{filename}</code>\n"
            "Size: {size_mb} MB\n\n"
            "⚠️ I only process music, video, and links. "
            "Document handling is not available."
        ),
        "quick_search_hint": (
            "💡 <b>Quick tip:</b>\n"
            "Send a social media link to download video or photo."
        ),
        "link_downloaded": (
            "📎 <b>Link received!</b>\n\n"
            "🔎 Downloading..."
        ),
        "video_from_link": (
            "🎬 <b>Video downloaded!</b>\n\n"
            "🔎 The link was processed successfully."
        ),
        "photo_from_link": (
            "📸 <b>Photo downloaded!</b>\n\n"
            "❤️ @videoSkachatbbot"
        ),
        # Admin panel keys
        "admin_channels": "📢 Required channels",
        "admin_langs": "🌐 Languages",
        "admin_stats": "📊 Analytics",
        "admin_groups": "👥 Groups",
        "admin_broadcast": "📨 Broadcast",
        "admin_blacklist": "🚫 Blacklist",
        "admin_close": "❌ Close panel",
        "admin_add_channel": "➕ Add channel",
        "admin_block_add": "➕ Block user",
        "admin_broadcast_text": "📝 Text",
        "admin_broadcast_photo": "🖼 Photo",
        "admin_broadcast_video": "🎬 Video",
        "admin_broadcast_skip": "⏭️ Send without caption",
        "admin_broadcast_cancel": "🔙 Cancel",
        "admin_channels_title": "📢 <b>Required channels:</b>\n\nClick to remove:",
        "admin_no_channels": "📢 <b>Required channels:</b>\n\nNo channels.",
        "admin_blacklist_title": "🚫 <b>Blocked users:</b>\n\nClick to unblock:",
        "admin_broadcast_title": "📨 <b>Broadcast</b>\n\nChoose message type:",
        "saved_media_btn": "📁 Saved media",
        "callback_not_for_you": "❌ This is not your video!",
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
                CREATE TABLE IF NOT EXISTS saved_media (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(telegram_id) ON DELETE CASCADE,
                    file_id VARCHAR(500) NOT NULL,
                    media_type VARCHAR(50) NOT NULL,
                    caption TEXT,
                    created_at TIMESTAMP DEFAULT NOW()
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

    async def save_user_media(self, user_id: int, file_id: str, media_type: str, caption: str | None = None):
        async with self.pool.acquire() as conn:
            await conn.execute(
                r"""
                INSERT INTO saved_media (user_id, file_id, media_type, caption)
                VALUES ($1, $2, $3, $4)
                """,
                user_id, file_id, media_type, caption,
            )

    async def get_saved_media(self, user_id: int):
        async with self.pool.acquire() as conn:
            return await conn.fetch(
                "SELECT * FROM saved_media WHERE user_id = $1 ORDER BY created_at DESC",
                user_id,
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
                InlineKeyboardButton(text="📝 Matn", callback_data="admin:bc_text"),
            ],
            [
                InlineKeyboardButton(text="🖼 Rasm", callback_data="admin:bc_photo"),
                InlineKeyboardButton(text="🎬 Video", callback_data="admin:bc_video"),
            ],
            [InlineKeyboardButton(text="🔙 Orqaga", callback_data="admin:back")],
        ]
    )


def broadcast_skip_caption_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⏭️ Matnsiz yuborish", callback_data="admin:bc_skip")],
            [InlineKeyboardButton(text="🔙 Bekor qilish", callback_data="admin:broadcast")],
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
                cookie_text = 'ha' if use_cookies else "yo'q"
                logging.warning(f"Video yuklashda xatolik ({prefix}, cookies={cookie_text}): {e}")
            return None

        # Har video uchun 8-10 xil proxy bilan urinish
        max_proxy_attempts = 8
        tried_proxies: set[str | None] = set()
        
        for proxy_attempt_num in range(max_proxy_attempts):
            proxy = get_current_proxy()
            if proxy in tried_proxies:
                logging.debug(f"[Video] Proxy takrorlanayapti, tugating: {proxy[:40] if proxy else 'NONE'}")
                break
            tried_proxies.add(proxy)

            proxy_str = proxy[:30] + "..." if proxy and len(proxy) > 30 else (proxy or "Yo'q")
            logging.info(f"[Video] Proxy attempt {proxy_attempt_num + 1}/{max_proxy_attempts}: {proxy_str}")

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

            if proxy:
                mark_proxy_blocked(proxy)
        
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
    try:
        await message.reply(TEXTS[lang]["force_sub"], reply_markup=kb)
    except Exception as e:
        # Agar bot guruhdan chiqarilgan bo'lsa yoki xato bo'lsa
        logging.warning(f"[ForceSubGroup] Bot javob bera olmadi (bot chiqarilgan?): {e}")
    return False


# ========================== BROADCAST ==========================
async def broadcast_to_all(bot: Bot, msg: Message, caption: str | None = None):
    users = await db.get_all_users()
    groups = await db.get_groups()
    mandatory_channels = await db.get_channels()
    
    # Majburiy kanallardagi username'larni set sifatida saqlash
    mandatory_usernames = set()
    for ch in mandatory_channels:
        uname = ch["username"].lstrip("@").lower()
        mandatory_usernames.add(uname)
    
    # Foydalanuvchilar (majburiy kanallar emas)
    user_targets = [u["telegram_id"] for u in users]
    
    # Guruhlar va kanallar — majburiy kanallarni o'tkazib yuborish
    group_targets = []
    for g in groups:
        gid = g["group_id"]
        gtitle = (g.get("title") or "").lstrip("@").lower()
        # Majburiy kanallar ro'yxatida bormi? (title yoki ID bo'yicha tekshiramiz)
        # Kanal ID'si manfiy bo'ladi, guruh ham manfiy; lekin majburiy kanallar username bilan saqlanadi
        # Shuning uchun title bo'yicha tekshiramiz
        if gtitle in mandatory_usernames:
            continue
        group_targets.append(gid)
    
    count = 0
    user_count = 0
    group_count = 0
    
    all_targets = user_targets + group_targets
    for target_id in all_targets:
        try:
            await msg.copy_to(target_id, caption=caption)
            count += 1
            if target_id in user_targets:
                user_count += 1
            else:
                group_count += 1
            await asyncio.sleep(0.05)
        except Exception:
            pass
    
    # Adminga xabar yuborish
    try:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        admin_notify = (
            f"✅ <b>Reklama yuborildi!</b>\n\n"
            f"📊 Jami: <b>{count}</b> ta\n"
            f"👤 Foydalanuvchilar: <b>{user_count}</b> ta\n"
            f"👥 Guruh/Kanallar: <b>{group_count}</b> ta\n"
            f"🕐 Vaqt: {now}"
        )
        await bot.send_message(ADMIN_ID, admin_notify)
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
    loading_msg = await call.message.answer(TEXTS[lang]["searching"])
    try:
        # Audio yuklash — maksimal 3 marta retry (cookies auto-refresh bilan)
        max_retries = 3
        audio_path = None
    
        for attempt in range(1, max_retries + 1):
            audio_path = await download_youtube_audio(url, f"{uploader} - {title}")
            
            if audio_path and os.path.exists(audio_path):
                logging.info(f"[Select] Audio muvaffaqiyatli yuklandi (attempt {attempt}/{max_retries})")
                break
            
            if attempt < max_retries:
                # Cookies avtomatik yangilash - eskirgan bo'lsa
                if AUTO_REFRESH_COOKIES:
                    logging.info(f"[Select] Cookies yangilash urinish {attempt}/{max_retries}...")
                    refresh_youtube_cookiefile(force=True)
                    await asyncio.sleep(2)  # 2 soniya kutish
            else:
                logging.warning(f"[Select] {attempt} marta urinish keyin ham audio yuklanmadi: {url}")

        if not audio_path or not os.path.exists(audio_path):
            # Silent error - foydalanuvchiga minimal xatolik
            logging.warning(f"[Select] Audio yuklash final xatosi: {uploader} - {title}")
            await call.message.answer("⚠️ Musiqa hozircha mavjud emas. Boshqa qo'shiq tanlang.", reply_markup=build_share_kb())
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
    finally:
        try:
            await loading_msg.delete()
        except Exception:
            pass
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

        # YouTube qidirish - vaqtinchalik loading xabari yuborish
        loading_msg = await call.message.answer(TEXTS[lang]["searching"])
        try:
            results = await search_youtube_tracks(query, max_results=15)
        finally:
            try:
                await loading_msg.delete()
            except Exception:
                pass

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

    # Vaqtinchalik loading xabari — foydalanuvchiga qidiruv ishlayotganini ko'rsatadi
    loading_msg = await call.message.answer(TEXTS[lang]["searching"])
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
            try:
                await loading_msg.delete()
            except Exception:
                pass
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
        try:
            await loading_msg.delete()
        except Exception:
            pass

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
        elif state == "waiting_broadcast_photo":
            if not message.photo:
                await message.answer("❌ Iltimos, rasm yuboring!")
                return
            # Rasmni saqlab, caption so'rash
            admin_state[message.from_user.id] = {"state": "waiting_broadcast_caption_photo", "media_msg": message}
            await message.answer(TEXTS["uz"]["broadcast_ask_caption"], reply_markup=broadcast_skip_caption_kb())
            return
        elif state == "waiting_broadcast_video":
            if not message.video:
                await message.answer("❌ Iltimos, video yuboring!")
                return
            # Videoni saqlab, caption so'rash
            admin_state[message.from_user.id] = {"state": "waiting_broadcast_caption_video", "media_msg": message}
            await message.answer(TEXTS["uz"]["broadcast_ask_caption"], reply_markup=broadcast_skip_caption_kb())
            return
        elif state == "waiting_broadcast_media":
            admin_state[message.from_user.id] = {"state": "waiting_broadcast_caption", "media_msg": message}
            await message.answer(TEXTS["uz"]["broadcast_ask_caption"], reply_markup=broadcast_skip_caption_kb())
            return
        elif state in ("waiting_broadcast_caption", "waiting_broadcast_caption_photo", "waiting_broadcast_caption_video"):
            media_msg = admin_state[message.from_user.id].get("media_msg")
            caption_text = message.text.strip() if message.text else None
            admin_state.pop(message.from_user.id, None)
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
        status_msg = await message.answer(TEXTS[lang]["searching"])
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
                                    reply_markup=build_media_kb(message.from_user.id, lang),
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
                                reply_markup=build_media_kb(message.from_user.id, lang),
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
                            reply_markup=build_media_kb(message.from_user.id, lang),
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
                        reply_markup=build_media_kb(message.from_user.id, lang),
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
        finally:
            try:
                await status_msg.delete()
            except Exception:
                pass

    if lang == "uz":
        text = "⚠️ Iltimos, faqat Instagram, YouTube, TikTok, Pinterest yoki Snapchat linkini yuboring."
    elif lang == "uz_kr":
        text = "⚠️ Илтимос, фақат Instagram, YouTube, TikTok, Pinterest ёки Snapchat ҳаволасини юборинг."
    elif lang == "ru":
        text = "⚠️ Пожалуйста, отправьте только ссылку из Instagram, YouTube, TikTok,"
    else:
        text = "⚠️ Please send only a social media link from Instagram, YouTube, TikTok, Pinterest, or Snapchat."
    await message.answer(text)


# ========================== PHOTO HANDLER ==========================
@router.message(F.photo)
async def photo_handler(message: Message):
    await db.add_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if await db.is_blocked(message.from_user.id):
        return
    
    # Admin broadcast tekshirish — RASM reklama
    if message.from_user.id == ADMIN_ID and message.from_user.id in admin_state:
        state_info = admin_state[message.from_user.id]
        state = state_info.get("state")
        
        # FIX: waiting_broadcast_photo ham tekshiriladi
        if state in ("waiting_broadcast_media", "waiting_broadcast_photo"):
            admin_state[message.from_user.id] = {"state": "waiting_broadcast_caption_photo", "media_msg": message}
            await message.answer(TEXTS["uz"]["broadcast_ask_caption"], reply_markup=broadcast_skip_caption_kb())
            return
    
    await db.update_activity(message.from_user.id)
    lang = await get_lang(message.from_user.id)
    
    if not await check_and_force_sub_group(message, lang):
        return
    photo = message.photo[-1]
    await message.answer_photo(
        photo=photo.file_id,
        caption=SHARE_PROMO_CAPTION,
        reply_markup=build_media_kb(message.from_user.id, lang),
    )


# ========================== DOCUMENT HANDLER ==========================
@router.message(F.document)
async def document_handler(message: Message):
    await db.add_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if await db.is_blocked(message.from_user.id):
        return
    
    # Admin broadcast tekshirish
    if message.from_user.id == ADMIN_ID and message.from_user.id in admin_state:
        state_info = admin_state[message.from_user.id]
        state = state_info.get("state")
        
        if state == "waiting_broadcast_media":
            admin_state[message.from_user.id] = {"state": "waiting_broadcast_caption", "media_msg": message}
            await message.answer(TEXTS["uz"]["broadcast_ask_caption"], reply_markup=broadcast_skip_caption_kb())
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

        try:
            await loading_msg.delete()
        except Exception:
            pass

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

        # YouTube topilmasa — foydalanuvchiga faqat link yuborishni aytish
        if lang == "uz":
            text = (
                f"🎵 <b>{artist}</b> — <b>{title}</b>\n\n"
                "❌ YouTube'da topilmadi.\n"
                "🔗 Iltimos, faqat ijtimoiy tarmoq linkini yuboring."
            )
        elif lang == "uz_kr":
            text = (
                f"🎵 <b>{artist}</b> — <b>{title}</b>\n\n"
                "❌ YouTube'да топилмади.\n"
                "🔗 Илтимос, фақат ижтимоий тармоқ ҳаволасини юборинг."
            )
        elif lang == "ru":
            text = (
                f"🎵 <b>{artist}</b> — <b>{title}</b>\n\n"
                "❌ Не найдено на YouTube.\n"
                "🔗 Пожалуйста, отправьте только ссылку из соцсети."
            )
        else:
            text = (
                f"🎵 <b>{artist}</b> — <b>{title}</b>\n\n"
                "❌ Not found on YouTube.\n"
                "🔗 Please send only a social media link."
            )

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

    if message.from_user.id == ADMIN_ID and message.from_user.id in admin_state:
        state_info = admin_state[message.from_user.id]
        state = state_info.get("state")
        if state == "waiting_broadcast_media":
            admin_state[message.from_user.id] = {"state": "waiting_broadcast_caption", "media_msg": message}
            await message.answer(TEXTS["uz"]["broadcast_ask_caption"], reply_markup=broadcast_skip_caption_kb())
            return

    await db.update_activity(message.from_user.id)
    lang = await get_lang(message.from_user.id)
    if not await check_and_force_sub_group(message, lang):
        return

    if lang == "uz":
        text = "❌ Voice orqali musiqa aniqlash qo'llab-quvvatlanmaydi. Iltimos, ijtimoiy tarmoq linkini yuboring."
    elif lang == "uz_kr":
        text = "❌ Овозли хабар орқали мусиқа аниқлаш қўллаб-қувватланмайди. Илтимос, ижтимоий тармоқ ҳаволасини юборинг."
    elif lang == "ru":
        text = "❌ Определение музыки через голос не поддерживается. Пожалуйста, отправьте ссылку из социальной сети."
    else:
        text = "❌ Voice-based music recognition is not supported. Please send a social media link."

    await message.answer(text)


# ========================== AUDIO HANDLER ==========================
@router.message(F.audio)
async def audio_handler(message: Message):
    await db.add_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if await db.is_blocked(message.from_user.id):
        return

    if message.from_user.id == ADMIN_ID and message.from_user.id in admin_state:
        state_info = admin_state[message.from_user.id]
        state = state_info.get("state")
        if state == "waiting_broadcast_media":
            admin_state[message.from_user.id] = {"state": "waiting_broadcast_caption", "media_msg": message}
            await message.answer(TEXTS["uz"]["broadcast_ask_caption"], reply_markup=broadcast_skip_caption_kb())
            return

    await db.update_activity(message.from_user.id)
    lang = await get_lang(message.from_user.id)
    if not await check_and_force_sub_group(message, lang):
        return

    if lang == "uz":
        text = "❌ Audio orqali musiqa aniqlash qo'llab-quvvatlanmaydi. Iltimos, ijtimoiy tarmoq linkini yuboring."
    elif lang == "uz_kr":
        text = "❌ Аудио орқали мусиқа аниқлаш қўллаб-қувватланмайди. Илтимос, ижтимоий тармоқ ҳаволасини юборинг."
    elif lang == "ru":
        text = "❌ Определение музыки через аудио не поддерживается. Пожалуйста, отправьте ссылку из социальной сети."
    else:
        text = "❌ Audio-based music recognition is not supported. Please send a social media link."

    await message.answer(text)

# ========================== VIDEO HANDLER ==========================
@router.message(F.video)
async def video_handler(message: Message):
    await db.add_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if await db.is_blocked(message.from_user.id):
        return

    if message.from_user.id == ADMIN_ID and message.from_user.id in admin_state:
        state_info = admin_state[message.from_user.id]
        state = state_info.get("state")
        if state in ("waiting_broadcast_media", "waiting_broadcast_video"):
            admin_state[message.from_user.id] = {"state": "waiting_broadcast_caption_video", "media_msg": message}
            await message.answer(TEXTS["uz"]["broadcast_ask_caption"], reply_markup=broadcast_skip_caption_kb())
            return

    await db.update_activity(message.from_user.id)
    lang = await get_lang(message.from_user.id)
    if not await check_and_force_sub_group(message, lang):
        return
    await message.answer_video(
        video=message.video.file_id,
        caption=SHARE_PROMO_CAPTION,
        reply_markup=build_media_kb(message.from_user.id, lang),
    )


# ========================== VIDEO NOTE HANDLER ==========================
@router.message(F.video_note)
async def video_note_handler(message: Message):
    await db.add_user(message.from_user.id, message.from_user.full_name, message.from_user.username)
    if await db.is_blocked(message.from_user.id):
        return

    if message.from_user.id == ADMIN_ID and message.from_user.id in admin_state:
        state_info = admin_state[message.from_user.id]
        state = state_info.get("state")
        if state == "waiting_broadcast_media":
            admin_state[message.from_user.id] = {"state": "waiting_broadcast_caption", "media_msg": message}
            await message.answer(TEXTS["uz"]["broadcast_ask_caption"], reply_markup=broadcast_skip_caption_kb())
            return

    await db.update_activity(message.from_user.id)
    lang = await get_lang(message.from_user.id)
    if not await check_and_force_sub_group(message, lang):
        return

    await message.answer_video_note(
        video=message.video_note.file_id,
        reply_markup=build_media_kb(message.from_user.id, lang),
    )

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
            inline_keyboard=[
                [InlineKeyboardButton(text=TEXTS[lang]["saved_media_btn"], callback_data="saved_media")],
                [InlineKeyboardButton(text=TEXTS[lang]["back"], callback_data="back_to_main")],
            ]
        ),
    )
    await call.answer()


@router.callback_query(F.data.startswith("save_media:"))
async def save_media_callback(call: CallbackQuery):
    _, user_id_str = call.data.split(":", 1)
    user_id = int(user_id_str)
    lang = await get_lang(call.from_user.id)
    if call.from_user.id != user_id:
        await call.answer(TEXTS[lang]["callback_not_for_you"], show_alert=True)
        return

    media_type = None
    file_id = None
    caption = call.message.caption or ""
    if call.message.video:
        media_type = "video"
        file_id = call.message.video.file_id
    elif call.message.photo:
        media_type = "photo"
        file_id = call.message.photo[-1].file_id
    elif call.message.animation:
        media_type = "animation"
        file_id = call.message.animation.file_id
    elif call.message.document:
        media_type = "document"
        file_id = call.message.document.file_id

    if not file_id:
        await call.answer(TEXTS[lang]["save_media_no_media"], show_alert=True)
        return

    await db.save_user_media(user_id, file_id, media_type, caption)
    await call.answer(TEXTS[lang]["save_media_success"], show_alert=True)


@router.callback_query(F.data.startswith("round_media:"))
async def round_media_callback(call: CallbackQuery):
    _, user_id_str = call.data.split(":", 1)
    user_id = int(user_id_str)
    lang = await get_lang(call.from_user.id)
    if call.from_user.id != user_id:
        await call.answer(TEXTS[lang]["callback_not_for_you"], show_alert=True)
        return

    if not call.message.video:
        await call.answer(TEXTS[lang]["round_media_only_video"], show_alert=True)
        return

    await call.answer(TEXTS[lang]["round_media_preparing"], show_alert=False)

    bot = call.bot

    try:
        file = await bot.get_file(call.message.video.file_id)

        ext = "mp4"
        if call.message.video.file_name:
            _, file_ext = os.path.splitext(call.message.video.file_name)
            if file_ext:
                ext = file_ext.lstrip('.')
        elif file.file_path and '.' in file.file_path:
            _, file_ext = os.path.splitext(file.file_path)
            if file_ext:
                ext = file_ext.lstrip('.')

        timestamp = int(datetime.now().timestamp())
        src_path = os.path.join(tempfile.gettempdir(), f"round_{user_id}_{timestamp}.{ext}")
        dst_path = os.path.join(tempfile.gettempdir(), f"round_{user_id}_{timestamp}_note.mp4")

        logging.info(f"[RoundVideo] Yuklanmoqda: {file.file_path} -> {src_path}")
        await bot.download_file(file.file_path, src_path)

        if not os.path.exists(src_path):
            logging.error("[RoundVideo] Yuklash muvaffaqiyatsiz - fayl yo'q")
            await call.message.answer(TEXTS[lang]["round_media_failed"])
            return

        logging.info(f"[RoundVideo] Yuklandi: {os.path.getsize(src_path)} bytes")

        # Video note yaratish (240x240) - 12MB limitini hisobga olib
        success = convert_to_video_note_hq(src_path, dst_path)
        _cleanup_file(src_path)

        if not success:
            logging.error("[RoundVideo] Konvertatsiya muvaffaqiyatsiz")
            await call.message.answer(TEXTS[lang]["round_media_failed"])
            return

        # TELEGRAM LIMIT TEKSHIRUVI (12MB)
        MAX_VIDEO_NOTE_BYTES = 12_582_912
        file_size = os.path.getsize(dst_path)
        size_mb = file_size / (1024 * 1024)

        if file_size > MAX_VIDEO_NOTE_BYTES:
            logging.error(f"[RoundVideo] {size_mb:.1f}MB > 12MB Telegram limiti")
            _cleanup_file(dst_path)
            await call.message.answer(
                f"❌ Video note hajmi juda katta ({size_mb:.1f}MB)."
                f"Telegram limiti: 12MB."
                f"Qisqa yoki kichikroq video yuboring."
            )
            return

        # Duration aniqlash
        duration = 0
        try:
            ffprobe_candidates = [
                shutil.which("ffprobe"),
                shutil.which("ffprobe.exe"),
            ]
            ffprobe_cmd = None
            for c in ffprobe_candidates:
                if c:
                    ffprobe_cmd = c
                    break

            if ffprobe_cmd:
                probe = subprocess.run(
                    [ffprobe_cmd, "-v", "error", "-select_streams", "v:0",
                     "-show_entries", "stream=duration", "-of", "csv=p=0", dst_path],
                    capture_output=True, text=True, timeout=10
                )
                if probe.returncode == 0:
                    duration = int(float(probe.stdout.strip()))
        except Exception:
            pass

        logging.info(f"[RoundVideo] Yuborilmoqda: {dst_path} ({duration}s, {size_mb:.1f}MB)")

        # HAqiqiy Telegram video note yuborish (dumaloq ko'rinadi)
        await call.message.answer_video_note(
            video_note=FSInputFile(dst_path),
            duration=duration if duration > 0 else None,
            length=240
        )
        await call.answer(TEXTS[lang]["round_media_ready"], show_alert=True)

    except TelegramBadRequest as e:
        if "too big" in str(e).lower():
            logging.error(f"[RoundVideo] Telegram hajm limiti: {e}")
            await call.message.answer(
                "❌ Video note hajmi Telegram limitidan oshib ketdi (12MB)."
                "Qisqa yoki kichikroq video yuboring."
            )
        else:
            raise
    except Exception as e:
        logging.error(f"[RoundVideo] Umumiy xato: {e}", exc_info=True)
        await call.message.answer(TEXTS[lang]["round_media_failed"])
    finally:
        _cleanup_file(dst_path)
        _cleanup_file(src_path)


@router.callback_query(F.data == "saved_media")
async def saved_media_callback(call: CallbackQuery):
    user = await db.get_user(call.from_user.id)
    if not user:
        return

    lang = user["language"] or "uz"
    items = await db.get_saved_media(call.from_user.id)
    if not items:
        await call.answer(TEXTS[lang]["saved_media_none"], show_alert=True)
        return

    # Barcha saqlanganlarni knopka sifatida ko'rsatish
    text = TEXTS[lang]["saved_media_title"] + f"\n\nJami: {len(items)} ta"

    kb = []
    for idx, item in enumerate(items[:20], start=1):  # 20 tagacha
        media_name = (
            "🎬 Video" if item["media_type"] == "video"
            else "📸 Rasm" if item["media_type"] == "photo"
            else "📄 " + item["media_type"].capitalize()
        )
        created = item["created_at"].strftime("%d.%m %H:%M") if item["created_at"] else "—"
        btn_text = f"{idx}. {media_name} ({created})"
        kb.append([InlineKeyboardButton(text=btn_text, callback_data=f"saved_item:{idx}")])

    kb.append([InlineKeyboardButton(text=TEXTS[lang]["back"], callback_data="back_to_main")])

    await call.message.edit_text(
        text,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=kb),
        parse_mode="HTML",
    )
    await call.answer()


@router.callback_query(F.data.startswith("saved_item:"))
async def saved_item_callback(call: CallbackQuery):
    """Saqlangan media elementini ko'rsatish"""
    user = await db.get_user(call.from_user.id)
    if not user:
        return

    lang = user["language"] or "uz"
    idx = int(call.data.split(":", 1)[1]) - 1  # 1-based to 0-based

    items = await db.get_saved_media(call.from_user.id)
    if idx < 0 or idx >= len(items):
        await call.answer("❌ Media topilmadi", show_alert=True)
        return

    item = items[idx]
    await call.answer()

    # Send the media based on type
    if item["media_type"] == "video":
        await call.message.answer_video(
            video=item["file_id"],
            caption=item.get("caption") or TEXTS[lang]["saved_media_preview_video"]
        )
    elif item["media_type"] == "photo":
        await call.message.answer_photo(
            photo=item["file_id"],
            caption=item.get("caption") or TEXTS[lang]["saved_media_preview_photo"]
        )
    else:
        await call.message.answer_document(
            document=item["file_id"],
            caption=item.get("caption") or "📄 Media"
        )


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

    elif action == "bc_photo":
        await call.message.edit_text(TEXTS["uz"]["broadcast_ask_photo"])
        admin_state[call.from_user.id] = {"state": "waiting_broadcast_photo"}

    elif action == "bc_video":
        await call.message.edit_text(TEXTS["uz"]["broadcast_ask_video"])
        admin_state[call.from_user.id] = {"state": "waiting_broadcast_video"}

    elif action == "bc_media":
        await call.message.edit_text(TEXTS["uz"]["broadcast_ask_media"])
        admin_state[call.from_user.id] = {"state": "waiting_broadcast_media"}

    elif action == "bc_skip":
        state_info = admin_state.get(call.from_user.id, {})
        media_msg = state_info.get("media_msg")
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

    # NEW: Cookie Rotation System startup
    print("[OK] ✨ YANGI Cookie Rotation System ishga tushmoqda:")
    print("[OK]   → Har operatsiyada YANGI cookies yaratish: ✓ Faol")
    print("[OK]   → Bloklanib qolgan cookies blacklist: ✓ Faol")
    print("[OK]   → Eski cookies avtomatik tozalash: ✓ Faol")
    print(f"[OK]   → Cookies pool directory: {cookie_rotation_manager.pool_dir}")
    print("[OK] 🎵 YouTube audio yuklash: Fresh cookies + Multi-attempt strategyasi bilan")
    print("")
    print("[CRITICAL] ⚠️  MUHIM: PROXY O'RNATISH KERAK!")
    print("[CRITICAL] YouTube regulyar ravishda bot-block qiladi. PROXY ishlatish ZARURIY:")
    print("[CRITICAL]")
    print("[CRITICAL] Download Pipeline (v7 - OPTIMIZED):")
    print("[CRITICAL]   1️⃣ Piped API (proxy-free, eng tez)")
    print("[CRITICAL]   2️⃣ Invidious API (YouTube zerkali, proxy-free)")
    print("[CRITICAL]   3️⃣ yt-dlp Cookie-Free (7 clients, proxy bilan)")
    print("[CRITICAL]   4️⃣ Fresh Cookies x3 (proxy rotation, container-optimized)")
    print("[CRITICAL]   5️⃣ Saved Cookies (last resort, proxy bilan)")
    print("[CRITICAL]")
    print("[CRITICAL] 🔧 SETUP:")
    if YOUTUBE_PROXY:
        print("[CRITICAL]   1. .env ga YOUTUBE_PROXY qo'shish yoki to'g'ri sozlash:")
        print(f"[CRITICAL]      YOUTUBE_PROXY={YOUTUBE_PROXY}")
    elif PROXY_API_KEY and PROXY_API_FORMAT:
        print("[CRITICAL]   1. .env ga PROXY_API_KEY va PROXY_API_FORMAT qo'shish:")
        print("[CRITICAL]      PROXY_API_KEY=YOUR_API_KEY")
        print("[CRITICAL]      PROXY_API_FORMAT=http://{api_key}@proxy.provider.com:8000")
    elif PROXY_API_KEY:
        print("[CRITICAL]   1. .env ga PROXY_API_KEY va PROXY_API_FORMAT qo'shish:")
        print("[CRITICAL]      PROXY_API_KEY=YOUR_API_KEY")
        print("[CRITICAL]      PROXY_API_FORMAT=http://{api_key}@proxy.provider.com:8000")
    else:
        print("[CRITICAL]   1. .env ga YOUTUBE_PROXY qo'shish yoki local/proxy list faylini sozlash:")
        print("[CRITICAL]      YOUTUBE_PROXY=http://proxy-ip:port")
        print("[CRITICAL]   2. Yoki PROXY_LIST / PROXY_LIST_FILE orqali lokal proxy ro'yxatini kiriting")
        print("[CRITICAL]      PROXY_LIST=socks5://host:port\n...\n")
        print("[CRITICAL]      PROXY_LIST_FILE=/app/proxies.txt")
    if PROXY_LIST_ENABLED and PROXY_LIST_URLS:
        print("[CRITICAL]   ➜ GitHub proxy ro'yxati ham ishlaydi: PROXY_LIST_URLS")
        print(f"[CRITICAL]      {PROXY_LIST_URLS[0]}")
    if PROXY_LIST:
        print("[CRITICAL]   ➜ Local PROXY_LIST env ichidagi proxylar ham ishlaydi")
    if PROXY_LIST_FILE:
        print(f"[CRITICAL]   ➜ Local proxy fayl ishlaydi: {PROXY_LIST_FILE}")
    print("[CRITICAL]   2. Bot qayta ishga tushirish")
    print("[CRITICAL]   3. Logs'da 'Proxy qo'llanilmoqda' xabarini ko'rish")
    print("[CRITICAL]")
    print("[CRITICAL] 📖 Setup guide: PROXY_SETUP_GUIDE.md ko'ring")
    print("")

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

    if AUTO_DEPLOY:
        _schedule_auto_deploy()

    if AUTO_REFRESH_COOKIES:
        _schedule_auto_cookie_refresh()

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
