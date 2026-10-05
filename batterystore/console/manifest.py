"""控制台接口清单：接口登记的唯一出处。

新增接口必须在这里登记一条 :class:`Spec`，处理器方法在 :class:`batterystore.console.api.Api`
上用清单里的 ``handler`` 同名实现。控制台启动时执行双向自检：

* 清单里的每条声明在路由表和 ``Api`` 上都必须真实存在（防止登记了却没接上）；
* 路由表里的每条路由都必须来自清单（防止有人绕过清单直接 ``router.add``，
  新增接口忘了登记在启动时立刻失败，而不是等交付巡检才发现）。

参数声明（``params``）同时是读接口（query/路径段）和写接口（body）的唯一
校验来源，页面与接口因此共用同一套参数口径。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .params import BODY, PATH, QUERY, ParamSpec


@dataclass(frozen=True)
class Spec:
    """一条接口声明。"""

    method: str
    path: str
    handler: str
    params: tuple[ParamSpec, ...] = field(default_factory=tuple)
    write: bool = False

    def as_dict(self) -> dict:
        return {
            "method": self.method,
            "path": self.path,
            "handler": self.handler,
            "write": self.write,
            "params": [
                {
                    "name": param.name,
                    "source": param.source,
                    "type": getattr(param.kind, "__name__", str(param.kind)),
                    "required": param.required,
                }
                for param in self.params
            ],
        }


# 所有 GET 读接口共用一个版本钉参数：先取一次拿到 revision，后续请求带上
# ``at=<revision>`` 就能钉住同一代数据，页面刷新与接口调用并发也不会串版本。
# 平台未发生变更时当前版本就是 0，因此下限取 0。
_PIN = ParamSpec("at", QUERY, int, minimum=0)

_LIMITS = {
    "audit": ParamSpec("limit", QUERY, int, default=100, minimum=1, maximum=500),
    "operations": ParamSpec("limit", QUERY, int, default=20, minimum=1, maximum=200),
    "trend": ParamSpec("limit", QUERY, int, default=60, minimum=1, maximum=240),
    "timeline": ParamSpec("limit", QUERY, int, default=200, minimum=1, maximum=500),
}


def _specs() -> tuple[Spec, ...]:
    return (
        # ---- 只读：总览与视图 ----
        Spec("GET", "/api/view", "h_view"),
        Spec("GET", "/api/status", "h_status", (_PIN,)),
        Spec("GET", "/api/snapshot", "h_snapshot", (_PIN,)),
        Spec("GET", "/api/readiness", "h_readiness", (_PIN,)),
        Spec("GET", "/api/topology", "h_topology", (_PIN,)),
        # ---- 只读：命名空间与容量 ----
        Spec("GET", "/api/namespaces", "h_namespaces", (_PIN,)),
        Spec(
            "GET",
            "/api/namespace/{address}",
            "h_namespace",
            (_PIN, ParamSpec("address", PATH, str, required=True, resource="address")),
        ),
        Spec("GET", "/api/capacity", "h_capacity", (_PIN,)),
        # ---- 只读：簇 ----
        Spec("GET", "/api/racks", "h_racks", (_PIN,)),
        Spec(
            "GET",
            "/api/rack/{rack_id}",
            "h_rack",
            (_PIN, ParamSpec("rack_id", PATH, str, required=True, resource="rack")),
        ),
        Spec("GET", "/api/mapping", "h_mapping", (_PIN,)),
        # ---- 只读：组件快照 ----
        Spec("GET", "/api/bms", "h_bms", (_PIN,)),
        Spec("GET", "/api/pcs", "h_pcs", (_PIN,)),
        Spec("GET", "/api/temp", "h_temp", (_PIN,)),
        Spec("GET", "/api/fire", "h_fire", (_PIN,)),
        Spec("GET", "/api/grid", "h_grid", (_PIN,)),
        Spec("GET", "/api/soc", "h_soc", (_PIN,)),
        Spec("GET", "/api/balance", "h_balance", (_PIN,)),
        Spec("GET", "/api/quota", "h_quota", (_PIN,)),
        # ---- 只读：审计、事件与流水 ----
        Spec(
            "GET",
            "/api/audit",
            "h_audit",
            (
                _PIN,
                ParamSpec("component", QUERY, str),
                ParamSpec("severity", QUERY, str, choices=("notice", "warning", "alarm")),
                ParamSpec("action", QUERY, str),
                _LIMITS["audit"],
            ),
        ),
        Spec("GET", "/api/audit/report", "h_audit_report", (_PIN,)),
        Spec("GET", "/api/audit/operations", "h_audit_operations", (_PIN, _LIMITS["operations"])),
        Spec("GET", "/api/alarms", "h_alarms", (_PIN,)),
        Spec("GET", "/api/alerts", "h_alerts", (_PIN,)),
        Spec(
            "GET",
            "/api/events",
            "h_events",
            (_PIN, ParamSpec("topic", QUERY, str)),
        ),
        Spec(
            "GET",
            "/api/trend",
            "h_trend",
            (
                _PIN,
                ParamSpec("signal", QUERY, str, default="rk-01.temperature", resource="signal"),
                _LIMITS["trend"],
            ),
        ),
        Spec("GET", "/api/store", "h_store", (_PIN,)),
        Spec(
            "GET",
            "/api/timeline",
            "h_timeline",
            (_PIN, ParamSpec("station", QUERY, str), _LIMITS["timeline"]),
        ),
        Spec(
            "GET",
            "/api/journal",
            "h_journal",
            (_PIN, ParamSpec("segment", QUERY, int, minimum=0, maximum=4095)),
        ),
        Spec("GET", "/api/config", "h_config", (_PIN,)),
        # ---- 自检 ----
        Spec("GET", "/api/pages", "h_pages"),
        # ---- 动作：平台运行态 ----
        Spec("POST", "/api/start", "h_start", (), write=True),
        Spec("POST", "/api/stop", "h_stop", (), write=True),
        Spec("POST", "/api/reset", "h_reset", (), write=True),
        Spec(
            "POST",
            "/api/step",
            "h_step",
            (ParamSpec("dt", BODY, float, default=5.0, minimum=0.001, maximum=3600.0),),
            write=True,
        ),
        # ---- 动作：并网与功率 ----
        Spec(
            "POST",
            "/api/transfer",
            "h_transfer",
            (
                ParamSpec("power_kw", BODY, float, default=120.0, minimum=0.0, maximum=10_000.0),
                ParamSpec("frequency_hz", BODY, float, default=50.0, minimum=0.0, maximum=100.0),
                ParamSpec("dt", BODY, float, default=1.0, minimum=0.001, maximum=3600.0),
            ),
            write=True,
        ),
        Spec("POST", "/api/off-grid", "h_off_grid", (), write=True),
        Spec(
            "POST",
            "/api/discharge",
            "h_discharge",
            (
                ParamSpec("power_kw", BODY, float, default=200.0, minimum=0.0, maximum=10_000.0),
                ParamSpec("dt", BODY, float, default=60.0, minimum=0.001, maximum=86_400.0),
            ),
            write=True,
        ),
        Spec(
            "POST",
            "/api/charge",
            "h_charge",
            (
                ParamSpec("rack_id", BODY, str, resource="rack"),
                ParamSpec("steps", BODY, int, default=3, minimum=1, maximum=100),
                ParamSpec("dt", BODY, float, default=60.0, minimum=0.001, maximum=86_400.0),
            ),
            write=True,
        ),
        Spec(
            "POST",
            "/api/balance",
            "h_balance_post",
            (
                ParamSpec("rack_id", BODY, str, resource="rack"),
                ParamSpec("dt", BODY, float, default=2.0, minimum=0.001, maximum=3600.0),
                ParamSpec("max_steps", BODY, int, default=400, minimum=1, maximum=100_000),
            ),
            write=True,
        ),
        # ---- 动作：温度与维护 ----
        Spec(
            "POST",
            "/api/temperature",
            "h_temperature",
            (
                ParamSpec("rack_id", BODY, str, resource="rack"),
                ParamSpec("temperature_c", BODY, float, default=30.0, minimum=-60.0, maximum=200.0),
                ParamSpec("dt", BODY, float, default=5.0, minimum=0.001, maximum=3600.0),
            ),
            write=True,
        ),
        Spec(
            "POST",
            "/api/replace-pack",
            "h_replace_pack",
            (
                ParamSpec("pack_index", BODY, int, default=0, minimum=0, depends_on="rack_id:pack"),
                ParamSpec("rating_v", BODY, float, default=3.35, minimum=0.1, maximum=10.0),
                ParamSpec("rack_id", BODY, str, resource="rack"),
            ),
            write=True,
        ),
        Spec(
            "POST",
            "/api/calibrate",
            "h_calibrate",
            (
                ParamSpec("soc_pct", BODY, float, default=50.0, minimum=0.0, maximum=100.0),
                ParamSpec("source", BODY, str, default="console"),
            ),
            write=True,
        ),
        # ---- 动作：故障 ----
        Spec(
            "POST",
            "/api/fault",
            "h_fault",
            (
                ParamSpec("rack_id", BODY, str, resource="rack"),
                ParamSpec("code", BODY, str, default="bms_fault"),
            ),
            write=True,
        ),
        Spec(
            "POST",
            "/api/fault/clear",
            "h_fault_clear",
            (ParamSpec("rack_id", BODY, str, resource="rack"),),
            write=True,
        ),
        # ---- 动作：消防 ----
        Spec(
            "POST",
            "/api/fire/drill",
            "h_drill",
            (
                ParamSpec("zone", BODY, str, default="Z1", resource="zone"),
                ParamSpec("smoke_ppm", BODY, float, default=1500.0, minimum=0.0, maximum=100_000.0),
                ParamSpec("temperature_c", BODY, float, default=30.0, minimum=-60.0, maximum=300.0),
                ParamSpec("confirm_dt", BODY, float, default=5.0, minimum=0.001, maximum=3600.0),
            ),
            write=True,
        ),
        Spec("POST", "/api/fire/release", "h_fire_release", (), write=True),
        # ---- 动作：电网与 SOC ----
        Spec(
            "POST",
            "/api/frequency",
            "h_frequency",
            (ParamSpec("frequency_hz", BODY, float, default=50.0, minimum=0.0, maximum=100.0),),
            write=True,
        ),
        Spec(
            "POST",
            "/api/soc",
            "h_set_soc",
            (
                ParamSpec("rack_id", BODY, str, resource="rack"),
                ParamSpec("soc_pct", BODY, float, default=50.0, minimum=0.0, maximum=100.0),
            ),
            write=True,
        ),
        Spec(
            "POST",
            "/api/disturb",
            "h_disturb",
            (
                ParamSpec("rack_id", BODY, str, resource="rack"),
                ParamSpec("cell_index", BODY, int, default=0, minimum=0, depends_on="rack_id:cell"),
                ParamSpec("delta_v", BODY, float, default=0.03, minimum=-1.0, maximum=1.0),
            ),
            write=True,
        ),
        # ---- 动作：自检探测 ----
        Spec(
            "POST",
            "/api/probe/pcs",
            "h_probe_pcs",
            (ParamSpec("rounds", BODY, int, default=32, minimum=1, maximum=10_000),),
            write=True,
        ),
        Spec(
            "POST",
            "/api/probe/fault",
            "h_probe_fault",
            (ParamSpec("rounds", BODY, int, default=16, minimum=1, maximum=10_000),),
            write=True,
        ),
    )


SPECS: tuple[Spec, ...] = _specs()
