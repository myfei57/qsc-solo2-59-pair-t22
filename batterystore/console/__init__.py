"""控制台：JSON API、页面服务与路径路由。"""

from __future__ import annotations

from .api import Api
from .pages import PageLibrary
from .router import Endpoint, Route, Router
from .selfcheck import SelfCheckError, verify
from .server import ConsoleServer, probe_endpoints
from .validation import RequestError, Schema, Field

__all__ = [
    "Api",
    "ConsoleServer",
    "Endpoint",
    "Field",
    "PageLibrary",
    "RequestError",
    "Route",
    "Router",
    "Schema",
    "SelfCheckError",
    "probe_endpoints",
    "verify",
]
