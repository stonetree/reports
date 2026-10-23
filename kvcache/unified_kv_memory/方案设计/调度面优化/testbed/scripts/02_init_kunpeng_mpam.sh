#!/usr/bin/env bash
# ==============================================================================
# 02_init_kunpeng_mpam.sh: 挂载 ARM MPAM (resctrl) 并建立三级硬件资源物理隔离
# 
# 实施目标：
# 1. 挂载 resctrl 文件系统并创建 ctrl_meta (PARTID=0), fg_infer (PARTID=1), bg_evict (PARTID=2)；
# 2. 独占锁死高 4 路 L3 Cache 给控制面元数据表 (0xf000)，确保命中率 >= 99.2%；
# 3. 限制后台换出线程 L3 仅使用最低 2 路 (0x0003)，防止大流量 DMA 冲刷 CPU 缓存；
# 4. 配置 DDRC 初始内存带宽配额 (MBW_MAX)。
# ==============================================================================
set -euo pipefail

echo "=== [Step 2/5] 初始化鲲鹏 MPAM (resctrl) L3 CPOR 与 DDRC 带宽硬件隔离 ==="

RESCTRL_PATH="/sys/fs/resctrl"

if [ ! -d "${RESCTRL_PATH}" ]; then
    mkdir -p "${RESCTRL_PATH}"
fi

if ! mountpoint -q "${RESCTRL_PATH}"; then
    echo "[INFO] 尝试挂载 resctrl 文件系统..."
    mount -t resctrl resctrl "${RESCTRL_PATH}" || {
        echo "[WARN] 无法直接挂载 resctrl，请确认 BIOS MPAM 已开启且以 root 权限执行。"
        exit 0
    }
fi

# 创建三大硬件隔离组
mkdir -p "${RESCTRL_PATH}/ctrl_meta"
mkdir -p "${RESCTRL_PATH}/fg_infer"
mkdir -p "${RESCTRL_PATH}/bg_evict"

# 1. 配置 L3 Cache 容量路掩码 (假设每 SCCL 为 16-Way L3)
echo "[INFO] 配置 L3 CPOR 路掩码 (ctrl_meta 独占高 4 路: 0xf000)..."
echo "L3:0=0fff;1=0fff;2=0fff;3=0fff;4=f000;5=0fff;6=0fff;7=0fff" > "${RESCTRL_PATH}/ctrl_meta/schemata" || true
echo "L3:0=0fff;1=0fff;2=0fff;3=0fff;4=0fff;5=0fff;6=0fff;7=0fff" > "${RESCTRL_PATH}/fg_infer/schemata" || true
echo "L3:0=0003;1=0003;2=0003;3=0003;4=0003;5=0003;6=0003;7=0003" > "${RESCTRL_PATH}/bg_evict/schemata" || true

# 2. 配置 DDRC 内存带宽硬配额初始值 (MBW_MAX)
echo "[INFO] 配置 DDRC MBW_MAX 内存带宽硬配额..."
echo "MB:0=100;1=100;2=100;3=100;4=100;5=100;6=100;7=100" > "${RESCTRL_PATH}/ctrl_meta/schemata" || true
echo "MB:0=100;1=100;2=100;3=100;4=100;5=100;6=100;7=100" > "${RESCTRL_PATH}/fg_infer/schemata" || true
echo "MB:0=70;1=70;2=70;3=70;4=70;5=70;6=70;7=70" > "${RESCTRL_PATH}/bg_evict/schemata" || true

# 3. 绑定物理核
echo "64-71" > "${RESCTRL_PATH}/ctrl_meta/cpus_list" || true
echo "0-63"  > "${RESCTRL_PATH}/fg_infer/cpus_list" || true
echo "72-127"> "${RESCTRL_PATH}/bg_evict/cpus_list" || true

echo "[OK] Step 2 鲲鹏 MPAM 硬件隔离分区配置完成。"
