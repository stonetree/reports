#!/usr/bin/env bash
# ==============================================================================
# run_online_benchmark.sh - vLLM 官方 benchmark_serving 在线打流驱动套件
# 支持 ShareGPT 真实对话轨迹与 Controlled 合成前缀数据集，全自动采集 TTFT 与 TPOT
# ==============================================================================

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RESULT_DIR="${SCRIPT_DIR}/results"
mkdir -p "${RESULT_DIR}"

MODEL_PATH=${MODEL_PATH:-"Qwen/Qwen2.5-72B-Instruct"}
PROXY_PORT=${PROXY_PORT:-"8000"}
DATASET_PATH=${DATASET_PATH:-"ShareGPT_V3_unfiltered_cleaned_split.json"}
NUM_PROMPTS=${NUM_PROMPTS:-"200"}
REQUEST_RATES=(5 10 20 30 50)

echo "======================================================================"
echo " [BENCHMARK] 启动 vLLM benchmark_serving 在线打流压测"
echo " 目标模型: ${MODEL_PATH}"
echo " 服务地址: http://localhost:${PROXY_PORT}/v1"
echo " 测试请求数: ${NUM_PROMPTS}"
echo "======================================================================"

for rate in "${REQUEST_RATES[@]}"; do
    echo ""
    echo ">>> [Rate: ${rate} req/s] 发起在线打流测试..."
    OUT_FILE="${RESULT_DIR}/bench_serving_rate_${rate}.json"
    
    python3 -m vllm.benchmarks.benchmark_serving \
        --backend vllm \
        --model "${MODEL_PATH}" \
        --dataset-name sharegpt \
        --dataset-path "${DATASET_PATH}" \
        --num-prompts "${NUM_PROMPTS}" \
        --request-rate "${rate}" \
        --port "${PROXY_PORT}" \
        --save-result \
        --result-filename "${OUT_FILE}"
        
    echo "Rate ${rate} req/s 压测完成，结果已保存至 ${OUT_FILE}"
done

echo ""
echo "[ANALYZE] 解析并生成消融对比表格..."
python3 "${SCRIPT_DIR}/parse_benchmark_metrics.py" --results-dir "${RESULT_DIR}" --output "${RESULT_DIR}/final_benchmark_summary.csv"

echo "======================================================================"
echo " [SUCCESS] 全量在线打流压测完成！汇总报告: ${RESULT_DIR}/final_benchmark_summary.csv"
echo "======================================================================"
