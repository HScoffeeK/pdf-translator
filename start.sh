#!/usr/bin/env bash
# macOS / Linux 一键启动:自动找 Python、建虚拟环境、装依赖、启动并打开浏览器
cd "$(dirname "$0")" || exit 1
echo "=========================================="
echo "   PDF 翻译工具 - 一键启动"
echo "=========================================="

# ---- 1. 查找 Python 3.11+ ----
PY=""
for c in python3.12 python3.11 python3; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c "import tomllib" >/dev/null 2>&1; then
        PY="$c"; break
    fi
done
if [ -z "$PY" ]; then
    echo "[!] 未检测到 Python 3.11+,请先安装后重新运行本脚本:"
    echo "    macOS:         brew install python@3.12"
    echo "    Ubuntu/Debian: sudo apt install python3.12 python3.12-venv"
    exit 1
fi
echo "[OK] Python: $PY"

# ---- 2. 虚拟环境与依赖(依赖清单变化时自动补装) ----
NEED=0
[ -x ".venv/bin/python" ] || NEED=1
[ -f ".venv/.reqs.txt" ] || NEED=1
if [ "$NEED" = "0" ] && ! cmp -s ".venv/.reqs.txt" "requirements.txt"; then
    NEED=1
fi
if [ "$NEED" = "1" ]; then
    echo "安装/更新依赖,需要几分钟..."
    [ -x ".venv/bin/python" ] || "$PY" -m venv .venv || exit 1
    .venv/bin/python -m pip install -r requirements.txt || \
        .venv/bin/python -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple || exit 1
    cp -f requirements.txt .venv/.reqs.txt
fi

# ---- 3. 启动(浏览器会自动打开) ----
echo
echo "启动中...浏览器将自动打开 http://127.0.0.1:8765 (Ctrl+C 停止)"
exec .venv/bin/python -m app
