import base64
import binascii
from functools import lru_cache
from typing import Annotated
from uuid import UUID

from pydantic import AliasChoices, AnyHttpUrl, Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """经过校验的运行配置；敏感值使用 SecretStr 降低意外暴露风险。"""
    model_config = SettingsConfigDict(env_file=("../TEST/.env", ".env"), env_file_encoding="utf-8", extra="ignore")

    database_url: str
    jwt_secret_key: SecretStr
    jwt_issuer: str
    jwt_audience: str = "sagtrag-api"
    jwt_access_token_minutes: Annotated[int, Field(ge=5, le=60)] = 15
    admin_bootstrap_password: SecretStr
    embedding_api_key: SecretStr = Field(validation_alias="DASHSCOPE_API_KEY")
    embedding_base_url: AnyHttpUrl = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    embedding_model: str = "text-embedding-v4"
    generation_model: str = "qwen3.7-plus"
    generation_base_url: AnyHttpUrl = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    rerank_model: str = "qwen3-rerank"
    rerank_url: AnyHttpUrl = "https://dashscope.aliyuncs.com/api/v1/services/rerank/text-rerank/text-rerank"
    rag_recall_limit: Annotated[int, Field(ge=1, le=100)] = 30
    rag_context_limit: Annotated[int, Field(ge=1, le=10)] = 5
    rag_min_relevance: Annotated[float, Field(ge=0, le=1)] = 0.25
    rag_message_retention_days: Annotated[int, Field(ge=1, le=90)] = 7
    asr_model: str = "qwen3-asr-flash"
    asr_base_url: AnyHttpUrl = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    voice_max_bytes: Annotated[int, Field(ge=1024, le=10_485_760)] = 2_097_152
    voice_max_seconds: Annotated[int, Field(ge=1, le=300)] = 60
    we_com_corp_id: str = Field(validation_alias=AliasChoices("WE_COM_CORP_ID", "WECOM_CORP_ID"))
    we_com_agent_id: str = Field(validation_alias=AliasChoices("WE_COM_AGENT_ID", "WECOM_AGENT_ID"))
    we_com_secret: SecretStr = Field(validation_alias=AliasChoices("WE_COM_SECRET", "WECOM_APP_SECRET"))
    we_com_token: SecretStr = Field(validation_alias=AliasChoices("WE_COM_TOKEN", "WECOM_TOKEN"))
    we_com_encoding_aes_key: SecretStr = Field(
        validation_alias=AliasChoices("WE_COM_ENCODING_AES_KEY", "WECOM_ENCODING_AES_KEY")
    )
    rabbitmq_url: SecretStr
    redis_password: SecretStr
    redis_host: str = "127.0.0.1"
    redis_port: Annotated[int, Field(ge=1, le=65535)] = 16379
    s3_endpoint_url: AnyHttpUrl = "http://127.0.0.1:8333"
    s3_region: str = "us-east-1"
    s3_bucket: str = "sagtrag-documents"
    s3_access_key: SecretStr
    s3_secret_key: SecretStr
    we_com_tenant_id: UUID
    allowed_web_origins: str = "http://localhost:5173,http://127.0.0.1:5173"
    we_com_api_base_url: AnyHttpUrl = "https://qyapi.weixin.qq.com"
    wecom_max_callback_body_bytes: Annotated[int, Field(ge=1024, le=1_048_576)] = 262_144
    wecom_max_text_chars: Annotated[int, Field(ge=1, le=10_000)] = 5_000
    max_upload_bytes: Annotated[int, Field(ge=1_048_576, le=104_857_600)] = 20_971_520
    max_document_chunks: Annotated[int, Field(ge=1, le=50_000)] = 5_000
    rabbitmq_publish_timeout_seconds: Annotated[float, Field(gt=0, le=4)] = 1.5

    @field_validator("s3_bucket")
    @classmethod
    def validate_s3_bucket(cls, value: str) -> str:
        """拒绝空值或不符合 S3 桶命名边界的配置。"""
        if not value or len(value) > 63 or not value[0].isalnum() or not value[-1].isalnum():
            raise ValueError("S3_BUCKET must be a valid non-empty bucket name")
        return value

    @field_validator("jwt_secret_key")
    @classmethod
    def validate_jwt_secret(cls, value: SecretStr) -> SecretStr:
        """要求 JWT 签名密钥至少包含 32 字节。"""
        if len(value.get_secret_value().encode("utf-8")) < 32:
            raise ValueError("JWT_SECRET_KEY must contain at least 32 bytes")
        return value

    @field_validator("we_com_encoding_aes_key")
    @classmethod
    def validate_wecom_aes_key(cls, value: SecretStr) -> SecretStr:
        """验证企微 AES 密钥的 Base64 格式和解码长度。"""
        encoded = value.get_secret_value()
        try:
            decoded = base64.b64decode(f"{encoded}=", validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("WECOM_ENCODING_AES_KEY must be a valid 43-character Base64 key") from exc
        if len(encoded) != 43 or len(decoded) != 32:
            raise ValueError("WECOM_ENCODING_AES_KEY must decode to exactly 32 bytes")
        return value


@lru_cache
def get_settings() -> Settings:
    """每个进程只加载一次配置，确保各组件使用相同设置。"""
    return Settings()

