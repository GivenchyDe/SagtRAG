# S03 固定评测基线

本目录的制度、数值、身份与对话均为专门编写的虚构资料。`cases.json` 是可跟踪的数据源，`freeze.json` 锁定 SHA-256、分组、分母、门禁和调用预算。修改数据需要显式建立新版本，不与原基线混算。

## 固定范围

| 子集 | 固定规模 | 标注/判断 |
|---|---:|---|
| 同义问法 | 20 知识点 × 5 问法 = 100 | 关键事实、逐字依据、知识点标识、资料版本 |
| 相同输入 | 10 知识点 × 5 次独立请求 = 50 | 保留每次输出，不复用答案 |
| 意图边界 | 16 对 = 32 | 规则阶段与完整分类分别记录 |
| 无依据 | 20 | 缺少主题的虚构问题，不能用服务错误充当正确拒答 |
| 证据负例 | 20 | 错数字、单位、比例、主体、否定、条件、例外、来源、结构与伪造摘录 |
| 对话 | 10 组 / 21 轮 | 短答、追问、更正、换话题；以当前单轮实现记录基线，不注入正确改写 |
| 应用 ACL | 10 个本机真实 SQL 场景 | 可见、跨租户、密级及授权变化、解绑/禁用、资料/索引/知识库失效 |
| 消息可靠性 | 10个新增本机Worker场景，另保留既有回归 | 重复消息、租约恢复、撤权、发送上限、发送检查点及unknown不自动重发 |

调参集与测试集按知识点划分为 12/8 组，整组问法和重复请求继承同一 split。首版边界/证据/可靠性集属于公开工程回归集，不宣称它们是独立保密测试集。

## 运行

在 `backend` 目录执行；使用项目现有虚拟环境，不新增依赖。

```powershell
# 无网络、无数据库的工程记录
.\.venv\Scripts\python.exe -m scripts.s03_baseline.evaluate --output ../TEST/s03-baseline-20261105/engineering.json

# 真实模型预检：仍会建立完整虚构语料及执行边界检查
.\.venv\Scripts\python.exe -u -m scripts.s03_baseline.evaluate --live --limit 1 --output ../TEST/s03-baseline-20261105/preflight.json

# 本轮完整基线采用并发4；后续时延比较须使用相同并发和配置
.\.venv\Scripts\python.exe -u -m scripts.s03_baseline.evaluate --live --concurrency 4 --output ../TEST/s03-baseline-20261105/baseline.json

# 同版本中断恢复；配置、源代码、数据、评测脚本指纹须全部匹配
.\.venv\Scripts\python.exe -u -m scripts.s03_baseline.evaluate --live --concurrency 4 --resume --output ../TEST/s03-baseline-20261105/baseline.json

# 定向工程回归；数据库仅使用本机随机schema，模型和发送使用替身
$env:SAGTRAG_RUN_DB_TESTS='1'
.\.venv\Scripts\python.exe -m pytest tests/test_s03_baseline.py tests/test_capability_routing.py tests/test_intent_routing.py tests/test_rag.py tests/test_rag_diagnostics_persistence.py tests/test_rag_delivery.py -q --tb=short

# 新增可靠性基线，与上面的86项不重复
.\.venv\Scripts\python.exe -m pytest tests/test_s03_reliability.py -q --tb=short
```

Windows 受限环境可能在 asyncio 创建本机回环时阻塞，此时真实模型命令和异步 pytest 需要环境批准的沙箱外执行。离线模式不创建事件循环。

## 数据与模型边界

- 真实评测仅连接 `localhost/127.0.0.1`，且数据库名必须与本项目 `TEST/.env` 一致。在随机 `s03_eval_<uuid>` schema 内创建、操作、清理测试表；不写 public 业务表。
- 使用现有 PostgreSQL FTS 函数及真实 `retrieve → rerank → model_json → validate_answer` 路径，不替换检索、模型或校验器。
- 使用本机 owner 连接，以应用 SQL 谓词验证 ACL；**不宣称本工具验证了数据库 RLS 策略**。发送前撤权、幂等、重试复用和 unknown 语义由现有定向 Worker/发送测试补充验证。
- `--routing on` 仅对本次进程启用路由，模拟核查报告中“线上已开启”的行为；本地原配置为关闭。报告记录该差异，不改 `.env` 或任何生产配置。
- 使用当前配置的旧向量/重排及生成模型；不切模型、不改分块、不调阈值、不改业务代码。虚构问句和资料会发送至这些已配置模型的接口。
- 最大191条问答，另32条完整意图分类；单次运行最多220条问答、累计850次模型HTTP请求，每条问答240秒上限。预算记录包括建库Embedding、分类重试、重排和生成；预检与完整运行分别计账。
- 输出仅包含虚构文本、公开技术参数、指纹与必要诊断，不输出凭据或真实员工/公司资料，不调用企微、对象存储、消息队列或生产 SSH。

## 指标与退出条件

1. 分母固定，拒答、超时和错误保留在原分母。未执行不算通过；所有执行结果即使失败也如实保存。
2. `fact_precheck` 只是关键字符串的必要条件。错误结论也可能命中相同词，不能当作事实正确率；`human_verified_correct` 和质量门禁默认留空，供独立证据复核。
3. 有效依据需要分别检查 Dense、FTS、RRF、最终上下文和实际引用；文档合法与逐字摘录合法不等于结论受支持。
4. 报每个阶段实际可测样本数与 P50/P95。缺失阶段不填0，各阶段分位数不能相加。分位数采用 `(n-1)*q` 位置的线性插值。
5. 本机harness耗时不包含生产排队、ASR、企微接收或发送，不能替代生产端到端等待。生产绝对等待目标须由受控负载与业务期望定稿；不从小样本编造SLA。
6. 模型调用次数真实计数，有供应商用量则保留。未返回token用量时不估造费用；人民币实际费用须以供应商账单核对。
7. 当前语料只有20个主文档、每个很短，Recall@30易饱和；不能用本基线宣称大规模召回性能或复杂解析已达标。S13/S14需增加长文档、跨块、DOCX/PDF结构专项版本，保持本基线可对照。
8. S03退出要求是固定数据、口径和当前实现结果能重跑；不要求旧版本先满足S19门禁。未完成的真实评测、人工复核、生产/故障场景必须逐项注明，不能用工程替身结果充数。

## 后续切分与混合检索方向

当前按段落合并，1200字符上限；150字符重叠只用于超长单段。当前召回为 Dense + PostgreSQL FTS（`ts_rank_cd`）→ RRF → Reranker，并非BM25。

根据用户2026-11-05提出的方向，在S13/S14优先比较结构感知切分，语义切分作为实验方案；S16比较 Dense + BM25 → RRF → Reranker。保留权限前置过滤，中文分词、索引同步、故障降级与成本一并评估，不在S03同时更换实现。

参考：[PostgreSQL全文检索排序](https://www.postgresql.org/docs/17/textsearch-controls.html)、[Elastic排序与重排](https://www.elastic.co/docs/solutions/search/ranking)。这些资料仅解释算法，不代表已决定引入Elasticsearch或新服务。
