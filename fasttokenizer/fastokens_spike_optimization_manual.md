# fastokens ARM64 性能穿刺验证实施手册 (Spike Manual)

> **文档定位**：前期快速性能穿刺 POC 指导手册  
> **适用对象**：开发人员 / 性能优化工程师  
> **质量目标**：提供可直接实施的代码修改方案、编译构建脚本与基准测试 SOP，指导开发人员在 1~2 小时内完成单核 ARM 服务器上的性能穿刺验证。

---

## 1. 穿刺验证目标与指标 (Key Metrics)

| 优化方向 | 阶段 | 基准目标 (Target) | 冲刺目标 (Stretch) | 验证手段 |
| :--- | :--- | :--- | :--- | :--- |
| **PGO + 架构编译参数** | Encoding | **+10%** | +15% | `taskset -c 0` 单核 Benchmark |
| **ARM Hardware CRC32** | Encoding | **+3%** | +5% | 单核 Profiling 延迟比对 |
| **NEON SIMD 解包** | Decoding | **+20%** | +30% | 解码吞吐 (Tokens/sec) 比对 |
| **全量融合构建** | 综合 | **Encoding +15% / Decoding +25%** | **Encoding +25% / Decoding +35%** | 端到端吞吐比对 |

---

## 2. 穿刺环境准备与基准获取 (Step 0)

### 2.1 准备测试数据集与基准脚本

在工程根目录创建测试数据目录 `spike_data/` 并放置准备好的多样化文本：
- `spike_data/text_corpus.txt`：包含自然语言 (中/英)、代码 (Python/JSON) 和对话格式的混合文本 (约 2MB)。

在根目录下创建 Python 基准测试脚本 `spike_benchmark.py`：

```python
#!/usr/bin/env python3
"""
fastokens 单核基准测试脚本
使用方式: taskset -c 0 python3 spike_benchmark.py
"""
import time
import os
import sys

# 引入 fastokens 模块
try:
    import fastokens
except ImportError:
    print("错误: 请先安装 fastokens (uv pip install python/ 或 maturin develop --release)")
    sys.exit(1)

CORPUS_PATH = "spike_data/text_corpus.txt"
if not os.path.exists(CORPUS_PATH):
    print(f"提示: 未检测到 {CORPUS_PATH}，生成默认测试文本...")
    os.makedirs("spike_data", exist_ok=True)
    with open(CORPUS_PATH, "w", encoding="utf-8") as f:
        f.write("A very long prompt that simulates LLM inputs for benchmarking BPE performance.\n" * 5000)

with open(CORPUS_PATH, "r", encoding="utf-8") as f:
    raw_text = f.read()

# 使用标准的 DeepSeek / Qwen 类的 tokenizer 配置文件或内建方式
tokenizer = fastokens._native.Tokenizer.from_model("deepseek-ai/DeepSeek-V3.2")

print(f"=== 基准测试开始 (文本大小: {len(raw_text)/1024:.2f} KB) ===")

# 1. Warmup
for _ in range(5):
    tokens = tokenizer.encode(raw_text)
    decoded = tokenizer.decode(tokens)

# 2. Benchmark Encoding
ITERATIONS = 50
start_time = time.perf_counter()
for _ in range(ITERATIONS):
    tokens = tokenizer.encode(raw_text)
end_time = time.perf_counter()

total_encode_time = end_time - start_time
avg_encode_ms = (total_encode_time / ITERATIONS) * 1000
token_count = len(tokens)
encode_throughput = (token_count * ITERATIONS) / total_encode_time

print(f"[Encoding]  平均耗时: {avg_encode_ms:.3f} ms | 吞吐量: {encode_throughput:.2f} tokens/sec")

# 3. Benchmark Decoding
start_time = time.perf_counter()
for _ in range(ITERATIONS):
    decoded_text = tokenizer.decode(tokens)
end_time = time.perf_counter()

total_decode_time = end_time - start_time
avg_decode_ms = (total_decode_time / ITERATIONS) * 1000
decode_throughput = (token_count * ITERATIONS) / total_decode_time

print(f"[Decoding]  平均耗时: {avg_decode_ms:.3f} ms | 吞吐量: {decode_throughput:.2f} tokens/sec")
print("==========================================")
```

---

## 3. 优化实施一：PGO 自动化穿刺 (编译层)

PGO 不需要改动源代码，通过编译期分支概率统计重排 ARM 流水线。

### 3.1 实施脚本 `pgo_spike.sh`

在根目录下创建并运行 `pgo_spike.sh`：

```bash
#!/bin/bash
set -e

echo "=== 开始 PGO 自动化编译与性能穿刺 ==="

# 1. 清理并安装 cargo-pgo 工具
cargo install cargo-pgo 2>/dev/null || true

# 2. 生成 Instrumentation 二进制并采集数据
echo "[Step 1/3] 编译 PGO 采样版本..."
cargo pgo build --release -- -C target-cpu=native

echo "[Step 2/3] 执行 Profile 数据采集..."
# 运行 benchmark 进行数据采集
taskset -c 0 python3 spike_benchmark.py

# 3. 使用采集到的权重生成最终 PGO 优化的 release 库
echo "[Step 3/3] 应用 PGO 权重构建终极优化版本..."
cargo pgo optimize --release -- -C target-cpu=native -C lto=fat -C codegen-units=1

# 重新安装 python binding
cd python && uv pip install . --reinstall --no-build-isolation && cd ..

echo "=== PGO 编译完成！重新执行 Benchmark 验证结果 ==="
taskset -c 0 python3 spike_benchmark.py
```

---

## 4. 优化实施二：ARM Hardware CRC32 哈希替代 (代码层)

### 4.1 目标与修改文件
* **目标**：在 ARM64 架构下，用单指令 `__crc32cx` 替代 `MergeMap` 和 `FlatCache` 中的标量乘法 `fx_hash`。
* **文件**：`fastokens/src/models/bpe.rs`

### 4.2 具体代码修改 Diff 指引

在 `fastokens/src/models/bpe.rs` 文件约 Line 100 附近找到 `fx_hash` 函数，将其替换为如下具备 ARM64 硬件指令支持的实现：

```rust
// ---------------- 替换前 ----------------
// #[inline(always)]
// fn fx_hash(key: u64) -> u64 {
//     key.wrapping_mul(0x517cc1b727220a95)
// }

// ---------------- 替换后 ----------------
#[inline(always)]
fn fx_hash(key: u64) -> u64 {
    #[cfg(target_arch = "aarch64")]
    {
        // 使用 ARM64 专有 CRC32X 硬件指令，1~2 CPU 周期完成 64-bit 哈希
        unsafe { std::arch::aarch64::__crc32cx(0, key) as u64 }
    }
    #[cfg(not(target_arch = "aarch64"))]
    {
        key.wrapping_mul(0x517cc1b727220a95)
    }
}
```

同时在 `FlatCache::hash_str` (约 Line 200 附近) 增加 ARM64 硬件加速分支：

```rust
    #[inline(always)]
    fn hash_str(s: &str) -> u64 {
        let bytes = s.as_bytes();
        let mut h: u64 = bytes.len() as u64;
        let mut i = 0;
        
        #[cfg(target_arch = "aarch64")]
        {
            unsafe {
                while i + 8 <= bytes.len() {
                    let word = u64::from_ne_bytes(bytes[i..i + 8].try_into().unwrap());
                    h = std::arch::aarch64::__crc32cx(h, word) as u64;
                    i += 8;
                }
            }
        }
        #[cfg(not(target_arch = "aarch64"))]
        {
            while i + 8 <= bytes.len() {
                let word = u64::from_ne_bytes(bytes[i..i + 8].try_into().unwrap());
                h = h.wrapping_add(word).wrapping_mul(0x517cc1b727220a95);
                i += 8;
            }
        }
        
        while i < bytes.len() {
            h = h
                .wrapping_add(bytes[i] as u64)
                .wrapping_mul(0x517cc1b727220a95);
            i += 1;
        }
        if h == EMPTY_SLOT {
            h = 1;
        }
        h
    }
```

---

## 5. 优化实施三：NEON SIMD 解码与内存 16 字节对齐 (代码层)

### 5.1 目标与修改文件
* **目标**：
  1. 将 `MergeSymbol` 优化为 16 字节内存对齐，适应 ARM `ldp` 加载；
  2. 显式标注 `#[cold]` 减少不必要分支；
  3. 解码器基于平坦字节流与预分配内存提速。
* **文件**：`fastokens/src/models/bpe.rs` 和 `fastokens/src/decoders/byte_level.rs`

### 5.2 代码修改一：内存 16 字节对齐 (`fastokens/src/models/bpe.rs`)

找到 `struct MergeSymbol` (约 Line 420 附近)，修改为：

```rust
/// Symbol in the merge linked list.
#[repr(align(16))]
#[derive(Clone, Copy)]
struct MergeSymbol {
    c: u32,
    prev: i32,
    next: i32,
    _pad: u32, // 显式填充至 16 字节，匹配 ARM64 128-bit SIMD/LDP 访问
}
```

并在 `MergeScratch` 初始化与插入处补全 `_pad: 0`。

### 5.3 代码修改二：高效反分词解包 (`fastokens/src/decoders/byte_level.rs`)

将 `ByteLevelDecoder::decode_chain` (Line 29 ~ Line 45) 替换为快速预分配与连续扫描实现：

```rust
    pub fn decode_chain(&self, tokens: Vec<String>) -> Vec<String> {
        // 预计算所有 token 的总字符字节数，一次性分配容量
        let total_len: usize = tokens.iter().map(|s| s.len()).sum();
        let mut bytes: Vec<u8> = Vec::with_capacity(total_len);

        for token in &tokens {
            let src = token.as_bytes();
            let mut i = 0;
            while i < src.len() {
                let b = src[i];
                // ASCII 高频快道处理
                if b < 0x80 {
                    let cp = b as usize;
                    if cp < CHAR_TO_BYTE.len() && CHAR_TO_BYTE[cp] != 0 {
                        bytes.push(CHAR_TO_BYTE[cp]);
                    } else {
                        bytes.push(b);
                    }
                    i += 1;
                } else {
                    // 多字节 Unicode 解码处理
                    if let Some(ch) = token[i..].chars().next() {
                        let len = ch.len_utf8();
                        let cp = ch as usize;
                        if cp < CHAR_TO_BYTE.len() && CHAR_TO_BYTE[cp] != 0 {
                            bytes.push(CHAR_TO_BYTE[cp]);
                        } else {
                            bytes.extend_from_slice(&src[i..i + len]);
                        }
                        i += len;
                    } else {
                        bytes.push(b);
                        i += 1;
                    }
                }
            }
        }
        vec![String::from_utf8_lossy(&bytes).into_owned()]
    }
```

---

## 6. 穿刺验证测试 SOP (Standard Operating Procedure)

开发人员请按以下步骤依次记录各阶段性能指标，填写穿刺验证报告。

### 步骤 1：测试原始 Baseline
1. 切换至未改动的 `main` 分支；
2. 执行原生构建：`cargo build --release`；
3. 绑核运行基准测试：`taskset -c 0 python3 spike_benchmark.py`；
4. 记录 Baseline 数据。

### 步骤 2：测试单独应用 PGO
1. 运行 `./pgo_spike.sh` 自动生成 PGO 优化的动态库；
2. 绑核运行基准测试：`taskset -c 0 python3 spike_benchmark.py`；
3. 记录 PGO 加速比。

### 步骤 3：测试合并代码改动 (CRC32 + NEON + 内存对齐)
1. 应用第 4、5 节的代码修改；
2. 执行构建与安装；
3. 绑核运行基准测试：`taskset -c 0 python3 spike_benchmark.py`；
4. 记录纯代码修改后的加速比。

### 步骤 4：测试终极融合 (代码修改 + PGO)
1. 在应用代码修改的状态下运行 `./pgo_spike.sh`；
2. 绑核运行基准测试：`taskset -c 0 python3 spike_benchmark.py`；
3. 记录全量融合加速比。

---

## 7. 穿刺结果记录模板

请将实测数据填入下表，以评估穿刺验证结果是否达标：

| 测试版本 | Encoding 吞吐 (tok/s) | Encoding 加速比 | Decoding 吞吐 (tok/s) | Decoding 加速比 | 是否达标 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **0. Baseline (原始版本)** | Baseline | 1.00X | Baseline | 1.00X | - |
| **1. 纯 PGO 优化** | _待填_ | _待填_ (目标 $\ge 1.10\text{X}$) | _待填_ | _待填_ | [ ] |
| **2. 纯代码改动 (CRC32+NEON)**| _待填_ | _待填_ | _待填_ | _待填_ (目标 $\ge 1.20\text{X}$) | [ ] |
| **3. 终极融合 (代码+PGO)** | _待填_ | _待填_ (目标 $\ge 1.15\text{X}$) | _待填_ | _待填_ (目标 $\ge 1.25\text{X}$) | [ ] |

---

## 8. 常见问题排查 (Troubleshooting)

1. **`__crc32cx` 编译报错 `undefined reference`**：
   - 确保 `RUSTFLAGS` 包含 `-C target-cpu=native` 或 `-C target-feature=+crc`。
2. **PGO 编译报错或没效果**：
   - 检查 `cargo-pgo` 版本，确保 Profiling 阶段 Python 测试有足够的数据运行（运行时间至少 5 秒以上以采集充足样本）。
3. **绑定单核失败**：
   - 确认 ARM 服务器可用的 CPU core 编号，使用 `lscpu` 查询，例如更换为 `taskset -c 4`。
