"""读写接口共用的参数校验层。

页面要读的接口和动作类写接口走同一套校验规则：每条路由声明路径参数
（``params``，含路径段与 query string）和请求体字段（``body``）的
:class:`Field` 规则，:meth:`Schema.validate` 在进入业务处理器之前一次性完成：

1. 取值（缺必填 → 400）；
2. 类型（不是 bool／数值／整数／字符串 → 400，``NaN``／``Infinity`` 也拒绝）；
3. 边界（``min``／``max``／枚举 → 400）。

**边界口径（400 与 409 怎么划）**：能光凭参数本身判定的"形状"问题——缺失、
类型不对、数值越出该字段的物理取值域（例如 ``dt <= 0``、``steps <= 0``、
序号为负）——一律是 :class:`RequestError`，HTTP ``400``，前端改参数后重试
即可，这类拒绝不写业务审计（参数根本没进到业务层）；参数形状合法、但当前
系统状态不允许动作（联锁未满足、配额不足、消防态未复位）由平台抛
:class:`~batterystore.errors.StoreError`，HTTP ``409``，这类拒绝照常写审计。
不采用"非法参数一律按请求错误返回"之外的任何含糊口径，但也不把业务拒绝
塞进 400——那样前端无法区分"改参数"还是"等状态/走复位流程"，配额不足会被
当成输入错误提示给用户。寻址失败（未知簇、未知设备地址）是 ``404``，由
分发层负责，不在本层。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Iterable

# 物理正量（步长、时长、功率）的下界：必须严格大于 0，用极小正数表达。
POSITIVE_EPS = 1e-9


class RequestError(Exception):
    """请求本身不合法（参数缺失、类型错误或越界），HTTP 边界翻译为 400。"""

    def __init__(self, message: str, *, field_name: str | None = None, code: str = "invalid_parameter") -> None:
        super().__init__(message)
        self.message = message
        self.field_name = field_name
        self.code = code

    def as_body(self) -> dict:
        """转成统一错误响应体（与 409 等错误响应同构）。"""

        body = {"error": "invalid_request", "code": self.code, "message": self.message}
        if self.field_name is not None:
            body["field"] = self.field_name
        return body


@dataclass(frozen=True)
class Field:
    """一个参数字段的校验规则。

    ``kind`` 取值：``"str"``、``"float"``、``"int"``、``"bool"``。
    ``required`` 为 ``False`` 时缺失与 ``null`` 都回落到 ``default``。
    """

    kind: str = "str"
    required: bool = False
    default: Any = None
    minimum: float | None = None
    maximum: float | None = None
    choices: tuple[str, ...] | None = None
    doc: str = ""

    def check(self, source: dict, name: str) -> Any:
        if name not in source or source[name] is None:
            if self.required:
                raise RequestError(f"缺少必填参数 {name!r}", field_name=name, code="missing_parameter")
            return self.default
        return self._coerce(source[name], name)

    def _coerce(self, raw: Any, name: str) -> Any:
        if self.kind == "str":
            if not isinstance(raw, str) or not raw.strip():
                raise RequestError(f"参数 {name!r} 必须是非空字符串", field_name=name)
            value: Any = raw.strip()
        elif self.kind == "bool":
            if not isinstance(raw, bool):
                raise RequestError(f"参数 {name!r} 必须是布尔值", field_name=name)
            value = raw
        elif self.kind == "int":
            value = self._as_int(raw, name)
        elif self.kind == "float":
            value = self._as_float(raw, name)
        else:  # pragma: no cover - 规则声明错误属于装配问题
            raise RequestError(f"参数 {name!r} 的校验规则未知: {self.kind}", field_name=name)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if self.minimum is not None and value < self.minimum:
                raise RequestError(f"参数 {name!r} 不能小于 {self.minimum:g}", field_name=name)
            if self.maximum is not None and value > self.maximum:
                raise RequestError(f"参数 {name!r} 不能大于 {self.maximum:g}", field_name=name)
        if self.choices is not None and value not in self.choices:
            allowed = ", ".join(self.choices)
            raise RequestError(f"参数 {name!r} 必须是以下取值之一: {allowed}", field_name=name)
        return value

    @staticmethod
    def _as_float(raw: Any, name: str) -> float:
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            raise RequestError(f"参数 {name!r} 必须是数值", field_name=name)
        try:
            value = float(raw)
        except ValueError as error:
            raise RequestError(f"参数 {name!r} 必须是数值", field_name=name) from error
        if not math.isfinite(value):
            raise RequestError(f"参数 {name!r} 必须是有限数值", field_name=name)
        return value

    @classmethod
    def _as_int(cls, raw: Any, name: str) -> int:
        if isinstance(raw, bool):
            raise RequestError(f"参数 {name!r} 必须是整数", field_name=name)
        if isinstance(raw, int):
            return raw
        if isinstance(raw, float):
            if not raw.is_integer():
                raise RequestError(f"参数 {name!r} 必须是整数", field_name=name)
            return int(raw)
        if isinstance(raw, str):
            text = raw.strip()
            try:
                return int(text)
            except ValueError:
                pass
            try:
                value = float(text)
            except ValueError as error:
                raise RequestError(f"参数 {name!r} 必须是整数", field_name=name) from error
            if not math.isfinite(value) or not value.is_integer():
                raise RequestError(f"参数 {name!r} 必须是整数", field_name=name)
            return int(value)
        raise RequestError(f"参数 {name!r} 必须是整数", field_name=name)


def required(kind: str, **bounds: Any) -> Field:
    """声明一个必填字段。"""

    return Field(kind=kind, required=True, **bounds)


def optional(kind: str, default: Any, **bounds: Any) -> Field:
    """声明一个带默认值的可选字段。"""

    return Field(kind=kind, required=False, default=default, **bounds)


def positive_float(default: float) -> Field:
    """正的浮点字段（严格大于 0，用于步长、时长、功率）。"""

    return Field(kind="float", default=default, minimum=POSITIVE_EPS)


def nonneg_int(default: int) -> Field:
    """非负整数字段（用于条数、序号、轮数）。"""

    return Field(kind="int", default=default, minimum=0)


def bounded_float(default: float, minimum: float, maximum: float) -> Field:
    """带闭区间边界的浮点字段（如百分比）。"""

    return Field(kind="float", default=default, minimum=minimum, maximum=maximum)


@dataclass(frozen=True)
class Schema:
    """一条路由的完整入参规则：路径/query 参数 + 请求体字段。"""

    params: dict[str, Field] = field(default_factory=dict)
    body: dict[str, Field] = field(default_factory=dict)

    def validate(self, params: dict, body: dict) -> tuple[dict, dict]:
        """校验并返回类型化、补全默认值后的 ``(params, body)``。"""

        if not isinstance(body, dict):
            raise RequestError("请求体必须是 JSON 对象", code="invalid_body")
        clean_params = {name: rule.check(params, name) for name, rule in self.params.items()}
        clean_body = {name: rule.check(body, name) for name, rule in self.body.items()}
        return clean_params, clean_body
