# SagtRAG企业知识库问答系统

企业知识库检索增强问答系统。管理员在后台维护文档、员工和管理员账号；员工通过企业微信发送文字或语音问题，系统按实时权限检索文档、重排、生成带引用的回答，并由后台任务异步发送。

> 当前仓库包含完整的 API、Worker、管理后台、数据库迁移和部署模板。生产部署前请阅读“部署”和“模型切换”两节。

---

## 1. 系统定位

SagtRAG 面向企业内部制度、流程、产品手册等资料的查询场景，重点解决：

- 员工只能看到自己有权访问的文档；
- 回答必须来自本轮检索到的原文，不能凭空补充企业事实；
- 问答、语音、文档摄取和发送都在后台异步完成；
- 慢任务、重试、超时和发送结果不确定时，不重复回复、不串会话；
- 管理端可以查看问答诊断、权限边界和操作审计。

---

## 2. 核心能力

### 2.1 企业知识问答

- 企业微信回调完成签名校验、解密和员工身份校验后快速 ACK；
- Worker 异步执行意图识别、权限过滤检索、重排、生成和引用校验；
- 回答以 claim 为单位绑定引文，服务端校验 chunk 白名单、原文摘录和确定性冲突；
- 结构化输出必须通过严格 JSON/schema 校验；首次失败最多触发一次受限修复，不接受半截 JSON。

### 2.2 权限与多租户

- 单租户单总知识库；文档按密级 0–3 管理；
- 员工可见性取决于有效企业微信绑定、账号启用、同租户、员工密级不低于文档密级；
- PostgreSQL RLS 约束文档、分块、会话和消息等数据；
- 发送前再次校验员工状态、权限版本和引用文档可见性。

### 2.3 文档摄取

- 支持 PDF、DOCX、TXT、Markdown；
- DOCX 按正文/表格原始顺序解析，保留标题、表头、章节路径；
- 结构化切块尽量保留条件、例外和完整表格；
- 管理端提供解析结构预览；
- 扫描 PDF 无文本时明确拒绝，不声称支持 OCR；
- 摄取任务使用租约、幂等键、心跳和失败重试；解析/分块在线程池执行，支持拆分独立 Worker。

### 2.4 语音问答

- 下载企业微信语音素材，在内存中转码为 WAV；
- 调用 ASR 转写后复用同一套权限检索和生成流程；
- 下载、转码、ASR 分别记录耗时，失败阶段保留实际发生的计时。

### 2.5 有限会话

- 按租户、员工、会话保存当前主题、已确认槽位、待补字段和有效期；
- 支持“请假流程 → 病假 → 需要什么材料”等短答承接；
- 支持“不是病假，是事假”等更正和主动换话题；
- 上下文只辅助理解，每轮仍重新检索当前授权资料，不保存历史 AI 回答作为事实。

### 2.6 可靠性与可观测性

- 单次处理尝试共享 240 秒期限，排队时间单独统计；
- ASR、分类、向量、重排、生成、修复和发送使用剩余预算；
- 分类、转写、生成次数和答案检查点可复用，重试不重复执行；
- 生成预算为首次生成 + 最多一次修复；
- 发送结果未知（`unknown`）不自动重发，管理员核对后可人工恢复；
- 超过等待阈值最多发送一次“处理中”提示，进度与最终答案分开存储；
- 诊断包含端到端、排队、首次领取、阶段耗时、尝试、发送和策略摘要。

### 2.7 管理后台

- 首页、企业文档、员工管理、操作日志、问答记录、管理员账号；
- 文档上传、解析预览、软删除；
- 问答详情查看召回、重排、引用、耗时、发送状态和诊断；
- 管理员操作写独立审计；
- 问答正文和诊断默认保留 7 天，到期由 Worker 清理。

### 2.8 并发与调度

- 每个 Worker 有可配置的问答并发额度；
- 同一员工由数据库领取、租约和 advisory lock 保证顺序；
- 不同员工可并行；
- 公平排序优先服务较久未处理的员工，避免单个员工积压占满额度；
- 文档摄取与问答可同进程或拆分进程运行。

---

## 3. 架构

```text
企业微信 ──回调──▶ FastAPI ──落库/ACK──▶ RabbitMQ ──▶ 问答 Worker
                                                       │
                                       权限过滤检索 / 重排 / 生成 / 引用校验
                                                       │
                                              企业微信发送 ◀── Redis / PostgreSQL / SeaweedFS

管理员浏览器 ──▶ Vue 管理后台 ──▶ FastAPI ──▶ PostgreSQL

文档上传 ──▶ 对象存储 ──▶ 摄取 Worker ──▶ 解析/切分/向量/索引激活
```

- **FastAPI**：认证、管理端读写、文档上传、企业微信回调和健康检查；
- **问答 Worker**：意图识别、检索、重排、生成、校验、发送、重试和过期清理；
- **摄取 Worker**：原件下载、解析、切分、向量化和索引版本切换；
- **PostgreSQL + pgvector**：业务数据、RLS、Dense 检索和 FTS；
- **RabbitMQ**：任务唤醒通知；数据库任务表仍是权威状态；
- **Redis**：企业微信 access token 缓存；
- **SeaweedFS S3**：原始文档对象存储。

---

## 4. 技术栈

| 层 | 技术 |
|---|---|
| 管理后台 | Vue 3、TypeScript、Vite、Vue Router、Element Plus |
| API | FastAPI、Pydantic Settings、SQLAlchemy 2 Async、Alembic |
| 数据库 | PostgreSQL 18 + pgvector，行级安全（RLS） |
| 异步任务 | RabbitMQ，独立 Worker 进程 |
| 缓存 / 对象存储 | Redis、SeaweedFS S3 |
| 模型服务 | DashScope：Embedding、重排、生成、ASR |
| 认证 | Argon2id 密码哈希、JWT Bearer |
| 容器 | Docker Compose |

---

## 5. 目录结构

```text
SagtRAG/
├─ backend/
│  ├─ app/                 # FastAPI、模型、检索、RAG、语音、Worker
│  ├─ alembic/             # 数据库迁移（当前 head：0015_progress_feedback）
│  ├─ scripts/             # 初始化、验收、重排校准等脚本
│  ├─ sql/                 # 运行时角色授权 SQL
│  ├─ tests/               # pytest
│  ├─ .env.example         # 后端配置模板
│  └─ pyproject.toml / uv.lock
├─ frontend/
│  ├─ public/              # favicon 和静态资源
│  ├─ src/                 # Vue 页面、组件、API 客户端、样式
│  ├─ .env.production      # 生产构建 API 基址（默认同源）
│  ├─ package.json / package-lock.json
│  └─ vite.config.ts
├─ deploy/
│  ├─ Dockerfile.api
│  ├─ requirements.lock.txt
│  ├─ apply_model_config.sh
│  └─ nginx-sagtrag.conf
├─ TEST/
│  ├─ init/                # PostgreSQL 初始化 SQL（pgvector）
│  ├─ .env.example         # 本地基础设施配置模板
│  └─ seaweedfs-s3.json.example
├─ compose.production.yml
└─ docker-compose.yml
```

---

## 6. 环境要求

- Docker Desktop 或 Docker Engine + Docker Compose v2；
- Python 3.12–3.13；
- [uv](https://docs.astral.sh/uv/)；
- Node.js 20 LTS 或更高版本；
- 可访问 DashScope 的网络（Embedding、重排、生成、ASR）。

---

## 7. 配置

项目使用两份本地配置，均已被 `.gitignore` 排除，**不要提交真实值**。

### 7.1 `TEST/.env`

用于本地基础设施和后端启动：

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

### 7.2 `backend/.env`

用于后端鉴权、企业微信应用和 RAG 策略：

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
| `WE_COM_TENANT_ID` | 本租户 UUID，必须与数据库租户一致 |
| `ADMIN_BOOTSTRAP_PASSWORD` | 可覆盖初始管理员密码 |
| `EMBEDDING_MODEL` | 查询/摄取共用的向量模型；切换时必须重建或删除旧索引 |
| `RERANK_ROLLOUT_MODE` | `legacy` / `shadow` / `target`；target 需已校准阈值 |
| `RAG_WORKER_CONCURRENCY` | 每个 Worker 进程的问答并发上限，默认 4 |
| `INGESTION_WORKER_MODE` | `combined` / `rag` / `ingestion` |
| `INGESTION_THREADS` | 摄取解析/分块线程上限，默认 1 |
| `RAG_PROGRESS_AFTER_SECONDS` | 超过该秒数最多发一次处理提示，默认 8 |
| `RAG_PROCESSING_DEADLINE_SECONDS` | 单次处理尝试总期限，默认 240 秒 |

### 7.3 `TEST/seaweedfs-s3.json`

SeaweedFS 的 S3 凭据文件，由 Docker Compose 挂载：

```powershell
Copy-Item TEST/seaweedfs-s3.json.example TEST/seaweedfs-s3.json
```

把 `accessKey` / `secretKey` 改成与 `TEST/.env` 的 `S3_ACCESS_KEY` / `S3_SECRET_KEY` 一致。该文件包含凭据，已被忽略。

---

## 8. 快速开始

### 8.1 启动基础设施

在项目根目录执行：

```powershell
docker compose --env-file TEST/.env up -d
```

包含 PostgreSQL + pgvector、RabbitMQ、Redis、SeaweedFS。默认端口：

- PostgreSQL：`55432`
- RabbitMQ：`5672` / 管理端 `15672`
- Redis：`16379`
- S3：`8333`

### 8.2 执行数据库迁移

迁移必须使用 schema owner 连接，不能用运行时应用账号：

```powershell
cd backend
$env:DATABASE_URL = "postgresql+asyncpg://<owner>:<owner-password>@127.0.0.1:55432/sagtrag_demo"
uv run --locked alembic upgrade head
```

当前 head：`0015_progress_feedback`。

迁移完成后，按需创建运行时角色 `sagtrag_app` 并执行 `backend/sql/grant_runtime_access.sql`；该角色不能是 owner、不能有 `BYPASSRLS`。

### 8.3 初始化租户（首次运行）

后端启动时要求 `WE_COM_TENANT_ID` 对应的租户已存在。使用一次性高权限连接创建租户和管理员：

```powershell
$env:BOOTSTRAP_DATABASE_URL = $env:DATABASE_URL
uv run --locked python -m scripts.bootstrap_first_admin `
  --tenant-name "Demo" `
  --corp-id "<与 WE_COM_CORP_ID 相同的值>" `
  --wecom-userid "<企微 userid>" `
  --display-name "演示管理员" `
  --clearance-level 3
```

### 8.4 启动 API

```powershell
cd backend
uv run --locked uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

首次启动且租户下没有后台管理员时，会自动创建账号 `admin`，密码取自 `ADMIN_BOOTSTRAP_PASSWORD`；首次登录会强制修改密码。

### 8.5 启动 Worker

```powershell
cd backend
uv run --locked python -m app.worker
```

Worker 同时承担文档摄取与企微问答。`INGESTION_WORKER_MODE=combined` 时同进程运行，`rag` 只跑问答，`ingestion` 只跑摄取。

**不启动 Worker 时，上传的文档不会完成入库，企微问题也不会被回答。**

生产建议设置：

```env
RAG_WORKER_CONCURRENCY=4
INGESTION_WORKER_MODE=combined
INGESTION_THREADS=1
```

多 Worker 总并发量为 **进程数 × `RAG_WORKER_CONCURRENCY`**；同一员工仍由数据库领取/租约保证顺序。

### 8.6 启动管理后台

```powershell
cd frontend
npm ci
npm run dev
```

打开 `http://127.0.0.1:5173`。默认请求 `http://127.0.0.1:8000`；如需修改，设置 `VITE_API_BASE_URL`。

---

## 9. 测试与检查

| 目的 | 命令 |
|---|---|
| 后端测试 | `cd backend; uv run --locked pytest -q` |
| 后端语法检查 | `cd backend; uv run --locked python -m compileall -q app alembic scripts tests` |
| 前端类型检查与构建 | `cd frontend; npm run build` |
| 查看迁移版本 | `cd backend; uv run --locked alembic current` |
| 新建迁移 | `cd backend; uv run --locked alembic revision -m "..."` |

部分定向脚本会访问真实模型服务或本地数据库，运行前请确认 `TEST/.env` 配置并阅读脚本说明。

---

## 10. 企业微信接入

- 回调地址：`https://<公网域名>/api/v1/wecom/callback`（GET 验证、POST 收消息）；
- 需要在企微后台配置可信 IP、回调 Token、EncodingAESKey，并确保应用可见范围包含员工；
- 本地开发如果没有公网 HTTPS，可先验证管理后台、文档摄取和本地接口；真实回调与员工收发需要公网联调；
- 员工身份以企业微信 userid 绑定关系为准，未绑定或已停用的员工不会获得文档访问权限。

---

## 11. 模型与检索策略

### 11.1 默认配置

- 生成：`qwen3.7-plus`；
- Embedding：`text-embedding-v4`；
- 重排：`qwen3-rerank`；
- 召回 30、上下文 5、重排阈值 0.25。

### 11.2 重排切换

- `RERANK_ROLLOUT_MODE=legacy`：默认，使用旧模型；
- `shadow`：返回旧模型结果，同时记录目标模型分数；
- `target`：使用 `qwen3.7-text-rerank`，必须同时设置：

```env
RERANK_TARGET_THRESHOLD_CALIBRATED=true
RERANK_TARGET_MIN_RELEVANCE=0.50
```

未校准时会自动回退 legacy 并记录 `rerank_target_blocked`。

### 11.3 向量模型切换

目标模型：`qwen3.7-text-embedding-flash`。

切换后新查询只匹配新模型索引，旧文档不会自动兼容。必须：

1. 删除旧文档并重新上传；或
2. 预构建 target 索引并完成活动索引切换。

不要在新旧向量混合的情况下直接切换生产查询模型。

### 11.4 重排切换不需要重建向量

重排只对候选重新评分，不改变文档向量；但切换后应观察无答案率、引用质量和延迟。

---

## 12. 部署

生产使用 `compose.production.yml`。API 与 Worker 共用一个镜像。

### 12.1 部署顺序

1. 备份数据库、对象存储和 `deploy/runtime.env`；
2. 保存旧镜像标签；
3. 拉取代码并构建新镜像；
4. 使用 owner 连接执行 `alembic upgrade head`；
5. 重启 API 和 Worker；
6. 验证健康、迁移版本、旧文档查询；
7. 再切换模型；
8. 最后做文档重传/索引重建和端到端验证。

### 12.2 镜像

`compose.production.yml` 当前使用固定标签：

```yaml
image: sagtrag-api:20261031
```

每次发布必须保存旧标签、构建新镜像并更新标签；只执行 `up -d` 可能继续运行旧镜像。

### 12.3 模型配置脚本

```bash
./deploy/apply_model_config.sh shadow
./deploy/apply_model_config.sh target
```

该脚本只更新重排/意图相关配置，不会修改 `EMBEDDING_MODEL`。向量模型切换必须单独处理旧文档。

### 12.4 回退

- 重排回退：

```env
RERANK_ROLLOUT_MODE=legacy
```

重启 API/Worker。

- 镜像回退：

```bash
docker tag sagtrag-api:<发布前备份标签> sagtrag-api:20261031
docker compose -f compose.production.yml up -d --force-recreate api worker
```

- Embedding 回退：恢复旧模型并恢复对应旧索引；不要混用新旧向量。

---

## 13. 安全与数据

- 管理员密码使用 Argon2id 哈希；JWT 有效期默认 15 分钟；
- 员工访问条件在数据库查询中统一执行，文档、分块、会话和消息受 RLS 约束；
- 操作日志独立保存，删除员工或管理员后仍保留审计记录；
- 问答正文与诊断默认保留 7 天，过期后由 Worker 清理；
- 发送结果未知的记录不会自动重发；管理员核对企微会话后可使用“人工确认后重新排队”；
- 超过等待阈值最多发送一次处理提示，进度不是最终答案；
- 不要把 `TEST/.env`、`backend/.env`、`TEST/seaweedfs-s3.json` 或任何真实密钥提交到仓库；如果曾提交过，请立即轮换。

---

## 14. 当前边界

- 扫描 PDF 不执行 OCR，会明确提示无法提取文本；
- 生产数据库迁移、对象存储备份和模型切换必须按部署流程人工执行；
- 目标重排/向量模型默认不启用，需完成真实对照和索引切换；
- 真实模型质量、生产时延和手机端阅读体验仍需上线验收；
- 第 5 节目录中列出的 `TEST/`、`MD/` 等本地材料不随仓库发布。

---

## 15. 许可

内部项目，未附带开源许可证。
