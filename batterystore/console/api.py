"""控制台接口处理器。

处理器不再各写一套参数解析：每条接口在 :mod:`batterystore.console.manifest`
里声明方法、路径、参数规格与处理器名，启动时双向自检（清单 ↔ 路由表 ↔
处理器方法），新增接口忘了登记会直接启动失败。

读接口（GET）一律从平台 :meth:`Platform.read_view` 返回的同一份不可变快照里
投影，支持 ``at=<revision>`` 钉版本；写接口（POST）调用平台动作并把最新版本号
随响应返回。错误口径只有两种，前端按状态码分流：

* ``400``：请求本身错（参数类型、取值、枚举、资源引用、多余字段、非法 JSON），
  响应体 ``{"error": "bad_request", "code": ..., "field": ..., "message": ...}``，
  不写审计——这是调用方的问题，改请求再发即可；
* ``409``：请求合法但现场状态拒绝（联锁、配额、消防、持久化），或钉住的快照
  版本已过期，响应体保留原异常类型与组件，同时写审计留痕。
"""

from __future__ import annotations

from ..errors import StoreError
from ..app import StaleSnapshot
from .manifest import SPECS, Spec
from .pages import PageLibrary
from .params import BadRequest, validate
from .router import Router


class Api:
    """把 HTTP 请求翻译成平台调用。"""

    def __init__(self, platform, pages: PageLibrary) -> None:
        self._platform = platform
        self._pages = pages
        self._router = Router()
        self._specs: dict[tuple[str, str], Spec] = {}
        self._register()
        self.self_check()

    @property
    def platform(self):
        return self._platform

    @property
    def pages(self) -> PageLibrary:
        return self._pages

    def _register(self) -> None:
        """按接口清单登记路由；清单是接口存在性的唯一出处。"""

        for spec in SPECS:
            key = (spec.method, spec.path)
            if key in self._specs:
                raise RuntimeError(f"接口清单里存在重复登记: {spec.method} {spec.path}")
            self._specs[key] = spec
            self._router.add(spec.method, spec.path, spec)

    def self_check(self) -> None:
        """启动自检：清单、路由表与处理器方法必须一一对应。

        任何一边对不上都直接抛错终止启动，而不是等到请求打过来才发现：
        清单多了处理器没实现、有人绕过清单直接加路由、路径或方法拼错，
        都属于"接线没接完"，不允许带上线。
        """

        problems: list[str] = []
        for spec in SPECS:
            handler = getattr(self, spec.handler, None)
            if not callable(handler):
                problems.append(f"清单声明的处理器 {spec.handler} 在 Api 上不存在（{spec.method} {spec.path}）")
            if self._router.match(spec.method, self._pattern_sample(spec.path)) is None:
                problems.append(f"清单声明的路由未进路由表: {spec.method} {spec.path}")
        routed = {(route.method, route.pattern) for route in self._router.routes()}
        declared = set(self._specs)
        for method, path in sorted(routed - declared):
            problems.append(f"路由表存在未登记接口: {method} {path}（新增接口必须进接口清单）")
        if problems:
            raise RuntimeError("控制台接口自检失败：\n  - " + "\n  - ".join(problems))

    @staticmethod
    def _pattern_sample(path: str) -> str:
        """给带参路径造一个能命中路由的样例路径。"""

        return "/".join("seg" if segment.startswith("{") and segment.endswith("}") else segment
                        for segment in path.split("/"))

    def routes(self) -> list[dict]:
        """返回接口清单，供自检与文档核对。"""

        return [spec.as_dict() for spec in SPECS]

    def handle(self, method: str, path: str, query: dict, body: dict) -> tuple[int, dict]:
        """处理一次请求，返回状态码与响应体。"""

        matched = self._router.match(method, path)
        if matched is None:
            return 404, {"error": "not_found", "code": "not_found", "path": path}
        spec, path_params = matched
        try:
            params = validate(self._platform, spec.params, query, body, path_params)
            view = None
            if method == "GET" and spec.path != "/api/pages":
                view = self._platform.read_view(params.get("at"))
            payload = getattr(self, spec.handler)(params, body, view)
        except BadRequest as error:
            return 400, {"error": "bad_request", **error.as_record()}
        except StaleSnapshot as error:
            return 409, {"error": type(error).__name__, **error.as_record()}
        except StoreError as error:
            # 控制台拒绝的动作也要留痕，便于巡检区分"谁触发了哪条联锁"。
            self._platform.note_error(error)
            return 409, {"error": type(error).__name__, **error.as_record()}
        except (KeyError, IndexError) as error:
            # 防御层：资源上界理论上已在参数校验里拦住，落到这里说明校验漏了，
            # 统一归成请求错误而不是裸 500，前端仍按 400 路径处理。
            bad = BadRequest(str(error), code="unknown_resource")
            return 400, {"error": "bad_request", **bad.as_record()}
        if spec.write:
            payload = {"revision": self._platform.revision(), **payload}
        return 200, payload

    # ------------------------------------------------------------------
    # 只读处理器：全部从同一份 read_view 投影，页面与接口因此同源同版本。
    # ------------------------------------------------------------------

    def h_view(self, params: dict, body: dict, view: dict) -> dict:
        return view

    def h_status(self, params: dict, body: dict, view: dict) -> dict:
        return view["status"]

    def h_snapshot(self, params: dict, body: dict, view: dict) -> dict:
        return {
            "revision": view["revision"],
            "generated_at": view["generated_at"],
            "status": view["status"],
            "capacity": view["capacity"],
            "quota": view["quota"],
            "audit": view["audit"],
            "mapping": view["mapping"],
            "config": view["config"],
        }

    def h_readiness(self, params: dict, body: dict, view: dict) -> dict:
        return view["readiness"]

    def h_topology(self, params: dict, body: dict, view: dict) -> dict:
        return view["topology"]

    def h_namespaces(self, params: dict, body: dict, view: dict) -> dict:
        return {"devices": view["namespaces"]}

    def h_namespace(self, params: dict, body: dict, view: dict) -> dict:
        return next(item for item in view["namespaces"] if item["address"] == params["address"])

    def h_capacity(self, params: dict, body: dict, view: dict) -> dict:
        return view["capacity"]

    def h_racks(self, params: dict, body: dict, view: dict) -> dict:
        return {"racks": view["racks"], "revision": view["revision"]}

    def h_rack(self, params: dict, body: dict, view: dict) -> dict:
        return next(rack for rack in view["racks"] if rack["rack_id"] == params["rack_id"])

    def h_mapping(self, params: dict, body: dict, view: dict) -> dict:
        return view["mapping"]

    def h_bms(self, params: dict, body: dict, view: dict) -> dict:
        return view["bms"]

    def h_pcs(self, params: dict, body: dict, view: dict) -> dict:
        return view["pcs"]

    def h_temp(self, params: dict, body: dict, view: dict) -> dict:
        return view["temp"]

    def h_fire(self, params: dict, body: dict, view: dict) -> dict:
        return view["fire"]

    def h_grid(self, params: dict, body: dict, view: dict) -> dict:
        return view["grid"]

    def h_soc(self, params: dict, body: dict, view: dict) -> dict:
        return view["soc"]

    def h_balance(self, params: dict, body: dict, view: dict) -> dict:
        return view["balance"]

    def h_quota(self, params: dict, body: dict, view: dict) -> dict:
        return view["quota"]

    def h_audit(self, params: dict, body: dict, view: dict) -> dict:
        entries = [
            entry
            for entry in view["audit_entries"]
            if (params["component"] is None or entry["component"] == params["component"])
            and (params["severity"] is None or entry["severity"] == params["severity"])
            and (params["action"] is None or entry["action"] == params["action"])
        ]
        entries = list(reversed(entries[-params["limit"]:]))
        return {"entries": entries, "revision": view["revision"]}

    def h_audit_report(self, params: dict, body: dict, view: dict) -> dict:
        return view["audit_report"]

    def h_audit_operations(self, params: dict, body: dict, view: dict) -> dict:
        return {"operations": view["audit_operations"][: params["limit"]]}

    def h_alarms(self, params: dict, body: dict, view: dict) -> dict:
        return {"alarms": view["audit_alarms"], "revision": view["revision"]}

    def h_alerts(self, params: dict, body: dict, view: dict) -> dict:
        return {"alerts": view["alerts"], "revision": view["revision"]}

    def h_events(self, params: dict, body: dict, view: dict) -> dict:
        topic = params["topic"]
        events = view["events"] if topic is None else [event for event in view["events"] if event["topic"] == topic]
        return {"events": events, "topics": view["event_topics"], "revision": view["revision"]}

    def h_trend(self, params: dict, body: dict, view: dict) -> dict:
        signal = params["signal"]
        data = view["trend"]["series"][signal]
        return {
            "signal": signal,
            "period_s": view["trend"]["period_s"],
            "mean": data["mean"],
            "samples": data["samples"][-params["limit"]:],
            "revision": view["revision"],
        }

    def h_store(self, params: dict, body: dict, view: dict) -> dict:
        return view["store"]

    def h_timeline(self, params: dict, body: dict, view: dict) -> dict:
        station = params["station"]
        entries = view["audit_entries"]
        if station is not None:
            entries = [entry for entry in entries if entry["station"] == station]
        timeline = [
            {
                "ts": entry["ts"],
                "component": entry["component"],
                "action": entry["action"],
                "severity": entry["severity"],
                "message": entry["message"],
                "op_id": entry["payload"].get("op_id", ""),
            }
            for entry in entries
        ]
        return {"timeline": timeline[-params["limit"]:], "revision": view["revision"]}

    def h_journal(self, params: dict, body: dict, view: dict) -> dict:
        payload = dict(view["journal"])
        segment = params["segment"]
        if segment is not None:
            payload["records"] = self._platform.audit.replay_segment(segment)
        return payload

    def h_config(self, params: dict, body: dict, view: dict) -> dict:
        return view["config"]

    def h_pages(self, params: dict, body: dict, view: dict | None) -> dict:
        return {"pages": self._pages.index(), "routes": self.routes()}

    # ------------------------------------------------------------------
    # 写处理器：直接透传清单校验后的参数，平台侧只处理业务规则。
    # ------------------------------------------------------------------

    def h_start(self, params: dict, body: dict, view: dict | None) -> dict:
        return self._platform.start()

    def h_stop(self, params: dict, body: dict, view: dict | None) -> dict:
        return self._platform.stop()

    def h_reset(self, params: dict, body: dict, view: dict | None) -> dict:
        return self._platform.reset_state()

    def h_step(self, params: dict, body: dict, view: dict | None) -> dict:
        return self._platform.cycle(dt=params["dt"])

    def h_transfer(self, params: dict, body: dict, view: dict | None) -> dict:
        return self._platform.transfer_grid_tie(
            power_kw=params["power_kw"], frequency_hz=params["frequency_hz"], dt=params["dt"]
        )

    def h_off_grid(self, params: dict, body: dict, view: dict | None) -> dict:
        return self._platform.transfer_off_grid()

    def h_discharge(self, params: dict, body: dict, view: dict | None) -> dict:
        return self._platform.discharge(power_kw=params["power_kw"], dt=params["dt"])

    def h_charge(self, params: dict, body: dict, view: dict | None) -> dict:
        return self._platform.charge(rack_id=params["rack_id"], steps=params["steps"], dt=params["dt"])

    def h_balance_post(self, params: dict, body: dict, view: dict | None) -> dict:
        return self._platform.balance(
            rack_id=params["rack_id"], dt=params["dt"], max_steps=params["max_steps"]
        )

    def h_temperature(self, params: dict, body: dict, view: dict | None) -> dict:
        return self._platform.sample_temperature(
            rack_id=params["rack_id"], temperature_c=params["temperature_c"], dt=params["dt"]
        )

    def h_replace_pack(self, params: dict, body: dict, view: dict | None) -> dict:
        return self._platform.replace_pack_rating(
            pack_index=params["pack_index"], rating_v=params["rating_v"], rack_id=params["rack_id"]
        )

    def h_calibrate(self, params: dict, body: dict, view: dict | None) -> dict:
        return self._platform.calibrate_soc(soc_pct=params["soc_pct"], source=params["source"])

    def h_fault(self, params: dict, body: dict, view: dict | None) -> dict:
        return self._platform.report_fault(rack_id=params["rack_id"], code=params["code"])

    def h_fault_clear(self, params: dict, body: dict, view: dict | None) -> dict:
        return self._platform.clear_fault(rack_id=params["rack_id"])

    def h_drill(self, params: dict, body: dict, view: dict | None) -> dict:
        return self._platform.fire_drill(
            zone=params["zone"],
            smoke_ppm=params["smoke_ppm"],
            temperature_c=params["temperature_c"],
            confirm_dt=params["confirm_dt"],
        )

    def h_fire_release(self, params: dict, body: dict, view: dict | None) -> dict:
        return self._platform.release_fire()

    def h_frequency(self, params: dict, body: dict, view: dict | None) -> dict:
        return self._platform.monitor_frequency(frequency_hz=params["frequency_hz"])

    def h_set_soc(self, params: dict, body: dict, view: dict | None) -> dict:
        return self._platform.set_soc(rack_id=params["rack_id"], soc_pct=params["soc_pct"])

    def h_disturb(self, params: dict, body: dict, view: dict | None) -> dict:
        return self._platform.disturb_cell(
            rack_id=params["rack_id"], cell_index=params["cell_index"], delta_v=params["delta_v"]
        )

    def h_probe_pcs(self, params: dict, body: dict, view: dict | None) -> dict:
        return self._platform.concurrency_probe(rounds=params["rounds"])

    def h_probe_fault(self, params: dict, body: dict, view: dict | None) -> dict:
        return self._platform.concurrency_fault_probe(rounds=params["rounds"])
