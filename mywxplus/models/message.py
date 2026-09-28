"""Independent message objects, keyed by database identity rather than UI IDs."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from mywxplus.errors import FeatureUnavailableError


class MessageActions(Protocol):
    def reply(self, message: "Message", text: str): ...
    def quote(self, message: "Message", text: str): ...
    def forward(self, message: "Message", targets, note: str | None): ...
    def download(self, message: "Message", original: bool = False): ...
    def transcribe(self, message: "Message"): ...
    def sender_info(self, message: "Message"): ...
    def chat_info(self, message: "Message"): ...


@dataclass
class Message:
    account_id: str
    chat_id: str
    local_id: int | None = None
    server_id: str | None = None
    type: str = "other"
    attr: str = "other"
    sender: str | None = None
    sender_wxid: str | None = None
    content: str = ""
    timestamp: int | None = None
    chat_name: str | None = None
    chat_type: str | None = None
    is_self: bool = False
    raw: dict[str, Any] = field(default_factory=dict)
    _actions: MessageActions | None = field(default=None, repr=False, compare=False)

    @property
    def hash(self) -> str:
        # The compact history query omits server_id. Prefer identifiers present
        # in both compact and full DB rows so identity survives rehydration.
        parts = [self.account_id, self.chat_id, self.local_id,
                 self.raw.get("sort_seq"), self.timestamp,
                 self.sender_wxid or self.sender or "", self.type]
        if self.local_id is None:
            parts.append(self.server_id or "")
        return hashlib.sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()

    @property
    def id(self) -> str:
        return self.hash

    def _action(self) -> MessageActions:
        if self._actions is None:
            raise FeatureUnavailableError("Message is detached from a service")
        return self._actions

    def reply(self, text: str):
        return self._action().reply(self, text)

    def quote(self, text: str):
        return self._action().quote(self, text)

    def forward(self, targets, message: str | None = None):
        return self._action().forward(self, targets, message)

    def tickle(self):
        raise FeatureUnavailableError("Message-specific poke target is not verified")

    def click(self):
        raise FeatureUnavailableError("DB message has no persistent UI element to click")

    def download(self, original: bool = False):
        return self._action().download(self, original=original)

    def download_head_image(self):
        raise FeatureUnavailableError("Sender avatar download is not yet verified")

    def sender_info(self):
        return self._action().sender_info(self)

    def chat_info(self):
        return self._action().chat_info(self)


class SystemMessage(Message): pass
class TimeMessage(SystemMessage): pass
class HumanMessage(Message): pass
class SelfMessage(HumanMessage): pass
class FriendMessage(HumanMessage): pass
class TextMessage(Message): pass
class QuoteMessage(Message):
    quote_content: str | None = None
    quote_sender: str | None = None
    quote_type: str | None = None
    def download_quote_media(self):
        raise FeatureUnavailableError("Quoted media identity is not verified")
    download_quote_image = download_quote_media
class VoiceMessage(Message):
    def to_text(self):
        return self._action().transcribe(self)
class ImageMessage(Message):
    def ocr(self):
        raise FeatureUnavailableError("Image OCR provider is not configured")
class VideoMessage(Message): pass
class FileMessage(Message): pass
class LocationMessage(Message): pass
class LinkMessage(Message):
    def get_url(self):
        return self.raw.get("url") or None
class EmotionMessage(Message): pass
class MergeMessage(Message): pass
class PersonalCardMessage(Message): pass
class NoteMessage(Message):
    def get_content(self):
        raise FeatureUnavailableError("Structured note parsing is not yet verified")
    def save_files(self):
        raise FeatureUnavailableError("Structured note file export is not yet verified")
    def to_markdown(self):
        raise FeatureUnavailableError("Structured note parsing is not yet verified")
class OtherMessage(Message): pass


class SelfTextMessage(SelfMessage, TextMessage): pass
class FriendTextMessage(FriendMessage, TextMessage): pass
class SelfImageMessage(SelfMessage, ImageMessage): pass
class FriendImageMessage(FriendMessage, ImageMessage): pass
class SelfVoiceMessage(SelfMessage, VoiceMessage): pass
class FriendVoiceMessage(FriendMessage, VoiceMessage): pass
class SelfVideoMessage(SelfMessage, VideoMessage): pass
class FriendVideoMessage(FriendMessage, VideoMessage): pass
class SelfFileMessage(SelfMessage, FileMessage): pass
class FriendFileMessage(FriendMessage, FileMessage): pass


_TYPE_CLASS = {"text": TextMessage, "quote": QuoteMessage, "voice": VoiceMessage,
               "image": ImageMessage, "video": VideoMessage, "file": FileMessage,
               "location": LocationMessage, "link": LinkMessage, "emotion": EmotionMessage,
               "merge": MergeMessage, "personal_card": PersonalCardMessage,
               "note": NoteMessage, "other": OtherMessage}
_COMBINED = {("self", "text"): SelfTextMessage, ("friend", "text"): FriendTextMessage,
             ("self", "image"): SelfImageMessage, ("friend", "image"): FriendImageMessage,
             ("self", "voice"): SelfVoiceMessage, ("friend", "voice"): FriendVoiceMessage,
             ("self", "video"): SelfVideoMessage, ("friend", "video"): FriendVideoMessage,
             ("self", "file"): SelfFileMessage, ("friend", "file"): FriendFileMessage}

_DB_TYPE_NAMES = {"文本": "text", "图片": "image", "语音": "voice",
                  "视频": "video", "文件": "file", "文件/链接/卡片": "link",
                  "引用": "quote", "位置": "location", "表情": "emotion",
                  "系统": "system", "时间": "time", "名片": "personal_card",
                  "合并转发": "merge", "笔记": "note"}


def from_db_row(row: dict, *, account_id: str, chat_id: str, chat_name: str | None = None,
                actions: MessageActions | None = None) -> Message:
    db_type = row.get("type")
    if db_type is None and row.get("local_type") is not None:
        from mywxplus._vendor.wechatauto.db import WeChatDB
        db_type = WeChatDB._msg_type_name(row["local_type"])
    kind = _DB_TYPE_NAMES.get(str(db_type or "other"), str(db_type or "other").lower())
    is_self = row.get("sender_id") == 2
    attr = "system" if kind in ("time", "system") else "self" if is_self else "friend"
    if attr == "system":
        cls = TimeMessage if kind == "time" else SystemMessage
    else:
        cls = _COMBINED.get((attr, kind), _TYPE_CLASS.get(kind, OtherMessage))
    sender_wxid = (row.get("sender_username") or "") if not is_self else account_id
    content = row.get("content") or ""
    if isinstance(content, bytes):
        from mywxplus._vendor.wechatauto.db import WeChatDB
        content = WeChatDB._friendly_content(content, db_type)
    return cls(account_id=account_id, chat_id=chat_id,
               local_id=row.get("local_id"), server_id=row.get("server_id"),
               type=kind, attr=attr, sender=row.get("sender") or sender_wxid or None,
               sender_wxid=sender_wxid or None, content=str(content),
               timestamp=row.get("create_time"), chat_name=chat_name,
               chat_type="group" if chat_id.endswith("@chatroom") else "friend",
               is_self=is_self, raw=dict(row), _actions=actions)
