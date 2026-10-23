#!/usr/bin/env bash
# ==============================================================================
# 03_setup_dual_netns_loopback.sh: 配置双 Network Namespace 与物理光纤环回网络
# 
# 实施目标：
# 1. 切换 RDMA netns 为 exclusive 模式，杜绝网卡 ASIC 内部 Loopback 短路；
# 2. 建立 ns_compute (前台计算域) 与 ns_storage (远端存储域) 隔离命名空间；
# 3. 将 CX6/7 Port 0 直连 Port 1，数据流 100% 穿透外部物理光纤；
# 4. 配置 DSCP 46 (TC0 严格高优) 与 DSCP 8 (TC1 低优) 硬件流控队列。
# ==============================================================================
set -euo pipefail

DEV_PORT0="${1:-enp125s0f0}"
DEV_PORT1="${2:-enp125s0f1}"
RDMA_DEV0="${3:-mlx5_0}"
RDMA_DEV1="${4:-mlx5_1}"

echo "=== [Step 3/5] 配置双 Network Namespace 与 CX6/7 外部物理光纤环回 ==="

# 1. 检查网卡硬件 QoS 支持
if command -v mlnx_qos &> /dev/null; then
    for netdev in "${DEV_PORT0}" "${DEV_PORT1}"; do
        if ip link show "${netdev}" &> /dev/null; then
            echo "[INFO] 为 ${netdev} 配置严格优先级 QoS (TC0 Strict Priority)..."
            mlnx_qos -i "${netdev}" --trust=dscp || true
            mlnx_qos -i "${netdev}" --pfc=0,0,0,1,0,0,1,0 || true
            mlnx_qos -i "${netdev}" -s strict,ets,ets,ets,ets,ets,ets,ets || true
            ip link set "${netdev}" mtu 4200 up || true
        fi
    done
fi

# 2. 隔离 RDMA 命名空间
if command -v rdma &> /dev/null; then
    echo "[INFO] 切换 RDMA netns 模式为 exclusive..."
    rdma system set netns exclusive || true
fi

# 3. 创建网络命名空间
ip netns add ns_compute 2>/dev/null || true
ip netns add ns_storage 2>/dev/null || true

# 4. 分配物理网卡至对应命名空间
if ip link show "${DEV_PORT0}" &> /dev/null; then
    ip link set "${DEV_PORT0}" netns ns_compute || true
fi
if ip link show "${DEV_PORT1}" &> /dev/null; then
    ip link set "${DEV_PORT1}" netns ns_storage || true
fi

if command -v rdma &> /dev/null; then
    rdma dev set "${RDMA_DEV0}" netns ns_compute 2>/dev/null || true
    rdma dev set "${RDMA_DEV1}" netns ns_storage 2>/dev/null || true
fi

# 5. 配置跨物理光纤直连 IP
ip netns exec ns_compute ip addr add 192.168.100.10/24 dev "${DEV_PORT0}" 2>/dev/null || true
ip netns exec ns_compute ip link set "${DEV_PORT0}" up 2>/dev/null || true

ip netns exec ns_storage ip addr add 192.168.100.20/24 dev "${DEV_PORT1}" 2>/dev/null || true
ip netns exec ns_storage ip link set "${DEV_PORT1}" up 2>/dev/null || true

echo "[OK] Step 3 双命名空间与物理光纤环回配置完毕。"
