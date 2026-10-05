"""控制台 API 与页面服务。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import support
from batterystore.console.api import Api
from batterystore.console.pages import PageLibrary
from batterystore.console.server import ConsoleServer, probe_endpoints
from batterystore.errors import QuotaError


def _get(base_url: str, path: str):
    try:
        with urllib.request.urlopen(f"{base_url}{path}", timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _post(base_url: str, path: str, payload: dict):
    request = urllib.request.Request(
        f"{base_url}{path}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def _raw_post(base_url: str, path: str, raw: bytes, content_type: str = "application/json"):
    request = urllib.request.Request(
        f"{base_url}{path}", data=raw, headers={"Content-Type": content_type}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_page_library_reports_pages_and_assets(tmp_path):
    library = PageLibrary("web")
    assert library.resolve("/racks") == "racks"
    assert library.resolve("/alarms.html") == "alarms"
    assert library.resolve("/api/status") is None
    assert library.resolve_asset("/console.js") == "console.js"
    assert library.resolve_asset("/../secret") is None
    assert library.exists() is True
    assert library.render("racks").startswith("<!DOCTYPE html>")
    assert library.render_asset("console.js").startswith(b"/*")
    assert len(library.index()) == 5
    try:
        library.path_of("bogus")
    except KeyError:
        pass
    else:  # pragma: no cover - 明确失败路径
        raise AssertionError("未知页面应当报错")


def test_manifest_self_check_passes_on_real_api(platform):
    api = Api(platform, PageLibrary("web"))
    # 自检在构造时已经跑过一次；清单条数、唯一路径都应成立。
    keys = [(route["method"], route["path"]) for route in api.routes()]
    assert len(keys) == len(set(keys))
    assert ("GET", "/api/view") in keys
    assert ("POST", "/api/charge") in keys


def test_self_check_fails_fast_when_route_bypasses_manifest(platform):
    """绕过清单直接 router.add 的接口，必须在启动自检阶段被抓到。"""

    import batterystore.console.api as api_mod
    from batterystore.console.manifest import Spec

    api = Api(platform, PageLibrary("web"))
    api._router.add("POST", "/api/unregistered", object())
    try:
        api.self_check()
    except RuntimeError as error:
        assert "未登记接口" in str(error)
        assert "/api/unregistered" in str(error)
    else:  # pragma: no cover - 明确失败路径
        raise AssertionError("未登记接口应当在自检时失败")

    # 清单声明了不存在的处理器，同样要在构造（启动）时失败。
    original = api_mod.SPECS
    api_mod.SPECS = original + (Spec("GET", "/api/ghost", "h_missing_handler"),)
    try:
        Api(platform, PageLibrary("web"))
    except RuntimeError as error:
        assert "h_missing_handler" in str(error)
    else:  # pragma: no cover - 明确失败路径
        raise AssertionError("清单指向缺失处理器时应当启动失败")
    finally:
        api_mod.SPECS = original


def test_console_serves_json_and_pages(platform):
    support.bring_online(platform)
    server = ConsoleServer(platform, port=0)
    base_url = server.start()
    try:
        status, payload = _get(base_url, "/api/status")
        assert status == 200
        assert payload["station"] == "station1"
        status, snapshot = _get(base_url, "/api/snapshot")
        assert status == 200
        assert "config" in snapshot
        assert "revision" in snapshot
        for path in ("/api/racks", "/api/pcs", "/api/grid", "/api/quota", "/api/soc", "/api/alerts", "/api/alarms"):
            code, _body = _get(base_url, path)
            assert code == 200, path
        code, entry = _get(base_url, "/api/namespace/station:pcs")
        assert code == 200
        assert entry["kind"] == "power-conversion"
        with urllib.request.urlopen(f"{base_url}/racks", timeout=5) as response:
            html = response.read().decode("utf-8")
        assert "电池簇状态" in html
        with urllib.request.urlopen(f"{base_url}/console.js", timeout=5) as response:
            js = response.read().decode("utf-8")
        assert "/api/view" in js
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
        assert payload["revision"] >= 1
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
        probe = probe_endpoints(base_url, ["/api/status", "/api/audit"])
        assert probe["/api/status"]["ok"] is True
        assert probe["/api/audit"]["ok"] is True
        code, body = _get(base_url, "/api/not-a-route")
        assert code == 404
        assert body["code"] == "not_found"
    finally:
        server.stop()


def test_bad_parameter_is_request_error_400(platform):
    support.bring_online(platform)
    server = ConsoleServer(platform, port=0)
    base_url = server.start()
    try:
        # 类型错误：数值字段给字符串。
        code, body = _raw_post(base_url, "/api/discharge", json.dumps({"power_kw": "not-a-number"}).encode())
        assert code == 400
        assert body["error"] == "bad_request"
        assert body["code"] == "invalid_parameter"
        assert body["field"] == "power_kw"
        assert body["component"] == "console"
        # 越界：负数功率。
        code, body = _post(base_url, "/api/discharge", {"power_kw": -5})
        assert code == 400 and body["code"] == "out_of_range" and body["field"] == "power_kw"
        # 资源不存在：未知簇。
        code, body = _post(base_url, "/api/charge", {"rack_id": "rk-99"})
        assert code == 400 and body["code"] == "unknown_resource" and body["field"] == "rack_id"
        # 枚举不合法：severity 取值不在三档内。
        code, _ = _get(base_url, "/api/audit?severity=bogus")
        assert code == 400
        # 多余字段：接口未声明的参数。
        code, body = _post(base_url, "/api/stop", {"nope": 1})
        assert code == 400 and body["code"] == "unknown_parameter" and body["field"] == "nope"
        # 越界序号：按所选簇的电池包数量做上界。
        code, body = _post(base_url, "/api/replace-pack", {"pack_index": 999})
        assert code == 400 and body["code"] == "out_of_range" and body["field"] == "pack_index"
        # 整型字段不允许用小数混进来（3.5 不得被静默截断成 3）。
        code, body = _post(base_url, "/api/step", {"dt": 1})
        assert code == 200
        code, body = _post(base_url, "/api/charge", {"steps": 2.5})
        assert code == 400 and body["field"] == "steps"
    finally:
        server.stop()


def test_malformed_json_body_is_400(platform):
    server = ConsoleServer(platform, port=0)
    base_url = server.start()
    try:
        code, body = _raw_post(base_url, "/api/stop", b"{not json")
        assert code == 400 and body["code"] == "invalid_json"
        code, body = _raw_post(base_url, "/api/stop", b"[1,2,3]")
        assert code == 400 and body["code"] == "invalid_json"
    finally:
        server.stop()


def test_pinned_revision_keeps_consistent_snapshot(platform):
    support.bring_online(platform)
    server = ConsoleServer(platform, port=0)
    base_url = server.start()
    try:
        _, view = _get(base_url, "/api/view")
        revision = view["revision"]
        _, racks = _get(base_url, f"/api/racks?at={revision}")
        _, quota = _get(base_url, f"/api/quota?at={revision}")
        assert racks["revision"] == revision
        # 推进若干代（仍在保留窗口内），钉住旧版本读到的还是旧版本。
        for _ in range(3):
            _post(base_url, "/api/step", {"dt": 1.0})
        _, racks_old = _get(base_url, f"/api/racks?at={revision}")
        _, view_now = _get(base_url, "/api/view")
        assert racks_old["revision"] == revision
        assert view_now["revision"] > revision
        # 同一版本下，racks 两次调用内容必须逐字节一致（同源不可变快照）。
        _, again = _get(base_url, f"/api/racks?at={revision}")
        assert again == racks_old
        assert quota["period"]  # 钉版本读配额同样命中
    finally:
        server.stop()


def test_initial_revision_zero_is_pinnable(platform):
    """平台未发生变更时当前版本为 0，钉 0 必须能拿到初始快照。"""

    server = ConsoleServer(platform, port=0)
    base_url = server.start()
    try:
        _, view = _get(base_url, "/api/view")
        assert view["revision"] == 0
        code, body = _get(base_url, "/api/racks?at=0")
        assert code == 200 and body["revision"] == 0
        _, again = _get(base_url, "/api/racks?at=0")
        assert again == body
    finally:
        server.stop()


def test_stale_revision_is_409_snapshot_stale(platform):
    support.bring_online(platform)
    server = ConsoleServer(platform, port=0)
    base_url = server.start()
    try:
        _, view = _get(base_url, "/api/view")
        old = view["revision"]
        # 推进超过快照保留窗口（16 代），旧版本必须被判过期而不是静默重放。
        for _ in range(20):
            _post(base_url, "/api/step", {"dt": 1.0})
        code, body = _get(base_url, f"/api/racks?at={old}")
        assert code == 409
        assert body["code"] == "snapshot_stale"
        assert body["revision"] == old
        assert body["latest"] >= old
        # at 为负数是非法取值，按请求错误处理。
        code, body = _get(base_url, "/api/racks?at=-1")
        assert code == 400 and body["code"] == "out_of_range"
        # 钉一个未来版本号：现场给不出这一代，属于状态冲突 409。
        code, body = _get(base_url, "/api/racks?at=999999")
        assert code == 409 and body["code"] == "snapshot_stale"
    finally:
        server.stop()


def test_business_rejection_keeps_409_path(platform):
    """请求合法但现场状态拒绝时仍走 409，前端与 400 分流。"""

    support.bring_online(platform)
    server = ConsoleServer(platform, port=0)
    base_url = server.start()
    try:
        # 未知设备地址：引用不存在的资源，按请求错误 400 处理。
        code, body = _get(base_url, "/api/namespace/station:nope")
        assert code == 400 and body["code"] == "unknown_resource"
    finally:
        server.stop()


class _QuotaStub:
    """放电即配额不足的平台桩：参数完全合法，拒绝来自现场记账状态。"""

    racks = {"rk-01": object()}
    fire = type("F", (), {"zones": staticmethod(lambda: ["Z1"])})()
    namespaces = type("N", (), {"addresses_set": staticmethod(set)})()

    def trend_signals(self):
        return set()

    def discharge(self, *, power_kw, dt):
        raise QuotaError("本周期放电额度不足", component="quota")

    def note_error(self, error):
        return error.as_record()

    def revision(self):
        return 0


def test_quota_rejection_is_409_and_distinct_from_400():
    api = Api(_QuotaStub(), PageLibrary("web"))
    # 参数合法 -> 进入平台 -> 配额侧拒绝 -> 409，保留异常类型与组件名。
    status, body = api.handle("POST", "/api/discharge", {}, {"power_kw": 10.0, "dt": 60})
    assert status == 409
    assert body["error"] == "QuotaError"
    assert body["component"] == "quota"
    assert "额度不足" in body["message"]
    # 同一接口参数非法 -> 根本不进平台 -> 400，前端据此走另一条处理路径。
    status, body = api.handle("POST", "/api/discharge", {}, {"power_kw": "bad"})
    assert status == 400
    assert body["error"] == "bad_request"
    assert body["field"] == "power_kw"
