from __future__ import annotations

import os
import sys
import threading
import traceback
from flask import Flask
import telebot
from telebot import types

from automod import AutoModeration
from commands import command_rows, dispatch, setup_callbacks
from config import BOT_NAME, BOT_TOKEN, PARSE_MODE, TELEGRAM_COMMANDS, VERSION
from database import Database

# --- Flask Server for Keep-Alive on Render ---
app = Flask(__name__)


@app.route("/")
def home():
    return "Bot is alive!"


def run_web_server():
    # Render передает порт через переменную PORT (по умолчанию 10000)
    port = int(os.environ.get("PORT", 10000))
    # use_reloader=False обязателен при запуске Flask в отдельном потоке
    app.run(host="0.0.0.0", port=port, use_reloader=False)


# ---------------------------------------------


def build_bot() -> telebot.TeleBot:
    if not BOT_TOKEN:
        raise RuntimeError(
            "Не найден BOT_TOKEN.\n"
            "Создайте переменную окружения BOT_TOKEN с новым токеном от BotFather."
        )

    bot = telebot.TeleBot(BOT_TOKEN, parse_mode=PARSE_MODE)
    return bot


bot = build_bot()
db = Database()
automod = AutoModeration(db)

# Заполняем реестр команд в SQLite.
db.seed_commands(command_rows())
setup_callbacks(bot, db)


def ensure_group(message: types.Message):
    if message.chat.type in {"group", "supergroup"}:
        db.ensure_group(message.chat.id, message.chat.title or "")


@bot.message_handler(content_types=["new_chat_members"])
def welcome(message: types.Message):
    ensure_group(message)
    if not db.get_setting(message.chat.id, "welcome_enabled", True):
        return

    for user in message.new_chat_members:
        if user.is_bot:
            continue
        text = (
            f"• <b>ДОБРО ПОЖАЛОВАТЬ</b>\n\n"
            f"{user.first_name}, добро пожаловать в группу.\n"
            f"Основные команды бота: <code>,помощь</code>"
        )
        try:
            bot.reply_to(
                message,
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except Exception:
            pass


@bot.message_handler(
    content_types=[
        "text",
        "photo",
        "video",
        "document",
        "animation",
        "sticker",
        "voice",
        "audio",
        "video_note",
    ]
)
def all_messages(message: types.Message):
    ensure_group(message)

    # Сначала команды, потом автомодерация.
    if dispatch(bot, db, message):
        return

    try:
        automod.process(bot, message)
    except Exception:
        traceback.print_exc()


@bot.my_chat_member_handler()
def chat_status(message: types.ChatMemberUpdated):
    # Когда бота добавляют/повышают в группе — просто фиксируем группу.
    try:
        if message.chat.type in {"group", "supergroup"}:
            db.ensure_group(message.chat.id, message.chat.title or "")
    except Exception:
        pass


def register_telegram_menu():
    """
    Telegram умеет показывать подсказки только для slash-команд.
    Основные команды бота всё равно работают через ','.
    """
    try:
        commands = [
            types.BotCommand(command, description)
            for command, description in TELEGRAM_COMMANDS
        ]
        bot.set_my_commands(commands)
    except Exception as exc:
        print(f"[MENU] Не удалось установить меню: {exc}")


def main():
    print("=" * 50)
    print(f"{BOT_NAME} v{VERSION}")
    print("Запуск Telegram-бота")
    print("=" * 50)

    # Запускаем Flask в отдельном фоновом потоке
    web_thread = threading.Thread(target=run_web_server, daemon=True)
    web_thread.start()

    register_telegram_menu()

    # Убираем старый webhook.
    try:
        print("Удаляем webhook...")
        bot.remove_webhook()
    except Exception:
        pass

    print("Бот запущен.")
    print("Основной префикс: ,")
    print("Подсказки: ,помощь")
    print("Slash-меню Telegram: /help, /info, /rules и др.")
    print("Ожидание сообщений...")

    bot.infinity_polling(
        skip_pending=True,
        allowed_updates=[
            "message",
            "callback_query",
            "my_chat_member",
        ],
        timeout=30,
        long_polling_timeout=30,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nБот остановлен пользователем.")
    except Exception as exc:
        print(f"\nКРИТИЧЕСКАЯ ОШИБКА: {exc}")
        traceback.print_exc()
        sys.exit(1)