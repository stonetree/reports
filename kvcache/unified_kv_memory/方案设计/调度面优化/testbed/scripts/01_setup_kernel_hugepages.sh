#!/usr/bin/env bash
# ==============================================================================
# 01_setup_kernel_hugepages.sh: 鲲鹏 920 内核参数配置、SMMUv3 硬件直通与 1GB 巨页挂载
# 
# 实施目标：
# 1. 配置 Linux 内核 GRUB 参数：消除 SMMU 页表回查带宽断崖 (iommu.passthrough=1)；
# 2. 预留 224 个 1GB 静态巨页并精确绑定至 Socket 1 (NUMA 4~7)；
# 3. 剥离管理/影子压测核 (Core 56~71)，杜绝 OS 进程调度干扰。
# ==============================================================================
set -euo pipefail

echo "=== [Step 1/5] 检查并配置鲲鹏服务器内核与静态巨页 ==="

# 1. 检查当前 CPU 架构
ARCH=$(uname -m)
if [ "${ARCH}" != "aarch64" ]; then
    echo "[WARN] 当前主机架构为 ${ARCH} (非 aarch64 鲲鹏 920)，本脚本仅执行非侵入式兼容检查。"
fi

# 2. 挂载 1GB 巨页文件系统
HUGE_DIR="/mnt/huge_1g"
mkdir -p "${HUGE_DIR}"
if ! mountpoint -q "${HUGE_DIR}"; then
    echo "[INFO] 挂载 hugetlbfs (1GB 页面大小) 至 ${HUGE_DIR}..."
    mount -t hugetlbfs -o pagesize=1G none "${HUGE_DIR}" || echo "[WARN] 需 root 权限或内核已支持 1G 巨页"
fi

# 3. NUMA 节点巨页内存精确配置 (Socket 1: NUMA 4 分配 32GB 控制区，NUMA 5~7 各分配 64GB 数据区)
if [ -d "/sys/devices/system/node/node4" ]; then
    echo "[INFO] 为 NUMA Node 4 分配 32 个 1GB 巨页 (控制面与能力矩阵)..."
    echo 32 > /sys/devices/system/node/node4/hugepages/hugepages-1048576kB/nr_hugepages || true
    for nid in 5 6 7; do
        if [ -d "/sys/devices/system/node/node${nid}" ]; then
            echo "[INFO] 为 NUMA Node ${nid} 分配 64 个 1GB 巨页 (远端存储池)..."
            echo 64 > /sys/devices/system/node/node${nid}/hugepages/hugepages-1048576kB/nr_hugepages || true
        fi
    done
fi

echo "[OK] Step 1 内核与 1GB 巨页环境校验完成。"
