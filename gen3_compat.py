"""双世代兼容辅助（KiraAI 3.0 GA 弃用面，见 GA_DEPRECATIONS DEP-2/3）。

3.0 正式版把事件类的首字段 `message_types` 改名为 `supported_elements`
（旧名仍被 `_accept_legacy_message_types` 兼容层接收，但已 @deprecated）；
适配器的 `message_types` 属性同样弃用，官方替代是
`adapter.get_capability(IMCapability).get_message_metadata().supported_elements`。

本模块用 dataclasses / 能力探测自动选对写法，**不绑版本号**，2.x 与 3.x 通吃。
"""
from __future__ import annotations


def elements_kw(cls, value):
    """按当前框架事件类的字段名构造关键字参数（2.x=message_types，3.0=supported_elements）。"""
    try:
        from dataclasses import fields as _dc_fields
        names = {f.name for f in _dc_fields(cls)}
    except Exception:
        names = set()
    key = "supported_elements" if "supported_elements" in names else "message_types"
    return {key: value}


def adapter_supported_elements(adapter, fallback=None):
    """双世代取适配器支持的元素类型列表：3.0 走 IMCapability 元数据；2.x 读 message_types。"""
    get_cap = getattr(adapter, "get_capability", None)
    if callable(get_cap):
        try:
            from core.adapter.capabilities import IMCapability
            meta = get_cap(IMCapability).get_message_metadata()
            els = getattr(meta, "supported_elements", None)
            if els is not None:
                return list(els)
        except Exception:
            pass
    els = getattr(adapter, "message_types", None)
    if els is not None:
        return list(els)
    return list(fallback or [])
