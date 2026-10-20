# 多租户分层加盐 Trace 数据集资产目录 (Traces)

本目录存储用于评估 KVCache 控制面性能放大效应的多租户测试日志。所有 Trace 均基于真实生产与开源评测基准（Mooncake FAST'25 与 SWE-bench 智能体工具调用轨迹）经过分层加盐克隆生成。

---

## 一、文件结构清单

| 相对路径 | 文件大小 / 记录数 | 租户属性 | 核心特征与测试用途 |
| :--- | :--- | :--- | :--- |
| `raw_benchmark_trace.json` | 2,000 条基准请求 | 原始基准 | 包含 10 个共享系统提示词前缀与长私有会话，作为加盐克隆母本 |
| `salted_20/trace_tenant_00.json` | 2,000 条请求 | 租户 0 (SUT 主力) | 直连 SUT1，执行主力业务推理评估与 TTFT/TPOT 采样 |
| `salted_20/trace_tenant_01.json` | 2,000 条请求 | 租户 1 (SUT 主力) | 直连 SUT2，模拟双节点跨机协同与跨卡状态查询 |
| `salted_20/trace_tenant_02.json` ~ `trace_tenant_19.json` | 各 2,000 条请求 (共 36,000 条) | 租户 2 ~ 19 (影子实例) | 分配给 18 个影子压测容器，制造 400 并发分布式元数据锁争用与微突发脉冲 |

---

## 二、Trace 字段规范说明

每条 Trace 记录采用标准 JSON 格式组织，主要字段如下：

```json
{
  "request_id": "req_000000",
  "session_id": "tenant_02_session_0000",
  "prefix_id": "SYS_PROMPT_HOTSPOT_000",
  "prefix_tokens": 16384,
  "generation_tokens": 128,
  "is_shared_hotspot": true,
  "pre_gap_sec": 3.42,
  "arrival_offset_ms": 0.0,
  "tenant_id": 2,
  "arrival_time_ms": 234.15
}
```

### 字段释义：
- `request_id` (string)：全局唯一请求编号；
- `session_id` (string)：多轮交互会话标识符（每 5 个请求构成一个多轮会话链，若为私有会话则附加租户前缀盐值）；
- `prefix_id` (string)：前缀键值标识符。若 `is_shared_hotspot` 为 `true`，则属于公共热点前缀（跨租户完全共享）；若为 `false`，则加盐隔离；
- `prefix_tokens` (int)：提示词 Prompt 前缀 Token 数量，取值范围 `[4096, 8192, 16384, 32768]`；
- `generation_tokens` (int)：模型生成输出 Token 数量，取值范围 `[16, 256]`；
- `is_shared_hotspot` (bool)：是否为跨租户共享热点（出现概率约 40%）；
- `pre_gap_sec` (float)：智能体工具调用停顿时间，取值范围 `2.0s ~ 5.5s`（精准模拟 SWE-bench 真实思考停顿）；
- `arrival_time_ms` (float)：绝对到达时间戳偏移（毫秒），注入了租户级随机到达抖动（0~1000ms）。

---

## 三、重新生成与扩展命令

若需调整租户数量或增加请求样本，可通过 `scripts/generate_salted_traces.py` 进行重构：

```bash
# 重新生成 20 个租户的加盐日志 (存入 ./traces/salted_20)
python scripts/generate_salted_traces.py --output-dir ./traces/salted_20 --tenants 20

# 指定外部开源基准 JSON 进行加盐克隆
python scripts/generate_salted_traces.py --input /path/to/real_swebench.json --output-dir ./traces/salted_20 --tenants 20
```
