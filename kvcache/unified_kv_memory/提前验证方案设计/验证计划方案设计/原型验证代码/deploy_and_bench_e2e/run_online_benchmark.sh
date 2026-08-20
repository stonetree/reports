#!/usr/bin/env bash
# 按 mode/run_id 隔离在线打流结果；版本、配置、环境和证据等级均进入结果目录与解析器。
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MODE=${MODE:?MODE is required}
RUN_ID=${RUN_ID:?RUN_ID is required}
PACKAGE_ID=${PACKAGE_ID:?PACKAGE_ID is required}
CONFIG_HASH=${CONFIG_HASH:?CONFIG_HASH is required}
HARDWARE_PROFILE=${HARDWARE_PROFILE:?HARDWARE_PROFILE is required}
TOPOLOGY_PROFILE=${TOPOLOGY_PROFILE:?TOPOLOGY_PROFILE is required}
WORKLOAD_ID=${WORKLOAD_ID:?WORKLOAD_ID is required}
EVIDENCE_LEVEL=${EVIDENCE_LEVEL:-DEMO}
MODEL_PATH=${MODEL_PATH:-"Qwen/Qwen2.5-72B-Instruct"}
MODEL_ID=${MODEL_ID:-"${MODEL_PATH}"}
ENDPOINT_HOST=${ENDPOINT_HOST:-"localhost"}
PROXY_PORT=${PROXY_PORT:-"8000"}
DATASET_PATH=${DATASET_PATH:-"ShareGPT_V3_unfiltered_cleaned_split.json"}
NUM_PROMPTS=${NUM_PROMPTS:-"200"}
WARMUP_ROUNDS=${WARMUP_ROUNDS:-"1"}
MEASURE_ROUNDS=${MEASURE_ROUNDS:-"3"}
REQUEST_RATES=${REQUEST_RATES:-"5 10 20 30 50"}
RESULT_DIR="${SCRIPT_DIR}/results/${MODE}/${RUN_ID}"
mkdir -p "${RESULT_DIR}"

echo "mode=${MODE} run_id=${RUN_ID} package_id=${PACKAGE_ID} endpoint=${ENDPOINT_HOST}:${PROXY_PORT}"
for warmup in $(seq 1 "${WARMUP_ROUNDS}"); do
  python3 -m vllm.benchmarks.benchmark_serving \
    --backend vllm --model "${MODEL_PATH}" --dataset-name sharegpt --dataset-path "${DATASET_PATH}" \
    --num-prompts "${NUM_PROMPTS}" --request-rate 5 --host "${ENDPOINT_HOST}" --port "${PROXY_PORT}" \
    --save-result --result-filename "${RESULT_DIR}/warmup_${warmup}.json"
done

for repeat in $(seq 1 "${MEASURE_ROUNDS}"); do
  for rate in ${REQUEST_RATES}; do
    python3 -m vllm.benchmarks.benchmark_serving \
      --backend vllm --model "${MODEL_PATH}" --dataset-name sharegpt --dataset-path "${DATASET_PATH}" \
      --num-prompts "${NUM_PROMPTS}" --request-rate "${rate}" --host "${ENDPOINT_HOST}" --port "${PROXY_PORT}" \
      --save-result --result-filename "${RESULT_DIR}/bench_serving_rate_${rate}_rep_${repeat}.json"
  done
done

python3 "${SCRIPT_DIR}/parse_benchmark_metrics.py" \
  --results-dir "${RESULT_DIR}" --output "${RESULT_DIR}/summary.csv" --output-json "${RESULT_DIR}/summary.json" \
  --mode "${MODE}" --run-id "${RUN_ID}" --package-id "${PACKAGE_ID}" --config-hash "${CONFIG_HASH}" \
  --hardware-profile "${HARDWARE_PROFILE}" --topology-profile "${TOPOLOGY_PROFILE}" \
  --workload-id "${WORKLOAD_ID}" --model-id "${MODEL_ID}" --evidence-level "${EVIDENCE_LEVEL}"
