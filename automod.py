from __future__ import annotations

from email.mime import message, text
import re
import time
from collections import defaultdict, deque

from telebot import types

from config import PREFIX
from database import Database

URL_RE = re.compile(
    r"(https?://\S+|www\.\S+|t\.me/\S+|telegram\.me/\S+)",
    re.IGNORECASE,
)


class AutoModeration:
    """
    Лёгкая автомодерация без отдельного потока/сервера.
    Повторные сообщения и flood хранятся в RAM, настройки/фильтры — в SQLite.
    """

    def __init__(self, db: Database):
        self.db = db
        self.flood: dict[tuple[int, int], deque[float]] = defaultdict(deque)
        self.repeats: dict[tuple[int, int], deque[tuple[float, str]]] = defaultdict(deque)

    def is_group(self, message: types.Message) -> bool:
        return message.chat.type in {"group", "supergroup"}

    def _is_message_from_bypass_user(self, bot, message: types.Message) -> bool:
        user = message.from_user
        if not user:
            return True
        try:
            member = bot.get_chat_member(message.chat.id, user.id)
            return member.status in {"administrator", "creator"}
        except Exception:
            return False

    def _text(self, message: types.Message) -> str:
        return (message.text or message.caption or "").strip()

    def _signature(self, message: types.Message) -> str:
        text = self._text(message).lower()

        if text:
            normalized_text = re.sub(r"\s+", " ", text)
            return f"text:{normalized_text}"

        if message.animation and getattr(message.animation, "file_unique_id", None):
            return f"animation:{message.animation.file_unique_id}"

        if message.document and getattr(message.document, "file_unique_id", None):
            return f"document:{message.document.file_unique_id}"

        if message.photo:
            photo = message.photo[-1]
            if getattr(photo, "file_unique_id", None):
                return f"photo:{photo.file_unique_id}"

        if message.video and getattr(message.video, "file_unique_id", None):
            return f"video:{message.video.file_unique_id}"

        if message.sticker and getattr(message.sticker, "file_unique_id", None):
            return f"sticker:{message.sticker.file_unique_id}"

        return ""

    def _safe_delete(self, bot, message: types.Message) -> bool:
        try:
            bot.delete_message(message.chat.id, message.message_id)
            return True
        except Exception:
            return False

    def _notice(self, bot, message: types.Message, text: str) -> None:
        try:
            sent = bot.send_message(
                message.chat.id,
                text,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            seconds = int(self.db.get_setting(
                message.chat.id, "delete_notice_seconds", 8
            ) or 0)
            if seconds > 0:
                # Не блокируем обработчик на долгий срок.
                # Удаление уведомления выполняется отдельным daemon-thread.
                import threading

                def delete_later():
                    time.sleep(seconds)
                    try:
                        bot.delete_message(message.chat.id, sent.message_id)
                    except Exception:
                        pass

                threading.Thread(target=delete_later, daemon=True).start()
        except Exception:
            pass

    def process(self, bot, message: types.Message) -> bool:
        if not self.is_group(message) or not message.from_user:
            return False

        if self._is_message_from_bypass_user(bot, message):
            return False

        chat_id = message.chat.id
        user_id = message.from_user.id
        text = self._text(message)

        # Команды не должны попадать под фильтр текста.
        if text.startswith(PREFIX):
            return False

        # Ссылки
        if self.db.get_setting(chat_id, "links_enabled", True) and URL_RE.search(text):
            if self._safe_delete(bot, message):
                self.db.audit(chat_id, user_id, "automod_link_delete", user_id)
                self._notice(
                    bot,
                    message,
                    "🛡 <b>АВТОМОДЕРАЦИЯ</b>\n\n"
                    "Сообщение удалено: размещение ссылок запрещено.",
                )
            return True

        # Слова-фильтры
        filters = self.db.get_filters(chat_id)
        if self.db.get_setting(chat_id, "bad_words_enabled", True) and filters:
            lowered = text.lower()
            hit = next((word for word in filters if word in lowered), None)
            if hit:
                if self._safe_delete(bot, message):
                    self.db.audit(
                        chat_id, user_id, "automod_filter_delete", user_id, hit
                    )
                    self._notice(
                        bot,
                        message,
                        "🛡 <b>АВТОМОДЕРАЦИЯ</b>\n\n"
                        "Сообщение удалено: сработал текстовый фильтр.",
                    )
                return True

        # Повторы
        signature = self._signature(message)
        if signature and self.db.get_setting(chat_id, "repeat_enabled", True):
            key = (chat_id, user_id)
            queue = self.repeats[key]
            now = time.monotonic()
            while queue and now - queue[0][0] > 20:
                queue.popleft()

            queue.append((now, signature))
            repeat_count = int(self.db.get_setting(chat_id, "repeat_count", 3) or 3)
            same = sum(sig == signature for _, sig in queue)
            if same >= repeat_count:
                queue.clear()
                if self._safe_delete(bot, message):
                    self.db.audit(chat_id, user_id, "automod_repeat_delete", user_id)
                    self._notice(
                        bot,
                        message,
                        "🛡 <b>АВТОМОДЕРАЦИЯ</b>\n\n"
                        "Сообщение удалено: обнаружен повтор.",
                    )
                return True

        # Flood
        if self.db.get_setting(chat_id, "flood_enabled", True):
            key = (chat_id, user_id)
            queue = self.flood[key]
            now = time.monotonic()
            seconds = int(self.db.get_setting(chat_id, "flood_seconds", 8) or 8)
            limit = int(self.db.get_setting(chat_id, "flood_messages", 6) or 6)
            while queue and now - queue[0] > seconds:
                queue.popleft()
            queue.append(now)

            if len(queue) >= limit:
                queue.clear()
                try:
                    until = int(time.time()) + 60
                    bot.restrict_chat_member(
                        chat_id,
                        user_id,
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
                    self.db.audit(chat_id, user_id, "automod_flood_mute", user_id, "60s")
                    self._notice(
                        bot,
                        message,
                        "🛡 <b>АВТОМОДЕРАЦИЯ</b>\n\n"
                        "Временное ограничение: обнаружен флуд (60 сек.).",
                    )
                    return True
                except Exception:
                    pass

        return False
