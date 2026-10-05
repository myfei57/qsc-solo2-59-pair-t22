"""控制台：JSON API、页面服务、接口清单与路径路由。"""

from __future__ import annotations

from .api import Api
from .manifest import SPECS, Spec
from .pages import PageLibrary
from .params import BadRequest, ParamSpec, validate
from .router import Route, Router
from .server import ConsoleServer, probe_endpoints

__all__ = [
    "Api",
    "SPECS",
    "Spec",
    "ParamSpec",
    "BadRequest",
    "validate",
    "ConsoleServer",
    "PageLibrary",
    "Route",
    "Router",
    "probe_endpoints",
]
