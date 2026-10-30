"""经过校验的 API 请求数据和响应投影模型。"""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.models import TenantRole, User
from app.permissions import Principal

WECOM_USERID_PATTERN = r"^[A-Za-z0-9_.@-]{1,128}$"


class CurrentUserResponse(BaseModel):
    """返回当前授权信息，不暴露 ORM 内部状态。"""
    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    tenant_id: UUID
    display_name: str
    clearance_level: int
    tenant_role: TenantRole | None
    permission_version: int

    @classmethod
    def from_principal(cls, user: User, principal: Principal) -> "CurrentUserResponse":
        """将实时请求主体权限转换为稳定的 API 响应结构。"""
        return cls(
            user_id=user.id,
            tenant_id=user.tenant_id,
            display_name=user.display_name,
            clearance_level=principal.clearance_level,
            tenant_role=principal.tenant_role,
            permission_version=principal.permission_version,
        )


class CreateEmployeeRequest(BaseModel):
    """新增本地员工的字段；本地标识由服务端生成。"""
    display_name: str = Field(min_length=1, max_length=200)
    clearance_level: int = Field(default=0, ge=0, le=3)
    wecom_userid: str | None = Field(default=None, pattern=WECOM_USERID_PATTERN)


class UpdateEmployeeRequest(BaseModel):
    """编辑员工姓名、密级和可选企微 userid。"""
    display_name: str = Field(min_length=1, max_length=200)
    clearance_level: int = Field(ge=0, le=3)
    wecom_userid: str | None = Field(default=None, pattern=WECOM_USERID_PATTERN)


class WeComBindingRequest(BaseModel):
    """为本地员工绑定企业微信通讯录标识。"""
    wecom_userid: str = Field(min_length=1, max_length=128)


class AdminLoginRequest(BaseModel):
    """管理员登录请求。"""
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=1024)


class AdminUserResponse(BaseModel):
    """管理员可公开的身份与改密状态。"""
    id: UUID
    username: str
    tenant_id: UUID
    role: str
    is_first_login: bool


class AdminLoginResponse(BaseModel):
    """管理员令牌及当前用户资料。"""
    access_token: str
    token_type: str = "bearer"
    user: AdminUserResponse


class AdminPasswordChangeRequest(BaseModel):
    """管理员首次登录或常规密码变更请求。"""
    current_password: str = Field(min_length=1, max_length=1024)
    new_password: str = Field(min_length=8, max_length=1024)


class CreateAdminUserRequest(BaseModel):
    """创建统一最高权限的后台账号，并要求首次登录时更改初始密码。"""
    username: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.@-]+$")
    initial_password: str = Field(min_length=8, max_length=1024)


class UpdateAdminUserRequest(BaseModel):
    """启用或停用后台账号。"""
    status: str = Field(pattern=r"^(active|disabled)$")
