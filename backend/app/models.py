from datetime import datetime
from enum import StrEnum
from uuid import UUID, uuid4

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import UserDefinedType


class Vector1024(UserDefinedType):
    """pgvector 1024 维类型，避免 ORM 依赖特定 pgvector Python 包。"""

    cache_ok = True

    def get_col_spec(self, **kwargs: object) -> str:
        """返回 PostgreSQL 列类型声明。"""
        return "vector(1024)"


class TSVECTOR(UserDefinedType):
    """PostgreSQL 全文检索向量类型。"""

    cache_ok = True

    def get_col_spec(self, **kwargs: object) -> str:
        """返回 PostgreSQL 列类型声明。"""
        return "tsvector"


class Base(DeclarativeBase):
    """所有 SQLAlchemy 数据模型的共同声明基类。"""
    pass


class UserStatus(StrEnum):
    """本地员工账号状态。"""
    ACTIVE = "active"
    DISABLED = "disabled"


class TenantRole(StrEnum):
    """租户范围员工管理角色。"""
    TENANT_ADMIN = "tenant_admin"


class KBRole(StrEnum):
    """知识库成员授权角色。"""
    KB_ADMIN = "kb_admin"
    EDITOR = "editor"
    VIEWER = "viewer"
    AUDITOR = "auditor"


class Tenant(Base):
    """保存租户注册信息及其企业微信 CorpID。"""
    __tablename__ = "tenants"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    wecom_corp_id: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class User(Base):
    """由本地管理的用户身份；企业微信标识用于绑定用户，本身不授予 ACL 权限。"""
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "external_id", name="uq_users_tenant_external"),
        UniqueConstraint("tenant_id", "wecom_userid", name="uq_users_tenant_wecom_user"),
        UniqueConstraint("tenant_id", "id", name="uq_users_tenant_id"),
        CheckConstraint("clearance_level BETWEEN 0 AND 3", name="ck_users_clearance_range"),
        CheckConstraint("tenant_role IS NULL OR tenant_role = 'tenant_admin'", name="ck_users_tenant_role"),
        CheckConstraint("status IN ('active', 'disabled')", name="ck_users_status"),
        CheckConstraint("permission_version > 0", name="ck_users_permission_version"),
        Index("ix_users_tenant_deleted", "tenant_id", "deleted_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    external_id: Mapped[str] = mapped_column(String(200), nullable=False)
    wecom_userid: Mapped[str | None] = mapped_column(String(128))
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    clearance_level: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tenant_role: Mapped[TenantRole | None] = mapped_column(
        Enum(TenantRole, name="tenant_role", native_enum=False, create_constraint=False, length=32, values_callable=lambda enum_cls: [item.value for item in enum_cls])
    )
    status: Mapped[UserStatus] = mapped_column(
        Enum(UserStatus, name="user_status", native_enum=False, create_constraint=False, length=24, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
        server_default=UserStatus.ACTIVE.value,
    )
    permission_version: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Group(Base):
    """租户范围内的授权组，其密级会参与用户有效密级计算。"""
    __tablename__ = "groups"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_groups_tenant_id"),
        UniqueConstraint("tenant_id", "name", name="uq_groups_tenant_name"),
        CheckConstraint("clearance_level BETWEEN 0 AND 3", name="ck_groups_clearance_range"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    clearance_level: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GroupMember(Base):
    """用户与用户组的租户内关联，由复合外键保证租户一致性。"""
    __tablename__ = "group_members"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"], ondelete="CASCADE", name="fk_group_members_tenant_user"),
        ForeignKeyConstraint(["tenant_id", "group_id"], ["groups.tenant_id", "groups.id"], ondelete="CASCADE", name="fk_group_members_tenant_group"),
        UniqueConstraint("tenant_id", "group_id", "user_id", name="uq_group_membership"),
        Index("ix_group_members_tenant_id", "tenant_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    group_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KnowledgeBase(Base):
    """归属于租户的知识库；每个租户只有一个默认总知识库承载全部文档。"""
    __tablename__ = "knowledge_bases"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_kbs_tenant_id"),
        Index("uq_kbs_tenant_default", "tenant_id", unique=True, postgresql_where=text("is_default")),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="active")
    is_default: Mapped[bool] = mapped_column(nullable=False, server_default="false")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class KBMember(Base):
    """为单个用户或用户组授予租户内知识库角色，二者必须且只能指定其一。"""
    __tablename__ = "kb_members"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "kb_id"], ["knowledge_bases.tenant_id", "knowledge_bases.id"], ondelete="CASCADE", name="fk_kb_members_tenant_kb"),
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"], ondelete="CASCADE", name="fk_kb_members_tenant_user"),
        ForeignKeyConstraint(["tenant_id", "group_id"], ["groups.tenant_id", "groups.id"], ondelete="CASCADE", name="fk_kb_members_tenant_group"),
        CheckConstraint("(user_id IS NOT NULL)::int + (group_id IS NOT NULL)::int = 1", name="ck_kb_member_one_subject"),
        CheckConstraint("role IN ('kb_admin', 'editor', 'viewer', 'auditor')", name="ck_kb_member_role"),
        UniqueConstraint("tenant_id", "kb_id", "user_id", name="uq_kb_user_member"),
        UniqueConstraint("tenant_id", "kb_id", "group_id", name="uq_kb_group_member"),
        Index("ix_kb_members_user", "tenant_id", "user_id", postgresql_where=text("user_id IS NOT NULL")),
        Index("ix_kb_members_group", "tenant_id", "group_id", postgresql_where=text("group_id IS NOT NULL")),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kb_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    group_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    role: Mapped[KBRole] = mapped_column(
        Enum(KBRole, name="kb_role", native_enum=False, create_constraint=False, length=32, values_callable=lambda enum_cls: [item.value for item in enum_cls]),
        nullable=False,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WeComInboundMessage(Base):
    """持久化的回调收件记录；唯一企业微信 MsgId 用作幂等键。"""
    __tablename__ = "wecom_inbound_messages"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"], name="fk_wecom_inbound_tenant_user"),
        UniqueConstraint("tenant_id", "wecom_msg_id", name="uq_wecom_inbound_msg"),
        CheckConstraint("msg_type IN ('text', 'voice')", name="ck_wecom_inbound_msg_type"),
        CheckConstraint("state IN ('received', 'queued', 'processing', 'completed', 'failed')", name="ck_wecom_inbound_state"),
        CheckConstraint("send_state IN ('pending','sending','retry','sent','unknown','cancelled','failed')", name="ck_wecom_send_state"),
        CheckConstraint("length(content) <= 10000", name="ck_wecom_inbound_content_size"),
        Index("ix_wecom_inbound_state", "tenant_id", "state", "received_at"),
        Index("ix_wecom_rag_claim", "tenant_id", "state", "next_attempt_at", "lease_expires_at"),
        Index("ix_wecom_qa_received", "tenant_id", "received_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id"), nullable=False)
    user_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    wecom_userid: Mapped[str] = mapped_column(String(128), nullable=False)
    wecom_msg_id: Mapped[str] = mapped_column(String(128), nullable=False)
    msg_type: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    media_id: Mapped[str | None] = mapped_column(String(256))
    state: Mapped[str] = mapped_column(String(24), nullable=False, server_default="received")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    send_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    worker_id: Mapped[str | None] = mapped_column(String(160))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    answer: Mapped[dict[str, object] | None] = mapped_column(JSON(none_as_null=True))
    diagnostics: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict, server_default=text("'{}'::json"))
    permission_version: Mapped[int | None] = mapped_column(Integer)
    send_state: Mapped[str] = mapped_column(String(24), nullable=False, server_default="pending")
    send_msg_id: Mapped[str | None] = mapped_column(String(128))
    error_code: Mapped[str | None] = mapped_column(String(80))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEvent(Base):
    """租户范围内的管理操作和安全相关事件记录。"""
    __tablename__ = "audit_events"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "actor_id"], ["users.tenant_id", "users.id"], name="fk_audit_events_tenant_actor"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id"), nullable=False, index=True)
    actor_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(80), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(160), nullable=False)
    details: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AdminUser(Base):
    """管理后台账号；独立于企业微信员工用户身份，删除为可复用用户名的软删除。"""
    __tablename__ = "admin_users"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id", name="uq_admin_users_tenant_id"),
        CheckConstraint("role IN ('tenant_admin', 'document_manager', 'auditor')", name="ck_admin_users_role"),
        CheckConstraint("status IN ('active', 'disabled')", name="ck_admin_users_status"),
        Index(
            "uq_admin_users_tenant_username_active",
            "tenant_id",
            "username",
            unique=True,
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True)
    username: Mapped[str] = mapped_column(String(128), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, server_default="tenant_admin")
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="active")
    must_change_password: Mapped[bool] = mapped_column(nullable=False, server_default="true")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AdminAuditEvent(Base):
    """管理员登录与管理操作审计记录。"""
    __tablename__ = "admin_audit_events"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "admin_user_id"], ["admin_users.tenant_id", "admin_users.id"], ondelete="RESTRICT", name="fk_admin_audit_tenant_admin"),
        Index("ix_admin_audit_tenant_time", "tenant_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    admin_user_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    details: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class Document(Base):
    """知识库中的逻辑文档及当前生效修订指针。"""
    __tablename__ = "documents"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "kb_id"], ["knowledge_bases.tenant_id", "knowledge_bases.id"], ondelete="CASCADE", name="fk_documents_tenant_kb"),
        ForeignKeyConstraint(["tenant_id", "active_revision_id"], ["document_revisions.tenant_id", "document_revisions.id"], name="fk_documents_active_revision", deferrable=True, initially="DEFERRED"),
        UniqueConstraint("tenant_id", "id", name="uq_documents_tenant_id"),
        CheckConstraint("clearance_level BETWEEN 0 AND 3", name="ck_documents_clearance_range"),
        CheckConstraint("status IN ('active', 'deleted')", name="ck_documents_status"),
        Index("ix_documents_tenant_kb_status", "tenant_id", "kb_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    kb_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="active")
    clearance_level: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    active_revision_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_by_admin_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class DocumentRevision(Base):
    """不可变的原件修订元数据与解析配置快照。"""
    __tablename__ = "document_revisions"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "document_id"], ["documents.tenant_id", "documents.id"], ondelete="CASCADE", name="fk_document_revisions_tenant_document"),
        UniqueConstraint("tenant_id", "document_id", "revision_number", name="uq_document_revisions_number"),
        UniqueConstraint("tenant_id", "id", name="uq_document_revisions_tenant_id"),
        CheckConstraint("revision_number > 0 AND clearance_level BETWEEN 0 AND 3", name="ck_document_revisions_values"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision_number: Mapped[int] = mapped_column(Integer, nullable=False)
    filename: Mapped[str] = mapped_column(String(512), nullable=False)
    media_type: Mapped[str] = mapped_column(String(160), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    object_key: Mapped[str] = mapped_column(String(1024), nullable=False, unique=True)
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    chunking_config: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    clearance_level: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by_admin_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IngestionJob(Base):
    """由数据库权威管理、可租约恢复的文档摄取任务。"""
    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "document_id"], ["documents.tenant_id", "documents.id"], ondelete="CASCADE", name="fk_ingestion_jobs_tenant_document"),
        ForeignKeyConstraint(["tenant_id", "revision_id"], ["document_revisions.tenant_id", "document_revisions.id"], ondelete="CASCADE", name="fk_ingestion_jobs_tenant_revision"),
        UniqueConstraint("tenant_id", "idempotency_key", name="uq_ingestion_jobs_idempotency"),
        CheckConstraint("status IN ('queued', 'validating', 'parsing', 'chunking', 'embedding', 'indexing', 'completed', 'failed')", name="ck_ingestion_jobs_status"),
        CheckConstraint("attempt_count >= 0 AND max_attempts > 0", name="ck_ingestion_jobs_attempts"),
        Index("ix_ingestion_jobs_claim", "status", "lease_expires_at", "created_at"),
        Index("ix_ingestion_jobs_tenant_status", "tenant_id", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    document_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="queued")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="5")
    worker_id: Mapped[str | None] = mapped_column(String(160))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    checkpoint: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(80))
    error_summary: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IndexVersion(Base):
    """按修订隔离构建的检索索引版本及其校验/激活状态。"""
    __tablename__ = "index_versions"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "document_id"], ["documents.tenant_id", "documents.id"], ondelete="CASCADE", name="fk_index_versions_tenant_document"),
        ForeignKeyConstraint(["tenant_id", "revision_id"], ["document_revisions.tenant_id", "document_revisions.id"], ondelete="CASCADE", name="fk_index_versions_tenant_revision"),
        UniqueConstraint("tenant_id", "document_id", "version_number", name="uq_index_versions_number"),
        UniqueConstraint("tenant_id", "id", name="uq_index_versions_tenant_id"),
        CheckConstraint("status IN ('building', 'ready', 'active', 'failed', 'retired')", name="ck_index_versions_status"),
        CheckConstraint("embedding_dimension > 0 AND chunk_count >= 0", name="ck_index_versions_counts"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    revision_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(24), nullable=False, server_default="building")
    embedding_model: Mapped[str] = mapped_column(String(160), nullable=False, server_default="text-embedding-v4")
    embedding_dimension: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1024")
    chunk_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    parser_version: Mapped[str] = mapped_column(String(64), nullable=False)
    chunking_config: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    activated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Chunk(Base):
    """索引版本内的文本块；RLS 与内容过滤始终携带租户、知识库和密级。"""
    __tablename__ = "chunks"
    __table_args__ = (
        ForeignKeyConstraint(["tenant_id", "index_version_id"], ["index_versions.tenant_id", "index_versions.id"], ondelete="CASCADE", name="fk_chunks_tenant_index_version"),
        ForeignKeyConstraint(["tenant_id", "document_id"], ["documents.tenant_id", "documents.id"], ondelete="CASCADE", name="fk_chunks_tenant_document"),
        ForeignKeyConstraint(["tenant_id", "kb_id"], ["knowledge_bases.tenant_id", "knowledge_bases.id"], ondelete="CASCADE", name="fk_chunks_tenant_kb"),
        UniqueConstraint("tenant_id", "index_version_id", "chunk_key", name="uq_chunks_version_key"),
        CheckConstraint("clearance_level BETWEEN 0 AND 3 AND ordinal >= 0", name="ck_chunks_security_order"),
        Index("ix_chunks_filter", "tenant_id", "kb_id", "clearance_level", "index_version_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    tenant_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    kb_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    document_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    index_version_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    chunk_key: Mapped[str] = mapped_column(String(128), nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    clearance_level: Mapped[int] = mapped_column(Integer, nullable=False)
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR())
    embedding: Mapped[list[float] | None] = mapped_column(Vector1024())
    metadata_json: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

