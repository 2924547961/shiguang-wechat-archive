"""Service contracts own the behavior; vendor code remains an adapter detail."""
from __future__ import annotations

from pathlib import Path
from typing import Iterator

from .backends import DatabaseBackend, GuiBackend
from .config import AppConfig, FeatureStatus
from .errors import FeatureUnavailableError, MediaUnavailableError, SendStatusUnknown
from .models.message import Message, from_db_row
from .models.session import Session
from .response import WxResponse


class ConversationService:
    def __init__(self, config: AppConfig, database: DatabaseBackend | None = None,
                 gui: GuiBackend | None = None):
        self.config = config
        self.database = database or DatabaseBackend(config)
        self.gui = gui or GuiBackend(config)
        self._media = None

    def resolve(self, target: str) -> tuple[str, str]:
        return self.database.resolve(target)

    def sessions(self, limit: int = 100) -> list[Session]:
        db = self.database.db
        names = db.nickname_map()
        return [Session(username=r["username"], name=names.get(r["username"], r["username"]),
                        unread=r.get("unread") or 0, summary=r.get("summary") or "",
                        last_time=r.get("last_time"), info=dict(r))
                for r in db.get_sessions(limit=limit)]

    def messages(self, target: str, limit: int = 20, offset: int = 0) -> list[Message]:
        ident, name = self.resolve(target)
        rows = self.database.messages(ident, limit, offset)
        return [from_db_row(r, account_id=self.database.account_id,
                            chat_id=ident, chat_name=name, actions=self) for r in rows]

    def history(self, target: str, page_size: int = 200) -> Iterator[Message]:
        offset = 0
        while True:
            batch = self.messages(target, page_size, offset)
            if not batch:
                return
            yield from batch
            offset += len(batch)
            if len(batch) < page_size:
                return

    def open_chat(self, target: str, exact: bool = True) -> bool:
        _, name = self.resolve(target)
        return self.gui.open_chat(name, exact=exact)

    @staticmethod
    def _checked_send(response):
        if not response and "已操作发送" in str(response.get("message", "")):
            raise SendStatusUnknown(str(response.get("message")))
        return WxResponse(status=response.get("status", "错误"),
                          message=response.get("message", ""), data=response.get("data"))

    def send_text(self, target: str, text: str):
        if not text:
            raise ValueError("消息不能为空")
        _, name = self.resolve(target)
        return self._checked_send(self.gui.send_text(name, text))

    def at_member(self, target: str, member: str, text: str):
        ident, name = self.resolve(target)
        if not ident.endswith("@chatroom"):
            raise ValueError("@成员仅适用于群聊")
        if not member:
            raise ValueError("成员不能为空")
        return self._checked_send(self.gui.at_member(name, member, text))

    def at_all(self, target: str, text: str):
        return self.at_member(target, "所有人", text)

    def send_file(self, target: str, path: str, *, image: bool = False):
        if not Path(path).is_file():
            raise FileNotFoundError(path)
        _, name = self.resolve(target)
        return self._checked_send(self.gui.send_file(name, path, image=image))

    def download(self, message: Message, original: bool = False):
        if message.local_id is None:
            raise MediaUnavailableError("消息缺少本地 ID")
        if self._media is None:
            from ._vendor.wechatauto.media import MediaDownloader
            self._media = MediaDownloader(self.database.db)
        if original and message.type == "image":
            path = self._media.download_image_original(message.chat_id, message.local_id)
        else:
            path = self._media.download_media(message.chat_id, message.local_id)
        if not path:
            raise MediaUnavailableError(f"媒体不可用：{message.hash}")
        return path

    def transcribe(self, message: Message):
        raise FeatureUnavailableError("未配置语音转文字提供方")

    def reply(self, message: Message, text: str):
        latest = self.messages(message.chat_id, 1)
        if not latest or latest[0].hash != message.hash:
            raise FeatureUnavailableError("仅支持回复当前会话的最新消息")
        _, name = self.resolve(message.chat_id)
        return self._checked_send(self.gui.reply_last(name, text))

    def quote(self, message: Message, text: str):
        if message.type != "text" or not message.content:
            raise FeatureUnavailableError("仅支持引用当前可见的文本消息")
        _, name = self.resolve(message.chat_id)
        # The source backend searches visible OCR text; duplicate contents are ambiguous.
        visible = self.messages(message.chat_id, 50)
        if sum(m.content == message.content for m in visible) != 1:
            raise FeatureUnavailableError("可见消息中存在同文或目标不在最近消息，无法唯一引用")
        return self._checked_send(self.gui.quote(name, text, message.content))

    def forward(self, message: Message, targets, note: str | None):
        raise FeatureUnavailableError("转发消息的 UI 定位尚未验证")

    def sender_info(self, message: Message):
        if not message.sender_wxid:
            raise FeatureUnavailableError("消息未提供发送者 ID")
        rows = self.database.db.search_contact(message.sender_wxid)
        return next((r for r in rows if r.get("username") == message.sender_wxid), None)

    def chat_info(self, message: Message):
        return {"chat_id": message.chat_id, "chat_name": message.chat_name,
                "chat_type": message.chat_type}


class ContactService:
    def __init__(self, database: DatabaseBackend): self.database = database
    def search(self, keyword: str) -> list[dict]: return self.database.db.search_contact(keyword)
    def get(self, target: str) -> dict | None:
        ident, _ = self.database.resolve(target)
        return next((r for r in self.search(ident) if r.get("username") == ident), None)
    def add(self, *args, **kwargs): raise FeatureUnavailableError("添加好友尚未验证")
    def edit(self, *args, **kwargs): raise FeatureUnavailableError("修改好友尚未验证")


class GroupService:
    def __init__(self, database: DatabaseBackend): self.database = database
    def list(self) -> list[dict]: return self.database.db.get_groups()
    def members(self, target: str) -> list[dict]:
        ident, _ = self.database.resolve(target)
        if not ident.endswith("@chatroom"):
            raise ValueError("目标不是群聊")
        return self.database.db.get_group_members(ident)
    def create(self, *args, **kwargs): raise FeatureUnavailableError("创建群聊尚未验证")
    def add_members(self, *args, **kwargs): raise FeatureUnavailableError("添加群成员尚未验证")


class MomentService:
    def __init__(self, database: DatabaseBackend):
        self.database = database
        self._reader = None
    @property
    def reader(self):
        if self._reader is None:
            from ._vendor.wechatauto.moment import MomentDB
            self._reader = MomentDB(self.database.db)
        return self._reader
    def list(self, limit: int = 20, offset: int = 0) -> list[dict]:
        return self.reader.get_moments(limit=limit, offset=offset)
    def interactions(self, limit: int = 50) -> list[dict]:
        return self.reader.get_interactions(limit=limit)
    def download_media(self, feed: dict, save_dir: str | None = None):
        return self.reader.download_moment_media(feed, save_dir=save_dir)
    def publish(self, *args, **kwargs): raise FeatureUnavailableError("发布朋友圈尚未验证")
    def like(self, *args, **kwargs): raise FeatureUnavailableError("朋友圈点赞尚未验证")
    def comment(self, *args, **kwargs): raise FeatureUnavailableError("朋友圈评论尚未验证")


FEATURES = {
    "uia.inspect": FeatureStatus.SUPPORTED,
    "account.info": FeatureStatus.SUPPORTED,
    "sessions": FeatureStatus.SUPPORTED,
    "history": FeatureStatus.SUPPORTED,
    "listener": FeatureStatus.EXPERIMENTAL,
    "next_new_message": FeatureStatus.EXPERIMENTAL,
    "contacts.read": FeatureStatus.SUPPORTED,
    "groups.read": FeatureStatus.EXPERIMENTAL,
    "moments.read": FeatureStatus.EXPERIMENTAL,
    "message.download": FeatureStatus.EXPERIMENTAL,
    "chat.send_text": FeatureStatus.EXPERIMENTAL,
    "chat.send_file": FeatureStatus.EXPERIMENTAL,
    "chat.at_member": FeatureStatus.EXPERIMENTAL,
    "message.quote": FeatureStatus.EXPERIMENTAL,
    "message.reply_last": FeatureStatus.EXPERIMENTAL,
    "contacts.mutate": FeatureStatus.UNAVAILABLE,
    "groups.mutate": FeatureStatus.UNAVAILABLE,
    "moments.mutate": FeatureStatus.UNAVAILABLE,
    "chat.send_audio": FeatureStatus.UNAVAILABLE,
    "chat.subwindow": FeatureStatus.UNAVAILABLE,
}
