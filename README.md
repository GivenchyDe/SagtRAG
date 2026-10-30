# SagtRAG

![SagtRAG](frontend/src/assets/logo.png)

SagtRAG 是一个面向企业知识库的 RAG 系统：管理员在后台维护企业文档、员工和管理员账号；员工通过企业微信以文字或语音提问，系统按员工密级检索文档、重排并生成带引用的回答，由后台 Worker 异步发送。

## 功能概览

- **单总知识库**：所有企业文档统一进入租户唯一的默认知识库，不再按知识库分别授权。
- **管理后台**：首页、企业文档、员工管理、操作日志、问答记录、管理员账号六个页面。
- **文档密级**：文档按密级 0–3 分组展示；上传支持 PDF、DOCX、TXT、Markdown，单文件默认上限 20 MB。
- **员工权限**：员工访问只取决于「有效企微绑定 + 账号启用 + 同租户 + 员工密级 ≥ 文档密级」。
- **企业微信问答**：回调完成签名校验、解密、员工身份校验后快速 ACK，由 Worker 异步处理。
- **语音问答**：下载企微语音素材，在内存中转码并调用 ASR，再复用同一套权限检索与生成流程。
- **引用与诊断**：回答必须引用本次召回的文档分块，服务端校验引用；后台可查看召回、重排、耗时和错误诊断。
- **操作日志**：登录、账号、员工、文档、问答详情查看等操作独立审计，支持搜索、分类和分页。
- **软删除**：文档、员工、管理员删除后保留历史记录与审计，可重新创建同名账号或重新上传同一文件。

## 技术栈

| 层 | 技术 |
|---|---|
| 管理后台 | Vue 3、TypeScript、Vite、Vue Router、Element Plus |
| API | FastAPI、Pydantic Settings、SQLAlchemy 2 Async、Alembic |
| 数据库 | PostgreSQL 18 + pgvector，行级安全（RLS） |
| 异步任务 | RabbitMQ，独立 Worker 进程 |
| 缓存 / 对象存储 | Redis、SeaweedFS S3 |
| 模型服务 | DashScope：Embedding、重排、生成、ASR |
| 认证 | Argon2id 密码哈希、JWT Bearer |

## 架构

```text
企业微信 ──回调──▶ FastAPI ──落库/ACK──▶ RabbitMQ ──▶ Worker
                                                     │
                                     权限过滤检索 / 重排 / 生成 / 引用校验
                                                     │
                                            企业微信主动发送 ◀── Redis / PostgreSQL / SeaweedFS

管理员浏览器 ──▶ Vue 管理后台 ──▶ FastAPI ──▶ PostgreSQL
```

API 负责鉴权、文档上传、管理端读写和企微回调；Worker 负责文档摄取、索引切换、问答处理、语音转写、发送重试和过期清理。

## 目录结构

```text
SagtRAG/
├─ backend/
│  ├─ app/                 # FastAPI、模型、检索、RAG、语音、Worker
│  ├─ alembic/             # 数据库迁移（当前 head：0009_qa_diagnostics）
│  ├─ scripts/             # 初始化、验收与评测脚本
│  ├─ sql/                 # 运行时角色授权 SQL
│  ├─ tests/               # pytest
│  ├─ .env.example         # 后端配置模板
│  └─ pyproject.toml / uv.lock
├─ frontend/
│  ├─ public/              # favicon（logo.svg / logo-transparent.png）
│  ├─ src/                 # Vue 页面、组件、API 客户端、样式
│  ├─ package.json / package-lock.json
│  └─ vite.config.ts
├─ deploy/
│  └─ requirements.lock.txt
├─ TEST/
│  ├─ init/                # PostgreSQL 初始化 SQL（pgvector）
│  ├─ .env.example         # 本地基础设施配置模板
│  └─ seaweedfs-s3.json.example
└─ docker-compose.yml
```


## 环境要求

- Docker Desktop（含 Docker Compose v2）
- Python 3.12–3.13
- [uv](https://docs.astral.sh/uv/)
- Node.js 20 LTS 或更高版本
- 可访问 DashScope 的网络（Embedding、重排、生成、ASR）

## 配置

项目使用两份本地配置，均已被 `.gitignore` 排除，**不要提交真实值**。

### 1. `TEST/.env`

复制 `TEST/.env.example` 后填写，docker compose 与后端都会读取：

```powershell
Copy-Item TEST/.env.example TEST/.env
```

主要变量：

| 变量 | 说明 |
|---|---|
| `POSTGRES_DB` / `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_PORT` | PostgreSQL 容器与 owner 账号 |
| `RABBITMQ_DEFAULT_USER` / `RABBITMQ_DEFAULT_PASS` / `RABBITMQ_AMQP_PORT` / `RABBITMQ_MANAGEMENT_PORT` | RabbitMQ |
| `REDIS_PASSWORD` / `REDIS_PORT` | Redis |
| `OBJECT_STORE_PORT` / `S3_ACCESS_KEY` / `S3_SECRET_KEY` | SeaweedFS S3 |
| `DASHSCOPE_API_KEY` | 模型服务密钥 |
| `DATABASE_URL` | 后端运行时数据库连接（应用账号） |
| `RABBITMQ_URL` | Worker/API 的 AMQP 连接 |
| `ADMIN_BOOTSTRAP_PASSWORD` | 首次启动时创建 `admin` 的初始密码 |

### 2. `backend/.env`

复制 `backend/.env.example` 后填写：

```powershell
Copy-Item backend/.env.example backend/.env
```

主要变量：

| 变量 | 说明 |
|---|---|
| `JWT_SECRET_KEY` | 至少 32 字节的随机签名密钥 |
| `JWT_ISSUER` | JWT 签发方标识 |
| `WE_COM_CORP_ID` / `WE_COM_AGENT_ID` / `WE_COM_SECRET` | 企业微信应用信息 |
| `WE_COM_TOKEN` / `WE_COM_ENCODING_AES_KEY` | 回调签名 Token 与 43 位 Base64 AES Key |
| `WE_COM_TENANT_ID` | 本租户 UUID，必须与数据库中的租户一致 |
| `ADMIN_BOOTSTRAP_PASSWORD` | 可覆盖 `TEST/.env` 中的初始管理员密码 |

### 3. `TEST/seaweedfs-s3.json`

SeaweedFS 的 S3 凭据文件，由 docker compose 挂载：

```powershell
Copy-Item TEST/seaweedfs-s3.json.example TEST/seaweedfs-s3.json
```

把 `accessKey` / `secretKey` 改成与 `TEST/.env` 的 `S3_ACCESS_KEY` / `S3_SECRET_KEY` 一致。该文件包含凭据，已被忽略。

## 快速开始

### 1. 启动基础设施

在项目根目录执行：

```powershell
docker compose --env-file TEST/.env up -d
```

包含 PostgreSQL + pgvector、RabbitMQ、Redis、SeaweedFS。默认端口：PostgreSQL `55432`、RabbitMQ `5672/15672`、Redis `16379`、S3 `8333`。

### 2. 执行数据库迁移

迁移必须使用 schema owner 连接，不能用运行时应用账号：

```powershell
cd backend
$env:DATABASE_URL = "postgresql+asyncpg://sagtrag_owner:<owner-password>@127.0.0.1:55432/sagtrag"
uv run --locked alembic upgrade head
```

迁移完成后，按需创建运行时角色 `sagtrag_app` 并执行 `backend/sql/grant_runtime_access.sql`；该角色不能是 owner、不能有 `BYPASSRLS`。

### 3. 初始化租户（首次运行）

后端启动时要求 `WE_COM_TENANT_ID` 对应的租户已存在。使用一次性高权限连接创建租户：

```powershell
$env:BOOTSTRAP_DATABASE_URL = $env:DATABASE_URL
uv run --locked python -m scripts.bootstrap_first_admin `
  --tenant-name "Demo" `
  --corp-id "<与 WE_COM_CORP_ID 相同的值>" `
  --wecom-userid "<企微 userid>" `
  --display-name "演示管理员" `
  --clearance-level 3
```

### 4. 启动 API

```powershell
cd backend
uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

首次启动且租户下没有后台管理员时，会自动创建账号 `admin`，密码取自 `ADMIN_BOOTSTRAP_PASSWORD`；首次登录会强制修改密码。

### 5. 启动 Worker

```powershell
cd backend
uv run --locked python -m app.worker
```

Worker 同时消费文档摄取与企微问答任务，并定期扫描数据库恢复未完成任务。**不启动 Worker 时，上传的文档不会完成入库，企微问题也不会被回答。**

### 6. 启动管理后台

```powershell
cd frontend
npm ci
npm run dev
```

打开 `http://127.0.0.1:5173`。默认请求 `http://127.0.0.1:8000`；如需修改，设置 `VITE_API_BASE_URL`。

## 常用命令

| 目的 | 命令 |
|---|---|
| 后端测试 | `cd backend; uv run --locked pytest -q` |
| 后端语法检查 | `cd backend; uv run --locked python -m compileall -q app alembic scripts tests` |
| 前端类型检查与构建 | `cd frontend; npm run build` |
| 查看迁移版本 | `cd backend; uv run --locked alembic current` |
| 新建迁移 | `cd backend; uv run --locked alembic revision -m "..."` |

部分定向脚本会访问真实模型服务或本地数据库，运行前请确认 `TEST/.env` 配置，并阅读脚本说明。

## 企业微信接入

- 回调地址：`https://<公网域名>/api/v1/wecom/callback`（GET 验证、POST 收消息）。
- 需要在企微后台配置可信 IP、回调 Token、EncodingAESKey，并确保应用可见范围包含员工。
- 本地开发如果没有公网 HTTPS，可先验证管理后台、文档摄取和本地接口；真实回调与员工收发需要公网联调。
- 员工身份以企微 userid 绑定关系为准，未绑定或已停用的员工不会获得文档访问权限。

## 安全与数据

- 管理员密码使用 Argon2id 哈希；JWT 有效期默认 15 分钟。
- 员工访问条件在数据库查询中统一执行，文档、分块和租户均受 RLS 约束。
- 操作日志独立保存，删除员工或管理员后仍保留审计记录。
- 问答正文与诊断默认保留 7 天，过期后由 Worker 清理；发送结果未知的记录不会自动重发，需要人工核对。
- 不要把 `TEST/.env`、`backend/.env`、`TEST/seaweedfs-s3.json` 或任何真实密钥提交到仓库；如果曾提交过，请立即轮换。

## 部署提示

- `deploy/requirements.lock.txt` 可用于固定后端部署依赖。
- API 与 Worker 需要作为两个长期进程运行；生产环境建议由进程管理器或容器编排托管。
- 对外只暴露反向代理的 HTTPS，PostgreSQL、RabbitMQ、Redis、S3 保持内网访问。
- 迁移使用 owner 连接，运行时使用最小权限账号；备份数据库与对象存储，并监控 Worker 积压与发送失败。

## 更多信息

- 后端接口实现位于 `backend/app/api/`，数据库迁移位于 `backend/alembic/versions/`。
- 历史重构、验收记录与后端交接文档保留在本地，不随仓库发布。

## 许可

内部项目，未附带开源许可证。
