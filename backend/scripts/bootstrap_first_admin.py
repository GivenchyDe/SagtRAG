import argparse
import asyncio
import os
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import get_settings
from app.models import Tenant, TenantRole, User, UserStatus


async def bootstrap(args: argparse.Namespace) -> None:
    """使用临时高权限数据库连接，一次性创建初始租户和管理员。"""
    settings = get_settings()
    tenant_id = settings.we_com_tenant_id
    if args.corp_id != settings.we_com_corp_id:
        raise SystemExit("--corp-id must match WE_COM_CORP_ID")

    bootstrap_url = os.environ.get("BOOTSTRAP_DATABASE_URL")
    # 一次性高权限凭据必须与 DATABASE_URL 分开；后者应配置为权限受限的应用运行账号。
    if not bootstrap_url:
        raise SystemExit("Set BOOTSTRAP_DATABASE_URL to a temporary privileged database URL")
    engine = create_async_engine(bootstrap_url, pool_pre_ping=True)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with factory() as session:
            async with session.begin():
                await session.execute(
                    text(
                        "SELECT set_config('app.tenant_id', :tenant_id, true), "
                        "set_config('app.subject_id', '', true), "
                        "set_config('app.clearance_level', '0', true)"
                    ),
                    {"tenant_id": str(tenant_id)},
                )
                tenant = await session.scalar(select(Tenant).where(Tenant.id == tenant_id).with_for_update())
                if tenant is not None:
                    raise SystemExit("Configured tenant already exists; bootstrap is intentionally non-destructive")
                session.add(Tenant(id=tenant_id, name=args.tenant_name, wecom_corp_id=args.corp_id))
                session.add(
                    User(
                        tenant_id=tenant_id,
                        external_id=f"wecom:{args.wecom_userid}",
                        wecom_userid=args.wecom_userid,
                        display_name=args.display_name,
                        clearance_level=args.clearance_level,
                        tenant_role=TenantRole.TENANT_ADMIN,
                        status=UserStatus.ACTIVE,
                    )
                )
    finally:
        await engine.dispose()
    print(f"Bootstrapped tenant {tenant_id} and tenant administrator {args.wecom_userid}")


def main() -> None:
    """解析一次性租户初始化参数并运行安全引导流程。"""
    parser = argparse.ArgumentParser(description="Create the first tenant and explicitly assigned SagtRAG administrator")
    parser.add_argument("--tenant-name", required=True)
    parser.add_argument("--corp-id", required=True)
    parser.add_argument("--wecom-userid", required=True)
    parser.add_argument("--display-name", required=True)
    parser.add_argument("--clearance-level", required=True, type=int, choices=range(0, 4))
    asyncio.run(bootstrap(parser.parse_args()))


if __name__ == "__main__":
    main()

