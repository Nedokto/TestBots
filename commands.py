from __future__ import annotations

import re
import time
import random
from dataclasses import dataclass
from typing import Callable

from telebot import types
from telebot.util import quick_markup

from config import (
    ACCESS_ALIASES,
    ACCESS_LABELS,
    ACCESS_LEVELS,
    BOT_NAME,
    ON_WORDS,
    OFF_WORDS,
    PREFIX,
)
from database import Database


# =========================
# Дизайн
# =========================

def card(title: str, body: str = "", icon: str = "•") -> str:
    if body:
        return f"{icon} <b>{title}</b>\n\n{body}"
    return f"{icon} <b>{title}</b>"


def ok(body: str) -> str:
    return card("ГОТОВО", body, "✓")


def error(body: str) -> str:
    return card("НЕ ВЫПОЛНЕНО", body, "✕")


def info(body: str) -> str:
    return card("ИНФОРМАЦИЯ", body, "i")


def normalize_command(value: str) -> str:
    value = value.strip().lower()
    if value.startswith(PREFIX):
        value = value[len(PREFIX):]
    if "@" in value:
        value = value.split("@", 1)[0]
    return value


def user_label(user) -> str:
    if not user:
        return "Пользователь"
    name = (user.first_name or "Пользователь").strip()
    if user.last_name:
        name += f" {user.last_name}"
    return name


def mention(user_id: int, label: str) -> str:
    return f'<a href="tg://user?id={user_id}">{label}</a>'


def parse_duration(value: str | None, default_minutes: int = 30) -> int | None:
    if not value:
        return default_minutes
    text = value.strip().lower().replace(",", ".")
    match = re.fullmatch(r"(\d+(?:\.\d+)?)([mhd]?)", text)
    if not match:
        return None
    number = float(match.group(1))
    unit = match.group(2) or "m"
    multiplier = {"m": 1, "h": 60, "d": 1440}[unit]
    minutes = int(number * multiplier)
    return max(1, min(minutes, 43200))


@dataclass
class Command:
    name: str
    description: str
    usage: str
    category: str
    default_access: str
    handler: Callable
    enabled_by_default: bool = True
    example: str = ""


COMMANDS: list[Command] = []


def command(
    name: str,
    description: str,
    usage: str,
    category: str,
    default_access: str,
    enabled_by_default: bool = True,
    example: str = "",
):
    def decorator(func):
        COMMANDS.append(
            Command(
                name=name,
                description=description,
                usage=usage,
                category=category,
                default_access=default_access,
                handler=func,
                enabled_by_default=enabled_by_default,
                example=example,
            )
        )
        return func
    return decorator


class CommandContext:
    def __init__(self, bot, db: Database, message: types.Message):
        self.bot = bot
        self.db = db
        self.message = message
        self.chat_id = message.chat.id
        self.user = message.from_user

    def reply(self, text: str, markup=None):
        return self.bot.reply_to(
            self.message,
            text,
            parse_mode="HTML",
            reply_markup=markup,
            disable_web_page_preview=True,
        )

    def chat_member(self, user_id: int):
        return self.bot.get_chat_member(self.chat_id, user_id)

    def telegram_level(self, user_id: int) -> str:
        member = self.chat_member(user_id)
        if member.status == "creator":
            return "owner"
        if member.status == "administrator":
            return "admin"
        return "member"

    def effective_level(self, user_id: int) -> str:
        telegram = self.telegram_level(user_id)
        if telegram in {"owner", "admin"}:
            return telegram

        custom = self.db.get_role(self.chat_id, user_id)
        return custom or "member"

    def can(self, user_id: int, required: str) -> bool:
        return ACCESS_LEVELS[self.effective_level(user_id)] >= ACCESS_LEVELS[required]

    def target_from_message(self):
        reply = self.message.reply_to_message
        if reply and reply.from_user:
            return reply.from_user

        parts = self.message.text.split()[1:]
        if not parts:
            return None

        raw = parts[0]
        if re.fullmatch(r"-?\d+", raw):
            user_id = int(raw)
            try:
                member = self.bot.get_chat_member(self.chat_id, user_id)
                return member.user
            except Exception:
                return None
        return None

    def target_id_and_user(self):
        user = self.target_from_message()
        return (user.id, user) if user else (None, None)

    def target_is_higher_or_equal(self, target_id: int) -> bool:
        actor_level = ACCESS_LEVELS[self.effective_level(self.user.id)]
        target_level = ACCESS_LEVELS[self.effective_level(target_id)]
        return target_level >= actor_level


def command_rows():
    return [
        {
            "name": c.name,
            "description": c.description,
            "usage": c.usage,
            "category": c.category,
            "default_access": c.default_access,
            "enabled_by_default": c.enabled_by_default,
        }
        for c in COMMANDS
    ]


def commands_by_name() -> dict[str, Command]:
    return {c.name: c for c in COMMANDS}


# =========================
# Вспомогательное
# =========================

def require_group(ctx: CommandContext) -> bool:
    return ctx.message.chat.type in {"group", "supergroup"}


def require_access(ctx: CommandContext, command_name: str) -> bool:
    cfg = ctx.db.get_command_config(ctx.chat_id, command_name)
    if not cfg or not cfg["enabled"]:
        ctx.reply(error("Эта команда сейчас отключена в этой группе."))
        return False

    if not ctx.can(ctx.user.id, cfg["access_level"]):
        ctx.reply(
            error(
                f"Недостаточно прав.\n"
                f"Требуется: <b>{ACCESS_LABELS[cfg['access_level']]}</b>"
            )
        )
        return False
    return True


def protect_target(ctx: CommandContext, target_id: int) -> bool:
    if target_id == ctx.user.id:
        ctx.reply(error("Нельзя применить эту модерацию к самому себе."))
        return False
    try:
        member = ctx.chat_member(target_id)
        if member.status in {"administrator", "creator"}:
            ctx.reply(error("Нельзя модерировать администратора Telegram."))
            return False
    except Exception:
        pass
    if ctx.target_is_higher_or_equal(target_id):
        ctx.reply(error("У цели такой же или более высокий уровень доступа."))
        return False
    return True


def moderation_failure(ctx: CommandContext, action: str, exc: Exception):
    ctx.reply(
        error(
            f"Не удалось выполнить: <b>{action}</b>.\n"
            "Проверьте, что у бота есть права администратора "
            "на удаление сообщений и управление участниками."
        )
    )


def command_category_keyboard():
    return quick_markup(
        {
            "📌 Основные": {"callback_data": "commands:Основное"},
            "🛡 Модерация": {"callback_data": "commands:Модерация"},
            "🎮 Игры": {"callback_data": "commands:Игры"},
            "✨ Развлечения": {"callback_data": "commands:Развлечения"},
            "🛠 Автомод": {"callback_data": "commands:Автомод"},
            "⚙ Управление": {"callback_data": "commands:Управление"},
            "📚 Все команды": {"callback_data": "commands:all"},
        },
        row_width=2,
    )


def format_command_book(ctx: CommandContext, category: str = "all") -> str:
    category_order = [
        "Основное",
        "Модерация",
        "Развлечения",
        "Игры",
        "Управление",
        "Автомод",
    ]

    if category == "all":
        selected = [c for cat in category_order for c in COMMANDS if c.category == cat]
    else:
        selected = [c for c in COMMANDS if c.category == category]

    parts = []
    current_category = None

    for c in selected:
        cfg = ctx.db.get_command_config(ctx.chat_id, c.name)
        if not cfg["enabled"]:
            continue

        if c.category != current_category:
            current_category = c.category
            parts.append(f"\n<b>{current_category.upper()}</b>")

        line = f"<code>{PREFIX}{c.name}</code> — {c.description}"
        if c.example:
            line += f"\n   <i>Пример:</i> <code>{c.example}</code>"
        parts.append(line)

    if not parts:
        return "В этой категории нет доступных включённых команд."

    return "\n".join(parts)


def help_keyboard():
    return quick_markup(
        {
            "🛡 Модерация": {"callback_data": "help:moderation"},
            "⚙ Управление": {"callback_data": "help:management"},
            "🛠 Автомод": {"callback_data": "help:automod"},
            "ℹ Команды": {"callback_data": "help:all"},
            "⌘ Каталог": {"callback_data": "commands:all"},
        },
        row_width=2,
    )


# =========================
# Команды
# =========================

@command(
    "помощь",
    "Список команд и подробные подсказки.",
    ",помощь [команда]",
    "Основное",
    "everyone",
)
def cmd_help(ctx: CommandContext, args: list[str]):
    if args:
        name = normalize_command(args[0])
        target = commands_by_name().get(name)
        if not target:
            ctx.reply(error(f"Команда <code>{name}</code> не найдена."))
            return
        cfg = ctx.db.get_command_config(ctx.chat_id, target.name)
        body = (
            f"<b>{PREFIX}{target.name}</b>\n"
            f"{target.description}\n\n"
            f"<b>Использование:</b>\n<code>{target.usage}</code>\n\n"
            f"<b>Категория:</b> {target.category}\n"
            f"<b>Доступ:</b> {ACCESS_LABELS[cfg['access_level']]}\n"
            f"<b>Состояние:</b> {'включена' if cfg['enabled'] else 'выключена'}"
        )
        ctx.reply(card("ПОДСКАЗКА", body, "❔"))
        return

    counts = {}
    for c in COMMANDS:
        counts.setdefault(c.category, []).append(c)

    body_parts = []
    for category, commands in counts.items():
        lines = [f"<b>{category}</b>"]
        for c in commands:
            cfg = ctx.db.get_command_config(ctx.chat_id, c.name)
            if cfg["enabled"]:
                lines.append(f"<code>{PREFIX}{c.name}</code> — {c.description}")
        body_parts.append("\n".join(lines))

    body = (
        f"Используйте <code>{PREFIX}помощь команда</code>, "
        "чтобы открыть подробную подсказку.\n\n"
        + "\n\n".join(body_parts)
    )
    ctx.reply(card("СПРАВКА", body, "❔"), help_keyboard())


@command(
    "команды",
    "Полный каталог команд с категориями, описаниями и примерами.",
    ",команды [категория]",
    "Основное",
    "everyone",
    example=",команды игры",
)
def cmd_commands(ctx: CommandContext, args: list[str]):
    aliases = {
        "основное": "Основное",
        "основные": "Основное",
        "модерация": "Модерация",
        "модер": "Модерация",
        "игры": "Игры",
        "игра": "Игры",
        "развлечения": "Развлечения",
        "развлечение": "Развлечения",
        "автомод": "Автомод",
        "управление": "Управление",
        "все": "all",
    }

    if args:
        category = aliases.get(args[0].lower())
        if not category:
            ctx.reply(error(
                "Неизвестная категория. Используйте: "
                "<code>основное</code>, <code>модерация</code>, "
                "<code>игры</code>, <code>развлечения</code>, "
                "<code>автомод</code>, <code>управление</code> или <code>все</code>."
            ))
            return
    else:
        category = "all"

    title = "КОМАНДЫ" if category == "all" else f"КОМАНДЫ · {category.upper()}"
    body = (
        "Полный каталог команд. Нажмите категорию ниже или используйте "
        f"<code>{PREFIX}команды игры</code>.\n"
        + format_command_book(ctx, category)
    )
    ctx.reply(card(title, body, "⌘"), command_category_keyboard())


@command(
    "инфо",
    "Информация о боте, группе и текущем профиле.",
    ",инфо",
    "Основное",
    "everyone",
)
def cmd_info(ctx: CommandContext, args: list[str]):
    role = ACCESS_LABELS[ctx.effective_level(ctx.user.id)]
    title = ctx.message.chat.title or "Личная переписка"
    body = (
        f"<b>Бот:</b> {BOT_NAME}\n"
        f"<b>Версия:</b> 2.0\n"
        f"<b>Группа:</b> {title}\n"
        f"<b>Ваш доступ:</b> {role}\n\n"
        "Основной префикс команд: <code>,</code>\n"
        "Подсказки доступны через <code>,помощь</code>."
    )
    ctx.reply(card("СВЕДЕНИЯ", body, "ℹ"))


@command(
    "правила",
    "Показывает базовую памятку по порядку модерации.",
    ",правила",
    "Основное",
    "everyone",
)
def cmd_rules(ctx: CommandContext, args: list[str]):
    body = (
        "<b>1.</b> Соблюдайте тему и правила группы.\n"
        "<b>2.</b> Не публикуйте запрещённые ссылки и содержимое.\n"
        "<b>3.</b> Не спамьте одинаковыми сообщениями или медиа.\n"
        "<b>4.</b> Не мешайте работе модерации.\n\n"
        "<b>Стандартная последовательность:</b>\n"
        "устное предупреждение → мут → варн → кик/бан."
    )
    ctx.reply(card("ПРАВИЛА", body, "§"))


@command(
    "пинг",
    "Проверяет, отвечает ли бот.",
    ",пинг",
    "Основное",
    "everyone",
    example=",пинг",
)
def cmd_ping(ctx: CommandContext, args: list[str]):
    started = time.perf_counter()
    sent = ctx.reply("Проверяю связь…")
    latency = round((time.perf_counter() - started) * 1000)
    try:
        ctx.bot.edit_message_text(
            chat_id=ctx.chat_id,
            message_id=sent.message_id,
            text=card("PONG", f"Бот отвечает.\n<b>Задержка:</b> {latency} мс", "⌁"),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
    except Exception:
        pass


@command(
    "кто",
    "Случайно выбирает участника среди упомянутых в сообщении или отвечает на пользователя.",
    ",кто — ответом на сообщение; или ответьте на сообщение с несколькими упоминаниями",
    "Развлечения",
    "everyone",
    example=",кто",
)
def cmd_who(ctx: CommandContext, args: list[str]):
    reply = ctx.message.reply_to_message
    candidates = []

    if reply and reply.from_user:
        candidates.append(reply.from_user)

        entities = reply.entities or reply.caption_entities or []
        source = reply.text or reply.caption or ""
        for entity in entities:
            if entity.type == "text_mention" and entity.user:
                candidates.append(entity.user)

    if ctx.message.entities:
        source = ctx.message.text or ""
        for entity in ctx.message.entities:
            if entity.type == "text_mention" and entity.user:
                candidates.append(entity.user)

    unique = {}
    for user in candidates:
        unique[user.id] = user

    if len(unique) == 1:
        target = next(iter(unique.values()))
    elif len(unique) > 1:
        target = random.choice(list(unique.values()))
    else:
        ctx.reply(info(
            "Чтобы выбрать пользователя, ответьте этой командой на его сообщение "
            "или используйте несколько текстовых упоминаний."
        ))
        return

    ctx.reply(card(
        "ВЫБОР",
        f"Выбран: {mention(target.id, user_label(target))}",
        "🎯",
    ))


@command(
    "данет",
    "Случайно отвечает «да» или «нет».",
    ",данет [вопрос]",
    "Развлечения",
    "everyone",
    example=",данет я сегодня выиграю?",
)
def cmd_yesno(ctx: CommandContext, args: list[str]):
    answer = random.choice(["Да.", "Нет."])
    if args:
        question = " ".join(args)
        ctx.reply(card("ДА / НЕТ", f"<b>Вопрос:</b> {question}\n\n<b>Ответ:</b> {answer}", "❓"))
    else:
        ctx.reply(card("ДА / НЕТ", f"<b>Ответ:</b> {answer}", "❓"))


@command(
    "рандом",
    "Выдаёт случайное число в указанном диапазоне.",
    ",рандом <от> <до>",
    "Развлечения",
    "everyone",
    example=",рандом 1 100",
)
def cmd_random(ctx: CommandContext, args: list[str]):
    if len(args) < 2:
        ctx.reply(info("Пример: <code>,рандом 1 100</code>"))
        return
    try:
        a, b = int(args[0]), int(args[1])
    except ValueError:
        ctx.reply(error("Оба значения должны быть целыми числами."))
        return
    if a > b:
        a, b = b, a
    if b - a > 1_000_000:
        ctx.reply(error("Диапазон слишком большой. Максимальная разница — 1 000 000."))
        return
    ctx.reply(card("СЛУЧАЙНОЕ ЧИСЛО", f"<b>Диапазон:</b> {a}–{b}\n<b>Результат:</b> {random.randint(a, b)}", "🎲"))


@command(
    "статус",
    "Показывает роль и количество предупреждений.",
    ",статус [ответ/ID]",
    "Модерация",
    "moderator",
)
def cmd_status(ctx: CommandContext, args: list[str]):
    target_id, target = ctx.target_id_and_user()
    if not target:
        ctx.reply(error("Ответьте на сообщение пользователя или укажите его числовой ID."))
        return

    try:
        role = ACCESS_LABELS[ctx.effective_level(target_id)]
    except Exception:
        role = "Участник"

    warns = ctx.db.warning_count(ctx.chat_id, target_id)
    label = user_label(target)
    body = (
        f"<b>Пользователь:</b> {mention(target_id, label)}\n"
        f"<b>ID:</b> <code>{target_id}</code>\n"
        f"<b>Доступ:</b> {role}\n"
        f"<b>Предупреждения:</b> {warns}"
    )
    ctx.reply(card("СТАТУС", body, "◉"))


@command(
    "варн",
    "Выдаёт предупреждение пользователю и сохраняет его в базе.",
    ",варн [причина] — ответом на сообщение",
    "Модерация",
    "senior_mod",
)
def cmd_warn(ctx: CommandContext, args: list[str]):
    target_id, target = ctx.target_id_and_user()
    if not target or not protect_target(ctx, target_id):
        if not target:
            ctx.reply(error("Ответьте на сообщение пользователя."))
        return

    reason = " ".join(args).strip() or "Причина не указана"
    warning_id = ctx.db.add_warning(
        ctx.chat_id, target_id, ctx.user.id, reason
    )
    count = ctx.db.warning_count(ctx.chat_id, target_id)
    limit = int(ctx.db.get_setting(ctx.chat_id, "warn_limit", 3) or 3)

    ctx.db.audit(
        ctx.chat_id, ctx.user.id, "warn", target_id,
        f"#{warning_id}: {reason}"
    )

    body = (
        f"{mention(target_id, user_label(target))}\n"
        f"<b>Причина:</b> {reason}\n"
        f"<b>Предупреждений:</b> {count}/{limit}"
    )

    if count >= limit:
        action = str(ctx.db.get_setting(ctx.chat_id, "warn_action", "mute"))
        minutes = int(ctx.db.get_setting(ctx.chat_id, "warn_mute_minutes", 30) or 30)
        if action == "mute":
            try:
                until = int(time.time()) + minutes * 60
                ctx.bot.restrict_chat_member(
                    ctx.chat_id,
                    target_id,
                    permissions=types.ChatPermissions(
                        can_send_messages=False,
                        can_send_audios=False,
                        can_send_documents=False,
                        can_send_photos=False,
                        can_send_videos=False,
                        can_send_video_notes=False,
                        can_send_voice_notes=False,
                        can_send_polls=False,
                        can_send_other_messages=False,
                        can_add_web_page_previews=False,
                    ),
                    until_date=until,
                )
                body += f"\n\n⚠ Лимит достигнут. Дополнительно: мут на {minutes} мин."
            except Exception:
                body += "\n\n⚠ Лимит достигнут, но автоматический мут не удалось применить."

    ctx.reply(card("ПРЕДУПРЕЖДЕНИЕ", body, "⚠"))


@command(
    "варны",
    "Показывает предупреждения пользователя.",
    ",варны — ответом на сообщение",
    "Модерация",
    "moderator",
)
def cmd_warns(ctx: CommandContext, args: list[str]):
    target_id, target = ctx.target_id_and_user()
    if not target:
        ctx.reply(error("Ответьте на сообщение пользователя."))
        return

    rows = ctx.db.get_warnings(ctx.chat_id, target_id, 10)
    if not rows:
        ctx.reply(info(f"{mention(target_id, user_label(target))}: предупреждений нет."))
        return

    lines = []
    for row in rows:
        reason = row["reason"] or "без причины"
        lines.append(f"• <b>#{row['id']}</b> — {reason}")
    body = (
        f"{mention(target_id, user_label(target))}\n"
        f"<b>Всего:</b> {len(rows)}\n\n" + "\n".join(lines)
    )
    ctx.reply(card("ПРЕДУПРЕЖДЕНИЯ", body, "⚠"))


@command(
    "снятьварн",
    "Снимает последнее предупреждение.",
    ",снятьварн — ответом на сообщение",
    "Модерация",
    "senior_mod",
)
def cmd_unwarn(ctx: CommandContext, args: list[str]):
    target_id, target = ctx.target_id_and_user()
    if not target:
        ctx.reply(error("Ответьте на сообщение пользователя."))
        return
    if not protect_target(ctx, target_id):
        return

    if not ctx.db.remove_last_warning(ctx.chat_id, target_id):
        ctx.reply(info("У этого пользователя нет предупреждений."))
        return

    count = ctx.db.warning_count(ctx.chat_id, target_id)
    ctx.db.audit(ctx.chat_id, ctx.user.id, "unwarn", target_id)
    ctx.reply(
        ok(
            f"{mention(target_id, user_label(target))}\n"
            f"Последнее предупреждение снято.\n"
            f"<b>Осталось:</b> {count}"
        )
    )


@command(
    "мут",
    "Временно ограничивает отправку сообщений.",
    ",мут <время> [причина] — ответом на сообщение",
    "Модерация",
    "moderator",
)
def cmd_mute(ctx: CommandContext, args: list[str]):
    target_id, target = ctx.target_id_and_user()
    if not target:
        ctx.reply(error("Ответьте на сообщение пользователя."))
        return
    if not protect_target(ctx, target_id):
        return

    duration_arg = args[0] if args else None
    minutes = parse_duration(duration_arg, 30)
    if minutes is None:
        ctx.reply(error("Время указано неверно. Примеры: <code>30</code>, <code>2h</code>, <code>1d</code>."))
        return

    reason = " ".join(args[1:]).strip() if len(args) > 1 else "Причина не указана"
    try:
        until = int(time.time()) + minutes * 60
        ctx.bot.restrict_chat_member(
            ctx.chat_id,
            target_id,
            permissions=types.ChatPermissions(
                can_send_messages=False,
                can_send_audios=False,
                can_send_documents=False,
                can_send_photos=False,
                can_send_videos=False,
                can_send_video_notes=False,
                can_send_voice_notes=False,
                can_send_polls=False,
                can_send_other_messages=False,
                can_add_web_page_previews=False,
            ),
            until_date=until,
        )
    except Exception as exc:
        moderation_failure(ctx, "мут", exc)
        return

    ctx.db.audit(ctx.chat_id, ctx.user.id, "mute", target_id, f"{minutes}m; {reason}")
    ctx.reply(
        ok(
            f"{mention(target_id, user_label(target))}\n"
            f"<b>Срок:</b> {minutes} мин.\n"
            f"<b>Причина:</b> {reason}"
        )
    )


@command(
    "размут",
    "Снимает ограничение на отправку сообщений.",
    ",размут — ответом на сообщение",
    "Модерация",
    "moderator",
)
def cmd_unmute(ctx: CommandContext, args: list[str]):
    target_id, target = ctx.target_id_and_user()
    if not target:
        ctx.reply(error("Ответьте на сообщение пользователя."))
        return
    if not protect_target(ctx, target_id):
        return

    try:
        ctx.bot.restrict_chat_member(
            ctx.chat_id,
            target_id,
            permissions=types.ChatPermissions(
                can_send_messages=True,
                can_send_audios=True,
                can_send_documents=True,
                can_send_photos=True,
                can_send_videos=True,
                can_send_video_notes=True,
                can_send_voice_notes=True,
                can_send_polls=True,
                can_send_other_messages=True,
                can_add_web_page_previews=True,
            ),
        )
    except Exception as exc:
        moderation_failure(ctx, "размут", exc)
        return

    ctx.db.audit(ctx.chat_id, ctx.user.id, "unmute", target_id)
    ctx.reply(ok(f"{mention(target_id, user_label(target))}\nОграничение снято."))


@command(
    "очистить",
    "Удаляет несколько последних сообщений, начиная с сообщения, на которое вы ответили.",
    ",очистить <количество>",
    "Модерация",
    "moderator",
    example=",очистить 20",
)
def cmd_clear(ctx: CommandContext, args: list[str]):
    if not args:
        ctx.reply(info("Пример: <code>,очистить 20</code>"))
        return
    try:
        count = int(args[0])
    except ValueError:
        ctx.reply(error("Количество сообщений должно быть числом."))
        return

    count = max(1, min(count, 100))
    reply = ctx.message.reply_to_message
    if not reply:
        ctx.reply(error("Ответьте на сообщение, с которого нужно начать очистку."))
        return

    deleted = 0
    for message_id in range(reply.message_id, reply.message_id + count):
        try:
            ctx.bot.delete_message(ctx.chat_id, message_id)
            deleted += 1
        except Exception:
            pass

    try:
        ctx.bot.delete_message(ctx.chat_id, ctx.message.message_id)
    except Exception:
        pass

    notice = ctx.bot.send_message(
        ctx.chat_id,
        ok(f"Удалено сообщений: <b>{deleted}</b>."),
        parse_mode="HTML",
        disable_web_page_preview=True,
    )
    ctx.db.audit(ctx.chat_id, ctx.user.id, "clear_messages", None, str(deleted))

    try:
        import threading
        def delete_notice():
            time.sleep(5)
            try:
                ctx.bot.delete_message(ctx.chat_id, notice.message_id)
            except Exception:
                pass
        threading.Thread(target=delete_notice, daemon=True).start()
    except Exception:
        pass


@command(
    "кик",
    "Удаляет пользователя из группы без постоянного бана.",
    ",кик [причина] — ответом на сообщение",
    "Модерация",
    "senior_mod",
)
def cmd_kick(ctx: CommandContext, args: list[str]):
    target_id, target = ctx.target_id_and_user()
    if not target:
        ctx.reply(error("Ответьте на сообщение пользователя."))
        return
    if not protect_target(ctx, target_id):
        return

    reason = " ".join(args).strip() or "Причина не указана"
    try:
        ctx.bot.ban_chat_member(ctx.chat_id, target_id)
        ctx.bot.unban_chat_member(ctx.chat_id, target_id, only_if_banned=True)
    except Exception as exc:
        moderation_failure(ctx, "кик", exc)
        return

    ctx.db.audit(ctx.chat_id, ctx.user.id, "kick", target_id, reason)
    ctx.reply(
        ok(
            f"{mention(target_id, user_label(target))}\n"
            f"<b>Причина:</b> {reason}"
        )
    )


@command(
    "бан",
    "Постоянно блокирует пользователя в группе.",
    ",бан [причина] — ответом на сообщение",
    "Модерация",
    "senior_mod",
)
def cmd_ban(ctx: CommandContext, args: list[str]):
    target_id, target = ctx.target_id_and_user()
    if not target:
        ctx.reply(error("Ответьте на сообщение пользователя."))
        return
    if not protect_target(ctx, target_id):
        return

    reason = " ".join(args).strip() or "Причина не указана"
    try:
        ctx.bot.ban_chat_member(ctx.chat_id, target_id)
    except Exception as exc:
        moderation_failure(ctx, "бан", exc)
        return

    ctx.db.audit(ctx.chat_id, ctx.user.id, "ban", target_id, reason)
    ctx.reply(
        ok(
            f"{mention(target_id, user_label(target))}\n"
            f"<b>Причина:</b> {reason}"
        )
    )


@command(
    "разбан",
    "Снимает бан по числовому ID пользователя.",
    ",разбан <ID>",
    "Модерация",
    "admin",
)
def cmd_unban(ctx: CommandContext, args: list[str]):
    if not args or not re.fullmatch(r"-?\d+", args[0]):
        ctx.reply(error("Укажите числовой ID пользователя."))
        return
    target_id = int(args[0])
    try:
        ctx.bot.unban_chat_member(ctx.chat_id, target_id, only_if_banned=True)
    except Exception as exc:
        moderation_failure(ctx, "разбан", exc)
        return

    ctx.db.audit(ctx.chat_id, ctx.user.id, "unban", target_id)
    ctx.reply(ok(f"Бан пользователя <code>{target_id}</code> снят."))


@command(
    "удалить",
    "Удаляет сообщение, на которое вы ответили.",
    ",удалить — ответьте на сообщение",
    "Модерация",
    "moderator",
)
def cmd_delete(ctx: CommandContext, args: list[str]):
    reply = ctx.message.reply_to_message
    if not reply:
        ctx.reply(error("Ответьте на сообщение, которое нужно удалить."))
        return
    try:
        ctx.bot.delete_message(ctx.chat_id, reply.message_id)
        ctx.db.audit(ctx.chat_id, ctx.user.id, "delete_message", reply.from_user.id if reply.from_user else None)
        ctx.reply(ok("Сообщение удалено."))
    except Exception as exc:
        moderation_failure(ctx, "удалить", exc)


@command(
    "монетка",
    "Подбрасывает монетку.",
    ",монетка",
    "Игры",
    "everyone",
    example=",монетка",
)
def cmd_coin(ctx: CommandContext, args: list[str]):
    result = random.choice(["Орёл", "Решка"])
    ctx.reply(card("МОНЕТКА", f"<b>Результат:</b> {result}", "🪙"))


@command(
    "кубик",
    "Бросает стандартный шестигранный кубик.",
    ",кубик [количество]",
    "Игры",
    "everyone",
    example=",кубик 3",
)
def cmd_dice(ctx: CommandContext, args: list[str]):
    try:
        count = int(args[0]) if args else 1
    except ValueError:
        ctx.reply(error("Количество кубиков должно быть числом."))
        return
    count = max(1, min(count, 10))
    rolls = [random.randint(1, 6) for _ in range(count)]
    ctx.reply(card(
        "КУБИК",
        f"<b>Броски:</b> {', '.join(map(str, rolls))}\n<b>Сумма:</b> {sum(rolls)}",
        "🎲",
    ))


@command(
    "8ball",
    "Отвечает на вопрос случайным предсказанием.",
    ",8ball <вопрос>",
    "Игры",
    "everyone",
    example=",8ball я сдам экзамен?",
)
def cmd_8ball(ctx: CommandContext, args: list[str]):
    if not args:
        ctx.reply(info("Задайте вопрос. Пример: <code>,8ball я сегодня выиграю?</code>"))
        return
    answers = [
        "Определённо да.",
        "Скорее да.",
        "Вероятно.",
        "Сложно сказать.",
        "Скорее нет.",
        "Определённо нет.",
        "Сейчас лучше не рассчитывать на это.",
        "Шансы есть.",
    ]
    ctx.reply(card("МАГИЧЕСКИЙ ШАР", f"<b>Ответ:</b> {random.choice(answers)}", "🔮"))


@command(
    "кнб",
    "Играет с вами в камень, ножницы, бумагу.",
    ",кнб <камень|ножницы|бумага>",
    "Игры",
    "everyone",
    example=",кнб камень",
)
def cmd_rps(ctx: CommandContext, args: list[str]):
    aliases = {
        "к": "камень", "камень": "камень",
        "н": "ножницы", "ножницы": "ножницы",
        "б": "бумага", "бумага": "бумага",
    }
    if not args or args[0].lower() not in aliases:
        ctx.reply(info(
            "Выберите: <code>камень</code>, <code>ножницы</code> или <code>бумага</code>.\n"
            "Пример: <code>,кнб камень</code>"
        ))
        return

    player = aliases[args[0].lower()]
    bot_choice = random.choice(["камень", "ножницы", "бумага"])

    if player == bot_choice:
        result = "Ничья."
    elif (
        (player == "камень" and bot_choice == "ножницы")
        or (player == "ножницы" and bot_choice == "бумага")
        or (player == "бумага" and bot_choice == "камень")
    ):
        result = "Вы победили."
    else:
        result = "Победил бот."

    ctx.reply(card(
        "КАМЕНЬ · НОЖНИЦЫ · БУМАГА",
        f"<b>Вы:</b> {player}\n<b>Бот:</b> {bot_choice}\n\n<b>{result}</b>",
        "✊",
    ))


@command(
    "настройки",
    "Показывает текущую конфигурацию автомодерации.",
    ",настройки",
    "Управление",
    "admin",
)
def cmd_settings(ctx: CommandContext, args: list[str]):
    s = ctx.db.get_all_settings(ctx.chat_id)
    def yn(v): return "вкл." if bool(v) else "выкл."
    body = (
        f"<b>Ссылки:</b> {yn(s.get('links_enabled'))}\n"
        f"<b>Фильтр слов:</b> {yn(s.get('bad_words_enabled'))}\n"
        f"<b>Повторы:</b> {yn(s.get('repeat_enabled'))}\n"
        f"<b>Флуд:</b> {yn(s.get('flood_enabled'))}\n"
        f"<b>Приветствие:</b> {yn(s.get('welcome_enabled'))}\n\n"
        f"<b>Лимит варнов:</b> {s.get('warn_limit')}\n"
        f"<b>Автодействие:</b> {s.get('warn_action')}\n"
        f"<b>Мут после лимита:</b> {s.get('warn_mute_minutes')} мин.\n"
        f"<b>Флуд:</b> {s.get('flood_messages')} сообщений / {s.get('flood_seconds')} сек."
    )
    ctx.reply(card("НАСТРОЙКИ", body, "⚙"))


@command(
    "автомод",
    "Включает/выключает модуль автомодерации или отдельную функцию.",
    ",автомод <ссылки|слова|повторы|флуд|приветствие> <вкл|выкл>",
    "Управление",
    "admin",
)
def cmd_automod(ctx: CommandContext, args: list[str]):
    if len(args) < 2:
        ctx.reply(
            info(
                "<code>,автомод ссылки вкл</code>\n"
                "<code>,автомод слова выкл</code>\n"
                "<code>,автомод повторы вкл</code>\n"
                "<code>,автомод флуд вкл</code>\n"
                "<code>,автомод приветствие выкл</code>"
            )
        )
        return

    aliases = {
        "ссылки": "links_enabled",
        "ссылка": "links_enabled",
        "слова": "bad_words_enabled",
        "фильтр": "bad_words_enabled",
        "повторы": "repeat_enabled",
        "повтор": "repeat_enabled",
        "флуд": "flood_enabled",
        "приветствие": "welcome_enabled",
    }
    key = aliases.get(args[0].lower())
    if not key:
        ctx.reply(error("Неизвестный модуль автомодерации."))
        return

    state = args[1].lower()
    if state in ON_WORDS:
        value = True
    elif state in OFF_WORDS:
        value = False
    else:
        ctx.reply(error("Используйте <code>вкл</code> или <code>выкл</code>."))
        return

    ctx.db.set_setting(ctx.chat_id, key, value)
    ctx.db.audit(ctx.chat_id, ctx.user.id, "set_automod", None, f"{key}={value}")
    ctx.reply(ok(f"<b>{key}</b>: {'включено' if value else 'выключено'}."))


@command(
    "доступ",
    "Меняет требуемый уровень доступа к команде.",
    ",доступ <команда> <уровень>",
    "Управление",
    "owner",
)
def cmd_access(ctx: CommandContext, args: list[str]):
    if len(args) < 2:
        ctx.reply(
            info(
                "<b>Уровни:</b> все, участник, модератор, старший, админ, владелец\n"
                "Пример: <code>,доступ варн модератор</code>"
            )
        )
        return

    name = normalize_command(args[0])
    command_obj = commands_by_name().get(name)
    if not command_obj:
        ctx.reply(error(f"Команда <code>{name}</code> не найдена."))
        return

    level = ACCESS_ALIASES.get(args[1].lower())
    if not level:
        ctx.reply(error("Неизвестный уровень доступа."))
        return

    ctx.db.set_command_config(ctx.chat_id, name, access_level=level)
    ctx.db.audit(ctx.chat_id, ctx.user.id, "set_access", None, f"{name}={level}")
    ctx.reply(
        ok(
            f"Команда <code>{PREFIX}{name}</code>\n"
            f"Теперь доступ: <b>{ACCESS_LABELS[level]}</b>"
        )
    )


@command(
    "вкл",
    "Включает указанную команду в этой группе.",
    ",вкл <команда>",
    "Управление",
    "owner",
)
def cmd_enable(ctx: CommandContext, args: list[str]):
    if not args:
        ctx.reply(error("Укажите команду. Например: <code>,вкл мут</code>"))
        return
    name = normalize_command(args[0])
    if name not in commands_by_name():
        ctx.reply(error("Такой команды нет."))
        return
    ctx.db.set_command_config(ctx.chat_id, name, enabled=True)
    ctx.db.audit(ctx.chat_id, ctx.user.id, "command_enable", None, name)
    ctx.reply(ok(f"<code>{PREFIX}{name}</code> включена."))


@command(
    "выкл",
    "Выключает указанную команду в этой группе.",
    ",выкл <команда>",
    "Управление",
    "owner",
)
def cmd_disable(ctx: CommandContext, args: list[str]):
    if not args:
        ctx.reply(error("Укажите команду. Например: <code>,выкл кик</code>"))
        return
    name = normalize_command(args[0])
    if name not in commands_by_name():
        ctx.reply(error("Такой команды нет."))
        return
    ctx.db.set_command_config(ctx.chat_id, name, enabled=False)
    ctx.db.audit(ctx.chat_id, ctx.user.id, "command_disable", None, name)
    ctx.reply(ok(f"<code>{PREFIX}{name}</code> выключена."))


@command(
    "фильтр",
    "Добавляет или удаляет слово из текстового фильтра.",
    ",фильтр <добавить|удалить> <слово>",
    "Управление",
    "admin",
)
def cmd_filter(ctx: CommandContext, args: list[str]):
    if len(args) < 2:
        ctx.reply(
            info(
                "<code>,фильтр добавить казино</code>\n"
                "<code>,фильтр удалить казино</code>\n"
                "<code>,фильтр список</code>"
            )
        )
        return

    action = args[0].lower()
    if action == "список":
        words = ctx.db.get_filters(ctx.chat_id)
        ctx.reply(
            card(
                "ФИЛЬТР",
                "Список пуст." if not words else "\n".join(f"• <code>{w}</code>" for w in words),
                "🛡",
            )
        )
        return

    word = " ".join(args[1:]).strip()
    if action in {"добавить", "add"}:
        if ctx.db.add_filter(ctx.chat_id, word):
            ctx.db.audit(ctx.chat_id, ctx.user.id, "filter_add", None, word)
            ctx.reply(ok(f"Фильтр добавлен: <code>{word}</code>"))
        else:
            ctx.reply(info("Такой фильтр уже существует."))
    elif action in {"удалить", "удалить", "remove"}:
        if ctx.db.remove_filter(ctx.chat_id, word):
            ctx.db.audit(ctx.chat_id, ctx.user.id, "filter_remove", None, word)
            ctx.reply(ok(f"Фильтр удалён: <code>{word}</code>"))
        else:
            ctx.reply(info("Такого фильтра нет."))
    else:
        ctx.reply(error("Действие должно быть: добавить, удалить или список."))


@command(
    "роли",
    "Показывает пользователей с дополнительными ролями бота.",
    ",роли",
    "Управление",
    "admin",
)
def cmd_roles(ctx: CommandContext, args: list[str]):
    rows = ctx.db.list_roles(ctx.chat_id)
    if not rows:
        ctx.reply(info("Дополнительных ролей нет. Telegram-администраторы определяются автоматически."))
        return

    lines = []
    for row in rows:
        lines.append(
            f"• <code>{row['user_id']}</code> — {ACCESS_LABELS.get(row['role'], row['role'])}"
        )
    ctx.reply(card("РОЛИ", "\n".join(lines), "♙"))


@command(
    "роль",
    "Назначает или снимает дополнительную роль. Работает ответом.",
    ",роль <модератор|старший|снять> — ответом на сообщение",
    "Управление",
    "owner",
)
def cmd_role(ctx: CommandContext, args: list[str]):
    target_id, target = ctx.target_id_and_user()
    if not target:
        ctx.reply(error("Ответьте на сообщение пользователя."))
        return

    if target_id == ctx.user.id:
        ctx.reply(error("Нельзя менять собственную роль этой командой."))
        return

    value = args[0].lower() if args else ""
    mapping = {
        "модератор": "moderator",
        "модер": "moderator",
        "старший": "senior_mod",
        "старший_модератор": "senior_mod",
        "снять": None,
        "убрать": None,
    }
    if value not in mapping:
        ctx.reply(
            info(
                "<code>,роль модератор</code>\n"
                "<code>,роль старший</code>\n"
                "<code>,роль снять</code>\n\n"
                "Командой нельзя вручную выдавать роль владельца/администратора."
            )
        )
        return

    role = mapping[value]
    if role is not None and ACCESS_LEVELS[role] >= ACCESS_LEVELS["admin"]:
        ctx.reply(error("Эта роль слишком высокая для ручного назначения."))
        return

    ctx.db.set_role(ctx.chat_id, target_id, role)
    ctx.db.audit(
        ctx.chat_id, ctx.user.id, "set_role", target_id, role or "removed"
    )
    role_text = "роль снята" if role is None else f"роль: {ACCESS_LABELS[role]}"
    ctx.reply(ok(f"{mention(target_id, user_label(target))}\n{role_text}."))


@command(
    "логи",
    "Показывает последние действия бота.",
    ",логи [количество]",
    "Управление",
    "admin",
)
def cmd_logs(ctx: CommandContext, args: list[str]):
    try:
        limit = max(1, min(int(args[0]), 20)) if args else 10
    except ValueError:
        limit = 10

    rows = ctx.db.recent_audit(ctx.chat_id, limit)
    if not rows:
        ctx.reply(info("Журнал действий пуст."))
        return

    lines = []
    for row in rows:
        target = f" → <code>{row['target_id']}</code>" if row["target_id"] else ""
        details = f" — {row['details']}" if row["details"] else ""
        lines.append(
            f"• <b>{row['action']}</b>{target}{details}"
        )

    ctx.reply(card("ЖУРНАЛ", "\n".join(lines), "▣"))


def setup_callbacks(bot, db: Database):
    @bot.callback_query_handler(func=lambda call: call.data.startswith("commands:"))
    def commands_callback(call):
        try:
            bot.answer_callback_query(call.id)
            category = call.data.split(":", 1)[1]
            if category == "all":
                category_key = "all"
                title = "КОМАНДЫ"
            else:
                category_key = category
                title = f"КОМАНДЫ · {category.upper()}"

            # База команд здесь только для отображения; права текущего пользователя
            # по отдельным командам всё равно проверяются при запуске.
            chat_id = call.message.chat.id
            parts = []
            current_category = None
            category_order = ["Основное", "Модерация", "Развлечения", "Игры", "Управление", "Автомод"]

            if category_key == "all":
                selected = [c for cat in category_order for c in COMMANDS if c.category == cat]
            else:
                selected = [c for c in COMMANDS if c.category == category_key]

            for c in selected:
                cfg = db.get_command_config(chat_id, c.name)
                if not cfg["enabled"]:
                    continue
                if c.category != current_category:
                    current_category = c.category
                    parts.append(f"<b>{current_category.upper()}</b>")
                line = f"<code>{PREFIX}{c.name}</code> — {c.description}"
                if c.example:
                    line += f"\n   <i>Пример:</i> <code>{c.example}</code>"
                parts.append(line)

            body = "\n".join(parts) or "В этой категории нет доступных включённых команд."

            bot.edit_message_text(
                chat_id=chat_id,
                message_id=call.message.message_id,
                text=card(title, body, "⌘"),
                parse_mode="HTML",
                reply_markup=command_category_keyboard(),
                disable_web_page_preview=True,
            )
        except Exception:
            pass

    @bot.callback_query_handler(func=lambda call: call.data.startswith("help:"))
    def help_callback(call):
        try:
            bot.answer_callback_query(call.id)
            category = call.data.split(":", 1)[1]
            cmds = COMMANDS
            if category == "moderation":
                cmds = [c for c in cmds if c.category == "Модерация"]
            elif category == "management":
                cmds = [c for c in cmds if c.category == "Управление"]
            elif category == "automod":
                cmds = [c for c in cmds if c.name in {"автомод", "фильтр", "настройки"}]
            body = "\n".join(
                f"<code>{PREFIX}{c.name}</code> — {c.description}"
                for c in cmds
            )
            if category == "all":
                body = "\n".join(
                    f"<code>{PREFIX}{c.name}</code> — {c.description}"
                    for c in COMMANDS
                )
            bot.edit_message_text(
                chat_id=call.message.chat.id,
                message_id=call.message.message_id,
                text=card("СПРАВКА", body, "❔"),
                parse_mode="HTML",
                reply_markup=help_keyboard(),
            )
        except Exception:
            pass


def dispatch(bot, db: Database, message: types.Message) -> bool:
    if message.chat.type not in {"group", "supergroup"}:
        if message.text and (
            message.text.startswith(PREFIX) or message.text.startswith("/")
        ):
            try:
                bot.send_message(
                    message.chat.id,
                    error("Я работаю только в группах."),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        return False

    text = message.text or message.caption or ""
    if not text:
        return False

    command_text = text.strip()
    if command_text.startswith(PREFIX):
        raw = command_text.split(maxsplit=1)
        name = normalize_command(raw[0])
        args = raw[1].split() if len(raw) > 1 else []
        command_obj = commands_by_name().get(name)
        if not command_obj:
            return False

        ctx = CommandContext(bot, db, message)
        if not require_access(ctx, command_obj.name):
            return True

        try:
            command_obj.handler(ctx, args)
        except Exception as exc:
            print(f"[COMMAND ERROR] {name}: {exc}")
            try:
                ctx.reply(error("Во время выполнения команды произошла ошибка. Проверьте консоль бота."))
            except Exception:
                pass
        return True

    # Slash-алиасы для меню Telegram.
    if command_text.startswith("/"):
        alias = command_text.split()[0].lower().split("@", 1)[0]
        alias_map = {
            "/help": "помощь",
            "/info": "инфо",
            "/rules": "правила",
            "/mute": "мут",
            "/warn": "варн",
            "/ban": "бан",
            "/kick": "кик",
            "/unmute": "размут",
            "/status": "статус",
            "/commands": "команды",
        }
        name = alias_map.get(alias)
        if not name:
            return False
        command_obj = commands_by_name().get(name)
        if not command_obj:
            return False

        args = command_text.split()[1:]
        ctx = CommandContext(bot, db, message)
        if not require_access(ctx, command_obj.name):
            return True

        try:
            command_obj.handler(ctx, args)
        except Exception as exc:
            print(f"[SLASH ERROR] {name}: {exc}")
        return True

    return False
