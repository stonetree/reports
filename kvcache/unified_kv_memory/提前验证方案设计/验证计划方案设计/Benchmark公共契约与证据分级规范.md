# Benchmark 公共契约与证据分级规范

## 1. 适用范围

本规范适用于 PVT-00～PVT-09、CVT-01 和 `原型验证代码/deploy_and_bench_e2e/` 公共测试驱动。它统一工作负载、被测对象、打点、统计、证据有效性和结果清单，不新增产品功能需求。

KVCache（大模型注意力键值缓存，即在自回归生成过程中保存历史 Key/Value 激活状态、避免后续 Token 重复计算注意力）在本文中简称 KVCache。

原型代码的当前目标是示范完整验证工作流。固定样例、Mock、localhost 和公式推演可以保留，但必须标为 DEMO，不能用来关闭 E0～E3 的正式结论。

## 2. 每项验证的必填输入

每次运行前冻结以下字段：

```text
run_id, workload_schema_version, workload_id, seed,
package_id, baseline_commit, config_hash, feature_state,
model_id, model_layout_manifest, tokenizer_hash,
hardware_profile, topology_profile, evidence_environment,
evidence_level, concurrency, request_rate, warmup_rounds,
measure_rounds, planned_path
```

其中：

- `package_id` 标识完整代码包。原生 Mooncake、单项增强和完整增强使用不同代码包或配置，并写入不同结果目录；
- `model_layout_manifest` 记录层数、KV 头数、每头维度、数据类型、张量并行切分、对齐和附加状态，精确定义各模型（如 DeepSeek MLA ~35KB/tok、Qwen MHA 320KB/tok）的单 Token KV 字节数与内存布局；
- `planned_path` 是调度计划，`actual_path` 是实际完成路径。无法证明实际路径时，性能证据无效；
- 设备型号、节点数、链路和 SSD 数量根据可用实验设备参数化，不在公共规范中写死。

## 3. 三种证据等级

| 等级 | 用途 | 允许实现 | 可形成的结论 |
|---|---|---|---|
| DEMO | 工作流和字段示范 | Mock、固定样例、公式推演、localhost | 仅证明命令、输入、输出、公式和失败关闭流程正确 |
| LAB | 限定实验环境实测 | 可用 NPU、网卡、SSD 和节点组合 | 形成绑定软硬件版本与拓扑的局部结论 |
| MEASURED | 代表性完整路径实测 | 跨节点真实数据路径、真实被测代码包和目标混压 | 可关闭对应 E0～E3 阶段结论 |

环境证据档位另行记录：W0 为单机/localhost/Mock，W1 为局部设备组合实测，W2 为代表性跨节点系统验证。这里的 W0/W1/W2 不是实施波次。

## 4. 被测模式与公平 A/B

同一次 A/B 必须保持设备、拓扑、模型、数据集、负载、并发、资源配额、编译参数、预热和统计口径一致。

每个模式必须实际绑定被测对象，不能只修改输出标签：

```text
recompute            -> 禁用远端 KV 复用的完整代码包或配置
mooncake_native      -> 固定 Commit 的原生 Mooncake 完整代码包或配置
unified_single       -> 只开启一项增强的完整代码包或配置
unified_full         -> 开启待验收增强集合的完整代码包或配置
```

PVT-07 使用两种不同的分母：

```text
TTFT 降幅 = (原生 Mooncake 混压 TTFT - 增强版混压 TTFT) / 原生 Mooncake 混压 TTFT
QPS 增益  = (增强版混压 QPS - 原生 Mooncake 混压 QPS) / 原生 Mooncake 混压 QPS
TPOT 干扰率 = (增强版混压 TPOT - 同一增强包纯前台 TPOT) / 同一增强包纯前台 TPOT
```

各混压模式必须采用相同目标后台负载，并同时记录实际后台带宽。若实际后台带宽偏差超过运行前冻结的容差，A/B 无效。

## 5. 统一事件字段

各模块至少输出：

```text
run_id, trace_id, request_id, event_name,
monotonic_timestamp_ns, duration_ns,
planned_path, actual_path, payload_bytes,
status, error_code, evidence_level,
package_id, config_hash, hardware_profile
```

未采集字段使用 `null` 并记录 `invalid_reason`，不能使用 `0` 代替。跨节点阶段耗时还需记录时钟同步方式和误差上限。

缓存预热完成必须以 `ready_event`、`visibility_epoch` 或等价可消费事件为准。固定休眠只允许用于 W0/DEMO。

## 6. 统计要求

- 运行前冻结预热、样本量、独立重复、异常值规则和后台负载容差；
- 默认至少 1 轮预热、3 轮独立重复；项目级门禁报告 P50/P95/P99 和置信区间或等价离散度；
- 保留失败请求，不得只统计成功样本；
- 分位数从原始样本计算，不能用平均值或最大值替代 P99；
- 门槛是验收条件，不是预置结果。原型固定值只用于展示汇总格式。

## 7. 失败关闭

以下任一情况将证据状态设为 `INVALID_EVIDENCE`，不得输出 PASS：

- 缺少正式探针、设备完成量或路径凭证；
- 输入 Schema 不一致、必填字段缺失或解析失败；
- 固定样例或模拟数据被标为 LAB/MEASURED；
- 对照模式没有实际切换被测代码包或配置；
- A/B 的设备、负载、资源配额或后台压力不等价；
- 原始样本、失败请求、版本清单或结果 manifest 缺失。

原生 Mooncake 对照失败时，可以标记 `BASELINE_INVALID` 或已知原生问题，不要求本项目修复，但不能构造替代成绩。

## 8. 结果清单

每个 `run_id` 使用独立目录：

```text
results/<validation_id>/<mode>/<run_id>/
├── manifest.json
├── environment.json
├── raw_events.jsonl
├── raw_metrics.*
├── summary.json
├── summary.csv
└── logs/
```

`manifest.json` 至少记录代码包、基线 Commit、配置哈希、输入 Schema、环境、证据等级、支持范围、执行命令、原始文件哈希和结论状态。结论状态统一为：

```text
GO | CONDITIONAL | NO-GO | NOT-SUPPORTED | INVALID-EVIDENCE
```

各分项方案未重复写出的公共字段、统计和失败规则，以本规范为准；若分项方案与本规范冲突，以本规范为准并在分项结果清单中记录偏差。
