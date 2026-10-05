"""读写接口共用的参数规格与校验。

页面与接口看到的口径必须一致，因此参数解析不允许散落在各个处理器里：每个
接口在 :mod:`batterystore.console.manifest` 里声明一份 :class:`ParamSpec`，
GET 的 query、路径段与 POST 的 body 全部走 :func:`validate` 这一条校验链路。

非法参数统一抛 :class:`BadRequest`（HTTP 400），与业务拒绝（HTTP 409）分开。
两者的边界是：

* **请求错误 400**：不看现场状态，光看请求本身就能判定它错了——类型不对
  （``power_kw`` 传了字符串、整数位传 2.5）、取值越界（``limit`` 为负、
  版本钉为负数）、引用了不存在的资源（未知簇 ``rk-99``、未知设备地址）、
  枚举值不合法、请求体不是 JSON 对象、出现接口未声明的多余字段。改请求
  就能修好，不会写审计。
* **业务拒绝 409**：请求本身合法，是当前运行态不允许这个动作——联锁顺序不满足、
  消防侧未复位、配额不足、持久化失败。需要现场换状态或人工复位才会消失，
  仍按原口径写审计留痕。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..errors import StoreError

QUERY = "query"
BODY = "body"
PATH = "path"

DEFAULT_RACK_ID = "rk-01"

_MISSING = object()


class BadRequest(StoreError):
    """请求本身不合法：参数类型、取值或资源引用有误，对应 HTTP 400。"""

    def __init__(self, message: str, *, field: str | None = None, code: str = "invalid_parameter") -> None:
        super().__init__(message, component="console")
        self.field = field
        self.code = code

    def as_record(self) -> dict:
        record = super().as_record()
        record["code"] = self.code
        if self.field is not None:
            record["field"] = self.field
        return record


@dataclass(frozen=True)
class ParamSpec:
    """单个参数的声明：名字、来源、类型与取值约束。

    ``kind`` 为 ``str`` 时可给 ``choices`` 做枚举校验；为数值时可给
    ``minimum`` / ``maximum`` 做闭区间校验。``required`` 为 ``False`` 时
    ``default`` 是缺省值。``resource`` 非空表示该值必须是平台上存在的资源
    （目前支持 ``rack`` / ``zone`` / ``address`` / ``signal``），引用不存在
    的资源属于请求错误而不是业务拒绝。
    """

    name: str
    source: str = QUERY
    kind: type | str = str
    default: Any = _MISSING
    required: bool = False
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[Any, ...] | None = None
    resource: str | None = None
    depends_on: str | None = None

    def has_default(self) -> bool:
        return self.default is not _MISSING


def _bad(message: str, *, field: str | None = None, code: str = "invalid_parameter") -> BadRequest:
    return BadRequest(message, field=field, code=code)


def _convert(spec: ParamSpec, raw: Any) -> Any:
    kind = spec.kind
    if kind is int:
        if isinstance(raw, bool):
            raise _bad(f"参数 {spec.name!r} 必须是整数", field=spec.name)
        if isinstance(raw, int):
            value = raw
        elif isinstance(raw, float):
            if not raw.is_integer():
                raise _bad(f"参数 {spec.name!r} 必须是整数", field=spec.name)
            value = int(raw)
        else:
            text = str(raw).strip()
            if not text.lstrip("+-").isdigit():
                raise _bad(f"参数 {spec.name!r} 必须是整数", field=spec.name)
            try:
                value = int(text)
            except ValueError as error:  # pragma: no cover - 上面的形态已挡住
                raise _bad(f"参数 {spec.name!r} 必须是整数", field=spec.name) from error
    elif kind is float:
        if isinstance(raw, bool):
            raise _bad(f"参数 {spec.name!r} 必须是数值", field=spec.name)
        try:
            value = float(raw)
        except (TypeError, ValueError) as error:
            raise _bad(f"参数 {spec.name!r} 必须是数值", field=spec.name) from error
        if value != value or value in (float("inf"), float("-inf")):
            raise _bad(f"参数 {spec.name!r} 必须是有限数值", field=spec.name)
    elif kind is str:
        if not isinstance(raw, str) or not raw.strip():
            raise _bad(f"参数 {spec.name!r} 必须是非空字符串", field=spec.name)
        value = raw
    elif kind is bool:
        if not isinstance(raw, bool):
            raise _bad(f"参数 {spec.name!r} 必须是布尔值", field=spec.name)
        value = raw
    else:
        value = raw
    return value


def _check_range(spec: ParamSpec, value: Any) -> None:
    if spec.kind not in (int, float):
        return
    if spec.minimum is not None and value < spec.minimum:
        raise _bad(f"参数 {spec.name!r} 不能小于 {spec.minimum}", field=spec.name, code="out_of_range")
    if spec.maximum is not None and value > spec.maximum:
        raise _bad(f"参数 {spec.name!r} 不能大于 {spec.maximum}", field=spec.name, code="out_of_range")


def _check_choices(spec: ParamSpec, value: Any) -> None:
    if spec.choices is not None and value not in spec.choices:
        allowed = "、".join(str(item) for item in spec.choices)
        raise _bad(f"参数 {spec.name!r} 只允许取：{allowed}", field=spec.name, code="invalid_choice")


def _check_resource(platform, spec: ParamSpec, value: Any) -> None:
    if spec.resource is None:
        return
    if spec.resource == "rack" and value not in platform.racks:
        raise _bad(f"未知的簇: {value}", field=spec.name, code="unknown_resource")
    if spec.resource == "zone" and value not in platform.fire.zones():
        raise _bad(f"未知的消防分区: {value}", field=spec.name, code="unknown_resource")
    if spec.resource == "address" and value not in platform.namespaces.addresses_set():
        raise _bad(f"未知设备地址: {value}", field=spec.name, code="unknown_resource")
    if spec.resource == "signal" and value not in platform.trend_signals():
        raise _bad(f"未知的趋势信号: {value}", field=spec.name, code="unknown_resource")


def _check_depends(platform, spec: ParamSpec, value: Any, cleaned: dict) -> None:
    """跨字段上界校验：序号上界取决于另一参数指定的簇。

    ``depends_on`` 写成 ``"<关联字段>:<数量类型>"``，如 ``"rack_id:pack"``
    表示用 ``rack_id`` 指定的簇的电池包数量做上界。关联字段缺失时按默认簇。
    """

    if spec.depends_on is None or value is None:
        return
    field_name, _, kind = spec.depends_on.partition(":")
    if not kind:
        return
    rack_id = cleaned.get(field_name) or DEFAULT_RACK_ID
    rack = platform.racks.get(rack_id)
    if rack is None:
        # 依赖字段本身非法时已在资源校验里报过，这里不重复报。
        return
    if kind == "pack":
        bound = len(rack.packs())
        unit = "个电池包"
    elif kind == "cell":
        bound = len(rack.cells())
        unit = "个单体"
    else:
        return
    if value >= bound:
        raise _bad(
            f"参数 {spec.name!r} 越界：簇 {rack_id} 只有 {bound} {unit}",
            field=spec.name,
            code="out_of_range",
        )


def validate(platform, specs: tuple[ParamSpec, ...], query: dict, body: dict, path: dict) -> dict:
    """按规格校验并清洗一次请求的全部参数，返回可直接透传的字典。

    任何未声明的字段一律拒绝（防止调用方把拼错的参数名当成已生效），缺必填
    或缺值、类型错误、越界、枚举不合法、资源不存在都抛 :class:`BadRequest`。
    """

    sources = {QUERY: dict(query), BODY: dict(body), PATH: dict(path)}
    declared = {(spec.source, spec.name) for spec in specs}
    for source, raw in sources.items():
        for name in raw:
            if (source, name) not in declared:
                raise _bad(f"接口不接受参数 {name!r}（{source}）", field=name, code="unknown_parameter")

    cleaned: dict[str, Any] = {}
    for spec in specs:
        raw = sources[spec.source].get(spec.name, _MISSING)
        if raw is _MISSING or raw is None:
            if spec.has_default():
                cleaned[spec.name] = spec.default
                continue
            if spec.required:
                raise _bad(f"缺少必填参数 {spec.name!r}", field=spec.name, code="missing_parameter")
            cleaned[spec.name] = None
            continue
        value = _convert(spec, raw)
        _check_range(spec, value)
        _check_choices(spec, value)
        _check_resource(platform, spec, value)
        cleaned[spec.name] = value
    for spec in specs:
        _check_depends(platform, spec, cleaned.get(spec.name), cleaned)
    return cleaned
