"""用例和适配器共享的稳定 monitor 拒绝错误。"""


class MonitorRefusedError(ValueError):
    """报告不能作为已认证的 monitor 观测接受。"""
