"""接口清单启动自检。

问题背景：接口靠手工在一张注册表里登记时，新增处理器忘了登记，要等前端
404 才发现；页面引用的接口被改名/删除，也要等页面白屏才发现。这组检查把
"清单即事实"反过来用——路由是从 ``@Endpoint`` 声明里**收集**出来的，启动时
再做三道交叉核对，任何一条不过都拒绝启动（:func:`verify` 抛
:class:`SelfCheckError`）：

1. **漏登记**：处理器类上凡是 ``handle_`` 前缀的方法，必须带 ``@Endpoint``
   声明。新加接口只写了方法、忘了加装饰器，这里立刻报出来；反过来也不允许
   路由指向不存在的处理器（收集机制本身已保证，这里再断言一次）。
2. **路径唯一**：同方法同路径只能登记一次（:class:`~batterystore.console.router.Router`
   收集期已拦，自检再核对一次清单）。
3. **页面对账**：四个页面 HTML 里 fetch 的每个 ``/api/...`` 路径（含 query
   string 去掉后的模板路径，``/api/rack/rk-01`` 这类实参路径会被归并回
   ``/api/rack/{rack_id}`` 模板），都必须能在清单里匹配到；清单里登记了
   但处理器返回结构缺关键字段的问题由接口测试覆盖，这里只管路径可达。

检查结果也通过 :meth:`Api.self_check` 暴露成 ``GET /api/self-check``，
运行期巡检可以直接读。
"""

from __future__ import annotations

import re

from ..errors import ConfigError
from .router import ENDPOINT_ATTR, Route

# 页面 JS 里 fetch("/api/xxx") / fetch(`/api/xxx`) 的路径。
_FETCH_RE = re.compile(r"""fetch\(\s*[`'"]((?:/api/)[^`'"?]+)""")
# 页面里 post("/api/xxx", ...) 这类封装调用。
_POST_RE = re.compile(r"""(?:post|request)\(\s*[`'"]((?:/api/)[^`'"?]+)""")

_HANDLE_PREFIX = "handle_"


class SelfCheckError(ConfigError):
    """接口清单自检失败，属于装配期必须拦下的接线问题。"""


def undeclared_handlers(api: object) -> list[str]:
    """找出 ``handle_`` 前缀但没有 ``@Endpoint`` 声明的处理器方法。"""

    missing: list[str] = []
    for name in sorted(vars(type(api))):
        if not name.startswith(_HANDLE_PREFIX):
            continue
        value = getattr(type(api), name)
        if not callable(value):
            continue
        if not hasattr(value, ENDPOINT_ATTR):
            missing.append(name)
    return missing


def duplicate_routes(routes: list[dict]) -> list[str]:
    """找出重复登记的 (方法, 路径)。"""

    seen: set[tuple[str, str]] = set()
    duplicates: list[str] = []
    for route in routes:
        key = (route["method"], route["path"])
        if key in seen:
            duplicates.append(f"{route['method']} {route['path']}")
        seen.add(key)
    return duplicates


def _normalize(path: str, routes: list[dict]) -> str | None:
    """把页面里的真实路径归并到路由模板；归并不了返回 None（不限方法）。"""

    wanted = [segment for segment in path.split("/") if segment]
    for route in routes:
        pattern = [segment for segment in route["path"].split("/") if segment]
        if len(pattern) != len(wanted):
            continue
        for expected, actual in zip(pattern, wanted):
            if expected.startswith("{") and expected.endswith("}"):
                continue
            if expected != actual:
                break
        else:
            return route["path"]
    return None


def page_references(pages: object) -> dict[str, list[str]]:
    """扫描每个页面 HTML，返回 ``{页面名: [引用的 api 路径]}``（去重排序）。"""

    found: dict[str, list[str]] = {}
    index = pages.index()  # type: ignore[attr-defined]
    for item in index:
        name = item["name"]
        if not item["present"]:
            found[name] = []
            continue
        html = pages.render(name)  # type: ignore[attr-defined]
        paths = set(_FETCH_RE.findall(html)) | set(_POST_RE.findall(html))
        found[name] = sorted(paths)
    return found


def verify(api: object, pages: object) -> dict:
    """跑全部启动自检，返回检查报告；任一不过抛 :class:`SelfCheckError`。"""

    routes = api.routes()  # type: ignore[attr-defined]
    undeclared = undeclared_handlers(api)
    duplicates = duplicate_routes(routes)

    missing_pages: list[str] = [item["name"] for item in pages.index() if not item["present"]]  # type: ignore[attr-defined]

    unresolved: list[dict] = []
    for name, paths in page_references(pages).items():
        for path in paths:
            if _normalize(path, routes) is None:
                unresolved.append({"page": name, "path": path})

    report = {
        "routes": len(routes),
        "undeclared_handlers": undeclared,
        "duplicate_routes": duplicates,
        "pages_missing": missing_pages,
        "page_references_unresolved": unresolved,
    }
    failures = [
        f"处理器未登记接口: {undeclared}" if undeclared else "",
        f"路由重复登记: {duplicates}" if duplicates else "",
        f"页面文件缺失: {missing_pages}" if missing_pages else "",
        f"页面引用了清单里没有的接口: {unresolved}" if unresolved else "",
    ]
    problems = [item for item in failures if item]
    report["ok"] = not problems
    if problems:
        raise SelfCheckError("接口清单自检失败：" + "；".join(problems), component="console")
    return report
