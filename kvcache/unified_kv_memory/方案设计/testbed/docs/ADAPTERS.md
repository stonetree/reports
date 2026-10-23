# 实验平台适配契约

这里约定工程与已部署服务之间的数据接口。配置值必须对应实验版本实际存在的接口；框架没有虚构的 `/control`、全局缓存清空或通用 NPU 性能计数接口。

## 服务

需要 `GET /v1/models` 和 `POST /v1/completions` 的 OpenAI 兼容接口。流式响应使用 SSE，包含最终 `usage.completion_tokens`、`finish_reason` 与 `[DONE]`。每个有效响应 chunk 的 token 数优先读取 `choices[].token_ids`，其次读取 `choices[].logprobs.tokens`。未经核验的流式 chunk 数不会当成 token 数。API key 通过配置指定的环境变量读取，不写入配置快照。

## 控制命令

`prepare_argv` 切换服务变体；`cache_reset_argv` 清理设备及外部池；`cache_verify_argv` 核验初态；`state_argv` 核验当前运行态。命令必须成功退出并等待动作真正完成。返回码不为零立即停止，stdout/stderr 全部归档。

资源注入必须提供成套的 `apply_argv`、`verify_argv` 和 `reset_argv`。支持以下 token 替换，每个替换仍处于原 argv 元素内：`{case_id}`、`{case_dir}`、`{variant}`、`{base_url}`、`{context_tokens}`、`{hook_control_file}`。不支持命令替换、管道或 shell 变量求值。SSH 可作为真实 argv 命令，但需要平台预先具备无交互访问及对应适配器。

运行态核验 `state_argv` 应在 stdout 输出：

```json
{
  "model": "served-model-name",
  "hardware_id": "lab-a2-8npu-01",
  "accelerator_count": 8,
  "external_cache": true,
  "hbm_budget_bytes": 123456789,
  "runtime_build": {"vllm_ascend_commit": "actual-commit", "mooncake_commit": "actual-commit"},
  "verified_from_runtime": true
}
```

数值为格式示例，必须由运行态读出。B0 `external_cache=false`；B1/B2/B3 为 true。返回的数据应来自真实进程的参数、监控或管理接口，不能只复制 testbed 配置。模型、资源数、硬件 ID、设备缓存预算等会被用于核验与配对。

CPU 亲和性适配器用法：

```bash
python3 -m kvcbench.resource apply --pids 123,456 --cpus 0-7 --state /srv/lab/cpu-state.json
python3 -m kvcbench.resource verify --state /srv/lab/cpu-state.json
python3 -m kvcbench.resource reset --state /srv/lab/cpu-state.json
```

它操作列出进程当前所有线程，保存原亲和性与线程启动代际；新建线程继承行为应在服务上另外观察。此操作限制 CPU 可用集合，不宣称等价于 DDR 带宽、LLC 容量或 CPU 功率上限。

## 观测

`telemetry.metrics` 将真实 Prometheus 指标映射为标准键。`labels` 做精确筛选，`reducer` 支持 `sum`、`max`、`mean`。`scale` 用于单位换算。默认保存所有原文，不猜测指标名字，也不把累计 counter 直接当作每秒速率；速率须由 exporter/JSON 采集器提供或在原始时间序列上单独求导。

JSON 命令采集器在 stdout 返回 `{标准键: 有限数值}`。原文模式只归档，不自动解析硬件厂商输出。

| 标准键 | 单位与含义 |
|---|---|
| live_kv_demand_bytes | 指定资源范围内应保留的 live KV 需求量，含仍有复用需求的外部状态 |
| kv_budget_bytes | 相同资源范围的设备 KV 可用预算，不含模型权重 |
| recompute_state_ms | 同模型、同前缀范围的重算测量 |
| fetch_state_ready_ms | 对应前缀从查找开始到可消费的完整加载测量 |
| state_stall_ratio | 加速器已有可执行工作但正在等待 KV 的时间比例，需调度/设备观测 |
| effective_external_kv_capacity_bytes | 逐对象去重且满足就绪 deadline 和经济性条件的有效外部容量 |
| cpu_cores | 服务 CPU 时间/墙钟时间，不是进程数 |
| ddr_bytes_per_s / nic_bytes_per_s / pcie_bytes_per_s / ssd_bytes_per_s | 实测每秒速率，必须注明设备、方向、范围和采样方法 |

完整状态等待、有效容量和重算/加载经济性无法从普通 HTTP 请求时序直接求得。若实验版本没有 exporter，请用配置化采集器对接已有 profiler，或补充源码级观测；缺失字段保持未观测。连接器入口钩子仅提供方法级干预与观测，不冒充这一层证据。

`calibration.jobs` 可调用平台现有 DDR/PCIe/NIC/SSD 等 benchmark，每个作业输出非空 `observations` 和 `conditions` 的 JSON；例如 block size、QD、方向、NUMA、并发数、持续时长、实测速率与原始数据路径。框架归档这些实际输出，不用规格峰值代替服务曲线。

## 跨机器时钟与数据关联

请求时延使用负载机 `perf_counter`；钩子提供进程内高精度耗时和 UTC 纳秒。跨节点拼接事件前应核验时钟同步与偏差。`X-Request-Id` 传给服务端，但框架是否将其传播到内部 request 对象取决于服务版本；无法对应时不得强行拼接。批量连接器事件可能属于整个调度 step，应按批次关联，不能均分到请求后当成实测。
