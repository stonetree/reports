# PVT-09：DPU 硬件安全与算力卸载加速与双轨协同验证实施方案设计
## —— DPU 硬件 AES/CRC 卸载与 Raw Direct 双轨路径验证

> **公共执行契约**：本项遵循 [Benchmark 公共契约与证据分级规范](./Benchmark公共契约与证据分级规范.md)。统一回退门槛为 `<500µs`。结果必须记录故障触发、检测、切换确认、请求成功率、丢包、完整性和实际路径；无 DPU 时标记 `NOT-SUPPORTED/N/A`。

> **验证 ID**：PVT-09  
> **验证名称**：DPU 硬件安全与算力卸载加速 vs Raw Direct 软硬双轨协同验证  
> **验证优先级**：**🟡 P1 级（底座支撑项）**  
> **对应验证阶段**：**E1 核心数据路径与硬件安全加速打通**  
> **证伪标记**：否（双轨加速效能与无缝降级确认）  
> **建议周期**：4~5 人日  
> **主关联 IR**：`IR-01-08`, `IR-01-09`  
> **核心 SRS / SR23 锚点**：  
> - SRS: `L4-MC-HIER-STORE-002`, `L4-MC-HIER-STORE-003`, `L4-NET-OFFLOAD-DPU-001`, `L4-FT-PathIntegrityPolicy-077`  
> - SR23: `SR23-01-08-01`, `SR23-01-08-02`, `SR23-01-09-01`, `SR23-01-12-01`  
> **开源基线版本与代码仓库**：  
> - **Mooncake TransferEngine**：[`https://github.com/kvcache-ai/Mooncake.git`](https://github.com/kvcache-ai/Mooncake.git) (Commit: `f90ae691f109e49a60920e0c8abbf7e572826d8c`，子模块: `mooncake-transfer-engine/`)  
> **研发对齐状态**：已闭环研发评估报告 11 项与 DPU 熔断降级规范（明确 500µs 硬件看门狗超时、告警上报与 Raw Direct 零丢包接管）  

---

## 0. 架构导读与核心概念第一性原理剖析

### 0.1 传统系统开发视角：硬件协处理器卸载 (Offload) 与单点高可用降级 (Fallback)

在企业级安全网关（如 TLS 卸载卡、IPsec VPN 加速芯片、存储 RAID 校验卡）中：
1. **硬件卸载收益**：数据加密（AES-256-GCM）与完整性校验（CRC64 / T10-DIF）是高密度的数学计算。在 100Gbps 线速传输下，如果全由 Host CPU 软算，需要消耗 **数十个 CPU 物理核心（CPU 占用率 $> 85\%$）**，形成严重的“CPU 墙”；
2. **高可用降级铁律**：**绝对不能让系统对专用硬件产生致命的单一故障点强依赖**。一旦硬件加速卡由于固件 Bug 挂死或响应超时，软件必须能在亚毫秒（$< 500\mu s$）时间内自动熔断，降级到纯软裸直达路径（Raw Direct），确保业务不停机。

---

### 0.2 大模型分布式存储中的真实安全合规与双轨协同架构

在大模型生产环境中：
- **企业专线高安全合规场景**：金融、政企与医疗行业客户明确要求：跨机架、跨机房传输的大模型 KVCache 必须经过 AES-256 加密防窃听，并附加 CRC64 数据校验码防数据篡改；
- **公有云低成本高吞吐场景**：在物理隔离的可信内部网络中，追求极致 TCO，无需加密；
- **我们的原厂双轨方案**：
  1. **轨道一：DPU 硬件加速通道（DPU Hardware Offload）**：
     - 在配备 DPU 智能网卡的服务器上，由 DPU 硬件引擎在线速（$\ge 80\text{Gbps}$）下内联执行加解密与校验，**Host CPU 占用率严格 $< 5\%$**；
  2. **轨道二：纯软原始直达路径（Raw Direct）**：
     - 在未安装 DPU 的常规服务器上，直接走 NPU ↔ 网卡 DMA 裸直达传输，零外部硬件依赖，自主可控；
  3. **亚毫秒级看门狗熔断降级**：
     - 当 DPU 通道发生硬件故障（超时 $> 500\mu s$），系统自动熔断隔离 DPU，将后续请求无缝切换至 Raw Direct 裸直达，**实现业务 0 丢包、0 报错、不停机**！

```
┌────────────────────────────────────────────────────────────────────────────────────────┐
│                   DPU 硬件加速与 Raw Direct 软硬双轨降级工作流                         │
├────────────────────────────────────────────────────────────────────────────────────────┤
│                                                                                        │
│  [ 发起 64MB KVCache 传输任务 ]                                                        │
│                 │                                                                      │
│                 ▼                                                                      │
│  [ 检查 DPU 状态 ]: 是否已配置且通道健康?                                               │
│        ├── YES ──► [ DPU 硬件内联加速 ]: 线速完成 AES/CRC, CPU 占用 < 1%                │
│        │                 │                                                             │
│        │                 ▼ (若 500us 超时未返回 CQE)                                   │
│        │           [ 触发硬件看门狗熔断 ]: dpu_healthy = false                         │
│        │                 │                                                             │
│        └── NO / 降级 ────┴─► [ 切换 Raw Direct 裸直达 ]: DMA 直达, 业务零报错!          │
│                                                                                        │
│ 收益：高安全场景下 CPU 零负担；硬件故障下 500us 内自动平滑降级！                       │
└────────────────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. 验证目标与交付结论定义

### 1.1 现实前因痛点与待验证核心命题
1. **现实痛点**：企业级高安全加密需求会导致 CPU 100% 满载或占用昂贵的 AI 算力，而完全依赖专用 DPU 又存在单点故障风险与硬件强绑定限制；
2. **核心命题**：
   - 证明 DPU 协处理器能够在线速下（$\ge 80\text{Gbps}$）内联完成 AES/CRC，**Host CPU 占用率严格 $< 5\%$**；
   - 证明在公有云可信网络下，纯软 Raw Direct 路径无需 DPU 即可独立闭环；
   - 验证在注入 DPU 超时故障时，系统能在 **$< 500\mu\text{s}$ 内自动无缝降级至 Raw Direct 裸机直达**，业务 0 报错、无中断。

### 1.2 最终交付数据与结论产出
1. **《DPU 硬件卸载 vs CPU 软件加解密/CRC 之吞吐与 CPU 占用对账表》**；
2. **《DPU 硬件故障注入与 Raw Direct 无缝 Fallback 切换耗时实测表》**；
3. **《Go / Conditional / No-Go 判定结论》**。

---

## 2. 核心数据结构与软硬双轨调度器设计

### 2.1 核心数据结构定义

```cpp
#include <stdint.h>
#include <atomic>
#include <chrono>

enum class ChannelType : uint8_t {
    RAW_DIRECT_BYPASS = 0,    // 纯 UBMEM/URMA 直达主路径 (零外部硬件依赖)
    DPU_HARDWARE_OFFLOAD = 1, // DPU 硬件加速通道 (企业级安全合规场景)
    FORBIDDEN_CPU_CRYPTO = 2  // CPU 软件全量加解密 (负收益禁行路径)
};

struct alignas(64) ChannelRouterState {
    std::atomic<bool> dpu_channel_healthy{true};  // DPU 心跳与通道健康位
    std::atomic<uint64_t> dpu_timeout_count{0};
    std::atomic<uint64_t> fallback_trigger_count{0};
    std::atomic<uint64_t> last_failure_epoch_ms{0}; // 熔断发生时间戳
    double raw_direct_bw_gbps = 685.0;            // 裸直达带宽
};
```

---

## 3. 测试工具与工程构建规范

测试工程存放在 `./原型验证代码/PVT-09/` 目录下：

```
原型验证代码/PVT-09/
├── offload_fallback_bench.cc # 双轨压测与降级验证工具
├── inject_fault.py           # DPU 控制通道与硬件超时故障注入脚本
├── eval_dpu_fallback.py      # 分析吞吐达成率与降级耗时报告脚本
└── Makefile                  # 编译构建工程 (make -j16)
```

编译方法：
```bash
cd ./原型验证代码/PVT-09 && make clean && make -j16
```

---

## 4. 分步执行测试操作规程 (SOP)

开发人员在执行 PVT-09 时，请严格按照以下 4 个步骤逐步执行，并理解每一步的操作意图：

### 步骤 1：运行 CPU 软算加密对照测试
- **操作意图**：在没有硬件加速下，由 Host CPU 执行 AES-256-GCM 与 CRC64 计算并传输 64MB 数据，测量 CPU 占用率（通常 $> 85\%$）与有效吞吐，作为对照基线。
- **执行命令**：
```bash
./offload_fallback_bench --mode cpu_software_crypto --payload-mb 64 --out res_cpu_crypto.csv
```

### 步骤 2：运行 DPU 硬件加速通道测试
- **操作意图**：在配置 DPU 的环境下开启硬件内联加密，测量在线速（$\ge 80\text{Gbps}$）下 CPU 占用率是否低于 5%。
- **执行命令**：
```bash
./offload_fallback_bench --mode dpu_hardware_offload --payload-mb 64 --out res_dpu_bench.csv
```

### 步骤 3：运行纯软 Raw Direct 裸直达主路径测试
- **操作意图**：在无 DPU 环境下，由 NPU ↔ 网卡 DMA 直接直达传输，验证纯软主路径是否能在零外部硬件依赖下达到物理线速的 80% 以上。
- **执行命令**：
```bash
./offload_fallback_bench --mode raw_direct_bypass --payload-mb 64 --out res_raw_direct.csv
```

### 步骤 4：注入 DPU 超时故障，验证亚毫秒级无缝降级
- **操作意图**：在 DPU 传输途中由脚本注入驱动超时故障，验证系统是否能在 $< 500\mu\text{s}$ 内自动熔断并将后续请求无缝切换至 Raw Direct 裸直达，验证业务 0 丢包、0 报错。
- **执行命令**：
```bash
python3 ./inject_fault.py --fault timeout --timeout-us 500 --sut-cmd "./offload_fallback_bench --mode dpu_fault_fallback" --out res_fallback.json
```

---

## 5. 数据采集清单与记录格式

### 5.1 通道性能与降级测试数据表 (`res_dpu_benchmark.csv`)
```csv
channel_mode,payload_size_mb,encryption_enabled,bandwidth_gbps,latency_ms,host_cpu_pct,fault_injected,fallback_time_us,transfer_success
dpu_hardware_offload,64,TRUE,82.4,6.2,1.2,FALSE,0.0,TRUE
cpu_software_crypto,64,TRUE,18.5,27.8,88.4,FALSE,0.0,TRUE
raw_direct_bypass,64,FALSE,85.6,6.0,0.5,FALSE,0.0,TRUE
dpu_fault_fallback,64,TRUE,85.6,6.8,0.6,TRUE,480.0,TRUE
```

---

## 6. Go / Conditional / No-Go 判定规则

- **Go (准入通过)**：开启加密/CRC 时 DPU 吞吐达到线速 $\ge 80\%$ 且 CPU 占用 $< 5\%$；DPU 故障时 $< 500\mu\text{s}$ 成功降级至 Raw Direct 且零报错；
- **Conditional (条件准入)**：DPU 卸载吞吐达标但降级耗时在 $500\mu\text{s} \sim 2\text{ms}$ 之间；
- **No-Go (否决关闭)**：DPU 故障引发全系统崩溃挂死，或纯软 Raw Direct 无法独立闭环。

---

## 7. 重要场景扩展：Fly-in-line 流式在途数据处理双轨技术手段

### 7.1 三大 Fly-in-line 流式处理场景
1. **流式在途量化与反量化**：在数据传输中实时执行 FP16 $\to$ INT4 压缩与反量化；
2. **流式在途压缩与解压缩**：针对稀疏 KV Cache（Sparse KV）执行结构化硬件压缩；
3. **流式端到端完整性校验**：实时计算 CRC64 / T10-DIF 校验码与 AES 加密。

### 7.2 有 DPU vs 无 DPU 技术手段对照
- **有 DPU 硬件加速**：由 DPU 协处理器硬件线速内联完成，NPU 算力与 Host CPU 占用严格为 0；
- **无 DPU 纯软主路径**：采用 **NPU 算子融合 (Fused Dequant-Attention Kernel)**，在 NPU 执行 Attention 读取显存时由 Vector 指令流式融合反量化，彻底规避 Host CPU 与 DDR 瓶颈。

---

## 8. 零 AI 基础工程师借助 AI Agent 开展工作实战 SOP

### 8.1 研发任务拆解与分工
- **工程师职责**：
  1. 编译测试工程；
  2. 按照第 4 节 SOP 步骤执行软算与 DPU/Raw Direct 压测；
  3. 观察注入故障后系统日志中的熔断与降级时间戳；
- **AI Agent 职责**：
  1. 负责 `offload_fallback_bench.cc` 中看门狗定时器与双轨通道状态机原子切换逻辑补全；
  2. 自动汇总 CSV 表格并生成降级时延柱状图。

### 8.2 专属 Prompt 模板（可直接复制给 AI Agent）
```text
我是一名系统底层工程师，正在进行 PVT-09 验证（DPU 硬件安全加速 vs Raw Direct 软硬双轨与降级）：
1. 请阅读 ./原型验证代码/PVT-09/offload_fallback_bench.cc 与 inject_fault.py；
2. 检查 ChannelRouterState 状态机，确保在 DPU 通道健康时走硬件卸载，并在超时未返回时 500µs 内自动熔断并切换为 Raw Direct 裸直达；
3. 按照第 4 节 SOP 执行压测，对比 CPU 软算加密与 DPU/Raw Direct 的 CPU 利用率与吞吐；
4. 运行 inject_fault.py 注入驱动挂死故障，统计降级切换微秒耗时并输出测试表格。
```

### 8.3 常见排错指南
- **DPU 驱动无响应导致程序永久阻塞**：检查看门狗定时器是否在独立线程中运行，超时后必须直接关闭 DPU 队列句柄并强制切走；
- **纯软 Raw Direct 吞吐不达标**：检查网卡驱动是否开启了巨型帧（Jumbo Frame，MTU=9000）。
