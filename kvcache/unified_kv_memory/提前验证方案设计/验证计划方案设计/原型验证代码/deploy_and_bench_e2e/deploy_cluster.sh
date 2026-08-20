#!/usr/bin/env bash
# ==============================================================================
# deploy_cluster.sh - 开源基线 vLLM + vLLM-Ascend + Mooncake 集群一键部署启动脚本
# 支持 Prefill / Decode 分离 (PD Disaggregation) 部署架构与 Mooncake KV 传输底座
# ==============================================================================

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="${SCRIPT_DIR}/logs"
mkdir -p "${LOG_DIR}"

EVIDENCE_ENVIRONMENT=${EVIDENCE_ENVIRONMENT:-"W0"}
if [[ "${EVIDENCE_ENVIRONMENT}" != "W0" ]]; then
    echo "deploy_cluster.sh only demonstrates the W0 localhost workflow. Use the site-specific multi-node orchestrator for W1/W2." >&2
    exit 2
fi

MODEL_PATH=${MODEL_PATH:-"Qwen/Qwen2.5-72B-Instruct"}
MOONCAKE_CONFIG=${MOONCAKE_CONFIG:-"${SCRIPT_DIR}/mooncake.config"}
MASTER_PORT=${MASTER_PORT:-"10001"}
PREFILL_PORT=${PREFILL_PORT:-"8100"}
DECODE_PORT=${DECODE_PORT:-"8200"}
PROXY_PORT=${PROXY_PORT:-"8000"}
KV_CONNECTOR=${KV_CONNECTOR:-"MooncakeLayerwiseConnector"} # 或 MooncakeStoreConnector

echo "======================================================================"
echo " [DEPLOY] 启动统一异构 KVCache 存储池开源基线集群 (vLLM + Mooncake)"
echo " 模型: ${MODEL_PATH}"
echo " 连接器: ${KV_CONNECTOR}"
echo " 日志目录: ${LOG_DIR}"
echo " 证据环境档位: ${EVIDENCE_ENVIRONMENT} (localhost workflow; DEMO only)"
echo "======================================================================"

# 1. 启动 Mooncake 元数据 Master 守护进程
echo "[1/4] 启动 Mooncake Master 服务 (Port: ${MASTER_PORT})..."
nohup mooncake_master --port "${MASTER_PORT}" > "${LOG_DIR}/mooncake_master.log" 2>&1 &
MASTER_PID=$!
echo "Mooncake Master PID: ${MASTER_PID}"
sleep 2

# 2. 启动 Node-0 Prefill 实例 (kv_role: kv_producer)
echo "[2/4] 启动 Prefill 实例 (Port: ${PREFILL_PORT})..."
export MOONCAKE_CONFIG_PATH="${MOONCAKE_CONFIG}"
export VLLM_WORKER_MULTIPROC_METHOD=spawn
export VLLM_ASCEND_ENABLE_LAYERWISE=1

nohup python3 -m vllm.entrypoints.openai.api_server \
    --model "${MODEL_PATH}" \
    --port "${PREFILL_PORT}" \
    --tensor-parallel-size 8 \
    --max-model-len 32768 \
    --gpu-memory-utilization 0.85 \
    --kv-transfer-config "{\"kv_connector\":\"${KV_CONNECTOR}\",\"kv_role\":\"kv_producer\"}" \
    > "${LOG_DIR}/prefill.log" 2>&1 &
PREFILL_PID=$!
echo "Prefill Instance PID: ${PREFILL_PID}"

# 3. 启动 Node-1 Decode 实例 (kv_role: kv_consumer)
echo "[3/4] 启动 Decode 实例 (Port: ${DECODE_PORT})..."
nohup python3 -m vllm.entrypoints.openai.api_server \
    --model "${MODEL_PATH}" \
    --port "${DECODE_PORT}" \
    --tensor-parallel-size 8 \
    --max-model-len 32768 \
    --gpu-memory-utilization 0.85 \
    --kv-transfer-config "{\"kv_connector\":\"${KV_CONNECTOR}\",\"kv_role\":\"kv_consumer\"}" \
    > "${LOG_DIR}/decode.log" 2>&1 &
DECODE_PID=$!
echo "Decode Instance PID: ${DECODE_PID}"

# 等待 vLLM 服务就绪
echo "等待 Prefill & Decode 实例初始化权重与显存池..."
for port in ${PREFILL_PORT} ${DECODE_PORT}; do
    until curl -s "http://localhost:${port}/v1/models" > /dev/null 2>&1; do
        sleep 3
        echo "等待端口 ${port} 响应..."
    done
    echo "服务端口 ${port} 已就绪！"
done

# 4. 启动 PD 分离调度代理 (Proxy Demo)
echo "[4/4] 启动 PD Disaggregation 调度代理 (Port: ${PROXY_PORT})..."
nohup python3 -m mooncake_integration.proxy_demo \
    --prefill-ports "${PREFILL_PORT}" \
    --decode-ports "${DECODE_PORT}" \
    --proxy-port "${PROXY_PORT}" \
    > "${LOG_DIR}/proxy.log" 2>&1 &
PROXY_PID=$!
echo "PD Proxy PID: ${PROXY_PID}"

echo "======================================================================"
echo " [READY] W0 localhost 工作流已就绪；该结果只能标记 DEMO。入口: http://localhost:${PROXY_PORT}/v1"
echo " 运行 ./run_online_benchmark.sh 即可发起端到端压测。"
echo "======================================================================"
