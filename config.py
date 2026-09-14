from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# =========================
# Основные настройки
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
BOT_NAME = "Мэт"
PREFIX = ","

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DB_PATH = DATA_DIR / "bot.db"

DATA_DIR.mkdir(parents=True, exist_ok=True)

# Telegram HTML
PARSE_MODE = "HTML"

# Автомодерация
DEFAULT_SETTINGS = {
    "links_enabled": True,
    "bad_words_enabled": True,
    "repeat_enabled": True,
    "flood_enabled": True,
    "welcome_enabled": True,
    "warn_limit": 3,
    "warn_action": "mute",
    "warn_mute_minutes": 30,
    "flood_messages": 6,
    "flood_seconds": 8,
    "repeat_count": 3,
    "delete_notice_seconds": 8,
}

# Уровни доступа бота.
# telegram_admin = Telegram-администратор группы
# owner = создатель группы
ACCESS_LEVELS = {
    "everyone": 0,
    "member": 10,
    "moderator": 20,
    "senior_mod": 30,
    "admin": 40,
    "owner": 50,
}

ACCESS_LABELS = {
    "everyone": "Все",
    "member": "Участник",
    "moderator": "Модератор",
    "senior_mod": "Старший модератор",
    "admin": "Администратор",
    "owner": "Владелец",
}

ACCESS_ALIASES = {
    "все": "everyone",
    "всем": "everyone",
    "everyone": "everyone",
    "участник": "member",
    "участники": "member",
    "member": "member",
    "модератор": "moderator",
    "модер": "moderator",
    "moderator": "moderator",
    "старший": "senior_mod",
    "старший_модератор": "senior_mod",
    "старший-модератор": "senior_mod",
    "senior": "senior_mod",
    "senior_mod": "senior_mod",
    "админ": "admin",
    "администратор": "admin",
    "admin": "admin",
    "владелец": "owner",
    "owner": "owner",
}

# Алиасы действий автомодерации
ON_WORDS = {"on", "вкл", "включить", "да", "1", "true"}
OFF_WORDS = {"off", "выкл", "выключить", "нет", "0", "false"}

# Команды, которые Telegram покажет в меню.
# Telegram не принимает кириллицу в BotCommand.command,
# поэтому здесь используются короткие slash-алиасы.
TELEGRAM_COMMANDS = [
    ("help", "Помощь и список команд"),
    ("info", "Информация о боте и группе"),
    ("rules", "Правила группы"),
    ("mute", "Мут: ответьте на сообщение"),
    ("warn", "Предупреждение: ответьте на сообщение"),
    ("ban", "Бан: ответьте на сообщение"),
    ("kick", "Кик: ответьте на сообщение"),
    ("unmute", "Снять мут: ответьте на сообщение"),
    ("status", "Статус пользователя"),
]

# Версии / тексты
VERSION = "2.0.0"
