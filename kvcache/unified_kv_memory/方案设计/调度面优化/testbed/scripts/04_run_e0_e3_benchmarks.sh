#!/usr/bin/env bash
# ==============================================================================
# 04_run_e0_e3_benchmarks.sh: 一键运行 E0 ~ E3 四阶段标准化验证与门限判定自动化套件
# ==============================================================================
set -euo pipefail

echo "========================================================================"
echo " HoloTestbed-4X: 调度面软硬件协同测试验证平台 E0~E3 自动化评测启动"
echo "========================================================================"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TESTBED_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"

# 检查 Python 环境
PYTHON_BIN="python3"
if ! command -v "${PYTHON_BIN}" &> /dev/null; then
    PYTHON_BIN="python"
fi

echo "[INFO] 使用 Python 执行器: $("${PYTHON_BIN}" --version)"

# 运行 Python 端到端全链路对账与可视化分析
echo "[INFO] 运行 E0~E3 仿真与全链路数学/物理闭环对账..."
"${PYTHON_BIN}" "${TESTBED_ROOT}/benchmarks/run_all_experiments.py"

echo "========================================================================"
echo " [SUCCESS] 全部验证阶段自动化对账完成，分析大盘已生成。"
echo "========================================================================"
