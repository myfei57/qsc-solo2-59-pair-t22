"""平台统一异常类型。

储能电站的失败必须按性质分开：顺序或联锁条件不满足属于可恢复的运行期拒绝；
消防相关异常属于安全侧，必须保持状态直到人工复位；配额不足属于记账侧拒绝；
配置与持久化失败则属于装配期就要拦下的接线问题。审计按异常类型分别归类，
巡检时才能一眼分清责任面。

HTTP 边界（``batterystore.console``）按下面的映射口径把这些异常翻译成状态码：

* 配置、装配期问题 —— :class:`ConfigError`、:class:`NamespaceError` 里的重复
  登记属于装配错误，正常运行期不该出现，落到 ``500``；
* 寻址失败 —— :class:`NamespaceError`（未知地址）、:class:`KeyError` 翻译成
  ``404``，属于"请求的资源不存在"，不是业务拒绝；
* 运行期拒绝 —— :class:`InterlockError`、:class:`FireError`、
  :class:`QuotaError` 翻译成 ``409``，请求本身合法，是当前系统状态不允许；
* 持久化失败 —— :class:`StorageError` 翻译成 ``503``，是服务侧故障而非请求问题。

请求格式与参数本身不合法（缺字段、类型不对、数值越界）由控制台校验层抛
``RequestError``，统一翻译为 ``400``，绝不与 409 混用。
"""

from __future__ import annotations


class StoreError(Exception):
    """所有平台异常的基类，携带出错的组件名。"""

    def __init__(self, message: str, *, component: str = "batterystore") -> None:
        super().__init__(message)
        self.message = message
        self.component = component

    def as_record(self) -> dict:
        """转成审计可以直接落盘的一条记录。"""

        return {
            "type": type(self).__name__,
            "component": self.component,
            "message": self.message,
        }


class InterlockError(StoreError):
    """顺序或联锁条件不满足，动作被拒绝。"""


class FireError(StoreError):
    """消防联动顺序失败或灭火介质未到位。"""


class QuotaError(StoreError):
    """充放电配额不足或跨周期记账失败。"""


class StorageError(StoreError):
    """持久化读写失败。"""


class ConfigError(StoreError):
    """配置缺失或取值非法。"""


class NamespaceError(StoreError):
    """命名空间重复注册或解析失败。"""
