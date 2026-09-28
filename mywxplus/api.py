"""Public facade and Plus-style compatibility aliases."""
from __future__ import annotations

from pathlib import Path
import tempfile
import time

from .config import AppConfig
from .errors import FeatureUnavailableError
from .listener import ListenerService
from .services import ContactService, ConversationService, FEATURES, GroupService, MomentService


class Chat:
    def __init__(self, service: ConversationService, target: str):
        self.service = service
        self.chat_id, self.name = service.resolve(target)
        self._last_seq: int | None = None

    def info(self):
        return {"chat_id": self.chat_id, "chat_name": self.name,
                "chat_type": "group" if self.chat_id.endswith("@chatroom") else "friend"}

    ChatInfo = info

    def Show(self):
        if not self.service.open_chat(self.chat_id):
            raise FeatureUnavailableError("未能显示指定聊天")

    def Close(self):
        raise FeatureUnavailableError("当前仅支持主窗口聊天，不支持关闭独立子窗口")

    def GetDialog(self, wait: int = 3):
        raise FeatureUnavailableError("对话框对象尚未实现")

    def EditFriendInfo(self, *args, **kwargs):
        raise FeatureUnavailableError("好友资料编辑尚未验证")

    def messages(self, limit: int = 20, offset: int = 0):
        return self.service.messages(self.chat_id, limit, offset)

    GetAllMessage = messages

    def get_new_messages(self, max_backlog: int = 5000):
        latest = self.service.database.messages(self.chat_id, 1)
        if self._last_seq is None:
            self._last_seq = int(latest[0]["sort_seq"]) if latest else 0
            return []
        if max_backlog <= 0:
            raise ValueError("max_backlog 必须大于 0")
        from .models.message import from_db_row
        out = []
        while len(out) < max_backlog:
            batch_limit = min(200, max_backlog - len(out))
            rows = self.service.database.new_messages(
                self.chat_id, self._last_seq, batch_limit)
            if not rows:
                break
            out.extend(from_db_row(row, account_id=self.service.database.account_id,
                                   chat_id=self.chat_id, chat_name=self.name,
                                   actions=self.service) for row in rows)
            self._last_seq = int(rows[-1]["sort_seq"])
            if len(rows) < batch_limit:
                break
        return out

    GetNewMessage = get_new_messages

    def history(self, page_size: int = 200):
        return self.service.history(self.chat_id, page_size)

    def send_text(self, text: str):
        return self.service.send_text(self.chat_id, text)

    def SendMsg(self, msg: str, who: str | None = None, clear: bool = True,
                at: str | list[str] | None = None, exact: bool = False):
        if clear is not True:
            raise FeatureUnavailableError("clear=False 的草稿保留语义尚未实现")
        if at is not None:
            if isinstance(at, list):
                if len(at) != 1:
                    raise FeatureUnavailableError("多成员 @ 尚未实现")
                at = at[0]
            return self.service.at_member(who or self.chat_id, at, msg)
        return self.service.send_text(who or self.chat_id, msg)

    def send_file(self, path: str):
        return self.service.send_file(self.chat_id, path)

    def send_image(self, path: str):
        return self.service.send_file(self.chat_id, path, image=True)

    def SendFiles(self, filepath, who: str | None = None, exact: bool = False):
        paths = filepath if isinstance(filepath, (list, tuple)) else [filepath]
        if not paths:
            raise ValueError("文件列表不能为空")
        return [self.service.send_file(who or self.chat_id, p) for p in paths] if len(paths) > 1 else self.service.send_file(who or self.chat_id, paths[0])

    def GetGroupMembers(self):
        if not self.chat_id.endswith("@chatroom"):
            return []
        return self.service.database.db.get_group_members(self.chat_id)

    def GetMessageById(self, msg_id):
        try:
            local_id = int(str(msg_id).removeprefix("db-"))
        except (TypeError, ValueError):
            return None
        row = self.service.database.db.get_message_row(self.chat_id, local_id)
        if not row:
            return None
        from .models.message import from_db_row
        return from_db_row(row, account_id=self.service.database.account_id,
                           chat_id=self.chat_id, chat_name=self.name, actions=self.service)

    def GetMessageByHash(self, msg_hash: str):
        return next((m for m in self.messages(200) if m.hash == msg_hash), None)

    def GetLastMessage(self):
        rows = self.messages(1)
        return rows[0] if rows else None

    def send_audio(self, *args, **kwargs):
        raise FeatureUnavailableError("语音发送尚未验证")

    SendAudio = send_audio

    def at_all(self, msg: str):
        return self.service.at_all(self.chat_id, msg)

    AtAll = at_all


class WeChat:
    def __init__(self, config: AppConfig | None = None, **config_kwargs):
        self.config = config or AppConfig(**config_kwargs)
        self.service = ConversationService(self.config)
        self.contacts = ContactService(self.service.database)
        self.groups = GroupService(self.service.database)
        self.moments = MomentService(self.service.database)
        state = (self.config.cache_dir or Path(tempfile.gettempdir()) / "mywxplus") / "listener.json"
        self.listener = ListenerService(self.service, state, self.config.poll_interval,
                                        chat_factory=self.chat)
        self._chat: Chat | None = None
        self._all_message_cursor: dict[str, int] | None = None
        self._all_message_session_time: dict[str, int] = {}

    @property
    def features(self): return dict(FEATURES)

    def inspect_ui_tree(self, max_nodes: int = 2000, max_depth: int = 40):
        from .diagnostics import inspect_ui_tree
        return inspect_ui_tree(max_nodes=max_nodes, max_depth=max_depth)

    def get_sessions(self, limit: int = 100): return self.service.sessions(limit)
    GetSession = get_sessions

    def open_chat(self, target: str, exact: bool = True,
                  force: bool = False, force_wait: float = 0.5) -> Chat:
        if force:
            raise FeatureUnavailableError("force=True 的无确认切换未实现")
        chat = Chat(self.service, target)
        if not self.service.open_chat(chat.chat_id, exact=exact):
            raise FeatureUnavailableError(f"无法在界面打开会话：{target}")
        self._chat = chat
        return chat

    ChatWith = open_chat

    def chat(self, target: str) -> Chat:
        return Chat(self.service, target)

    def history(self, target: str, page_size: int = 200):
        return self.service.history(target, page_size)

    def GetHistoryMessage(self, n: int, callback=None, interval: float = 0.2,
                          speed: int = 1, goback: bool = True,
                          timeout: float | None = None):
        if n <= 0:
            return []
        messages = self.service.messages(self._current().chat_id, n)
        if callback:
            for message in messages:
                callback(message)
        return messages

    def _current(self) -> Chat:
        if self._chat is None:
            raise FeatureUnavailableError("请先调用 ChatWith 选择会话")
        return self._chat

    def ChatInfo(self): return self._current().info()
    def SendMsg(self, msg: str, who: str | None = None, clear: bool = True,
                at: str | list[str] | None = None, exact: bool = False, max_retries: int = 3):
        if who:
            return self.chat(who).SendMsg(msg, clear=clear, at=at, exact=exact)
        return self._current().SendMsg(msg, clear=clear, at=at, exact=exact)

    def GetAllMessage(self, limit: int = 20): return self._current().messages(limit)
    def GetNewMessage(self, max_backlog: int = 5000): return self._current().get_new_messages(max_backlog)
    def GetMessageById(self, msg_id): return self._current().GetMessageById(msg_id)
    def GetMessageByHash(self, msg_hash: str): return self._current().GetMessageByHash(msg_hash)
    def GetLastMessage(self): return self._current().GetLastMessage()
    def SendFiles(self, filepath, who: str | None = None, exact: bool = False, max_retries: int = 3):
        return self.chat(who).SendFiles(filepath) if who else self._current().SendFiles(filepath)
    def AtAll(self, msg: str, who: str | None = None, exact: bool = False):
        return self.chat(who).at_all(msg) if who else self._current().at_all(msg)
    def GetMyInfo(self): return self.service.database.db.get_self_info()
    def IsOnline(self):
        raise FeatureUnavailableError("窗口存在无法证明账号网络在线；IsOnline 尚无可靠判据")

    def AddListenChat(self, who: str, callback, replay: bool = False): self.listener.add(who, callback, replay=replay)
    def RemoveListenChat(self, who: str, close_window: bool = True): self.listener.remove(who)
    def StartListening(self): self.listener.start()
    def StopListening(self, remove: bool = True):
        self.listener.stop()
        if remove:
            self.listener._callbacks.clear()

    def GetAllRecentGroups(self): return self.groups.list()
    def GetFriendDetails(self, who: str | None = None):
        if who is None or isinstance(who, int):
            raise FeatureUnavailableError("完整通讯录枚举尚未验证；请传入唯一联系人名称")
        return self.contacts.get(who)
    def GetGroupMembers(self): return self._current().GetGroupMembers()
    def SendAudio(self, *args, **kwargs):
        raise FeatureUnavailableError("语音发送尚未验证")
    def GetDialog(self, wait: int = 3): return self._current().GetDialog(wait)
    def Show(self): return self._current().Show()
    def Close(self): return self._current().Close()
    def Moments(self): return self.moments
    def GetMoments(self, limit: int = 20): return self.moments.list(limit)
    def PublishMoment(self, *args, **kwargs): return self.moments.publish(*args, **kwargs)

    def SwitchToChat(self):
        from ._vendor.wechatauto.uia_driver import WeChatUIA
        driver = WeChatUIA()
        if not driver.back_to_chat_tab():
            raise FeatureUnavailableError("UIA 未能确认切回聊天页")

    def SwitchToContact(self):
        raise FeatureUnavailableError("通讯录页导航控件尚未验证")

    def GetNewFriends(self, acceptable: bool = True):
        raise FeatureUnavailableError("好友申请列表尚未验证")

    def AddNewFriend(self, keywords: str, addmsg: str | None = None,
                     remark: str | None = None, tags: list[str] | None = None,
                     permission: str = "朋友圈"):
        return self.contacts.add(keywords, addmsg=addmsg, remark=remark,
                                 tags=tags, permission=permission)

    def EditFriendInfo(self, *args, **kwargs):
        return self.contacts.edit(*args, **kwargs)

    def GetTagContacts(self, tag: str, **kwargs):
        raise FeatureUnavailableError("标签联系人枚举尚未验证")

    def CreateGroup(self, contacts: list[str]):
        return self.groups.create(contacts)

    def SetGroupName(self, value: str):
        raise FeatureUnavailableError("群名称修改尚未验证")

    def SetGroupRemark(self, value: str):
        raise FeatureUnavailableError("群备注修改尚未验证")

    def SetGroupAnnouncement(self, value: str):
        raise FeatureUnavailableError("群公告修改尚未验证")

    def SetGroupMyNickname(self, value: str):
        raise FeatureUnavailableError("群昵称修改尚未验证")

    def SendUrlCard(self, url: str, friends, message: str | None = None, timeout: int = 10):
        raise FeatureUnavailableError("链接卡片发送尚未验证")

    def GetSubWindow(self, nickname: str):
        raise FeatureUnavailableError("独立聊天子窗口尚未验证")

    def GetAllSubWindow(self):
        raise FeatureUnavailableError("独立聊天子窗口尚未验证")

    def GetNextNewMessage(self, filter_mute: bool = False, callback=None,
                          timeout: float | None = None):
        if filter_mute:
            raise FeatureUnavailableError("会话免打扰状态无法从当前数据库字段可靠确认")
        if timeout is not None and timeout < 0:
            raise ValueError("timeout 不能为负")
        deadline = None if timeout is None else time.monotonic() + timeout
        database = self.service.database
        if self._all_message_cursor is None:
            self._all_message_cursor = {}
            for session in self.get_sessions(limit=10000):
                stamp = int(session.last_time or 0)
                self._all_message_session_time[session.username] = stamp
                self._all_message_cursor[session.username] = stamp * 1000
        from .models.message import from_db_row
        while True:
            result = {}
            for session in self.get_sessions(limit=10000):
                ident = session.username
                stamp = int(session.last_time or 0)
                if ident not in self._all_message_cursor:
                    self._all_message_cursor[ident] = stamp * 1000
                    self._all_message_session_time[ident] = stamp
                    continue
                if stamp <= self._all_message_session_time.get(ident, 0):
                    continue
                rows = database.new_messages(ident, self._all_message_cursor[ident], limit=200)
                self._all_message_session_time[ident] = stamp
                if not rows:
                    continue
                messages = [from_db_row(row, account_id=database.account_id,
                                        chat_id=ident, chat_name=session.name,
                                        actions=self.service) for row in rows]
                if callback:
                    for message in messages:
                        callback(message)
                self._all_message_cursor[ident] = int(rows[-1]["sort_seq"])
                result[ident] = messages
            if result:
                return result
            if deadline is not None and time.monotonic() >= deadline:
                return {}
            time.sleep(self.config.poll_interval)

    def KeepRunning(self):
        raise FeatureUnavailableError("阻塞式 KeepRunning 不适合库调用；请使用 listener.start()")

    def ShutDown(self):
        raise FeatureUnavailableError("进程关闭操作未实现")
