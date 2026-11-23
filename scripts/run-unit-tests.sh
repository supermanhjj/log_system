#!/usr/bin/env sh
# 单元测试总入口：逐个服务在各自目录下运行（app 包按服务独立打包，必须在服务目录内执行）。
#
# 用法：
#   sh scripts/run-unit-tests.sh
#   PYTHON_BIN=/path/to/venv/bin/python sh scripts/run-unit-tests.sh
#
# 前置依赖：python 环境已安装 services/requirements.txt 中的依赖 + pytest。
# 不写 pytest 缓存与 .pyc，避免把测试产物留在仓库里。
set -eu

ROOT=$(cd "$(dirname "$0")/.." && pwd)
PY=${PYTHON_BIN:-python3}
SERVICES="ingest ai search auth system dashboard alarm"
FAIL=0

for SVC in $SERVICES; do
    echo "=== ${SVC}-service"
    ( cd "$ROOT/services/${SVC}-service" \
      && PYTHONDONTWRITEBYTECODE=1 "$PY" -m pytest tests -q -p no:cacheprovider ) || FAIL=1
done

if [ "$FAIL" -ne 0 ]; then
    echo "存在失败用例"
    exit 1
fi
echo "全部通过"
