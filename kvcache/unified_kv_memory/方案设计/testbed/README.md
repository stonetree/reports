# HOST-KVC Value Surface 测试工程

本工程用于验证 HOST（主机侧 CPU、内存和 I/O 资源）围绕 KVCache（大模型注意力键值缓存，用于避免历史上下文重复计算）的系统贡献。它向已经部署的 vLLM Ascend + Mooncake 服务发送请求，执行受控注入，保存原始数据，并生成可追溯的 Value Surface（不同容量压力、复用经济性和负载下的有效产出变化图）。它不部署模型，也不替代推理服务。

主要入口是 [`config.toml`](config.toml)。部署后修改其中的路径、服务地址、模型名、硬件标识、负载范围和观测项即可运行。服务端内部注入需要在启动服务时加载工程附带的钩子；只有源码路径不能给一个已经运行的进程增加钩子。

## 1. 已实现的流程

```text
语料/规范化 trace → 校验与来源哈希 → 多轮构造、加盐与压力变形
    → B0 标定 → 生成实验矩阵 → 随机化对照顺序
    → 资源控制 apply/verify → 明确的 warmup
    → 有界开放负载回放 + 流式计时 + 观测采集
    → 停止到达 → 单独记录 drain → reset 恢复控制
    → 配对差值、曲面、拐点候选、报告 → 局部加密
```

所有实际运行产物默认写入项目 `tmp/kvcbench/runs/`。每次运行使用新目录和新缓存命名空间，不覆盖已有结果。

| 模块 | 责任 |
|---|---|
| `trace.py` | 三种本地任务集、JSONL trace、可复现多轮请求与加盐、来源哈希 |
| `runner.py` / `replay.py` | 计划、对照、开放负载、会话顺序、队列上限、超时、流式响应 |
| `hooks_runtime.py` | 服务端连接器的观测、查找延迟与安全未命中注入 |
| `control.py` / `resource.py` | 外部控制命令的执行、核验、恢复；Linux CPU 亲和性适配器 |
| `telemetry.py` | Prometheus 原文、指标映射、外部 JSON/原文采集器 |
| `calibrate.py` | B0 可持续负载范围标定、硬件服务曲线作业归档 |
| `analysis.py` | 有效吞吐量、配对差值、置信区间、曲面、拐点候选与报告 |
| `tests/` | 协议、过载、时间口径、数据来源、注入和归因回归测试 |

## 2. 安装与第一次运行

实验机需要 Python 3.11 或更新版本。使用能访问推理端点的负载机运行；负载机可以就是服务所在主机。建议独立客户端 CPU，以免负载生成器与被测 HOST 争用资源。

```bash
cd /srv/testbed
python3 -m pip install -e '.[trace]'
python3 -m kvcbench prepare --config config.toml
python3 -m kvcbench doctor --config config.toml --online
python3 -m kvcbench calibrate --config config.toml
```

离线实验机先在联网且相同 Python/CPU 架构的环境准备 wheel，再用 `pip install --no-index --find-links /srv/wheels -e '.[trace]'`。`aiohttp` 是回放所需依赖，`pyarrow` 用于 Parquet，`transformers` 仅在配置本地 tokenizer 时使用。框架不下载模型或 tokenizer。

先修改这些字段：

| 字段 | 填写内容 |
|---|---|
| `trace.root` | 实验机上的完整 `trace/` 根目录 |
| `sources.vllm_ascend` / `sources.mooncake` / `sources.vllm` | 实验机提供的源码根目录，读取版本与连接器接口 |
| `variants.B0/B1.base_url` | 完整服务根地址，不含 `/v1`；可以是 PD 代理的统一根地址 |
| `variants.*.model` | `/v1/models` 返回的 served model ID |
| `variants.*.accelerator_count` | 为该端点服务的全部加速器数，含 TP/DP/PD 中参与服务的设备 |
| `variants.*.hardware_id` | 对照实验共用的物理资源标识，不能用不同设备冒充相同资源 |
| `calibration.qps` / `duration_s` | 能覆盖低负载到超载、且每点有足够样本的范围 |
| `slo.*` | 本实验的时延目标，即 SLO（服务质量约束） |

`calibrate` 输出目录中的 `calibration.json` 记录最大通过点及其后的失败点。把该文件路径写入 `calibration.evidence_path`，然后：

```bash
python3 -m kvcbench plan --config config.toml
python3 -m kvcbench run --config config.toml
python3 -m kvcbench report --run-dir /srv/results/<run-id>
```

默认矩阵包含 **144 个 case**，测量、最大 drain 和冷却时间合计约 **3.68 小时**，另加 warmup 与控制命令时间。`plan` 会先给出规模。可先把 `sweep.context_tokens/load/gap_s` 各设一个值，确认整条流程，再扩大范围。

`reference_qps` 也可手填用于试运行，但此时只生成计划负载代理图，不能把归一化负载 L 写成实测坐标。标定分母是指定 B0 工作负载下的 SLO 可持续请求率，固定为全矩阵的参考锚点；它不是每个上下文长度重新求得的饱和吞吐量，也不是芯片理论峰值。

## 3. B0–B3 的真实含义

| 变体 | 实验含义 | 差值用途 |
|---|---|---|
| B0 | 设备本地缓存与重算，无外部缓存池 | 绝对基线 |
| B1 | 外部池与最简单固定数据路径 | B1−B0：外部缓存系统增量 |
| B2 | 相同外部容量，当前 HOST 控制策略 | B2−B1：策略增量 |
| B3 | 相同容量与服务配置，待验证的优化 | B3−B2：优化系统增量 |

工程默认只启用 B0/B1。B2/B3 是实际部署变体的接入位置；框架不会虚构一个并不存在的调度器。对照须保持模型、精度、并行方式、加速器资源、设备缓存预算、输出长度与服务目标一致。服务如果分时复用同一组设备，可填写各变体 `prepare_argv` 切换已部署配置；命令成功退出前必须等到服务就绪。直接填写同一端点却没有切换命令会被拒绝。

`state_argv` 可接入实验平台的运行态核验，输出标准 JSON（格式见 [适配契约](docs/ADAPTERS.md)）。源码路径只能证明提供了哪份源码，不能证明当前服务确实使用该构建；运行态核验用于补上这个边界。

缓存初态有两种模式：

- `namespace`：每次运行、case、会话都有可复现的隔离前缀。它隔离缓存命中，**不会移除旧条目的容量占用**。结果作为探索性对照。
- `reset`：必须配置 `cache_reset_argv` 和 `cache_verify_argv`，同时处理设备缓存和 Mooncake 外部池。命令与核验输出归档。只有初态、变体语义和测量有效性都核验完成的硬件运行才具备严格对照条件。

无论哪种模式，warmup 使用同一预定规则，原始 warmup 记录与测量数据分开。不要把有预热的测量阶段称为冷启动性能。

## 4. 本地 trace 的处理

本地实际数据源为 SWE-bench Verified **500** 个任务、DeepSWE **113** 个任务、SWE-marathon **20** 个任务。它们是任务语料，并不包含完整的线上请求到达时间、真实工具等待和模型回答。因此默认执行的是“基于真实任务内容构造的合成多轮实验负载”。程序只读取问题或指令，不读取答案作为 prompt，也不执行任务代码。

`turns` 控制会话轮数；每轮保留上一轮序列化 prompt，再追加标识为合成的内容。模型本次生成的回答不反馈到下一轮，以保证不同变体接收到相同请求。`gap_s` 是预定到达间隔；回放时会话内有 FIFO 锁，前轮未完成时后轮等待，其等待计入 TTFT。它不是实际 agent 工具执行时间，也不代表预取已经实现。

`context_tokens>0` 添加带标识的合成填充并保留原始任务。计划填充量、实际 tokenizer 计数和估计值分别记录。配置 tokenizer 时只读取本地模型目录；未配置时的字符估计仅供规划，不参与实测 token 指标。

加盐模式：

| `salt_mode` | 作用 |
|---|---|
| session | 会话内复用，不同合成会话隔离 |
| request | 每轮不同盐，主动破坏跨轮复用 |
| tenant | 同租户共享盐 |
| shared | 所有任务共用同一盐，是否共享仍取决于真实前缀内容 |
| none | 不追加任务层盐；case 级隔离仍按缓存策略执行 |

`prefix` 直接改变输入内容；`cache_salt` 通过请求字段传入，不改变输入文本。vLLM 的前缀缓存支持该字段，但它是否进入实验版本的 Mooncake 外部键必须另行核验，不能据此自动宣称多租户隔离。使用 token ID prompt 且需要 case 命名空间时，必须核验 `cache_salt`，或者采用 `reset` 初态策略。

规范化真实请求记录可通过 `trace.normalized_path` 接入，示例：

```json
{"request_id":"s1-t0","session_id":"s1","tenant_id":"tenant-a","arrival_s":0.0,"prompt":"original prompt","max_tokens":128,"turn":0,"provenance":"recorded_request"}
{"request_id":"s1-t1","session_id":"s1","tenant_id":"tenant-a","arrival_s":5.0,"token_ids":[12,34,56],"max_tokens":128,"turn":1,"provenance":"recorded_request"}
```

规范化输入保留已有 prompt/盐和来源，不再按任务语料模式自动改写多轮内容。时间轴按原始平均到达率与目标 qps 比例缩放；`normalized_window_s` 可明确原始观测窗口。有限 trace 不会偷偷循环补齐；需要检查 `offered_qps`、请求覆盖长度和窗口末端是否有空档。`time_scale`/负载变形后的时间线必须标为变形输入。

## 5. 控制点注入

客户端内置控制包含负载、上下文填充、预定会话间隔、前缀共享与盐。服务端内置控制包含连接器方法观测、入口前延迟、外部查找返回未命中。未命中只在 `get_num_new_matched_tokens` 返回 `(0, False)`，不会在已命中后损坏数据或模拟不安全的半写缓存。

要启用服务端控制：

1. `doctor` 在本地源码中通过 AST（只解析语法、不导入模型库）查找真实连接器类、方法与源码哈希；据此修改 `hooks.targets` 和 `controls.*.rules`。
2. 配置 `hooks.enabled=true`、共享控制文件与事件目录；填写 `variants.B1.server_argv` 为实验平台原本可以运行的启动命令。
3. 在服务节点使用 `python3 -m kvcbench server --config config.toml --variant B1` 启动。它通过 `sitecustomize` 延迟导入钩子包装目标实例方法，不修改 vLLM/Mooncake 源码。需要时重启原进程一次；框架不会向活进程强行附加补丁。
4. 将 `lookup_delay` 或 `lookup_miss` 加入 `sweep.control_profiles`。它们默认只作用于 B1/B2/B3，B0 留作不受该注入影响的对照。

跨节点时，各服务进程必须能看到**相同的控制文件内容和事件目录**，可使用共享文件系统；或通过 apply/verify/reset 命令完成分发与收集。仅设置负载机路径不会自动把文件传到远端。容器也必须挂载目录，且启动环境不能清除 `PYTHONPATH` 和 `KVCBENCH_*`。

事件记录实际注入时间、方法总耗时、进程/线程、case 和可提取的请求 ID。批量接口不一定有请求 ID；不能把方法耗时直接改名为完整状态就绪时延或加速器等待时间。小于毫秒的 sleep 是扰动实验，实际值以高精度计时结果为准。钩子自身也有观测开销，应使用相同的钩子基线比较。

DDR、PCIe、网络、SSD、NUMA、CPU 配额等资源限制使用 `apply_argv` / `verify_argv` / `reset_argv` 三联适配。工程附带 Linux 全线程 CPU 亲和性适配器；其他资源需要平台上真实有效的控制工具。TCP 的 `tc` 限速不自动等于 RDMA/设备直传限速。控制调用均为 argv 数组，不执行 shell 字符串。

异常退出会在 `finally` 中恢复控制；系统断电或 SIGKILL 无法执行清理，使用 `reset-controls` 和保留的状态文件恢复。CPU 适配器会核对线程的启动代际，避免向复用的 PID 错误恢复。

## 6. 采集与报告

默认保存推理端点的完整 Prometheus 文本。不同版本指标名称不同，在 `telemetry.metrics` 配置真实名称和 label；平台工具可以输出原文，也可以输出标准键的 JSON。映射契约与控制边界见 [ADAPTERS.md](docs/ADAPTERS.md)。

主坐标只在有对应测量时产生：

- `P_HBM = P95(live_kv_demand_bytes) / mean(kv_budget_bytes)`。需求量应包括仍需保留的 live 状态；不能拿被容量截断的设备驻留量来表达大于 1 的压力。
- `E_reuse = recompute_state_ms / fetch_state_ready_ms`。必须是同模型、相同前缀范围的可比测量，fetch 包含查询、排队、提交、搬运和可见性。
- `L = 实际 offered_qps / 实测参考 qps`。未提供兼容的硬件标定时为空。

报告同时输出计划上下文 × 计划负载的代理图；不会把它冒充实测坐标。缺失的 State Stall（已有可执行工作却在等缓存的加速器等待）、有效外部容量、DDR/NIC/PCIe 带宽等字段保持空值。

| 文件 | 用途 |
|---|---|
| `manifest.json` / `config.resolved.json` / `plan.json` | 运行身份、证据等级、源码快照、配置与矩阵 |
| `cases/*/trace/` | 语料来源、SHA-256、请求构造参数与规范化请求 |
| `cases/*/requests.jsonl` / `summary.json` | 原始请求与测量汇总 |
| `cases/*/telemetry.jsonl` / `prometheus-*.txt` | 原始观测，含时间戳；汇总排除 drain 样本 |
| `cases/*/hook_events.jsonl` / `control_*.json` | 注入证据与控制恢复记录 |
| `reports/report.md` / `report.html` | 可离线查看的中文报告 |
| `reports/cells.csv` / `attribution.csv` | 全部指标、证据缺口和配对差值 |
| `reports/surface*.svg` / `surface_facets/` | 实测坐标与明确标识的计划代理曲面 |
| `reports/knees.csv` / `refinement.json` | 拐点候选、置信区间与后续加密建议 |
| `reports/regions.csv` / `diagnostic_surfaces.svg` | 有证据支撑的区域候选与诊断图；未观测处为空 |

TTFT（首字响应延迟）从预定到达时刻开始，包含客户端排队；另列 dispatch delay。TPOT（平均每个输出 token 的生成耗时）只在有可信逐 token 时序时参与严格判定。默认请求 `logprobs=0` 取得每个流式 chunk 的 token 数；合批或缺失时严格 TPOT 不可观测，相关请求不会被自动判为全部 SLO 达标，另保留交付速率代理。所有时序是客户端观测值，不能替代设备算子执行时长。

SLO goodput（满足时延目标的有效请求吞吐量）只统计固定到达窗口内且窗口结束前完成的合格请求。错误、丢弃和超时留在到达集合中。停止到达后完成的 drain 吞吐量单列。短窗口、样本不足、不断增长的排队不能作为稳态容量证明。

差值要求同一 cell、重复编号、工作负载哈希与实验环境签名匹配。独立重复少于 3 次时不输出差值置信区间。拐点拟合只在同模型、变体、上下文、间隔和控制档位内进行，至少需要 6 个不同负载点；它提供候选而非自动物理归因。不同 HOST 资源的因果定位仍要用对应的资源注入和恢复实验。

```bash
python3 -m kvcbench refine --config config.toml --run-dir /srv/results/<coarse-run>
```

`refine` 读取候选附近的负载建议，在新目录中对配置所列切片补测；不覆盖旧结果，也不自动把新旧运行合成一张统计曲面。没有受支持候选时拒绝盲目加密。R0–R5 的全面自动归类需要额外机制证据；当前只对实测 `E_reuse<1` 标记复用经济性候选，其余留为 `unknown`。

## 7. 本地软件验证与验收边界

```bash
python3 -m unittest discover -s tests -v
python3 -m kvcbench mock --config config.toml
```

`mock` 使用本地任务语料和回环 HTTP 服务，实际跑回放、采集、汇总和报告，所有结果标为 `MOCK_SOFTWARE_ONLY`。它验证软件流程，不生成硬件带宽、HOST 收益或项目验收结论。交付时已完成三种语料的读取，以及真实本地 HTTP 的合批、截断流、503、过载丢弃和超时回归。

实验平台的第一轮验收应确认：B0/B1 的实际服务配置、完整缓存初态、服务端注入事件存在、服务器没有把响应整体缓冲、客户端没有成为瓶颈、实际到达率与计划一致，以及内部指标的来源与范围。仅完成请求回放时，报告具备端到端测量；只有补齐对应内部观测，才能画实测主坐标和解释物理拐点。

## 8. 官方接口依据

已核对 [Ascend KV Cache Pool 设计说明](https://docs.vllm.ai/projects/ascend/en/latest/developer_guide/Design_Documents/KV_Cache_Pool_Guide.html)、[Ascend Store 用户指南](https://docs.vllm.ai/projects/ascend/en/latest/user_guide/feature_guide/kv_cache_pool_ascend_store.html)、[当前连接器源码](https://github.com/vllm-project/vllm-ascend/blob/main/vllm_ascend/distributed/kv_transfer/kv_pool/ascend_store/ascend_store_connector.py) 与 [vLLM cache_salt 说明](https://docs.vllm.ai/en/stable/design/prefix_caching/)。

文档与源码存在历史命名及功能边界变化，例如 `MooncakeStoreConnector` 和 `AscendStoreConnector`，以及存储介质和失败回退支持差异。工程不把网页最新行为当成实验版本行为，实际以 `doctor` 输出和实验平台运行态核验为准。
