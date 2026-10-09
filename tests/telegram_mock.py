"""A fake Telegram Bot API session: records every request and returns well-formed results."""

from __future__ import annotations

import itertools
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    DeleteMessage,
    EditMessageReplyMarkup,
    EditMessageText,
    GetChatMember,
    GetFile,
    GetMe,
    SendDocument,
    SendMessage,
    SendPoll,
    StopPoll,
    TelegramMethod,
)
from aiogram.types import (
    Chat,
    ChatMemberLeft,
    ChatMemberMember,
    Document,
    File,
    Message,
    Poll,
    PollOption,
    Update,
    User,
)

_ids = itertools.count(1000)


class RecordingSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod] = []
        self.member_ids: set[int] = set()  # users getChatMember reports as members of ANY group
        self.members_by_chat: dict[int, set[int]] = {}  # per-group membership (checked first)
        self.forbidden_chats: set[int] = set()  # chats where the bot may not write
        self.files: dict[str, bytes] = {}  # file_id -> content served to bot.download()

    async def make_request(self, bot: Bot, method: TelegramMethod, timeout: int | None = None) -> Any:
        self.requests.append(method)
        now = datetime.now(UTC)
        if isinstance(method, GetMe):
            return User(id=42, is_bot=True, first_name="Court Bot", username="court_test_bot")
        if isinstance(method, SendMessage) and int(method.chat_id) in self.forbidden_chats:
            from aiogram.exceptions import TelegramForbiddenError

            raise TelegramForbiddenError(method=method, message="Forbidden: bot is not a member of the chat")
        if isinstance(method, (SendMessage, SendDocument)):
            chat_id = int(method.chat_id)
            return Message(
                message_id=next(_ids),
                date=now,
                chat=Chat(
                    id=chat_id, type="private" if chat_id > 0 else "supergroup", title=None if chat_id > 0 else "Sud"
                ),
                text=getattr(method, "text", None) or "",
                from_user=User(id=42, is_bot=True, first_name="Court Bot"),
            )
        if isinstance(method, SendPoll):
            chat_id = int(method.chat_id)
            return Message(
                message_id=next(_ids),
                date=now,
                chat=Chat(id=chat_id, type="private"),
                from_user=User(id=42, is_bot=True, first_name="Court Bot"),
                poll=_poll(method, str(next(_ids))),
            )
        if isinstance(method, StopPoll):
            return Poll(id="stopped", question="q", options=[PollOption(persistent_id="0", text="x", voter_count=0)],
                        total_voter_count=0, is_closed=True, is_anonymous=False, type="regular",
                        allows_multiple_answers=False, allows_revoting=False, members_only=False)  # fmt: skip
        if isinstance(method, EditMessageText):
            return Message(
                message_id=method.message_id or 1,
                date=now,
                chat=Chat(id=int(method.chat_id or 1), type="private"),
                text=method.text,
            )
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id=f"u{method.file_id}", file_path=f"docs/{method.file_id}")
        if isinstance(method, GetChatMember):
            member_user = User(id=int(method.user_id), is_bot=False, first_name="x")
            chat_members = self.members_by_chat.get(int(method.chat_id))
            if chat_members is not None:
                is_member = int(method.user_id) in chat_members
            else:
                is_member = int(method.user_id) in self.member_ids
            if is_member:
                return ChatMemberMember(user=member_user)
            return ChatMemberLeft(user=member_user)
        if isinstance(method, (AnswerCallbackQuery, EditMessageReplyMarkup, DeleteMessage)):
            return True
        return True

    async def close(self) -> None:
        return None

    async def stream_content(self, url: str, *args, **kwargs):
        yield self.files[url.rsplit("/", 1)[-1]]

    def texts(self) -> list[str]:
        return [m.text for m in self.requests if isinstance(m, (SendMessage, EditMessageText))]

    def sent_to(self, chat_id: int) -> list[SendMessage]:
        return [m for m in self.requests if isinstance(m, SendMessage) and int(m.chat_id) == chat_id]

    def polls(self) -> list[SendPoll]:
        return [m for m in self.requests if isinstance(m, SendPoll)]

    def alerts(self) -> list[str]:
        return [m.text or "" for m in self.requests if isinstance(m, AnswerCallbackQuery)]

    def last_markup(self):
        for m in reversed(self.requests):
            if isinstance(m, (SendMessage, EditMessageText)) and m.reply_markup is not None:
                return m.reply_markup
        return None

    def clear(self) -> None:
        self.requests.clear()


_update_ids = itertools.count(1)
_poll_ids: dict[int, str] = {}  # id(SendPoll request) -> poll id given to it


def _poll(method: SendPoll, poll_id: str) -> Poll:
    _poll_ids[id(method)] = poll_id
    return Poll(
        id=poll_id,
        question=method.question,
        options=[
            PollOption(persistent_id=str(i), text=o if isinstance(o, str) else o.text, voter_count=0)
            for i, o in enumerate(method.options)
        ],
        total_voter_count=0,
        is_closed=False,
        is_anonymous=False,
        type=method.type or "regular",
        allows_multiple_answers=False,
        allows_revoting=bool(method.allows_revoting),
        members_only=False,
    )


def poll_id_of(method: SendPoll) -> str:
    return _poll_ids[id(method)]


def poll_answer_update(user_id: int, poll_id: str, option_ids: list[int], first_name: str = "Ali") -> Update:
    from aiogram.types import PollAnswer

    user = User(id=user_id, is_bot=False, first_name=first_name)
    return Update(
        update_id=next(_update_ids),
        poll_answer=PollAnswer(
            poll_id=poll_id, user=user, option_ids=option_ids, option_persistent_ids=[str(i) for i in option_ids]
        ),
    )


def message_update(
    user_id: int, text: str, first_name: str = "Ali", chat_type: str = "private", chat_id: int | None = None
) -> Update:
    user = User(id=user_id, is_bot=False, first_name=first_name)
    chat = Chat(id=chat_id or user_id, type=chat_type, title="Group" if chat_type != "private" else None)
    return Update(
        update_id=next(_update_ids),
        message=Message(message_id=next(_ids), date=datetime.now(UTC), chat=chat, from_user=user, text=text),
    )


def callback_update(user_id: int, data: str, first_name: str = "Ali", chat_id: int | None = None) -> Update:
    from aiogram.types import CallbackQuery

    user = User(id=user_id, is_bot=False, first_name=first_name)
    chat = Chat(id=chat_id, type="supergroup", title="Sud") if chat_id else Chat(id=user_id, type="private")
    message = Message(
        message_id=next(_ids),
        date=datetime.now(UTC),
        chat=chat,
        from_user=User(id=42, is_bot=True, first_name="Court Bot"),
        text="previous",
    )
    return Update(
        update_id=next(_update_ids),
        callback_query=CallbackQuery(
            id=str(next(_ids)), from_user=user, chat_instance="ci", message=message, data=data
        ),
    )


def document_update(user_id: int, file_id: str, file_name: str, size: int, mime: str = "text/plain") -> Update:
    user = User(id=user_id, is_bot=False, first_name="Admin")
    return Update(
        update_id=next(_update_ids),
        message=Message(
            message_id=next(_ids),
            date=datetime.now(UTC),
            chat=Chat(id=user_id, type="private"),
            from_user=user,
            document=Document(
                file_id=file_id, file_unique_id=f"u{file_id}", file_name=file_name, file_size=size, mime_type=mime
            ),
        ),
    )
