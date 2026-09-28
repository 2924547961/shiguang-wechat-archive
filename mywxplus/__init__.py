"""Independent Weixin desktop automation API."""
from .api import Chat, WeChat
from .config import AppConfig, FeatureStatus, SafetyConfig
from .diagnostics import inspect_ui_tree
from .errors import *
from .models.message import *
from .models.session import Session
from .response import WxResponse

__version__ = "0.2.0"
