#!/usr/bin/env bash
# 在服务器 /opt/SagtRAG 下执行：./deploy/apply_model_config.sh shadow|target
# 只更新重排/意图相关键，保留 runtime.env 里的所有凭据和其他配置。
# 不切换 EMBEDDING_MODEL：target embedding 必须先删除旧文档或完成索引重建。
set -euo pipefail

MODE="${1:-shadow}"
case "$MODE" in
  shadow|target) ;;
  *) echo "usage: $0 shadow|target" >&2; exit 1 ;;
esac

cd "$(dirname "$0")/.."
ENV_FILE="deploy/runtime.env"
[ -f "$ENV_FILE" ] || { echo "missing $ENV_FILE" >&2; exit 1; }

cp -a "$ENV_FILE" "$ENV_FILE.bak.$(date +%s)"

python3 - "$MODE" <<'PY'
import pathlib
import sys

mode = sys.argv[1]
path = pathlib.Path("deploy/runtime.env")
values = {
    "RERANK_TARGET_MODEL": "qwen3.7-text-rerank",
    "RERANK_ROLLOUT_MODE": mode,
    "RERANK_TARGET_MIN_RELEVANCE": "0.50",
    "RERANK_TARGET_THRESHOLD_CALIBRATED": "true",
    "INTENT_ROUTING_ENABLED": "true",
    "INTENT_RULES_VERSION": "rules-v3",
    "INTENT_PROMPT_VERSION": "prompt-v2",
}
lines = path.read_text().splitlines()
seen: set[str] = set()
out: list[str] = []
for line in lines:
    key = line.split("=", 1)[0] if "=" in line else None
    if key in values:
        out.append(f"{key}={values[key]}")
        seen.add(key)
    else:
        out.append(line)
for key, value in values.items():
    if key not in seen:
        out.append(f"{key}={value}")
path.write_text("\n".join(out) + "\n")
for key in values:
    print(f"{key}={values[key]}")
PY

chmod 600 "$ENV_FILE"
echo "backup: $ENV_FILE.bak.*"
echo "CR_COUNT=$(grep -c $'\r' "$ENV_FILE" || true)"
