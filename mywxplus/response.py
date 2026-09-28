"""Small public response type matching the documented Plus result shape."""
from __future__ import annotations


class WxResponse(dict):
    def __init__(self, status: str, message: str = "", data=None):
        super().__init__(status=status, message=message, data=data)

    @property
    def is_success(self) -> bool:
        return self["status"] == "成功"

    def __bool__(self) -> bool:
        return self.is_success

    def to_dict(self) -> dict:
        return dict(self)

    @classmethod
    def success(cls, message: str = "", data=None):
        return cls("成功", message, data)

    @classmethod
    def failure(cls, message: str, data=None):
        return cls("失败", message, data)

    @classmethod
    def error(cls, message: str, data=None):
        return cls("错误", message, data)
