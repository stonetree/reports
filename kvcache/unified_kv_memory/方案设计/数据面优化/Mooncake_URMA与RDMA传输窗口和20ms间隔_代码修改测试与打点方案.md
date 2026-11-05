# Mooncake URMA（用户态远程内存访问接口）与 RDMA（远程直接内存访问）传输窗口和约 20 ms 间隔：代码修改、测试验证与打点方案

**版本：** V1.1  
**日期：** 2026-10-24  
**代码基线：** D:/codes/vllm/Mooncake  
**用途：** 指导后续代码交付；本文不是代码已修改或测试已通过的声明。

## 0. 执行总览

### 0.1 优化方案与代码修改点

| 编号 | 优化方案 | 代码修改点 | 交付结果 |
|---|---|---|---|
| O1 | 增加每 endpoint Jetty 数配置，并按设备能力限制 Jetty、endpoint 与 JFS 深度 | `mooncake-transfer-engine/src/config.cpp`；`include/transport/kunpeng_transport/urma/urma_endpoint.h` 的 `updateUrmaGlobalConfig()`；`src/transport/kunpeng_transport/urma/urma_endpoint.cpp` 的设备初始化、JFS 创建和 Jetty 数不匹配提示；`tests/config_test.cpp` | `MC_NUM_JETTY_PER_EP` 可配置，启动日志显示生效值，硬件上限受控 |
| O2 | URMA submit 从随机使用一个 Jetty 改为轮转分配到可用 Jetty；共享 JFC 使用原子额度预留 | 新增 `include/transport/kunpeng_transport/urma/jetty_post_plan.h`；修改 `include/transport/kunpeng_transport/urma/urma_endpoint.h`、`src/transport/kunpeng_transport/urma/urma_endpoint.cpp`；新增 `tests/urma_post_plan_test.cpp` 并登记 CMake 目标 | 多 Jetty 的 WR window 可被实际利用，JFC outstanding 不超过容量 |
| O3 | 增加默认关闭的单调时钟观测 helper，按 endpoint/Jetty/QP 汇总提交 WR 与 payload bytes | 新增 `include/transport/transport_timing_trace.h` | 输出限频的 worker、post、poll、completion 与 credit release 时间记录 |
| O4 | 观测 UB worker 活跃间隙、空 JFC poll、URMA post 与 JFC credit 归还 | 修改 `src/transport/kunpeng_transport/ub_context.cpp`、`src/transport/kunpeng_transport/urma/urma_endpoint.cpp` | 区分线程调度、completion 可见性、JFC 额度回收和 post 路径延迟 |
| O5 | 观测 RDMA worker 活跃间隙、QP post、CQ poll 与 CQ credit 归还 | 修改 `src/transport/rdma_transport/rdma_endpoint.cpp`、`src/transport/rdma_transport/worker_pool.cpp` | 与 URMA 使用同一时间口径对照；保留 RDMA 按 QP 提交的既有行为 |
| O6 | 对 WR 深度、Jetty 数、slice 大小及应用 block/batch 做分层扫描 | 使用第 4 节 tebench 与现有 UB benchmark 启动方式；无需新增业务代码 | 分开验证窗口容量、传输粒度和 20 ms 间隔来源 |

### 0.2 测试方案与测试意图

| 编号 | 测试方案 | 测试意图 | 详细步骤 |
|---|---|---|---|
| T1 | 配置解析与硬件能力边界单测 | 验证 Jetty 环境变量合法性、生效默认值及 JFS/JFC 上限处理 | 第 4.1、7.1 步骤 1 |
| T2 | Jetty planner 与共享 JFC 并发单测 | 验证轮转次序、满队列跳过、pending/JFC 限额和并发预留不超额 | 第 3.1 节及第 7.1 步骤 1 |
| T3 | 编译、CTest 与 URMA 功能测试 | 验证改动可构建，读写完成、失败重试、握手和 outstanding 回收正确 | 第 4.1、4.2 节及第 7.1 步骤 2 |
| T4 | `MC_MAX_WR` 与 Jetty 数参数扫描 | 判断观测到的 burst bytes 是否随 WR window 和实际 slice 长度变化，并检查 JFC 限额 | 第 4.3 节及第 7.1 步骤 3 |
| T5 | slice 与 block/batch 固定字节量对照 | 分离底层 slice 粒度、应用请求粒度与 benchmark 批量的影响 | 第 4.3、4.4 节及第 7.1 步骤 4 |
| T6 | 关闭/开启打点的同负载对照与端口时间线关联 | 量出打点开销，并定位约 20 ms 停顿位于 worker、poll、completion、credit 或队列路径 | 第 5 节及第 7.1 步骤 5 |

## 1. 目标与证据边界

本方案拆分处理三个问题：URMA 与 RDMA 每次约 17.3 MB/35.5 MB 的传输量是否由窗口大小造成；UB（统一总线）400 Gbps 链路小切片吞吐偏低是否由在途工作请求、完成处理或软件提交成本造成；以及两种传输看起来都有约 20 ms 间隔时，停顿发生在 worker 调度、完成记录可见、credit（可提交工作请求额度）回收还是 Mooncake 队列控制逻辑。

本文中的 KVCache 指大模型注意力键值缓存；KVC 描述符是拆分后的传输请求描述。WR（Work Request，设备工作请求）是向传输队列提交的一项读写操作；QP（Queue Pair，RDMA 收发队列对）、Jetty（URMA 收发队列对象）、JFC（Jetty Completion Queue，Jetty 完成队列）和 CQ（Completion Queue，完成队列）分别指两类传输队列及其完成队列。本文中的 burst 指端口计数器观察到的一段集中传输期，不预设为 Mooncake 单次提交的完整请求集合。

以下分类必须保留，不能把推断当成实测结论：

| 类别 | 依据 | 当前结论 |
|---|---|---|
| 用户确认 | 全部 KVC 描述符一次性提交至 UB WorkerPool；“1024 个”只是近似数 | 不把 17 MB 归因于应用每 20 ms 仅能产生这么多描述符；仍需记录队列内实际 slice 数 |
| 用户实测 | UB burst 约 17.285 MB，RDMA burst 约 35.463 MB，间隔约 20 ms；UB 使用过约 3 ms 采样 | 外层端口字节口径可能不同；3 ms 采样无法精确观察 0.34 ms 级的 UB burst 发送时间 |
| 源码事实 | 默认 slice_size=65536 B、max_wr=256、RDMA 每 endpoint 2 个 QP、UB 每 endpoint 1 个 Jetty | 单 Jetty 软件上限为 256 WR；RDMA 软件上限为 2×256 WR，实际还受 JFC/CQ 余量及排队 slice 数限制 |
| 源码事实 | URMA submit 随机选择一个 Jetty；RDMA submit 遍历 QP | 单纯把 Jetty 配成 2 个不足以证明两条 Jetty 都被使用 |
| 源码事实 | UB 活跃 worker 执行 performPostSend 后执行 performPoll；只有 submitted==processed 且空闲超过 100 ms 时才可能进入 1 s 条件变量等待 | 当前该 worker 活跃路径没有显式 20 ms 定时器，但尚不能解释实际 20 ms 停顿 |
| 待验证推断 | 17.3 MB 与 35.5 MB 接近 1:2，可能对应 1×256×64 KiB 与 2×256×64 KiB | 需采集 posted slice 数、每 slice 实际长度、Jetty/QP depth 和 completion 时间才能确认因果关系 |
| 开放问题 | 20 ms 的成因尚未定位 | 以单调时钟记录 worker、post、provider poll、completion 和下一次 post |

默认窗口换算：UB 256×65536 B=16 MiB=16.777216 MB；RDMA 2×256×65536 B=32 MiB=33.554432 MB。此吻合度支持窗口假设，但不能证明窗口就是 PMU 观察到的 burst。UB/RDMA 端口计数器可能统计不同协议层字节，不应将约 3%/5.7%差异直接解释为固定协议头。实际窗口按各 slice 的真实长度累加，不能假设每个 slice 都等于 MC_SLICE_SIZE。

交付分两步：先交付诊断打点和可控的多 Jetty 提交；之后扫描 max WR、Jetty 数、slice、应用 block/batch。未经数据定位前，不同时改 worker 并发、完成策略、切片大小和业务请求大小。

## 2. 源码核对结论与改动文件

当前 checkout 中已核对到：

| 文件 | 现状/计划 |
|---|---|
| mooncake-transfer-engine/include/config.h | 已有 num_jetty_per_ep=1；增加 URMA JFS 深度常量 |
| mooncake-transfer-engine/src/config.cpp | 已解析 MC_MAX_WR；没有 MC_NUM_JETTY_PER_EP 解析 |
| mooncake-transfer-engine/include/transport/kunpeng_transport/urma/urma_endpoint.h | 增加轮转索引、可选诊断阻塞时间 |
| mooncake-transfer-engine/include/transport/kunpeng_transport/urma/jetty_post_plan.h | 新增纯分配规划与 JFC 有界原子预留 helper |
| mooncake-transfer-engine/src/transport/kunpeng_transport/urma/urma_endpoint.cpp | 遍历 Jetty、遵守每 Jetty max_wr 和共享 JFC 上限、计时 provider post |
| mooncake-transfer-engine/src/transport/kunpeng_transport/ub_context.cpp | 记录 worker gap、JFC 空 poll 跨度；现有 thread_local timing 结构未被使用，应替换或移除 |
| mooncake-transfer-engine/include/transport/transport_timing_trace.h | 新增默认关闭、steady_clock、稀疏输出的观测 helper |
| mooncake-transfer-engine/src/transport/rdma_transport/rdma_endpoint.cpp | 在 ibv_post_send 前后记录 RDMA QP 提交与窗口状态 |
| mooncake-transfer-engine/src/transport/rdma_transport/worker_pool.cpp | 在 CQ poll 前后记录空 poll 跨度和完成数 |
| mooncake-transfer-engine/tests/config_test.cpp | 测试新增 Jetty 环境变量 |
| mooncake-transfer-engine/tests/urma_post_plan_test.cpp | 测试 Jetty 分配和 JFC 并发预留 |
| mooncake-transfer-engine/tests/CMakeLists.txt | 注册新单测；现有 ub_transport_test 当前未注册到 CTest |

时间源约束：当前 common.h 的 getCurrentTimeInNano() 使用 CLOCK_REALTIME，会受系统校时影响。新增耗时测量全部使用 std::chrono::steady_clock；不要混算 wall-clock 与 steady-clock 差值。

## 3. 代码修改

### 3.1 Jetty 分配与共享 JFC 原子预留

新增 mooncake-transfer-engine/include/transport/kunpeng_transport/urma/jetty_post_plan.h：

~~~cpp
#pragma once
#include <algorithm>
#include <cstddef>
#include <vector>

namespace mooncake {
struct UrmaJettyPostPlan {
    size_t jetty_index;
    int wr_count;
};

inline std::vector<UrmaJettyPostPlan> makeUrmaJettyPostPlan(
    const std::vector<int>& jetty_depths, int max_wr_depth,
    int jfc_available, size_t pending_slices, size_t first_jetty) {
    std::vector<UrmaJettyPostPlan> plan;
    if (jetty_depths.empty() || max_wr_depth <= 0 || jfc_available <= 0 ||
        pending_slices == 0) return plan;

    size_t remaining_slices = pending_slices;
    int remaining_jfc = jfc_available;
    const size_t start = first_jetty % jetty_depths.size();
    for (size_t offset = 0;
         offset < jetty_depths.size() && remaining_slices > 0 &&
         remaining_jfc > 0;
         ++offset) {
        const size_t index = (start + offset) % jetty_depths.size();
        const int jetty_available = max_wr_depth - jetty_depths[index];
        if (jetty_available <= 0) continue;
        const size_t count = std::min(
            {static_cast<size_t>(jetty_available),
             static_cast<size_t>(remaining_jfc), remaining_slices});
        if (count == 0) continue;
        plan.push_back({index, static_cast<int>(count)});
        remaining_slices -= count;
        remaining_jfc -= static_cast<int>(count);
    }
    return plan;
}

inline int reserveUrmaJfcCredits(volatile int* outstanding, int limit,
                                 int requested) {
    if (!outstanding || limit <= 0 || requested <= 0) return 0;
    int current = __sync_fetch_and_add(outstanding, 0);
    while (current < limit) {
        const int grant = std::min(requested, limit - current);
        if (__sync_bool_compare_and_swap(outstanding, current,
                                         current + grant)) return grant;
        current = __sync_fetch_and_add(outstanding, 0);
    }
    return 0;
}
}  // namespace mooncake
~~~

规划 helper 负责跳过已满 Jetty 并计算共享 JFC 上限；实际 post 前仍须调用原子预留函数。JFC（Jetty 完成队列）可能被多个 endpoint 共用，不能沿用当前“先读取 outstanding，稍后普通加法”的非原子检查，否则并发 endpoint 可能超额占用同一容量。失败 WR 必须释放对应预留，成功 WR 则由 poll completion 归还。

新增 mooncake-transfer-engine/tests/urma_post_plan_test.cpp，覆盖以下精确断言：

- 空 Jetty、max_wr<=0、JFC<=0、pending=0 均返回空 plan；
- 一个空 Jetty、max_wr=256、pending=1024 时返回 1 个批次，数量为 256；
- 两个空 Jetty、max_wr=256、JFC=4096、pending=1024 时返回两个各 256 的批次；
- 四个空 Jetty、start=2 时分配顺序为 2、3、0、1；
- 四 Jetty、JFC=600 时批次数量为 256、256、88，总量不得超过 600；
- depth 为 {256,128,0}、max_wr=256 时跳过 Jetty 0，随后分给 Jetty 1 的 128 和 Jetty 2 的 256；
- pending=300 时总 plan 正好 300；
- 8 个线程并发调用 reserveUrmaJfcCredits(...,4096,1)，累计授予数和 outstanding 最终均恰好为 4096。

在 tests/CMakeLists.txt 的 if(USE_UB) 区块增加：

~~~cmake
  add_executable(urma_post_plan_test
                 ${WORKSPACE}/urma_post_plan_test.cpp)
  target_link_libraries(urma_post_plan_test PUBLIC transfer_engine gtest
                                                    gtest_main pthread)
  add_test(NAME urma_post_plan_test COMMAND urma_post_plan_test)
~~~

### 3.2 MC_NUM_JETTY_PER_EP 配置与硬件边界

config.h 已有 num_jetty_per_ep，默认值为 1，无需重复定义。在 config.cpp 的 MC_NUM_QP_PER_EP 解析块之后加入以下完整环境变量解析。若文件当前没有 `<cstdlib>`，在 include 区增加 `#include <cstdlib>`：

~~~cpp
    const char* num_jetty_per_ep_env = std::getenv("MC_NUM_JETTY_PER_EP");
    if (num_jetty_per_ep_env) {
        char* end = nullptr;
        const long val = std::strtol(num_jetty_per_ep_env, &end, 10);
        if (end != num_jetty_per_ep_env && *end == '\0' && val > 0 &&
            val <= UINT16_MAX) {
            config.num_jetty_per_ep = static_cast<size_t>(val);
        } else {
            LOG(WARNING)
                << "Ignore value from environment variable "
                   "MC_NUM_JETTY_PER_EP";
        }
    }
~~~

在 `dumpGlobalConfig()` 增加：

~~~cpp
    LOG(INFO) << "num_jetty_per_ep = " << config.num_jetty_per_ep;
~~~

在 `UrmaContext` 类的 private 区增加：

~~~cpp
    static constexpr size_t kUrmaJfsDepth = 2048;
~~~

将 `updateUrmaGlobalConfig()` 完整替换为以下函数；它拒绝零能力设备、将每 endpoint Jetty 数量和 endpoint 总数限制在设备能力内，并限制 WR 深度不超过当前源码中固定的 JFS 深度：

~~~cpp
    int updateUrmaGlobalConfig(urma_device_attr_t& device_attr) {
        auto& config = globalConfig();
        const size_t device_max_jetties = device_attr.dev_cap.max_jetty;
        const size_t device_max_jfcs = device_attr.dev_cap.max_jfc;
        if (device_max_jetties == 0 || device_max_jfcs == 0) {
            LOG(ERROR) << "URMA device reports invalid queue capability: max_jetty="
                       << device_max_jetties << ", max_jfc=" << device_max_jfcs;
            return ERR_CONTEXT;
        }
        if (config.num_jetty_per_ep == 0) {
            LOG(ERROR) << "MC_NUM_JETTY_PER_EP must be positive";
            return ERR_INVALID_ARGUMENT;
        }
        if (config.num_jetty_per_ep > device_max_jetties) {
            LOG(WARNING) << "MC_NUM_JETTY_PER_EP="
                         << config.num_jetty_per_ep
                         << " exceeds device max_jetty="
                         << device_max_jetties << "; clamp to device limit";
            config.num_jetty_per_ep = device_max_jetties;
        }
        if (config.max_wr > kUrmaJfsDepth) {
            LOG(WARNING) << "MC_MAX_WR=" << config.max_wr
                         << " exceeds URMA JFS depth=" << kUrmaJfsDepth
                         << "; clamp to JFS depth";
            config.max_wr = kUrmaJfsDepth;
        }
        const size_t max_endpoints =
            device_max_jetties / config.num_jetty_per_ep;
        if (static_cast<size_t>(config.max_ep_per_ctx) > max_endpoints)
            config.max_ep_per_ctx = static_cast<int>(max_endpoints);
        if (config.num_jfc_per_ctx > device_max_jfcs)
            config.num_jfc_per_ctx = device_max_jfcs;
        if (config.num_jfc_per_ctx == 0) {
            LOG(ERROR) << "URMA context requires at least one JFC";
            return ERR_CONTEXT;
        }
        return 0;
    }
~~~

在 `UrmaContext::openDevice()` 中，把原来的 `updateUrmaGlobalConfig(dev_attr_);` 调用替换为以下清理和返回代码：

~~~cpp
        const int config_rc = updateUrmaGlobalConfig(dev_attr_);
        if (config_rc != 0) {
            if (urma_delete_context(context)) {
                PLOG(ERROR) << "urma_delete_context(" << device_name
                            << ") failed after invalid device capability";
            }
            urma_free_device_list(devices);
            return config_rc;
        }
~~~

URMA endpoint 的 `jfs_cfg.depth` 字段替换为 `UrmaContext::kUrmaJfsDepth`。该常量是本 checkout 中已核实的 2048，不能将软件 `MC_MAX_WR` 设置到更大后仍按 2048 以外的深度分配。

在 `UrmaEndpoint::doSetupConnection()` 的 Jetty 数量不匹配分支中，保留现有拒绝连接逻辑，将错误提示中的 `MC_MAX_EP_PER_CTX` 改为正确配置项：

~~~cpp
        std::string message =
            "jetty count mismatch in peer and local endpoints, check "
            "MC_NUM_JETTY_PER_EP";
~~~

在 `config_test.cpp` 的 `PkeyIndexEnvTest::TearDown()` 增加：

~~~cpp
        ::unsetenv("MC_NUM_JETTY_PER_EP");
~~~

并新增以下完整测试：

~~~cpp
TEST_F(PkeyIndexEnvTest, JettyCountDefaultsToOneWhenUnset) {
    ::unsetenv("MC_NUM_JETTY_PER_EP");
    GlobalConfig config;
    loadGlobalConfig(config);
    EXPECT_EQ(config.num_jetty_per_ep, 1u);
}

TEST_F(PkeyIndexEnvTest, JettyCountAcceptsValidOverride) {
    ASSERT_EQ(::setenv("MC_NUM_JETTY_PER_EP", "4", 1), 0);
    GlobalConfig config;
    loadGlobalConfig(config);
    EXPECT_EQ(config.num_jetty_per_ep, 4u);
}

TEST_F(PkeyIndexEnvTest, JettyCountRejectsInvalidOverrides) {
    for (const char* value : {"0", "-1", "65536", "abc", "2x"}) {
        ASSERT_EQ(::setenv("MC_NUM_JETTY_PER_EP", value, 1), 0);
        GlobalConfig config;
        config.num_jetty_per_ep = 7;
        loadGlobalConfig(config);
        EXPECT_EQ(config.num_jetty_per_ep, 7u) << "value=" << value;
    }
}
~~~

在 `tests/CMakeLists.txt` 的现有 `if(USE_UB)` 区块内增加以下完整目标定义；该测试只执行纯计算与原子预留 helper，不创建 URMA 设备：

~~~cmake
  add_executable(urma_post_plan_test
                 ${WORKSPACE}/urma_post_plan_test.cpp)
  target_link_libraries(urma_post_plan_test PUBLIC transfer_engine gtest
                                                    gtest_main pthread)
  add_test(NAME urma_post_plan_test COMMAND urma_post_plan_test)
~~~

新增 `tests/urma_post_plan_test.cpp` 完整内容：

~~~cpp
#include <gtest/gtest.h>

#include <array>
#include <cstddef>
#include <thread>
#include <vector>

#include "transport/kunpeng_transport/urma/jetty_post_plan.h"

namespace mooncake {
namespace {

TEST(UrmaJettyPostPlanTest, RejectsEmptyOrInvalidInputs) {
    EXPECT_TRUE(makeUrmaJettyPostPlan({}, 256, 4096, 10, 0).empty());
    EXPECT_TRUE(makeUrmaJettyPostPlan({0}, 0, 4096, 10, 0).empty());
    EXPECT_TRUE(makeUrmaJettyPostPlan({0}, 256, 0, 10, 0).empty());
    EXPECT_TRUE(makeUrmaJettyPostPlan({0}, 256, 4096, 0, 0).empty());
}

TEST(UrmaJettyPostPlanTest, SingleJettyIsCappedAtItsDepth) {
    const auto plan = makeUrmaJettyPostPlan({0}, 256, 4096, 1024, 0);
    ASSERT_EQ(plan.size(), 1u);
    EXPECT_EQ(plan[0].jetty_index, 0u);
    EXPECT_EQ(plan[0].wr_count, 256);
}

TEST(UrmaJettyPostPlanTest, TwoJettiesProvideTwoDepthWindows) {
    const auto plan = makeUrmaJettyPostPlan({0, 0}, 256, 4096, 1024, 0);
    ASSERT_EQ(plan.size(), 2u);
    EXPECT_EQ(plan[0].wr_count, 256);
    EXPECT_EQ(plan[1].wr_count, 256);
}

TEST(UrmaJettyPostPlanTest, StartsFromRoundRobinIndex) {
    const auto plan = makeUrmaJettyPostPlan({0, 0, 0, 0}, 256, 4096,
                                            1024, 2);
    ASSERT_EQ(plan.size(), 4u);
    EXPECT_EQ(plan[0].jetty_index, 2u);
    EXPECT_EQ(plan[1].jetty_index, 3u);
    EXPECT_EQ(plan[2].jetty_index, 0u);
    EXPECT_EQ(plan[3].jetty_index, 1u);
}

TEST(UrmaJettyPostPlanTest, SharedJfcLimitCapsAggregatePlan) {
    const auto plan = makeUrmaJettyPostPlan({0, 0, 0, 0}, 256, 600,
                                            1024, 0);
    ASSERT_EQ(plan.size(), 3u);
    EXPECT_EQ(plan[0].wr_count, 256);
    EXPECT_EQ(plan[1].wr_count, 256);
    EXPECT_EQ(plan[2].wr_count, 88);
}

TEST(UrmaJettyPostPlanTest, SkipsFullJettyAndUsesRemainingCapacity) {
    const auto plan = makeUrmaJettyPostPlan({256, 128, 0}, 256, 4096,
                                            1024, 0);
    ASSERT_EQ(plan.size(), 2u);
    EXPECT_EQ(plan[0].jetty_index, 1u);
    EXPECT_EQ(plan[0].wr_count, 128);
    EXPECT_EQ(plan[1].jetty_index, 2u);
    EXPECT_EQ(plan[1].wr_count, 256);
}

TEST(UrmaJettyPostPlanTest, NeverPlansMoreThanPendingSlices) {
    const auto plan = makeUrmaJettyPostPlan({0, 0, 0}, 256, 4096, 300, 0);
    int total = 0;
    for (const auto& batch : plan) total += batch.wr_count;
    EXPECT_EQ(total, 300);
}

TEST(UrmaJettyPostPlanTest, SharedJfcReservationIsBoundedAcrossThreads) {
    volatile int outstanding = 0;
    std::array<int, 8> grants{};
    std::vector<std::thread> workers;
    for (std::size_t worker = 0; worker < grants.size(); ++worker) {
        workers.emplace_back([&, worker] {
            for (int request = 0; request < 512; ++request) {
                grants[worker] += reserveUrmaJfcCredits(
                    &outstanding, 4096, 1);
            }
        });
    }
    for (auto& worker : workers) worker.join();

    int total_granted = 0;
    for (int grant : grants) total_granted += grant;
    EXPECT_EQ(total_granted, 4096);
    EXPECT_EQ(__sync_fetch_and_add(&outstanding, 0), 4096);
}

}  // namespace
}  // namespace mooncake
~~~

### 3.3 URMA endpoint 成员与完整 submitPostSend 替换

在 UrmaEndpoint 私有成员增加：

~~~cpp
    size_t next_jetty_index_ = 0;
    uint64_t diag_no_credit_since_ns_ = 0;
    int diag_no_credit_reason_ = 0;  // 0=none, 1=jetty depth, 2=JFC
~~~

这些字段在 endpoint 写锁内访问；诊断状态仅在 MC_TRANSPORT_TIMING=1 时更新。加入头文件：

~~~cpp
#include "transport/kunpeng_transport/urma/jetty_post_plan.h"
#include "transport/transport_timing_trace.h"
~~~

用以下函数完整替换 urma_endpoint.cpp 的 UrmaEndpoint::submitPostSend()：

~~~cpp
int UrmaEndpoint::submitPostSend(
    std::vector<Transport::Slice*>& slice_list,
    std::vector<Transport::Slice*>& failed_slice_list) {
    RWSpinlock::WriteGuard guard(lock_);
    if (!active_) return 0;
    if (slice_list.empty()) return 0;
    if (jetty_list_.empty()) {
        LOG(ERROR) << "Cannot post send: endpoint has no URMA Jetty";
        return ERR_ENDPOINT;
    }

    const bool timing_enabled = TransportTimingTrace::enabled();
    const size_t requested_slices = slice_list.size();
    const size_t jetty_count = jetty_list_.size();
    const size_t start_jetty = next_jetty_index_ % jetty_count;
    std::vector<int> jetty_depths(jetty_count);
    for (size_t i = 0; i < jetty_count; ++i)
        jetty_depths[i] = __sync_fetch_and_add(&wr_depth_list_[i], 0);

    const int jfc_before_plan = __sync_fetch_and_add(jfc_outstanding_, 0);
    const int jfc_available =
        std::max(0, static_cast<int>(globalConfig().max_jfc_e) -
                        jfc_before_plan);
    const auto plan = makeUrmaJettyPostPlan(
        jetty_depths, max_wr_depth_, jfc_available, requested_slices,
        start_jetty);

    if (plan.empty()) {
        if (timing_enabled && diag_no_credit_since_ns_ == 0) {
            diag_no_credit_since_ns_ = TransportTimingTrace::nowNs();
            diag_no_credit_reason_ = jfc_available == 0 ? 2 : 1;
        }
        next_jetty_index_ = (start_jetty + 1) % jetty_count;
        return 0;
    }

    std::vector<urma_jfs_wr_t> wr_list(
        static_cast<size_t>(max_wr_depth_));
    std::vector<urma_sge_t> l_sge_list(
        static_cast<size_t>(max_wr_depth_));
    std::vector<urma_sge_t> r_sge_list(
        static_cast<size_t>(max_wr_depth_));

    size_t slice_offset = 0;
    size_t total_posted = 0;
    bool stop_after_batch = false;
    size_t last_jetty_index = start_jetty;
    const uint64_t blocked_since_ns = diag_no_credit_since_ns_;
    const int blocked_reason = diag_no_credit_reason_;

    for (const auto& batch : plan) {
        if (slice_offset >= slice_list.size()) break;
        const size_t jetty_index = batch.jetty_index;
        last_jetty_index = jetty_index;
        const int requested_wr = std::min(
            batch.wr_count,
            static_cast<int>(slice_list.size() - slice_offset));
        if (requested_wr <= 0) continue;

        const int jetty_depth_before =
            __sync_fetch_and_add(&wr_depth_list_[jetty_index], 0);
        const int jfc_depth_before =
            __sync_fetch_and_add(jfc_outstanding_, 0);
        const int wr_count = reserveUrmaJfcCredits(
            jfc_outstanding_, static_cast<int>(globalConfig().max_jfc_e),
            requested_wr);
        if (wr_count <= 0) {
            stop_after_batch = true;
            break;
        }

        int failed_wr_count = 0;

        for (int i = 0; i < wr_count; ++i) {
            auto* slice = slice_list[slice_offset + static_cast<size_t>(i)];
            auto& l_sge = l_sge_list[i];
            auto& r_sge = r_sge_list[i];
            auto& wr = wr_list[i];
            memset(&wr, 0, sizeof(urma_jfs_wr_t));

            l_sge.addr = static_cast<uint64_t>(slice->source_addr);
            l_sge.len = slice->length;
            l_sge.tseg = static_cast<urma_target_seg_t*>(slice->ub.l_seg);
            r_sge.addr = slice->ub.dest_addr;
            r_sge.len = slice->length;
            r_sge.tseg = static_cast<urma_target_seg_t*>(slice->ub.r_seg);

            wr.user_ctx = static_cast<uint64_t>(slice);
            wr.opcode = slice->opcode == Transport::TransferRequest::READ
                            ? URMA_OPC_READ
                            : URMA_OPC_WRITE;
            wr.rw.src.sge =
                slice->opcode == Transport::TransferRequest::READ ? &r_sge
                                                                  : &l_sge;
            wr.rw.src.num_sge = 1;
            wr.rw.dst.sge =
                slice->opcode == Transport::TransferRequest::READ ? &l_sge
                                                                  : &r_sge;
            wr.rw.dst.num_sge = 1;
            wr.next = i + 1 == wr_count ? nullptr : &wr_list[i + 1];
            wr.flag.bs.complete_enable = 1;
            wr.flag.bs.inline_flag = 0;

            const auto imported =
                imported_jetty_map_.find(jetty_list_[jetty_index]);
            if (imported == imported_jetty_map_.end()) {
                LOG(ERROR) << "Jetty not imported for endpoint, jetty_index="
                           << jetty_index << ", local_nic=";
                wr.tjetty = nullptr;
            } else {
                wr.tjetty = imported->second;
            }

            slice->ts = getCurrentTimeInNano();
            slice->status = Transport::Slice::POSTED;
            slice->ub.jetty_depth = &wr_depth_list_[jetty_index];
            slice->ub.endpoint = this;
        }

        __sync_fetch_and_add(&wr_depth_list_[jetty_index], wr_count);
        urma_jfs_wr_t* bad_wr = nullptr;
        const uint64_t post_begin_ns =
            timing_enabled ? TransportTimingTrace::nowNs() : 0;
        const int rc = urma_post_jetty_send_wr(
            jetty_list_[jetty_index], wr_list.data(), &bad_wr);
        const uint64_t post_end_ns =
            timing_enabled ? TransportTimingTrace::nowNs() : 0;

        if (rc) {
            PLOG(ERROR) << "Failed to urma_post_jetty_send_wr";
            while (bad_wr) {
                const int failed_index =
                    static_cast<int>(bad_wr - wr_list.data());
                LOG(ERROR) << "slice (" << failed_index
                           << ") post send failed.";
                failed_slice_list.push_back(
                    slice_list[slice_offset +
                               static_cast<size_t>(failed_index)]);
                __sync_fetch_and_sub(&wr_depth_list_[jetty_index], 1);
                __sync_fetch_and_sub(jfc_outstanding_, 1);
                ++failed_wr_count;
                bad_wr = bad_wr->next;
            }
            stop_after_batch = true;
        }

        const int posted_wr = wr_count - failed_wr_count;
        total_posted += static_cast<size_t>(posted_wr);
        if (timing_enabled) {
            uint64_t attempted_payload_bytes = 0;
            uint64_t posted_payload_bytes = 0;
            for (int i = 0; i < wr_count; ++i) {
                const uint64_t bytes =
                    slice_list[slice_offset + static_cast<size_t>(i)]->length;
                attempted_payload_bytes += bytes;
                if (i < posted_wr) posted_payload_bytes += bytes;
            }
            const int depth_after =
                __sync_fetch_and_add(&wr_depth_list_[jetty_index], 0);
            const int jfc_after = __sync_fetch_and_add(jfc_outstanding_, 0);
            const uint64_t credit_wait_ns =
                blocked_since_ns != 0 && post_end_ns >= blocked_since_ns
                    ? post_end_ns - blocked_since_ns
                    : 0;
            TransportTimingTrace::recordPost(
                "UB", -1, reinterpret_cast<std::uintptr_t>(this),
                jetty_index, requested_slices, wr_count, posted_wr,
                failed_wr_count, jetty_depth_before, depth_after,
                jfc_depth_before, jfc_after, attempted_payload_bytes,
                posted_payload_bytes, post_begin_ns, post_end_ns,
                credit_wait_ns, blocked_reason);
        }

        slice_offset += static_cast<size_t>(wr_count);
        if (stop_after_batch) break;
    }

    if (slice_offset > 0) {
        slice_list.erase(slice_list.begin(),
                         slice_list.begin() +
                             static_cast<ptrdiff_t>(slice_offset));
    }
    next_jetty_index_ = (last_jetty_index + 1) % jetty_count;

    if (timing_enabled) {
        if (slice_list.empty()) {
            diag_no_credit_since_ns_ = 0;
            diag_no_credit_reason_ = 0;
        } else if (total_posted > 0 || slice_offset > 0 ||
                   diag_no_credit_since_ns_ == 0) {
            diag_no_credit_since_ns_ = TransportTimingTrace::nowNs();
            diag_no_credit_reason_ =
                __sync_fetch_and_add(jfc_outstanding_, 0) >=
                        static_cast<int>(globalConfig().max_jfc_e)
                    ? 2
                    : 1;
        }
    }
    return 0;
}
~~~

实施前必须检查 URMA API 的 rc/bad_wr 约定，确认失败链指针相对 wr_list.data() 的偏移有效；验证仅失败 slice 进入 retry 且 JFC/Jetty depth 逐 WR 回退。上面的 endpoint 不能可靠获得 worker_id，因此传入 -1；trace helper 使用同线程 worker 最近登记的 id。无 Jetty 情况必须在 Context 初始化阶段失败，因为当前 performPostSend() 不处理 submitPostSend() 的返回码。

### 3.4 计时 helper

新增 mooncake-transfer-engine/include/transport/transport_timing_trace.h：

~~~cpp
#pragma once
#include <array>
#include <chrono>
#include <cstddef>
#include <cstdint>
#include <cstdlib>
#include <cstring>
#include <glog/logging.h>

namespace mooncake {
class TransportTimingTrace {
   public:
    static constexpr uint64_t kGapLogThresholdNs = 2'000'000;
    static constexpr uint64_t kSlowApiThresholdNs = 100'000;
    static constexpr uint64_t kEventLogIntervalNs = 100'000'000;

    static bool enabled() {
        static const bool value = [] {
            const char* env = std::getenv("MC_TRANSPORT_TIMING");
            return env && std::strcmp(env, "0") != 0 &&
                   std::strcmp(env, "false") != 0;
        }();
        return value;
    }

    static uint64_t nowNs() {
        return static_cast<uint64_t>(
            std::chrono::duration_cast<std::chrono::nanoseconds>(
                std::chrono::steady_clock::now().time_since_epoch()).count());
    }

    static void workerLoopBegin(const char* transport, int worker,
                                bool outstanding, uint64_t submitted,
                                uint64_t processed) {
        if (!enabled()) return;
        auto& s = state();
        s.worker_id = worker;
        const uint64_t now = nowNs();
        if (outstanding && s.last_loop_end_ns && now >= s.last_loop_end_ns) {
            const uint64_t gap = now - s.last_loop_end_ns;
            if (gap >= kGapLogThresholdNs && shouldLogEvent(now))
                LOG(INFO) << "TE_TIMING event=worker_gap transport=" << transport
                          << " worker=" << worker << " event_ns=" << now
                          << " gap_us=" << gap / 1000
                          << " submitted=" << submitted
                          << " processed=" << processed;
        }
    }

    static void workerLoopEnd(bool outstanding) {
        if (!enabled()) return;
        auto& s = state();
        s.last_loop_end_ns = outstanding ? nowNs() : 0;
    }

    static void recordPoll(const char* transport, int worker, int queue,
                           int count, int outstanding_before_release,
                           uint64_t begin_ns, uint64_t end_ns) {
        if (!enabled() || queue < 0 ||
            static_cast<size_t>(queue) >= state().empty_poll_since_ns.size() ||
            end_ns < begin_ns) return;
        auto& s = state();
        const uint64_t duration = end_ns - begin_ns;
        uint64_t& empty_since =
            s.empty_poll_since_ns[static_cast<size_t>(queue)];
        if (count == 0) {
            if (!empty_since) empty_since = begin_ns;
            if (duration >= kSlowApiThresholdNs && shouldLogEvent(end_ns))
                LOG(INFO) << "TE_TIMING event=poll_slow transport=" << transport
                          << " worker=" << worker << " queue=" << queue
                          << " poll_begin_ns=" << begin_ns
                          << " poll_end_ns=" << end_ns
                          << " poll_us=" << duration / 1000
                          << " outstanding=" << outstanding_before_release;
            return;
        }
        const uint64_t empty_span =
            empty_since && end_ns >= empty_since ? end_ns - empty_since : 0;
        empty_since = 0;
        const uint64_t completion_gap =
            s.last_completion_ns && end_ns >= s.last_completion_ns
                ? end_ns - s.last_completion_ns : 0;
        s.last_completion_ns = end_ns;
        if ((empty_span >= kGapLogThresholdNs ||
             duration >= kSlowApiThresholdNs ||
             completion_gap >= 10'000'000) &&
            shouldLogEvent(end_ns))
            LOG(INFO) << "TE_TIMING event=poll_progress transport=" << transport
                      << " worker=" << worker << " queue=" << queue
                      << " poll_begin_ns=" << begin_ns
                      << " poll_end_ns=" << end_ns
                      << " nr_poll=" << count
                      << " empty_span_us=" << empty_span / 1000
                      << " completion_gap_us=" << completion_gap / 1000
                      << " poll_us=" << duration / 1000
                      << " outstanding_before_release="
                      << outstanding_before_release;
    }

    static void recordCreditRelease(const char* transport, int worker,
                                    int queue, int count,
                                    uint64_t poll_end_ns,
                                    uint64_t release_ns) {
        if (!enabled() || count <= 0 || queue < 0 ||
            static_cast<size_t>(queue) >= state().empty_poll_since_ns.size() ||
            release_ns < poll_end_ns ||
            release_ns - poll_end_ns < kGapLogThresholdNs ||
            !shouldLogEvent(release_ns))
            return;
        LOG(INFO) << "TE_TIMING event=credit_release transport=" << transport
                  << " worker=" << worker << " queue=" << queue
                  << " released=" << count
                  << " poll_end_ns=" << poll_end_ns
                  << " release_ns=" << release_ns
                  << " poll_to_release_us="
                  << (release_ns - poll_end_ns) / 1000;
    }

    static void recordPost(
        const char* transport, int worker, std::uintptr_t endpoint_id,
        size_t queue, size_t pending,
        int attempted, int posted, int failed, int depth_before, int depth_after,
        int completion_before, int completion_after,
        uint64_t attempted_payload_bytes,
        uint64_t posted_payload_bytes, uint64_t begin_ns, uint64_t end_ns,
        uint64_t credit_wait_ns, int reason) {
        if (!enabled() || end_ns < begin_ns) return;
        auto& s = state();
        const int effective_worker = worker >= 0 ? worker : s.worker_id;
        if (s.post_summary_begin_ns == 0)
            s.post_summary_begin_ns = begin_ns;
        s.post_summary_calls++;
        s.post_summary_attempted_wr += static_cast<uint64_t>(attempted);
        s.post_summary_posted_wr += static_cast<uint64_t>(posted);
        s.post_summary_failed_wr += static_cast<uint64_t>(failed);
        s.post_summary_attempted_bytes += attempted_payload_bytes;
        s.post_summary_posted_bytes += posted_payload_bytes;
        State::QueueSummary* queue_summary = nullptr;
        for (auto& q : s.post_queue_summaries) {
            if (q.used && q.endpoint_id == endpoint_id && q.queue == queue) {
                queue_summary = &q;
                break;
            }
            if (!q.used && !queue_summary) queue_summary = &q;
        }
        if (queue_summary) {
            auto& q = *queue_summary;
            if (!q.used) {
                q.used = true;
                q.endpoint_id = endpoint_id;
                q.queue = queue;
            }
            q.calls++;
            q.attempted_wr += static_cast<uint64_t>(attempted);
            q.posted_wr += static_cast<uint64_t>(posted);
            q.failed_wr += static_cast<uint64_t>(failed);
            q.attempted_bytes += attempted_payload_bytes;
            q.posted_bytes += posted_payload_bytes;
        } else {
            s.post_summary_queue_overflow++;
        }
        if (end_ns >= s.post_summary_begin_ns &&
            end_ns - s.post_summary_begin_ns >= 1'000'000'000) {
            LOG(INFO) << "TE_TIMING event=post_summary transport=" << transport
                      << " worker=" << effective_worker
                      << " window_begin_ns=" << s.post_summary_begin_ns
                      << " window_end_ns=" << end_ns
                      << " window_us="
                      << (end_ns - s.post_summary_begin_ns) / 1000
                      << " calls=" << s.post_summary_calls
                      << " attempted_wr=" << s.post_summary_attempted_wr
                      << " posted_wr=" << s.post_summary_posted_wr
                      << " failed_wr=" << s.post_summary_failed_wr
                      << " attempted_payload_bytes="
                      << s.post_summary_attempted_bytes
                      << " posted_payload_bytes=" << s.post_summary_posted_bytes;
            if (s.post_summary_queue_overflow) {
                LOG(WARNING) << "TE_TIMING event=post_queue_summary_overflow"
                             << " transport=" << transport
                             << " worker=" << effective_worker
                             << " dropped_updates="
                             << s.post_summary_queue_overflow;
            }
            for (size_t i = 0; i < s.post_queue_summaries.size(); ++i) {
                auto& q = s.post_queue_summaries[i];
                if (q.calls == 0) continue;
                LOG(INFO) << "TE_TIMING event=post_queue_summary"
                          << " transport=" << transport
                          << " worker=" << effective_worker
                          << " endpoint_id=" << q.endpoint_id
                          << " queue=" << q.queue
                          << " window_begin_ns=" << s.post_summary_begin_ns
                          << " window_end_ns=" << end_ns
                          << " calls=" << q.calls
                          << " attempted_wr=" << q.attempted_wr
                          << " posted_wr=" << q.posted_wr
                          << " failed_wr=" << q.failed_wr
                          << " attempted_payload_bytes=" << q.attempted_bytes
                          << " posted_payload_bytes=" << q.posted_bytes;
                q = State::QueueSummary{};
            }
            s.post_summary_begin_ns = end_ns;
            s.post_summary_calls = 0;
            s.post_summary_attempted_wr = 0;
            s.post_summary_posted_wr = 0;
            s.post_summary_failed_wr = 0;
            s.post_summary_attempted_bytes = 0;
            s.post_summary_posted_bytes = 0;
            s.post_summary_queue_overflow = 0;
        }
        const uint64_t duration = end_ns - begin_ns;
        const uint64_t completion_to_post =
            s.last_completion_ns && end_ns >= s.last_completion_ns
                ? end_ns - s.last_completion_ns : 0;
        if (credit_wait_ns < kGapLogThresholdNs &&
            completion_to_post < kGapLogThresholdNs &&
            duration < kSlowApiThresholdNs && failed == 0) return;
        if (!shouldLogEvent(end_ns)) return;
        const char* blocked_by =
            reason == 2 ? "jfc_credit" : (reason == 1 ? "jetty_depth" : "none");
        LOG(INFO) << "TE_TIMING event=post_progress transport=" << transport
                  << " worker=" << effective_worker << " queue=" << queue
                  << " pending_slices=" << pending
                  << " attempted_wr=" << attempted << " posted_wr=" << posted
                  << " failed_wr=" << failed
                  << " attempted_payload_bytes=" << attempted_payload_bytes
                  << " posted_payload_bytes=" << posted_payload_bytes
                  << " depth_before=" << depth_before
                  << " depth_after=" << depth_after
                  << " completion_outstanding_before=" << completion_before
                  << " completion_outstanding_after=" << completion_after
                  << " post_begin_ns=" << begin_ns
                  << " post_end_ns=" << end_ns
                  << " provider_post_us=" << duration / 1000
                  << " credit_wait_us=" << credit_wait_ns / 1000
                  << " completion_to_post_us=" << completion_to_post / 1000
                  << " blocked_by=" << blocked_by;
    }

   private:
    static bool shouldLogEvent(uint64_t now_ns) {
        auto& s = state();
        if (s.last_event_log_ns != 0 && now_ns >= s.last_event_log_ns &&
            now_ns - s.last_event_log_ns < kEventLogIntervalNs)
            return false;
        s.last_event_log_ns = now_ns;
        return true;
    }

    struct State {
        struct QueueSummary {
            bool used = false;
            std::uintptr_t endpoint_id = 0;
            size_t queue = 0;
            uint64_t calls = 0;
            uint64_t attempted_wr = 0;
            uint64_t posted_wr = 0;
            uint64_t failed_wr = 0;
            uint64_t attempted_bytes = 0;
            uint64_t posted_bytes = 0;
        };
        int worker_id = -1;
        uint64_t last_loop_end_ns = 0;
        uint64_t last_completion_ns = 0;
        uint64_t last_event_log_ns = 0;
        uint64_t post_summary_begin_ns = 0;
        uint64_t post_summary_calls = 0;
        uint64_t post_summary_attempted_wr = 0;
        uint64_t post_summary_posted_wr = 0;
        uint64_t post_summary_failed_wr = 0;
        uint64_t post_summary_attempted_bytes = 0;
        uint64_t post_summary_posted_bytes = 0;
        uint64_t post_summary_queue_overflow = 0;
        std::array<QueueSummary, 256> post_queue_summaries{};
        std::array<uint64_t, 256> empty_poll_since_ns{};
    };
    static State& state() {
        static thread_local State value;
        return value;
    }
};
}  // namespace mooncake
~~~

### 3.5 调用端打点

以下给出完整新增 include、UB worker 函数替换和 poll/post 的调用点修改。UB 的空闲分支调用 `workerLoopEnd(false)` 清掉活跃循环起点，避免把正常条件变量等待误报为 20 ms 活跃服务间隔。

在 `ub_context.cpp` 的 include 区增加：

~~~cpp
#include "transport/transport_timing_trace.h"
~~~

用以下完整函数替换 `UbWorkerPool::transferWorker()`：

~~~cpp
void UbWorkerPool::transferWorker(int thread_id) {
    bindToSocket(numa_socket_id_);
    const static uint64_t kWaitPeriodInNano = 100000000;  // 100ms
    uint64_t last_wait_ts = getCurrentTimeInNano();
    while (workers_running_.load(std::memory_order_relaxed)) {
        const auto processed_slice_count =
            processed_slice_count_.load(std::memory_order_relaxed);
        const auto submitted_slice_count =
            submitted_slice_count_.load(std::memory_order_relaxed);
        const bool outstanding =
            processed_slice_count != submitted_slice_count;
        TransportTimingTrace::workerLoopBegin(
            "UB", thread_id, outstanding, submitted_slice_count,
            processed_slice_count);
        if (!outstanding) {
            const uint64_t curr_wait_ts = getCurrentTimeInNano();
            if (curr_wait_ts - last_wait_ts > kWaitPeriodInNano) {
                std::unique_lock<std::mutex> lock(cond_mutex_);
                suspended_flag_.fetch_add(1);
                cond_var_.wait_for(lock, std::chrono::seconds(1));
                suspended_flag_.fetch_sub(1);
                last_wait_ts = curr_wait_ts;
            }
            TransportTimingTrace::workerLoopEnd(false);
            continue;
        }
        performPostSend(thread_id);
#ifndef USE_FAKE_POST_SEND
        performPoll(thread_id);
#endif
        TransportTimingTrace::workerLoopEnd(true);
    }
}
~~~

在 `UbWorkerPool::performPoll()` 内，将 `context_.poll()` 调用段替换为：

~~~cpp
        const bool trace = TransportTimingTrace::enabled();
        const uint64_t poll_begin_ns =
            trace ? TransportTimingTrace::nowNs() : 0;
        int nr_poll = context_.poll(kPollCount, failed, num_failed,
                                    jetty_depth_set, jfc_index);
        const uint64_t poll_end_ns =
            trace ? TransportTimingTrace::nowNs() : 0;
        if (trace && nr_poll >= 0) {
            TransportTimingTrace::recordPoll(
                "UB", thread_id, jfc_index, nr_poll,
                __sync_fetch_and_add(context_.outstandingCount(jfc_index), 0),
                poll_begin_ns, poll_end_ns);
        }
~~~

保留现有 Jetty depth/JFC outstanding 回收代码；不可在 `markSuccess()` 后再次解引用可能被上层回收的 slice。记录 `recordPoll` 的位置必须紧接 `context_.poll()` 返回之后、现有 JFC/Jetty depth 递减之前。将现有 JFC outstanding 递减小块替换为：

~~~cpp
        if (nr_poll) {
            __sync_fetch_and_sub(context_.outstandingCount(jfc_index), nr_poll);
            if (trace) {
                TransportTimingTrace::recordCreditRelease(
                    "UB", thread_id, jfc_index, nr_poll,
                    poll_end_ns, TransportTimingTrace::nowNs());
            }
        }
~~~

在 `rdma_endpoint.cpp` 的 include 区增加：

~~~cpp
#include "transport/transport_timing_trace.h"
~~~

用以下完整函数替换 `RdmaEndPoint::submitPostSend()`。这保留当前 QP 分配与 posted-slice 跟踪行为，补齐每批 post 的单调时钟、WR/字节数和队列深度观测。`bad_wr` 必须按当前 libibverbs 合约指向首个未提交 WR；若实际 provider 不满足该约定，先停止参数扫描并适配错误边界解析。

~~~cpp
int RdmaEndPoint::submitPostSend(
    std::vector<Transport::Slice*>& slice_list,
    std::vector<Transport::Slice*>& failed_slice_list) {
    RWSpinlock::WriteGuard guard(lock_);
    if (!active_.load(std::memory_order_acquire) ||
        status_.load(std::memory_order_relaxed) != CONNECTED) {
        for (auto& slice : slice_list) failed_slice_list.push_back(slice);
        slice_list.clear();
        return 0;
    }

    const size_t num_qp = qp_list_.size();
    if (slice_list.empty()) return 0;
    const size_t requested = slice_list.size();
    const int cq_initial =
        cq_outstanding_->load(std::memory_order_relaxed);
    int cq_remaining = static_cast<int>(globalConfig().max_cqe) - cq_initial;
    if (cq_remaining <= 0) return 0;

    const size_t max_postable_per_qp = std::min(
        {static_cast<size_t>(max_wr_depth_),
         static_cast<size_t>(cq_remaining), requested});
    std::vector<ibv_send_wr> wr_list(max_postable_per_qp, ibv_send_wr{});
    std::vector<ibv_sge> sge_list(max_postable_per_qp);
    size_t total_consumed = 0;
    size_t cursor = 0;

    for (size_t qp_index = 0;
         qp_index < num_qp && cq_remaining > 0 && cursor < requested;
         ++qp_index) {
        const int qp_depth_before =
            wr_depth_list_[qp_index].load(std::memory_order_relaxed);
        const int cq_before =
            cq_outstanding_->load(std::memory_order_relaxed);
        const int qp_avail = max_wr_depth_ - qp_depth_before;
        if (qp_avail <= 0) continue;

        const size_t remaining_qps = num_qp - qp_index;
        const size_t remaining_slices = requested - cursor;
        const size_t chunk =
            (remaining_slices + remaining_qps - 1) / remaining_qps;
        const size_t start = cursor;
        const size_t end = std::min(start + chunk, requested);
        const int assigned_count = static_cast<int>(end - start);
        int wr_count = std::min(assigned_count, qp_avail);
        wr_count = std::min(wr_count, cq_remaining);
        if (wr_count <= 0) continue;

        for (int i = 0; i < wr_count; ++i) {
            auto* slice = slice_list[start + static_cast<size_t>(i)];
            auto& sge = sge_list[static_cast<size_t>(i)];
            sge.addr = static_cast<uint64_t>(slice->source_addr);
            sge.length = slice->length;
            sge.lkey = slice->rdma.source_lkey;

            auto& wr = wr_list[static_cast<size_t>(i)];
            memset(&wr, 0, sizeof(ibv_send_wr));
            wr.wr_id = static_cast<uint64_t>(slice);
            wr.opcode = slice->opcode == Transport::TransferRequest::READ
                            ? IBV_WR_RDMA_READ
                            : IBV_WR_RDMA_WRITE;
            wr.num_sge = 1;
            wr.sg_list = &sge;
            wr.send_flags = IBV_SEND_SIGNALED;
            wr.next = (i + 1 == wr_count) ? nullptr : &wr_list[i + 1];
            wr.wr.rdma.remote_addr = slice->rdma.dest_addr;
            wr.wr.rdma.rkey = slice->rdma.dest_rkey;
            slice->ts = getCurrentTimeInNano();
            slice->status = Transport::Slice::POSTED;
            slice->rdma.qp_depth = &wr_depth_list_[qp_index];
        }

        wr_depth_list_[qp_index].fetch_add(wr_count,
                                           std::memory_order_acq_rel);
        cq_outstanding_->fetch_add(wr_count, std::memory_order_acq_rel);
        context_.trackPostedSlices(slice_list, start, wr_count);
        ibv_send_wr* bad_wr = nullptr;
        const bool trace = TransportTimingTrace::enabled();
        const uint64_t post_begin_ns =
            trace ? TransportTimingTrace::nowNs() : 0;
        const int rc = ibv_post_send(qp_list_[qp_index], wr_list.data(),
                                     &bad_wr);
        const uint64_t post_end_ns =
            trace ? TransportTimingTrace::nowNs() : 0;
        size_t first_failed = static_cast<size_t>(wr_count);
        if (rc) {
            LOG(ERROR) << "Failed to ibv_post_send: " << strerror(rc);
            first_failed = bad_wr
                              ? static_cast<size_t>(bad_wr - wr_list.data())
                              : 0;
            context_.untrackPostedSlices(
                slice_list, start + first_failed,
                static_cast<size_t>(wr_count) - first_failed);
            for (size_t i = first_failed;
                 i < static_cast<size_t>(wr_count); ++i) {
                failed_slice_list.push_back(slice_list[start + i]);
                wr_depth_list_[qp_index].fetch_sub(
                    1, std::memory_order_acq_rel);
                cq_outstanding_->fetch_sub(1, std::memory_order_acq_rel);
            }
        }

        const int posted_wr = static_cast<int>(first_failed);
        const int failed_wr_count = wr_count - posted_wr;
        const int qp_depth_after =
            wr_depth_list_[qp_index].load(std::memory_order_relaxed);
        const int cq_after =
            cq_outstanding_->load(std::memory_order_relaxed);
        if (trace) {
            uint64_t attempted_payload_bytes = 0;
            uint64_t posted_payload_bytes = 0;
            for (int i = 0; i < wr_count; ++i) {
                const uint64_t bytes =
                    slice_list[start + static_cast<size_t>(i)]->length;
                attempted_payload_bytes += bytes;
                if (i < posted_wr) posted_payload_bytes += bytes;
            }
            TransportTimingTrace::recordPost(
                "RDMA", -1, reinterpret_cast<std::uintptr_t>(this),
                qp_index, requested - cursor, wr_count,
                posted_wr, failed_wr_count, qp_depth_before, qp_depth_after,
                cq_before, cq_after, attempted_payload_bytes,
                posted_payload_bytes, post_begin_ns, post_end_ns, 0, 0);
        }

        total_consumed += static_cast<size_t>(wr_count);
        cursor += static_cast<size_t>(wr_count);
        if (rc) break;
        cq_remaining -= wr_count;
    }

    slice_list.erase(
        slice_list.begin(),
        slice_list.begin() + static_cast<std::ptrdiff_t>(total_consumed));
    return 0;
}
~~~

在 `worker_pool.cpp` 的 include 区增加同一头文件，并用以下完整函数替换 RDMA 的 `WorkerPool::transferWorker()`。活跃状态同时考虑软件 submitted/processed 计数和当前 worker 负责的 CQ outstanding；正常 park 不会计入活跃 worker gap。

~~~cpp
void WorkerPool::transferWorker(int thread_id) {
    bindToSocket(numa_socket_id_);
    const static uint64_t kWaitPeriodInNano = 100000000;  // 100ms
    uint64_t last_wait_ts = getCurrentTimeInNano();
    const bool can_post = workerCanPost(thread_id);
    const bool can_poll = workerCanPoll(thread_id);
    while (workers_running_.load(std::memory_order_relaxed)) {
        const auto processed_slice_count =
            processed_slice_count_.load(std::memory_order_relaxed);
        const auto submitted_slice_count =
            submitted_slice_count_.load(std::memory_order_relaxed);
        const bool outstanding_cq = hasOutstandingCq(thread_id);
        const bool outstanding =
            processed_slice_count != submitted_slice_count || outstanding_cq;
        TransportTimingTrace::workerLoopBegin(
            "RDMA", thread_id, outstanding, submitted_slice_count,
            processed_slice_count);
        if (!outstanding) {
            const uint64_t curr_wait_ts = getCurrentTimeInNano();
            if (curr_wait_ts - last_wait_ts > kWaitPeriodInNano) {
                std::unique_lock<std::mutex> lock(cond_mutex_);
                parked_worker_count_.fetch_add(1, std::memory_order_acq_rel);
                if (processed_slice_count_.load(std::memory_order_relaxed) ==
                        submitted_slice_count_.load() &&
                    !hasOutstandingCq(thread_id)) {
                    cond_var_.wait_for(lock, std::chrono::seconds(1));
                }
                parked_worker_count_.fetch_sub(1, std::memory_order_acq_rel);
                last_wait_ts = curr_wait_ts;
            }
            TransportTimingTrace::workerLoopEnd(false);
            continue;
        }
        if (can_post) performPostSend(thread_id);
#ifndef USE_FAKE_POST_SEND
        if (can_poll) performPollCq(thread_id);
#endif
        last_wait_ts = getCurrentTimeInNano();
        TransportTimingTrace::workerLoopEnd(true);
    }
}
~~~

在该文件 `WorkerPool::performPollCq()` 内，将循环中的 `context_.poll()` 调用替换为：

~~~cpp
        const bool trace = TransportTimingTrace::enabled();
        const uint64_t poll_begin_ns =
            trace ? TransportTimingTrace::nowNs() : 0;
        int nr_poll = context_.poll(kPollCount, wc, cq_index);
        const uint64_t poll_end_ns =
            trace ? TransportTimingTrace::nowNs() : 0;
        if (trace && nr_poll >= 0) {
            TransportTimingTrace::recordPoll(
                "RDMA", thread_id, cq_index, nr_poll,
                context_.cqOutstandingCount(cq_index)->load(
                    std::memory_order_relaxed),
                poll_begin_ns, poll_end_ns);
        }
~~~

将该 worker 对 CQ outstanding 的现有递减块替换为以下完整代码，以区分 provider 返回 completion 与软件归还可提交额度的时间。该事件只在两者相差至少 2 ms 时输出：

~~~cpp
        if (nr_poll) {
            context_.cqOutstandingCount(cq_index)->fetch_sub(
                nr_poll, std::memory_order_acq_rel);
            if (trace) {
                TransportTimingTrace::recordCreditRelease(
                    "RDMA", thread_id, cq_index, nr_poll, poll_end_ns,
                    TransportTimingTrace::nowNs());
            }
        }
~~~

RDMA 现有 `last_poll_ts_ns_`、`last_poll_interval_ns_`、`max_poll_interval_ns_` 使用 `getCurrentTimeInNano()`（CLOCK_REALTIME）；本实验结论以新 `steady_clock` 指标为准，不混写旧字段。

### 3.6 诊断开关

在进程启动前启用或关闭：

~~~sh
export MC_TRANSPORT_TIMING=1
unset MC_TRANSPORT_TIMING
~~~

关闭时不得输出逐轮日志或采样时钟；打点位置只做缓存开关判断。开启时也不能逐 poll/逐 WR 输出；仅记录 worker gap/空 poll/credit wait 超过 2 ms、provider API 超过 100 μs、失败 WR，以及每 worker 约每秒一条提交汇总和每个活动 Jetty/QP 一条队列分项。一般事件日志按 worker 限制为最多每 100 ms 一条；提交汇总独立输出。若仍超过每 worker 每秒数十行，先调阈值再测性能。

## 4. 测试验证方案

### 4.1 编译与自动化测试

在现有 Mooncake build 目录编译：

~~~sh
cmake --build <build-dir> --target config_test urma_post_plan_test ub_transport_test tebench -j
ctest --test-dir <build-dir> --output-on-failure -R '^(config_test|urma_post_plan_test)$'
~~~

config_test 与 urma_post_plan_test 必须在 CTest 中运行。现有 ub_transport_test 当前未注册进 CTest；在具备 mock URMA 或测试硬件的环境手工执行，记录启动参数、环境和退出状态。

### 4.2 功能检查

验证 1/2/4 Jetty 下读写请求完成、slice 恰好进入一个终态；pending 超出 Jetty 窗口时剩余 slice 留在软件队列并在 completion 后继续提交；一个 Jetty 满时其他 Jetty仍可 post；共享 JFC 达上限后不超额预留，completion 后正确释放；部分 bad_wr 仅重试失败 slice；两端 Jetty 配置不一致时握手明确失败；MC_MAX_WR 为 128/256/512/1024/2048 均不超过 JFS depth=2048，大于硬件边界的配置会输出生效值；非法 Jetty 参数被拒绝或钳制。

### 4.3 参数扫描

每组使用新进程，防止设备初始化对 GlobalConfig 的修改污染下一组：

| 阶段 | MC_MAX_WR | MC_NUM_JETTY_PER_EP | MC_SLICE_SIZE | 目标 |
|---|---:|---:|---:|---|
| 基线 | 256 | 1 | 65536 | 复现默认行为 |
| WR 深度 | 128、256、512、1024、2048 | 1 | 65536 | 判断 burst 是否随 max_wr 线性变化 |
| Jetty 数 | 256 | 1、2、4 | 65536 | 确认一次 submit 是否使用全部 Jetty |
| 联合扫描 | 256、512、1024 | 1、2、4 | 65536 | 观察 JFC、CPU、链路拐点 |
| slice 敏感性 | 已验证安全窗口 | 固定 1 或 2 | 64、128、256、512 KiB | 单独检查 WR/CQE 处理速率 |

每一组都先在 `MC_TRANSPORT_TIMING` 关闭时测吞吐基线，再以相同负载、相同线程和绑核设置开启打点测时间线；最终性能数字仍以关闭打点的重复结果为准。诊断运行报告开关引入的观测开销，不把打点运行的吞吐与基线混成同一组。URMA JFS depth 固定 2048，MC_MAX_WR 扫描不超过 2048。多 Jetty 总窗口仍受共享 JFC 余量限制。

另做固定总字节数的 block_size×batch_size 对照，例如 1 MiB 总数据：64 KiB×16、256 KiB×4、1 MiB×1。分别记录应用 request block size、benchmark batch size、MC_SLICE_SIZE 和页面粒度，不能把它们统称为“包大小”。用户观察的页面参数从默认 16 调至 64、约 7 MB 工作量达到约 30 GB/s，应使用原始启动脚本独立复测；该观察不证明底层 WR 为 7 MB，也不证明页面大小等于 slice_size。

### 4.4 固定 tebench 工作负载

transfer-engine/benchmark 中 tebench 固定扫描区间示例：

~~~sh
./tebench \
  --backend=classic \
  --target_seg_name=<target_segment_name> \
  --start_block_size=65536 \
  --max_block_size=65536 \
  --start_batch_size=128 \
  --max_batch_size=128 \
  --start_num_threads=1 \
  --max_num_threads=1 \
  --duration=30
~~~

目标端按 tebench 输出启动；实际 transport 选择受本地构建及 transport 安装配置影响，实施人员必须沿用当前 UB benchmark 启动脚本，并保存完整命令、构建选项、transport 名称、页面粒度和全部环境变量。每组预热至少 10 秒、稳定测 30 秒、重复 5 次；固定双端 CPU/NUMA 绑定、NIC、路由、方向、目标内存类型和对端负载。两端 Jetty 参数必须一致。

## 5. 打点字段与 20 ms 判据

最小字段：worker id/间隙/submitted/processed；软件队列输入和剩余 slice 数；进程内 endpoint id、Jetty/QP index、depth before/after、attempted/posted/failed WR 及 payload bytes；JFC/CQ outstanding/capacity/poll 返回数/连续空 poll 跨度；provider post/poll 时长；completion 与 credit 释放时间；端口计数原始值、采样周期、统计口径；应用吞吐、block/batch、线程数、slice_size、max_wr、Jetty/QP 数；线程 TID、绑核、CPU 和 off-CPU 时间。`endpoint_id` 是进程内指针标识，仅用于同一次运行关联，不可跨进程比较。

3 ms PMU 采样只能看粗粒度周期。16 MiB 在 50 GB/s 下理论发送时间约 0.34 ms，需采用更高频端口计数器、硬件时间戳或将 Mooncake post/completion 时间与端口计数器对齐。

所有 Mooncake 打点均写出 steady-clock 纳秒时间；同机端口计数采集必须同时记录 `CLOCK_MONOTONIC` 时间，或通过成对采样校准到该时钟。不同主机的 steady-clock 起点不可直接相减；双端关联使用 PTP/NTP 校时后的时间戳或硬件包时间戳。由于一般事件有阈值和速率限制，`TE_TIMING` 事件用于判断停顿类别和时间边界；P50/P95/P99 应由未抽样的端口/硬件时间戳或系统调度 trace 计算。不要从被筛选、限速的日志样本声称完整延迟分位数。

| 观测结果 | 优先归因 | 下一步 |
|---|---|---|
| 有 outstanding 时 worker_gap 约 20 ms，恢复后 poll 立即读到 completion | CPU 调度/抢占/绑核不足 | perf sched timehist、线程 schedstat、独占核与 IRQ 分离对照 |
| worker_gap 小，但 nr_poll=0 持续约 20 ms，随后一次 poll 返回大量 completion | 完成可见性、JFC/CQ 或 provider 进展 | 对比 URMA driver/firmware/JFC 和 RDMA poll |
| completion 很快、depth 已释放，但 completion_to_post 或重新入队间隔约 20 ms | Mooncake 队列、锁、redispatch 或 endpoint 控制路径 | 分段测 performPostSend、endpoint lock、slice_queue、retry 队列 |
| post 和 completion 持续，但端口没有对应增量 | 对端、路由、流控或设备侧背压 | 双端同时采集，检查链路/优先级队列/错误计数 |
| burst bytes 随 Jetty 数×max_wr×slice_size 变化而间隔不变 | 窗口决定 burst 容量，间隔是独立 refill/service 延迟 | 分别处理窗口容量与 credit 回收时间 |
| burst 不随窗口变，posted WR 未到预算 | 限制不在 max_wr 总窗口 | 检查 pending slices、JFC 余量、transport 选择与队列长度 |

结果报告必须给出实际生效配置；每 endpoint/Jetty 的输入 slice、posted WR 和 payload bytes；从原始 PMU/硬件时间戳计算的 burst 间隔与吞吐 P50/P95/P99/max；从 `perf sched` 或未抽样系统调度 trace 计算的 worker 调度等待分布；`TE_TIMING` 记录的空 poll、completion-to-post、credit release 慢事件数量和最大值；应用及端口吞吐、WR/s、CQE/s、worker CPU/off-CPU；business-flit 与 application payload 的口径转换。

## 6. 验收、回退与实施顺序

**代码验收：**默认仍为 1 Jetty；两端配置不一致明确报错；测试覆盖窗口分配、轮转、满 Jetty 跳过、共享 JFC 上限、并发原子预留与非法配置；mock/硬件下无 slice 丢失、重复 completion、负计数或 outstanding 泄漏；诊断关闭时不逐轮写日志，开启时低频且使用单调时间；bad_wr 失败 WR 必须回退 Jetty/JFC credit。

**性能判定：**burst 随窗口线性增长仅支持窗口假设，不证明 20 ms 已解决；若 Jetty 增加后窗口和有效吞吐提高且 CPU/设备错误稳定，可将更优值作为默认候选，仍须验证设备 max_jetty、JFC 和 endpoint 总数；若只扩大 burst，按时间线定位 refill 延迟，不继续盲目放大窗口。

**回退：**MC_NUM_JETTY_PER_EP=1 回到单 Jetty；关闭 MC_TRANSPORT_TIMING 回到无诊断路径；多 Jetty 出现握手/completion/计数问题时先恢复单 Jetty post 行为，保留诊断结果修复故障；不删除现有 MR、retry、endpoint teardown 和故障恢复。

**实现 Agent 执行顺序：**

1. 对照本文确认 checkout 源码一致，保留现有其他改动。
2. 依次完成 Jetty planner、配置与硬件边界、submitPostSend、trace helper、UB/RDMA 调用点。
3. 先编译并运行 config_test、urma_post_plan_test，再运行 mock URMA 功能验证，最后做双节点测试。
4. 先测默认基线和 max_wr，再测 Jetty 数；slice 大小及页面/请求粒度单独后测。
5. 保存源码 commit/ref、二进制版本、完整命令、环境变量、设备能力、日志和端口计数器原始文件。
6. 先定位 worker gap、completion 可见性或 post 控制路径，再给性能结论。前序对话中的成因判断均是待验证推断，不能替代本方案的打点结果。

## 7. 详细测试验证步骤与通过/失败标准

### 7.1 执行步骤

1. **固定并记录环境。**记录 Mooncake commit、构建选项、URMA/RDMA 设备与固件版本、设备 `max_jetty/max_jfc`、两端环境变量、线程绑核、NUMA、传输方向、slice/block/batch 参数。两端 Jetty 配置保持一致。每组参数使用新进程。
2. **构建并运行单测。**在 Mooncake build 目录执行：

   ~~~sh
   cmake --build <build-dir> --target config_test urma_post_plan_test ub_transport_test tebench -j
   ctest --test-dir <build-dir> --output-on-failure -R '^(config_test|urma_post_plan_test)$'
   ~~~

3. **运行功能验证。**在 mock URMA 或目标硬件上运行 `ub_transport_test`，再分别以 1、2、4 个 Jetty 执行读写；覆盖 pending 超过单 Jetty window、某个 Jetty 满、共享 JFC 用尽后完成归还，以及两端 Jetty 数不一致的握手拒绝。
4. **建立基线并扫描 window。**先用 `MC_MAX_WR=256`、`MC_NUM_JETTY_PER_EP=1`、`MC_SLICE_SIZE=65536` 跑固定工作负载；然后分别扫描 `MC_MAX_WR=128/256/512/1024/2048` 和 Jetty 数 `1/2/4`。记录每 endpoint/queue 的 submitted、posted、failed WR 与真实 payload bytes。
5. **分离传输粒度影响。**固定总传输字节数，运行 64 KiB×16、256 KiB×4、1 MiB×1；再以现有启动脚本复测页面参数 16 与 64 的场景。每组预热 10 秒、稳定测 30 秒、重复 5 次。
6. **定位 20 ms 间隔。**先关闭打点测基线，再以同一配置开启 `MC_TRANSPORT_TIMING=1`；同步采集高频端口计数或硬件时间戳、`perf sched`/线程调度数据。根据第 5 节判据对齐 worker gap、poll、completion、credit release、下一次 post 和端口字节变化。
7. **整理结果。**按下面的通过/失败标准给每项标记状态，附上生效配置、完整命令、原始日志、端口计数、重复次数及打点开关状态。吞吐结论使用关闭打点的重复结果；打点数据用于判断成因。

### 7.2 通过与失败标准

| 验证项 | 通过标准 | 失败标准或结论 |
|---|---|---|
| 配置解析与硬件边界 | 默认值为 1；合法值生效；非法值不覆盖原配置；设备能力上限和 JFS 深度限制反映在启动日志中 | 非法值生效、创建的 Jetty/WR 深度超过能力上限，或配置与启动日志不一致 |
| Jetty planner 与共享 JFC 额度 | 单测精确通过预期分配；每 Jetty WR 不超过 `max_wr`；并发 JFC 预留总数不超过 4096，完成后 outstanding 正常归还 | planner 超额分配、并发额度超限、完成后计数未归零或出现负值 |
| 端到端读写与重试 | 每个 slice 恰好进入一个终态；失败 WR 只重试失败 slice；队列清空且没有重复 completion、死锁或 outstanding 泄漏 | slice 丢失/重复终态、成功 WR 被重复重试、worker 停滞或额度无法归还 |
| Jetty 数不一致握手 | 两端配置不一致时连接被拒绝，并明确提示检查 `MC_NUM_JETTY_PER_EP` | 配置不一致仍建立连接，或拒绝信息无法定位配置项 |
| window 容量验证 | 每队列 posted WR 不超过该 Jetty/QP 可用深度，JFC/CQ 总 outstanding 不超过其容量；报告 bytes 使用实际 slice 长度求和 | 计数超过任一容量、日志与队列计数不一致或实际 slice bytes 无法对账 |
| 17 MB/35 MB window 假设 | 若 burst bytes 随实际 `posted WR × slice length` 和 Jetty/QP 数变化，且受 JFC/CQ 上限约束，则该假设得到支持 | 实验数据不随 window 变化，则该假设被证伪，转查 slice 供给、JFC/CQ、worker 和 provider；这属于假设未成立，不等同于代码功能失败 |
| 小 slice 与页面/batch 扫描 | 每种粒度均完成固定负载，记录吞吐、WR/s、CQE/s、时延和错误数；性能提升需在 5 次重复中超过基线的运行波动且无正确性或时延退化 | 任一配置出现正确性错误；或目标是性能提升但中位吞吐未超过基线波动范围，则记为“性能目标未通过”，不将其写成代码功能失败 |
| 20 ms 成因定位 | 时间戳能稳定落入 worker 调度、completion 可见性、credit 归还、Mooncake 队列/post 控制或设备/网络背压中的具体路径，并有重复样本支持 | 仅有 3 ms 粗采样、时钟未对齐、关键事件缺失或观测互相矛盾；记为“定位未通过”，补采时间线后再判断 |

最终报告分别给出**代码功能状态**、**window 假设结论**、**性能目标状态**和**20 ms 定位状态**。假设被证伪时保留实验数据并进入下一排查项；不得把“实验有效但假设不成立”写成代码测试失败，也不得把 mock 测试结果作为真实硬件带宽结论。

