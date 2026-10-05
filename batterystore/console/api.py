"""控制台接口处理器。

每个接口都是对平台公开方法的一次调用，且**只能**通过 :class:`Endpoint`
装饰器登记——路由表由 :class:`~batterystore.console.router.Router` 从这些
声明里收集，启动自检 :mod:`batterystore.console.selfcheck` 再把声明与
``handle_`` 方法、页面引用交叉核对，漏登记在启动时直接失败。

读写口径：

* **入参**——所有接口（读接口的 path/query、写接口的 body）共用
  :mod:`batterystore.console.validation` 一套规则，缺字段/类型错/越界统一
  抛 :class:`~batterystore.console.validation.RequestError` → ``400``；
* **错误**——按性质分四类，响应体同构（``error``/``code``/``message``，
  400 额外带 ``field``）：``400`` 请求本身不合法；``404`` 寻址不到资源
  （未知簇、未知设备地址）；``409`` 参数合法但被业务拒绝（联锁、消防态、
  配额不足），写审计；``503`` 持久化故障。前端按状态码走不同分支：
  400 高亮入参、404 检查寻址、409 提示现场状态/引导复位；
* **同源**——所有处理器在平台同一把读锁内执行并在响应体盖 ``revision``
  戳；页面改成只调各自的 ``/api/view/<page>`` 聚合接口，一页一快照，
  页面与单接口读数 revision 相同即同源。
"""

from __future__ import annotations

from ..audit.timeline import build_timeline
from ..errors import ConfigError, InterlockError, NamespaceError, StorageError, StoreError
from ..store.index import summarize_documents
from .pages import PageLibrary
from .router import Endpoint, Router
from .selfcheck import verify
from .validation import RequestError, Schema, bounded_float, nonneg_int, optional, positive_float, required

# 动作类 body 里"可空字符串"字段（如 rack_id 缺省走默认簇）的统一声明。
_OPTIONAL_ID = optional("str", None)


class Api:
    """把 HTTP 请求翻译成平台调用；接口全部以 ``@Endpoint`` 声明。"""

    def __init__(self, platform, pages: PageLibrary) -> None:
        self._platform = platform
        self._pages = pages
        self._router = Router()
        self._router.collect(self)
        self._self_check: dict | None = None

    @property
    def platform(self):
        return self._platform

    @property
    def pages(self) -> PageLibrary:
        return self._pages

    def routes(self) -> list[dict]:
        """返回接口清单，供自检与文档核对。"""

        return [route.as_dict() for route in self._router.routes()]

    def startup_check(self) -> dict:
        """启动自检：漏登记、重复登记、页面缺失或引用悬空都在这一步失败。"""

        self._self_check = verify(self, self._pages)
        return self._self_check

    def handle(self, method: str, path: str, query: dict, body: dict) -> tuple[int, dict]:
        """处理一次请求，返回状态码与响应体。"""

        matched = self._router.match(method, path)
        if matched is None:
            if self._router.allows(method, path):
                return 405, {"error": "method_not_allowed", "message": f"{method} 不支持该路径", "path": path}
            return 404, {"error": "not_found", "message": "接口不存在", "path": path}
        route, params = matched
        try:
            if body is None:
                raise RequestError("请求体不是合法 JSON", code="invalid_body")
            clean_params, clean_body = route.schema.validate({**query, **params}, body)
        except RequestError as error:
            # 参数没进业务层，不写业务审计；HTTP 访问路径仍由服务器留痕。
            return 400, error.as_body()
        try:
            # 整个处理器（含 POST 内部的 commit，RLock 可重入）在同一把平台锁内
            # 跑完，盖戳时读到的 revision 与响应数据严格对应，且读不会被写撕裂。
            with self._platform.read_lock():
                payload = route.handler(clean_params, clean_body)
                payload["revision"] = self._platform.revision
        except RequestError as error:
            return 400, error.as_body()
        except (NamespaceError, KeyError) as error:
            # 寻址失败：资源身份不存在。区别于 400 的"取值域越界"（如序号超界）。
            return self._fail(404, "not_found", error)
        except IndexError as error:
            # 序号越界属于参数取值域问题（域随寻址到的资源而定），仍是请求错误。
            return 400, RequestError(str(error), code="out_of_range").as_body()
        except StorageError as error:
            return self._fail(503, "service_unavailable", error)
        except ConfigError as error:
            return self._fail(500, "config_error", error)
        except (InterlockError, StoreError) as error:
            # 参数合法、当前系统状态不允许：业务拒绝，留痕后回 409。
            return self._fail(409, "business_rejected", error)
        return 200, payload

    def _fail(self, status: int, code: str, error: StoreError | KeyError | NamespaceError) -> tuple[int, dict]:
        if isinstance(error, StoreError):
            record = error.as_record()
            if status == 409:
                self._platform.note_error(error)
        else:  # 内置 KeyError
            record = {"type": type(error).__name__, "component": "console", "message": str(error.args[0])}
        return status, {"error": type(error).__name__, "code": code, **record}

    # ------------------------------------------------------------------ 读数

    @Endpoint("GET", "/api/status")
    def handle_status(self, params: dict, body: dict) -> dict:
        return self._platform.status()

    @Endpoint("GET", "/api/snapshot")
    def handle_snapshot(self, params: dict, body: dict) -> dict:
        return self._platform.snapshot()

    @Endpoint("GET", "/api/readiness")
    def handle_readiness(self, params: dict, body: dict) -> dict:
        return self._platform.readiness()

    @Endpoint("GET", "/api/topology")
    def handle_topology(self, params: dict, body: dict) -> dict:
        return self._platform.topology_summary()

    @Endpoint("GET", "/api/namespaces")
    def handle_namespaces(self, params: dict, body: dict) -> dict:
        return {"devices": self._platform.namespaces_describe()}

    @Endpoint("GET", "/api/namespace/{address}", Schema(params={"address": required("str")}))
    def handle_namespace(self, params: dict, body: dict) -> dict:
        return self._platform.namespace(params["address"])

    @Endpoint("GET", "/api/capacity")
    def handle_capacity(self, params: dict, body: dict) -> dict:
        return self._platform.capacity().as_dict()

    @Endpoint("GET", "/api/racks")
    def handle_racks(self, params: dict, body: dict) -> dict:
        return {"racks": self._platform.rack_snapshots()}

    @Endpoint("GET", "/api/rack/{rack_id}", Schema(params={"rack_id": required("str")}))
    def handle_rack(self, params: dict, body: dict) -> dict:
        return self._platform.rack_snapshot(params["rack_id"])

    @Endpoint("GET", "/api/mapping")
    def handle_mapping(self, params: dict, body: dict) -> dict:
        return self._platform.rack_mapping()

    @Endpoint("GET", "/api/bms")
    def handle_bms(self, params: dict, body: dict) -> dict:
        return self._platform.bms_snapshot()

    @Endpoint("GET", "/api/pcs")
    def handle_pcs(self, params: dict, body: dict) -> dict:
        return self._platform.pcs_snapshot()

    @Endpoint("GET", "/api/temp")
    def handle_temp(self, params: dict, body: dict) -> dict:
        return self._platform.temp_snapshot()

    @Endpoint("GET", "/api/fire")
    def handle_fire(self, params: dict, body: dict) -> dict:
        return self._platform.fire_snapshot()

    @Endpoint("GET", "/api/grid")
    def handle_grid(self, params: dict, body: dict) -> dict:
        return self._platform.grid_snapshot()

    @Endpoint("GET", "/api/soc")
    def handle_soc(self, params: dict, body: dict) -> dict:
        return self._platform.soc_snapshot()

    @Endpoint("GET", "/api/balance")
    def handle_balance_view(self, params: dict, body: dict) -> dict:
        return self._platform.balance_snapshot()

    @Endpoint("GET", "/api/quota")
    def handle_quota(self, params: dict, body: dict) -> dict:
        return self._platform.quota_snapshot()

    @Endpoint(
        "GET",
        "/api/audit",
        Schema(
            params={
                "component": optional("str", None),
                "severity": optional("str", None),
                "action": optional("str", None),
                "limit": nonneg_int(100),
            }
        ),
    )
    def handle_audit(self, params: dict, body: dict) -> dict:
        return {
            "entries": self._platform.audit.query(
                component=params["component"],
                severity=params["severity"],
                action=params["action"],
                limit=params["limit"],
            )
        }

    @Endpoint("GET", "/api/audit/report")
    def handle_audit_report(self, params: dict, body: dict) -> dict:
        return self._platform.audit.report()

    @Endpoint("GET", "/api/audit/operations", Schema(params={"limit": nonneg_int(20)}))
    def handle_operations(self, params: dict, body: dict) -> dict:
        return {"operations": self._platform.audit.operations(limit=params["limit"])}

    @Endpoint("GET", "/api/alarms")
    def handle_alarms(self, params: dict, body: dict) -> dict:
        return {"alarms": self._platform.audit.alarms()}

    @Endpoint("GET", "/api/alerts")
    def handle_alerts(self, params: dict, body: dict) -> dict:
        return {"alerts": self._platform.alerts()}

    @Endpoint("GET", "/api/events", Schema(params={"topic": optional("str", None)}))
    def handle_events(self, params: dict, body: dict) -> dict:
        return {"events": [event.as_dict() for event in self._platform.bus.history(params["topic"])]}

    @Endpoint(
        "GET",
        "/api/trend",
        Schema(
            params={
                "signal": optional("str", "rk-01.temperature"),
                "limit": nonneg_int(60),
            }
        ),
    )
    def handle_trend(self, params: dict, body: dict) -> dict:
        signal = params["signal"]
        samples = self._platform.trend.series(signal, limit=params["limit"])
        filled = self._platform.trend.fill_gaps(signal, samples)
        return {
            "signal": signal,
            "period_s": self._platform.trend.period_s,
            "mean": self._platform.trend.mean(signal),
            "samples": [sample.as_dict() for sample in filled],
        }

    @Endpoint("GET", "/api/store")
    def handle_store(self, params: dict, body: dict) -> dict:
        store = self._platform.store
        return {
            "root": store.root,
            "documents": summarize_documents(store),
            "platform_state_path": store.path_of("platform_state"),
            "platform_state": store.read("platform_state"),
        }

    @Endpoint("GET", "/api/timeline", Schema(params={"station": optional("str", None), "limit": nonneg_int(200)}))
    def handle_timeline(self, params: dict, body: dict) -> dict:
        records = self._platform.audit.records(limit=params["limit"])
        return {"timeline": build_timeline(records, station=params["station"])}

    @Endpoint("GET", "/api/journal", Schema(params={"segment": optional("int", None, minimum=0)}))
    def handle_journal(self, params: dict, body: dict) -> dict:
        journal = self._platform.audit.journal
        payload: dict = {
            "topic": journal.topic,
            "count": journal.count(),
            "active_segment": journal.active_segment(),
            "segments": journal.segments(),
        }
        segment = params["segment"]
        if segment is not None:
            payload["records"] = self._platform.audit.replay_segment(segment)
        return payload

    @Endpoint("GET", "/api/config")
    def handle_config(self, params: dict, body: dict) -> dict:
        return self._platform.config_export()

    @Endpoint("GET", "/api/pages")
    def handle_pages(self, params: dict, body: dict) -> dict:
        return {"pages": self._pages.index(), "routes": self.routes()}

    @Endpoint("GET", "/api/self-check")
    def handle_self_check(self, params: dict, body: dict) -> dict:
        return verify(self, self._pages)

    # ------------------------------------------------- 页面同源聚合视图（一页一快照）

    @Endpoint("GET", "/api/view/racks")
    def handle_view_racks(self, params: dict, body: dict) -> dict:
        """racks.html 的唯一数据源：簇表与总状态在同一 revision 下取齐。"""

        return {"racks": self._platform.rack_snapshots(), "status": self._platform.status()}

    @Endpoint("GET", "/api/view/pcs")
    def handle_view_pcs(self, params: dict, body: dict) -> dict:
        """pcs.html 的唯一数据源：PCS 与 BMS 同快照。"""

        return {"pcs": self._platform.pcs_snapshot(), "bms": self._platform.bms_snapshot()}

    @Endpoint("GET", "/api/view/power", Schema(params={"signal": optional("str", "rk-01.voltage")}))
    def handle_view_power(self, params: dict, body: dict) -> dict:
        """power.html 的唯一数据源：并网点、配额与趋势同快照。"""

        signal = params["signal"]
        samples = self._platform.trend.fill_gaps(signal, self._platform.trend.series(signal, limit=60))
        return {
            "grid": self._platform.grid_snapshot(),
            "quota": self._platform.quota_snapshot(),
            "trend": {
                "signal": signal,
                "period_s": self._platform.trend.period_s,
                "mean": self._platform.trend.mean(signal),
                "samples": [sample.as_dict() for sample in samples],
            },
        }

    @Endpoint("GET", "/api/view/alarms", Schema(params={"limit": nonneg_int(30)}))
    def handle_view_alarms(self, params: dict, body: dict) -> dict:
        """alarms.html 的唯一数据源：告警与最近审计同快照。"""

        return {
            "alerts": self._platform.alerts(),
            "audit": {"entries": self._platform.audit.query(limit=params["limit"])},
        }

    # ------------------------------------------------------------------ 动作

    @Endpoint("POST", "/api/start")
    def handle_start(self, params: dict, body: dict) -> dict:
        return self._platform.start()

    @Endpoint("POST", "/api/stop")
    def handle_stop(self, params: dict, body: dict) -> dict:
        return self._platform.stop()

    @Endpoint("POST", "/api/reset")
    def handle_reset(self, params: dict, body: dict) -> dict:
        return self._platform.reset_state()

    @Endpoint("POST", "/api/step", Schema(body={"dt": positive_float(5.0)}))
    def handle_cycle(self, params: dict, body: dict) -> dict:
        return self._platform.cycle(dt=body["dt"])

    @Endpoint(
        "POST",
        "/api/transfer",
        Schema(
            body={
                "power_kw": positive_float(120.0),
                "frequency_hz": positive_float(50.0),
                "dt": positive_float(1.0),
            }
        ),
    )
    def handle_transfer(self, params: dict, body: dict) -> dict:
        return self._platform.transfer_grid_tie(
            power_kw=body["power_kw"],
            frequency_hz=body["frequency_hz"],
            dt=body["dt"],
        )

    @Endpoint("POST", "/api/off-grid")
    def handle_off_grid(self, params: dict, body: dict) -> dict:
        return self._platform.transfer_off_grid()

    @Endpoint(
        "POST",
        "/api/discharge",
        Schema(body={"power_kw": positive_float(200.0), "dt": positive_float(60.0)}),
    )
    def handle_discharge(self, params: dict, body: dict) -> dict:
        return self._platform.discharge(power_kw=body["power_kw"], dt=body["dt"])

    @Endpoint(
        "POST",
        "/api/charge",
        Schema(
            body={
                "rack_id": _OPTIONAL_ID,
                "steps": optional("int", 3, minimum=1),
                "dt": positive_float(60.0),
            }
        ),
    )
    def handle_charge(self, params: dict, body: dict) -> dict:
        return self._platform.charge(rack_id=body["rack_id"], steps=body["steps"], dt=body["dt"])

    @Endpoint(
        "POST",
        "/api/balance",
        Schema(
            body={
                "rack_id": _OPTIONAL_ID,
                "dt": positive_float(2.0),
                "max_steps": optional("int", 400, minimum=1),
            }
        ),
    )
    def handle_balance(self, params: dict, body: dict) -> dict:
        return self._platform.balance(
            rack_id=body["rack_id"], dt=body["dt"], max_steps=body["max_steps"]
        )

    @Endpoint(
        "POST",
        "/api/temperature",
        Schema(
            body={
                "rack_id": _OPTIONAL_ID,
                "temperature_c": optional("float", 30.0),
                "dt": positive_float(5.0),
            }
        ),
    )
    def handle_temperature(self, params: dict, body: dict) -> dict:
        return self._platform.sample_temperature(
            rack_id=body["rack_id"],
            temperature_c=body["temperature_c"],
            dt=body["dt"],
        )

    @Endpoint(
        "POST",
        "/api/replace-pack",
        Schema(
            body={
                "pack_index": nonneg_int(0),
                "rating_v": positive_float(3.35),
                "rack_id": _OPTIONAL_ID,
            }
        ),
    )
    def handle_replace_pack(self, params: dict, body: dict) -> dict:
        return self._platform.replace_pack_rating(
            pack_index=body["pack_index"],
            rating_v=body["rating_v"],
            rack_id=body["rack_id"],
        )

    @Endpoint(
        "POST",
        "/api/calibrate",
        Schema(body={"soc_pct": bounded_float(50.0, 0.0, 100.0), "source": optional("str", "console")}),
    )
    def handle_calibrate(self, params: dict, body: dict) -> dict:
        return self._platform.calibrate_soc(soc_pct=body["soc_pct"], source=body["source"])

    @Endpoint(
        "POST",
        "/api/fault",
        Schema(body={"rack_id": _OPTIONAL_ID, "code": optional("str", "bms_fault")}),
    )
    def handle_fault(self, params: dict, body: dict) -> dict:
        return self._platform.report_fault(rack_id=body["rack_id"], code=body["code"])

    @Endpoint("POST", "/api/fault/clear", Schema(body={"rack_id": _OPTIONAL_ID}))
    def handle_fault_clear(self, params: dict, body: dict) -> dict:
        return self._platform.clear_fault(rack_id=body["rack_id"])

    @Endpoint(
        "POST",
        "/api/fire/drill",
        Schema(
            body={
                "zone": optional("str", "Z1"),
                "smoke_ppm": optional("float", 1500.0, minimum=0.0),
                "temperature_c": optional("float", 30.0),
                "confirm_dt": positive_float(5.0),
            }
        ),
    )
    def handle_drill(self, params: dict, body: dict) -> dict:
        return self._platform.fire_drill(
            zone=body["zone"],
            smoke_ppm=body["smoke_ppm"],
            temperature_c=body["temperature_c"],
            confirm_dt=body["confirm_dt"],
        )

    @Endpoint("POST", "/api/fire/release")
    def handle_fire_release(self, params: dict, body: dict) -> dict:
        return self._platform.release_fire()

    @Endpoint("POST", "/api/frequency", Schema(body={"frequency_hz": positive_float(50.0)}))
    def handle_frequency(self, params: dict, body: dict) -> dict:
        return self._platform.monitor_frequency(frequency_hz=body["frequency_hz"])

    @Endpoint(
        "POST",
        "/api/soc",
        Schema(body={"rack_id": _OPTIONAL_ID, "soc_pct": bounded_float(50.0, 0.0, 100.0)}),
    )
    def handle_set_soc(self, params: dict, body: dict) -> dict:
        return self._platform.set_soc(rack_id=body["rack_id"], soc_pct=body["soc_pct"])

    @Endpoint(
        "POST",
        "/api/disturb",
        Schema(
            body={
                "rack_id": _OPTIONAL_ID,
                "cell_index": nonneg_int(0),
                "delta_v": optional("float", 0.03),
            }
        ),
    )
    def handle_disturb(self, params: dict, body: dict) -> dict:
        return self._platform.disturb_cell(
            rack_id=body["rack_id"],
            cell_index=body["cell_index"],
            delta_v=body["delta_v"],
        )

    @Endpoint("POST", "/api/probe/pcs", Schema(body={"rounds": optional("int", 32, minimum=1)}))
    def handle_probe_pcs(self, params: dict, body: dict) -> dict:
        return self._platform.concurrency_probe(rounds=body["rounds"])

    @Endpoint("POST", "/api/probe/fault", Schema(body={"rounds": optional("int", 16, minimum=1)}))
    def handle_probe_fault(self, params: dict, body: dict) -> dict:
        return self._platform.concurrency_fault_probe(rounds=body["rounds"])
