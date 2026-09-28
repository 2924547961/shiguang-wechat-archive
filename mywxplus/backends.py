"""Lazy adapters to the independently licensed Weixin 4.x implementation."""
from __future__ import annotations

from pathlib import Path

from .config import AppConfig
from .errors import AmbiguousTargetError, BackendError, FeatureUnavailableError


class DatabaseBackend:
    def __init__(self, config: AppConfig):
        self.config = config
        self._db = None

    @property
    def db(self):
        if self._db is None:
            try:
                from ._vendor.wechatauto.db import WeChatDB
                self._db = WeChatDB(
                    db_dir=str(self.config.db_dir) if self.config.db_dir else None,
                    workdir=str(self.config.cache_dir) if self.config.cache_dir else None,
                    account=self.config.account,
                )
            except Exception as exc:
                raise BackendError(f"无法打开微信本地数据库：{exc}") from exc
        return self._db

    @property
    def account_id(self) -> str:
        return self.db.wxid

    def resolve(self, target: str) -> tuple[str, str]:
        target = target.strip()
        if not target:
            raise ValueError("会话名称不能为空")
        if target in ("文件传输助手", "filehelper"):
            return "filehelper", "文件传输助手"
        rows = self.db.search_contact(target)
        rows += [dict(username=s["username"], nick_name=self.db.get_nickname(s["username"]), remark="")
                 for s in self.db.get_sessions() if target in (s["username"], self.db.get_nickname(s["username"]))]
        by_id = {r["username"]: r for r in rows if r.get("username") and
                 target in (r["username"], r.get("nick_name"), r.get("remark"))}
        if len(by_id) > 1:
            raise AmbiguousTargetError(f"多个会话匹配 {target!r}：{', '.join(sorted(by_id))}")
        if not by_id:
            raise BackendError(f"未找到唯一会话：{target!r}")
        ident, row = next(iter(by_id.items()))
        return ident, row.get("remark") or row.get("nick_name") or ident

    def messages(self, chat_id: str, limit: int = 20, offset: int = 0) -> list[dict]:
        return self.db.get_messages(chat_id, limit=limit, offset=offset)

    def new_messages(self, chat_id: str, since_seq: int, limit: int = 200) -> list[dict]:
        return self.db.get_new_messages(chat_id, since_seq=since_seq, limit=limit)


class GuiBackend:
    def __init__(self, config: AppConfig):
        self.config = config
        self._gui = None

    @property
    def gui(self):
        if self._gui is None:
            try:
                from ._vendor.wechatauto.guia import WeChatGUI
                self._gui = WeChatGUI()
            except Exception as exc:
                raise BackendError(f"无法连接微信窗口：{exc}") from exc
        return self._gui

    def open_chat(self, name: str, exact: bool = True) -> bool:
        return bool(self.gui.open_chat(name, exact=exact))

    def send_text(self, name: str, text: str):
        if not self.config.safety.allow_send:
            raise FeatureUnavailableError("SafetyConfig 禁止发送")
        return self.gui.send_msg(text, who=name, verify=True)

    def at_member(self, name: str, member: str, text: str):
        if not self.config.safety.allow_send:
            raise FeatureUnavailableError("SafetyConfig 禁止发送")
        return self.gui.at_member(member, text, who=name, verify=True)

    def quote(self, name: str, text: str, target_text: str):
        if not self.config.safety.allow_send:
            raise FeatureUnavailableError("SafetyConfig 禁止发送")
        return self.gui.quote_msg(text, who=name, target_text=target_text, verify=True)

    def reply_last(self, name: str, text: str):
        if not self.config.safety.allow_send:
            raise FeatureUnavailableError("SafetyConfig 禁止发送")
        return self.gui.reply_msg(text, who=name, verify=True)

    def send_file(self, name: str, path: str, image: bool = False):
        if not self.config.safety.allow_send:
            raise FeatureUnavailableError("SafetyConfig 禁止发送")
        method = self.gui.send_image if image else self.gui.send_file
        return method(str(Path(path)), who=name, verify=True)
