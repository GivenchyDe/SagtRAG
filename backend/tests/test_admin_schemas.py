"""验证后台重构后的管理员与员工请求字段约束。"""

import pytest
from pydantic import ValidationError

from app.schemas import (
    AdminPasswordChangeRequest,
    CreateAdminUserRequest,
    CreateEmployeeRequest,
    UpdateAdminUserRequest,
    UpdateEmployeeRequest,
)


def test_create_admin_only_needs_username_and_temporary_password() -> None:
    """新增后台账号不再接受角色，初始密码至少 8 位。"""
    request = CreateAdminUserRequest(username="reviewer@example.test", initial_password="temporary-password")
    assert request.username == "reviewer@example.test"
    assert "role" not in CreateAdminUserRequest.model_fields
    assert CreateAdminUserRequest(username="reviewer", initial_password="12345678").initial_password == "12345678"
    with pytest.raises(ValidationError):
        CreateAdminUserRequest(username="ops", initial_password="1234567")
    with pytest.raises(ValidationError):
        CreateAdminUserRequest(username="ops", initial_password="short")
    with pytest.raises(ValidationError):
        CreateAdminUserRequest(username="ops space", initial_password="temporary-password")


def test_password_change_requires_eight_characters() -> None:
    """改密接口同样只要求 8 位，低于 8 位拒绝。"""
    request = AdminPasswordChangeRequest(current_password="old-password", new_password="12345678")
    assert request.new_password == "12345678"
    with pytest.raises(ValidationError):
        AdminPasswordChangeRequest(current_password="old-password", new_password="1234567")


def test_update_admin_only_accepts_known_status() -> None:
    """后台账号更新只允许启用或停用，不提供角色编辑。"""
    request = UpdateAdminUserRequest(status="disabled")
    assert request.status == "disabled"
    assert "role" not in UpdateAdminUserRequest.model_fields
    with pytest.raises(ValidationError):
        UpdateAdminUserRequest(status="locked")


def test_employee_requests_validate_clearance_and_wecom_userid() -> None:
    """员工新增与编辑只校验姓名、密级和可选企微 userid。"""
    created = CreateEmployeeRequest(display_name="张三", clearance_level=2, wecom_userid="zhangsan")
    assert created.clearance_level == 2
    updated = UpdateEmployeeRequest(display_name="张三", clearance_level=3)
    assert updated.wecom_userid is None
    with pytest.raises(ValidationError):
        UpdateEmployeeRequest(display_name="", clearance_level=0)
    with pytest.raises(ValidationError):
        CreateEmployeeRequest(display_name="张三", clearance_level=4)
    with pytest.raises(ValidationError):
        CreateEmployeeRequest(display_name="张三", clearance_level=0, wecom_userid="bad userid")
