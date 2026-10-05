"""极简路径路由与接口登记。

支持 ``/api/audit`` 这类静态路径与 ``/api/namespace/{address}`` 这类带参
路径。匹配按段比较：静态段必须相同，``{name}`` 段捕获任意非空段并放进参数
字典。

路由**只能**通过 :class:`Endpoint` 装饰器声明（见 :mod:`batterystore.console.api`），
不再提供"先写处理器再手动 add"的第二条登记路径——两条登记路径必然漂移，
新增接口忘了登记时启动自检无从发现。装饰器在类定义期就把方法、方法、路径、
参数规则绑成一条 :class:`Route`，:meth:`Router.collect` 从处理器对象上统一
收集，重复登记同一路径立刻报错。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .validation import Schema

Handler = Callable[[dict, dict], dict]

# 挂在被装饰处理器方法上的登记标记。
ENDPOINT_ATTR = "__console_endpoint__"


@dataclass(frozen=True)
class Route:
    """一条路由：方法、路径模式、绑定处理器与其入参校验规则。"""

    method: str
    pattern: str
    handler: Handler
    schema: Schema
    name: str

    def as_dict(self) -> dict:
        return {
            "method": self.method,
            "path": self.pattern,
            "handler": self.name,
            "params": sorted(self.schema.params),
            "body": sorted(self.schema.body),
        }


class Endpoint:
    """接口登记装饰器：把 ``(方法, 路径, 入参规则)`` 绑定到处理器方法。

    被装饰的方法必须在 :class:`Api` 子类上以 ``handle_`` 前缀命名；
    :meth:`Router.collect` 只收集带本标记的方法，没登记的 ``handle_``
    方法会在启动自检里被当成漏登记拦截。
    """

    def __init__(self, method: str, pattern: str, schema: Schema | None = None) -> None:
        self._method = method.upper()
        self._pattern = pattern
        self._schema = schema or Schema()

    def __call__(self, func: Handler) -> Handler:
        bound = Route(self._method, self._pattern, func, self._schema, func.__name__)
        setattr(func, ENDPOINT_ATTR, bound)
        return func


class Router:
    """按方法与路径段匹配的路由表，从处理器对象上声明式收集。"""

    def __init__(self) -> None:
        self._routes: list[Route] = []
        self._keys: set[tuple[str, str]] = set()

    def add(self, route: Route, owner: object) -> None:
        """登记一条已声明的路由；同方法同路径重复登记直接报错。"""

        key = (route.method, route.pattern)
        if key in self._keys:
            raise RuntimeError(f"路由重复登记: {route.method} {route.pattern}")
        self._keys.add(key)
        # 绑定到实际处理器对象，调用时无需再传 self。
        bound = Route(route.method, route.pattern, route.handler.__get__(owner), route.schema, route.name)
        self._routes.append(bound)

    def collect(self, owner: object) -> None:
        """从处理器对象上收集全部 ``@Endpoint`` 声明，按路径排序登记。"""

        declared = [
            getattr(value, ENDPOINT_ATTR)
            for value in (getattr(owner, name) for name in dir(owner))
            if hasattr(value, ENDPOINT_ATTR)
        ]
        # dir() 顺序不稳定，按方法名排序保证路由表与清单顺序确定。
        for route in sorted(declared, key=lambda item: item.name):
            self.add(route, owner)

    def routes(self) -> list[Route]:
        """返回全部路由（登记顺序）。"""

        return list(self._routes)

    def allows(self, method: str, path: str) -> bool:
        """路径是否被其他方法的路由占用（用于区分 404 与 405）。"""

        wanted = [segment for segment in path.split("/") if segment]
        for route in self._routes:
            if route.method == method.upper():
                continue
            pattern = [segment for segment in route.pattern.split("/") if segment]
            if len(pattern) != len(wanted):
                continue
            if all(expected.startswith("{") and expected.endswith("}") or expected == actual
                   for expected, actual in zip(pattern, wanted)):
                return True
        return False

    def match(self, method: str, path: str) -> tuple[Route, dict] | None:
        """匹配一条路由，返回路由与路径参数字典。"""

        wanted = [segment for segment in path.split("/") if segment]
        for route in self._routes:
            if route.method != method.upper():
                continue
            pattern = [segment for segment in route.pattern.split("/") if segment]
            if len(pattern) != len(wanted):
                continue
            params: dict[str, str] = {}
            for expected, actual in zip(pattern, wanted):
                if expected.startswith("{") and expected.endswith("}"):
                    params[expected[1:-1]] = actual
                elif expected != actual:
                    break
            else:
                return route, params
        return None
