"""控制台 API、页面服务、校验口径与快照一致性。"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request

import support
from batterystore.console.api import Api
from batterystore.console.pages import PageLibrary
from batterystore.console.selfcheck import SelfCheckError
from batterystore.console.server import ConsoleServer, probe_endpoints
from batterystore.console.validation import Field, RequestError, Schema


def _get(base_url: str, path: str):
    try:
        with urllib.request.urlopen(f"{base_url}{path}", timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _post(base_url: str, path: str, payload: dict | str):
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=raw.encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_page_library_reports_pages(tmp_path):
    library = PageLibrary("web")
    assert library.resolve("/racks") == "racks"
    assert library.resolve("/alarms.html") == "alarms"
    assert library.resolve("/api/status") is None
    assert library.exists() is True
    assert library.render("racks").startswith("<!DOCTYPE html>")
    assert len(library.index()) == 4
    try:
        library.path_of("bogus")
    except KeyError:
        pass
    else:  # pragma: no cover - 明确失败路径
        raise AssertionError("未知页面应当报错")


def test_console_serves_json_and_pages(platform):
    support.bring_online(platform)
    server = ConsoleServer(platform, port=0)
    base_url = server.start()
    try:
        status, payload = _get(base_url, "/api/status")
        assert status == 200
        assert payload["station"] == "station1"
        assert payload["revision"] == platform.revision
        status, snapshot = _get(base_url, "/api/snapshot")
        assert status == 200
        assert "config" in snapshot
        for path in ("/api/racks", "/api/pcs", "/api/grid", "/api/quota", "/api/soc", "/api/alerts"):
            code, _body = _get(base_url, path)
            assert code == 200, path
        code, entry = _get(base_url, "/api/namespace/station:pcs")
        assert code == 200
        assert entry["kind"] == "power-conversion"
        with urllib.request.urlopen(f"{base_url}/racks", timeout=5) as response:
            html = response.read().decode("utf-8")
        assert "电池簇状态" in html
    finally:
        server.stop()


def test_console_actions_and_errors(platform):
    support.bring_online(platform)
    server = ConsoleServer(platform, port=0)
    base_url = server.start()
    try:
        code, payload = _post(base_url, "/api/transfer", {"power_kw": 150.0})
        assert code == 200
        assert payload["ok"] is True
        assert payload["revision"] == platform.revision
        code, payload = _post(base_url, "/api/fault", {"rack_id": "rk-01", "code": "cell_over_temp"})
        assert code == 200
        assert payload["count"] == 1
        code, payload = _post(base_url, "/api/temperature", {"temperature_c": 30.0})
        assert code == 200
        code, payload = _post(base_url, "/api/balance", {"max_steps": 50})
        assert code == 200
        code, payload = _post(base_url, "/api/charge", {"steps": 2, "dt": 60.0})
        assert code == 200
        code, payload = _post(base_url, "/api/disturb", {"cell_index": 3, "delta_v": 0.02})
        assert code == 200
        code, payload = _post(base_url, "/api/calibrate", {"soc_pct": 55.0})
        assert code == 200
        code, payload = _post(base_url, "/api/fire/drill", {"zone": "Z1", "confirm_dt": 5.0})
        assert code == 200
        assert payload["ok"] is True
        code, payload = _post(base_url, "/api/fire/release", {})
        assert code == 200
        # 每接受一个写动作，数据版本严格加一，且响应盖戳等于平台版本。
        revision_before = platform.revision
        code, payload = _post(base_url, "/api/step", {"dt": 1.0})
        assert code == 200
        assert platform.revision == revision_before + 1
        assert payload["revision"] == platform.revision
        probe = probe_endpoints(base_url, ["/api/status", "/api/audit"])
        assert probe["/api/status"]["ok"] is True
        assert probe["/api/audit"]["ok"] is True
        code, body = _get(base_url, "/api/not-a-route")
        assert code == 404
        code, body = _post(base_url, "/api/status", {})
        assert code == 405
    finally:
        server.stop()


def test_invalid_parameters_are_request_errors_not_business_rejections(platform):
    """400 与 409 的边界：只凭参数就能判定的形状问题一律 400。"""

    support.bring_online(platform)
    server = ConsoleServer(platform, port=0)
    base_url = server.start()
    revision = platform.revision
    try:
        # 类型错误：非数值 / 非整数 / 布尔冒充数值。
        code, body = _post(base_url, "/api/discharge", {"power_kw": "not-a-number"})
        assert code == 400 and body["error"] == "invalid_request" and body["field"] == "power_kw"
        code, body = _post(base_url, "/api/charge", {"steps": 2.5})
        assert code == 400 and body["field"] == "steps"
        code, body = _post(base_url, "/api/discharge", {"power_kw": True})
        assert code == 400 and body["field"] == "power_kw"
        # 取值域错误：非正步长/时长、越界百分比、负序号。
        code, body = _post(base_url, "/api/discharge", {"dt": 0})
        assert code == 400 and body["field"] == "dt"
        code, body = _post(base_url, "/api/discharge", {"dt": -3})
        assert code == 400 and body["field"] == "dt"
        code, body = _post(base_url, "/api/calibrate", {"soc_pct": 120})
        assert code == 400 and body["field"] == "soc_pct"
        code, body = _post(base_url, "/api/disturb", {"cell_index": -1})
        assert code == 400 and body["field"] == "cell_index"
        # 读接口的 query 参数走同一套规则。
        code, body = _get(base_url, "/api/audit?limit=abc")
        assert code == 400 and body["field"] == "limit"
        code, body = _get(base_url, "/api/journal?segment=1.5")
        assert code == 400 and body["field"] == "segment"
        # 请求体不是 JSON / 不是对象。
        code, body = _post(base_url, "/api/discharge", "{not json")
        assert code == 400 and body["code"] == "invalid_body"
        # 寻址问题是 404，不是 400 也不是 409。
        code, body = _get(base_url, "/api/rack/rk-99")
        assert code == 404 and body["code"] == "not_found"
        code, body = _post(base_url, "/api/charge", {"rack_id": "rk-99"})
        assert code == 404
        # 参数被 400 拦下时没进业务层，数据版本不允许推进。
        assert platform.revision == revision
    finally:
        server.stop()


def test_page_views_share_one_snapshot_with_raw_endpoints(platform):
    """页面聚合视图与单接口读数同 revision：同一快照的两块同源。"""

    support.bring_online(platform)
    server = ConsoleServer(platform, port=0)
    base_url = server.start()
    try:
        _post(base_url, "/api/step", {"dt": 1.0})
        code, view = _get(base_url, "/api/view/racks")
        assert code == 200
        code, racks = _get(base_url, "/api/racks")
        code, status = _get(base_url, "/api/status")
        assert view["revision"] == racks["revision"] == status["revision"]
        assert view["racks"] == racks["racks"]
        assert view["status"]["operations"] == status["operations"]

        code, power = _get(base_url, "/api/view/power?signal=rk-01.voltage")
        code, grid = _get(base_url, "/api/grid")
        code, quota = _get(base_url, "/api/quota")
        assert power["revision"] == grid["revision"] == quota["revision"]
        assert power["grid"]["flow"] == grid["flow"]
        assert power["quota"]["rolls"] == quota["rolls"]

        for name in ("pcs", "alarms"):
            code, view_payload = _get(base_url, f"/api/view/{name}")
            assert code == 200
            assert "revision" in view_payload
    finally:
        server.stop()


def test_views_are_linearizable_against_concurrent_writes(platform):
    """读视图在平台锁内组装：并发读写下不出现撕裂或 5xx。"""

    support.bring_online(platform)
    server = ConsoleServer(platform, port=0)
    base_url = server.start()
    errors: list = []
    observed: list[int] = []
    baseline = platform.revision
    lock = threading.Lock()

    def writer() -> None:
        for _ in range(30):
            code, body = _post(base_url, "/api/step", {"dt": 1.0})
            if code != 200:
                errors.append(code)
            else:
                with lock:
                    observed.append(body["revision"])

    def reader() -> None:
        for _ in range(50):
            for path in ("/api/view/racks", "/api/view/pcs", "/api/view/power", "/api/view/alarms"):
                code, body = _get(base_url, path)
                if code != 200:
                    errors.append((path, code))

    threads = [threading.Thread(target=writer) for _ in range(3)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert observed == sorted(observed)
    # 写动作完成数等于 revision 增量（基线扣除测试开始前的版本）。
    assert platform.revision - baseline == len(observed)
    server.stop()


def test_self_check_passes_on_shipped_inventory(platform):
    server = ConsoleServer(platform, port=0)
    report = server.startup_report()
    assert report["ok"] is True
    assert report["undeclared_handlers"] == []
    assert report["duplicate_routes"] == []
    assert report["pages_missing"] == []
    assert report["page_references_unresolved"] == []
    code, live = _get(server.start(), "/api/self-check")
    assert code == 200 and live["ok"] is True
    server.stop()


def test_self_check_catches_handler_without_endpoint_declaration(platform):
    """路由由 @Endpoint 反射收集：handle_ 方法漏加装饰器时启动自检必须报错。"""

    from batterystore.console.router import ENDPOINT_ATTR
    from batterystore.console.selfcheck import undeclared_handlers

    api = Api(platform, PageLibrary("web"))
    assert undeclared_handlers(api) == []

    # 模拟"新增接口只写了 handle_ 方法、忘了登记"：临时摘掉类上的装饰标记。
    target = Api.handle_journal
    marker = getattr(target, ENDPOINT_ATTR)
    delattr(target, ENDPOINT_ATTR)
    try:
        assert "handle_journal" in undeclared_handlers(api)
    finally:
        setattr(target, ENDPOINT_ATTR, marker)
    assert undeclared_handlers(api) == []


def test_self_check_catches_page_referencing_missing_route(platform, tmp_path):
    pages = PageLibrary("web")
    api = Api(platform, pages)
    api.startup_check()

    class StubPages(PageLibrary):
        def render(self, name: str) -> str:  # type: ignore[override]
            return '<script>fetch("/api/view/does-not-exist")</script>'

    from batterystore.console.selfcheck import verify

    try:
        verify(api, StubPages("web"))
    except SelfCheckError as error:
        assert "/api/view/does-not-exist" in str(error)
    else:  # pragma: no cover - 明确失败路径
        raise AssertionError("页面引用悬空接口时启动自检必须失败")


def test_validation_schema_is_shared_by_params_and_body():
    schema = Schema(
        params={"limit": Field(kind="int", default=5, minimum=0)},
        body={"power_kw": Field(kind="float", default=1.0, minimum=1e-9)},
    )
    params, body = schema.validate({"limit": "12"}, {"power_kw": "3.5"})
    assert params == {"limit": 12} and body == {"power_kw": 3.5}
    try:
        schema.validate({"limit": -1}, {})
    except RequestError as error:
        assert error.field_name == "limit"
    else:  # pragma: no cover - 明确失败路径
        raise AssertionError("越界必须拒绝")
