#!/usr/bin/env bash
# ==============================================================================
# 控制面性能放大效应测试平台 - 集群物理节点与网络环境变量配置
# 
# 使用说明：
#   在进入测试或执行任何脚本前，请根据实际测试集群的真实 IP 和端口修改本文件，
#   并在测试机控制台执行：source configs/cluster_env.sh
# ==============================================================================

# [1] 被测 SUT 节点 1 (Subject Under Test 1: vLLM / Mooncake 实例 1)
export SUT1_IP="${SUT1_IP:-192.168.10.101}"
export SUT1_PORT="${SUT1_PORT:-8000}"
export SUT1_HOST="${SUT1_HOST:-sut1}"
export SUT1_URL="http://${SUT1_IP}:${SUT1_PORT}"

# [2] 被测 SUT 节点 2 (Subject Under Test 2: vLLM / Mooncake 实例 2)
export SUT2_IP="${SUT2_IP:-192.168.10.102}"
export SUT2_PORT="${SUT2_PORT:-8000}"
export SUT2_HOST="${SUT2_HOST:-sut2}"
export SUT2_URL="http://${SUT2_IP}:${SUT2_PORT}"

# [3] 压测主控端与影子集群宿主机 (Benchmark Controller & Shadow Cluster Host)
export CONTROLLER_IP="${CONTROLLER_IP:-192.168.10.100}"
export SHADOW_WORKER_COUNT=18
export SHADOW_BURST_CONCURRENCY=22

# [4] 物理硬件与网络绑定
export ROCE_DEV="${ROCE_DEV:-mlx5_0}"
export ROCE_GID_INDEX="${ROCE_GID_INDEX:-3}"
export SUT_NUMA_NODE="${SUT_NUMA_NODE:-0}"

# [5] 探测器与压测路径
export PROBE_LIB_PATH="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/csrc/libcontrol_probe.so"
export TRACES_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/traces/salted_20"
export LOGS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/logs"

# [6] 自动写入 /etc/hosts 解析映射提示
echo "=============================================================================="
echo " [Cluster Config] 已成功加载集群环境变量:"
echo "   - SUT1 Target: ${SUT1_URL} (Hostname: ${SUT1_HOST})"
echo "   - SUT2 Target: ${SUT2_URL} (Hostname: ${SUT2_HOST})"
echo "   - Controller : ${CONTROLLER_IP}"
echo "   - C++ Probe  : ${PROBE_LIB_PATH}"
echo "   - Traces Dir : ${TRACES_DIR}"
echo "   - Logs Dir   : ${LOGS_DIR}"
echo "=============================================================================="
echo " 提示：若使用容器或主机名访问，请确保本机的 /etc/hosts 包含如下映射："
echo "   ${SUT1_IP}    ${SUT1_HOST}"
echo "   ${SUT2_IP}    ${SUT2_HOST}"
echo "=============================================================================="
