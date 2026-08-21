"""
服务管理模块

提供systemd服务管理和自动回退功能。
"""

from peng.service.auto_rollback import (check_and_rollback, get_failure_count,
                                        record_failure, reset_failures)
from peng.service.systemd_manager import (get_service_status, install_service,
                                          is_service_installed,
                                          restart_service, stop_service,
                                          uninstall_service)

__all__ = [
    "install_service",
    "uninstall_service",
    "restart_service",
    "stop_service",
    "is_service_installed",
    "get_service_status",
    "record_failure",
    "reset_failures",
    "get_failure_count",
    "check_and_rollback",
]
