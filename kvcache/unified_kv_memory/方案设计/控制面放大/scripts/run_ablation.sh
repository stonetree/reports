#!/usr/bin/env bash
# ==============================================================================
# run_ablation.sh - 四大架构逐层消融验证自动化执行脚本
# 验证流程：
#   Exp-0 (Legacy)   : 传统同步控制面基线
#   Exp-1 (+A)       : 开启鲲鹏 MPAM 物理锁 L3 + 后台限流 30% + CX6 UMR 模板环
#   Exp-2 (+A+B)     : 追加两阶段意图租约 + 500µs PMU 遥测与 1µs 原子预扣选路门禁
#   Exp-3 (+A+B+C)   : 追加六级显存水位容量信用流水线 + 16GB 本地物理硬保底区
# ==============================================================================

set -euo pipefail

BASE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 0. 加载集群网络与节点环境变量 (若存在)
if [ -f "${BASE_DIR}/configs/cluster_env.sh" ]; then
    echo "[Ablation Study] 加载集群环境配置: ${BASE_DIR}/configs/cluster_env.sh"
    source "${BASE_DIR}/configs/cluster_env.sh"
fi

# 0.1 执行集群连通性与软硬件前置就绪检查
echo "[Ablation Study] 触发集群前置就绪体检 (Preflight Check)..."
if ! python3 "${BASE_DIR}/scripts/preflight_check.py" --quick; then
    echo "❌ 前置检查存在阻塞性错误，请修复后重新执行消融测试！"
    exit 1
fi

RESULTS_ROOT="${BASE_DIR}/results/ablation_$(date +%Y%m%d_%H%M%S)"
mkdir -p "${RESULTS_ROOT}"

echo "================================================================================"
echo " [Ablation Study] 开始执行四大架构逐层消融验证"
echo " 输出根目录: ${RESULTS_ROOT}"
echo "================================================================================"

# 1. 实验 Exp-0: 传统基线 (Legacy)
echo -e "\n>>> [1/4] 启动 Exp-0: 传统同步控制面 (Legacy Baseline)..."
EXP0_DIR="${RESULTS_ROOT}/exp0_legacy"
mkdir -p "${EXP0_DIR}"
# 环境变量重置为无优化基线
export ENABLE_MPAM_ISOLATION=0
export ENABLE_UMR_TEMPLATE=0
export ENABLE_INTENT_LEASE=0
export ENABLE_CAPACITY_CREDIT=0

# 运行扫描
python3 "${BASE_DIR}/scripts/run_delay_sweep.py" \
    --config "${BASE_DIR}/configs/sweep_matrix.json" \
    --output-dir "${EXP0_DIR}" \
    --points 81 --requests-per-point 20000

# 2. 实验 Exp-1: 底层软硬协同 (+A)
echo -e "\n>>> [2/4] 启动 Exp-1: 开启底层软硬协同 (+A MPAM + UMR)..."
EXP1_DIR="${RESULTS_ROOT}/exp1_plus_A"
mkdir -p "${EXP1_DIR}"
# 生效 MPAM 与 UMR
export ENABLE_MPAM_ISOLATION=1
export ENABLE_UMR_TEMPLATE=1
export ENABLE_INTENT_LEASE=0
export ENABLE_CAPACITY_CREDIT=0

python3 "${BASE_DIR}/scripts/run_delay_sweep.py" \
    --config "${BASE_DIR}/configs/sweep_matrix.json" \
    --output-dir "${EXP1_DIR}" \
    --points 81 --requests-per-point 20000

# 3. 实验 Exp-2: 时效感知预测路由 (+A+B)
echo -e "\n>>> [3/4] 启动 Exp-2: 开启预测路由与意图租约 (+A+B)..."
EXP2_DIR="${RESULTS_ROOT}/exp2_plus_AB"
mkdir -p "${EXP2_DIR}"
export ENABLE_MPAM_ISOLATION=1
export ENABLE_UMR_TEMPLATE=1
export ENABLE_INTENT_LEASE=1
export ENABLE_CAPACITY_CREDIT=0

python3 "${BASE_DIR}/scripts/run_delay_sweep.py" \
    --config "${BASE_DIR}/configs/sweep_matrix.json" \
    --output-dir "${EXP2_DIR}" \
    --points 81 --requests-per-point 20000

# 4. 实验 Exp-3: 全异步容量信用换出 (+A+B+C)
echo -e "\n>>> [4/4] 启动 Exp-3: 开启去临界路径化全异步换出 (+A+B+C 终极版)..."
EXP3_DIR="${RESULTS_ROOT}/exp3_plus_ABC"
mkdir -p "${EXP3_DIR}"
export ENABLE_MPAM_ISOLATION=1
export ENABLE_UMR_TEMPLATE=1
export ENABLE_INTENT_LEASE=1
export ENABLE_CAPACITY_CREDIT=1

python3 "${BASE_DIR}/scripts/run_delay_sweep.py" \
    --config "${BASE_DIR}/configs/sweep_matrix.json" \
    --output-dir "${EXP3_DIR}" \
    --points 81 --requests-per-point 20000

echo -e "\n================================================================================"
echo " [Ablation Study] 四大消融实验全部执行完毕！"
echo " 正在触发数据归约、Logistic 拟合与高清对比图表绘制..."
echo "================================================================================"

python3 "${BASE_DIR}/scripts/analyze_and_fit.py" \
    --ablation-dir "${RESULTS_ROOT}" \
    --output-dir "${RESULTS_ROOT}/summary_report"

echo "[Ablation Study] 消融归约完成，对账报告已生成于: ${RESULTS_ROOT}/summary_report"
