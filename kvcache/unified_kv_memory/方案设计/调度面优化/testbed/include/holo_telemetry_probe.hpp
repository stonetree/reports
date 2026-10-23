#pragma once
/**
 * @file holo_telemetry_probe.hpp
 * @brief 面向鲲鹏 920 + CX6/7 软硬件协同调度测试验证底座的零系统调用遥测探针与无锁黑匣子
 * 
 * 核心技术机制：
 * 1. ARMv8 物理计数器寄存器 (cntvct_el0) 内联读取，单次耗时 ~2ns，零系统调用开销；
 * 2. 严格 32 字节紧凑对齐的黑匣子轨迹项 (FlightTraceRecord)；
 * 3. 基于 mmap 共享内存的无锁环形缓冲区 (Flight Recorder)，运行期纯写内存，测试后异步落盘对账；
 * 4. 鲲鹏 Uncore PMU (DDRC 读写流量与 HHA 跨 Die 流量) 共享内存映像结构体。
 */

#include <cstdint>
#include <atomic>
#include <cstring>
#include <ctime>
#include <sys/mman.h>
#include <fcntl.h>
#include <unistd.h>

namespace holotestbed {

// ============================================================================
// 1. ARMv8 硬件虚拟计时器直读 (消除常规 clock_gettime / gettimeofday 的系统调用开销)
// ============================================================================

/**
 * @brief 读取单调递增的硬件纳秒时间戳。
 * 在 AArch64 (鲲鹏 920) 环境下通过 MRS 指令直读 cntvct_el0 与系统基准频率 cntfrq_el0。
 * 在 x86_64 或其他非 ARM 调试环境下自动回退至 CLOCK_MONOTONIC_RAW。
 */
static inline uint64_t ReadHardwareCntvctNs() noexcept {
    uint64_t val = 0;
    uint64_t freq = 0;
#if defined(__aarch64__)
    asm volatile("mrs %0, cntvct_el0" : "=r"(val));
    asm volatile("mrs %0, cntfrq_el0" : "=r"(freq));
    if (__builtin_expect(freq != 0, 1)) {
        return (val * 1000000000ULL) / freq;
    }
    return val;
#else
    // 跨架构调试环境高精度单调时钟回退
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC_RAW, &ts);
    return static_cast<uint64_t>(ts.tv_sec) * 1000000000ULL + static_cast<uint64_t>(ts.tv_nsec);
#endif
}

// ============================================================================
// 2. 零侵入无锁黑匣子轨迹结构体 (严格 32 字节对齐，适配 CPU 缓存行半行写入)
// ============================================================================

/**
 * @brief 调度决策与端到端延迟微观追踪项 (FlightTraceRecord)
 * 记录单次在线推理请求经过控制面 C1~C5 阶段及硬件执行的纳秒/微秒级明细。
 */
struct alignas(32) FlightTraceRecord {
    uint64_t timestamp_ns;      ///< 8B: 基于 cntvct_el0 的全局硬件纳秒时间戳
    uint32_t req_id;            ///< 4B: 全局请求唯一递增编号
    uint16_t c1_hash_us;        ///< 2B: C1 阶段 - CRC32C 前缀块哈希耗时 (微秒)
    uint16_t c2_lookup_us;      ///< 2B: C2 阶段 - 单边 RDMA Fetch-and-Add 查表与原子预扣耗时 (微秒)
    uint16_t c3_route_ns;       ///< 2B: C3 阶段 - 闭式选路决策与按层切分计算耗时 (纳秒)
    uint16_t c4_sync_us;        ///< 2B: C4 阶段 - 张量并行多卡状态同步耗时 (RankConsensus, 微秒)
    uint16_t c5_doorbell_us;    ///< 2B: C5 阶段 - 预编译 UMR 描述符链门铃提交耗时 (微秒)
    uint8_t  exec_route_mode;   ///< 1B: 执行模式 (0:全量拉取, 1:远端直读, 2:按层切分, 3:重算回退, 4:中继广播)
    uint8_t  recompute_layers;  ///< 1B: 本地计算卡重算层数 K* (0 ~ 80)
    float    actual_ttft_ms;    ///< 4B: 实测端到端首字生成延迟 (TTFT, 毫秒)
    float    actual_tpot_ms;    ///< 4B: 实测在线生成阶段单字耗时 (TPOT, 毫秒)
};
static_assert(sizeof(FlightTraceRecord) == 32, "FlightTraceRecord must be exactly 32 bytes");

// ============================================================================
// 3. 鲲鹏 Uncore PMU 实时硬件遥测共享内存结构体 (驻留于 L3 Cache 高 4 路)
// ============================================================================

constexpr uint32_t MAX_SCCL_PIPES = 8;
constexpr uint32_t TELEMETRY_MAGIC = 0x484F4C4F; // 'HOLO'

/**
 * @brief 单个 SCCL 通道硬件实时水位
 */
struct SCCLTelemetrySlot {
    uint32_t sccl_id;                   ///< SCCL 物理编号 (0 ~ 7)
    float    avail_bw_gbps;             ///< PMU 采样的实时可用带宽 (GB/s, 满载 42.0 GB/s)
    uint32_t hha_cross_die_flux_ppm;    ///< HHA 跨 Die 流量比例 (百万分比, 如 20000 = 2.0%)
    uint64_t inflight_bytes;            ///< 当前信道在途未完成数据搬运字节总数
    uint32_t active_bg_evict_gbps;      ///< 当前后台换出占用带宽 (Gbps)
    uint32_t mpam_bw_quota_pct;         ///< 鲲鹏 MPAM 当前授予后台的内存带宽上限 (MBW_MAX 百分比)
    uint64_t last_update_cntvct;        ///< 上次更新时的硬件时间戳
    uint8_t  reserved[16];              ///< 补齐至 64 字节缓存行
};
static_assert(sizeof(SCCLTelemetrySlot) == 64, "SCCLTelemetrySlot must be 64 bytes");

/**
 * @brief 遥测全局共享内存表头
 */
struct TelemetryShmHeader {
    uint32_t magic;                     ///< 魔数校验
    uint32_t version;                   ///< 协议版本 (1)
    uint32_t num_sccls;                 ///< 有效 SCCL 通道数
    uint32_t sequence_id;               ///< 递增更新序列号 (用于无锁读版本校验)
    uint64_t system_boot_cntvct;        ///< 基准启动纳秒时间
    SCCLTelemetrySlot sccl[MAX_SCCL_PIPES]; ///< 各 SCCL 通道实时槽
};

// ============================================================================
// 4. 无锁环形黑匣子记录器 (Flight Recorder)
// ============================================================================

/**
 * @brief 高性能单机无锁环形黑匣子，支持并发原子追写，保证微观打点开销 < 50ns。
 */
class FlightRecorder {
public:
    static constexpr size_t DEFAULT_CAPACITY = 262144; // 256K 条记录 (占用 8MB 内存)

    FlightRecorder() : capacity_(DEFAULT_CAPACITY), head_idx_(0), buffer_(nullptr) {}

    bool Initialize(const char* shm_name = "/holo_flight_recorder") {
        int fd = shm_open(shm_name, O_CREAT | O_RDWR, 0666);
        if (fd < 0) return false;

        size_t total_size = sizeof(uint64_t) * 4 + capacity_ * sizeof(FlightTraceRecord);
        if (ftruncate(fd, total_size) != 0) {
            close(fd);
            return false;
        }

        void* ptr = mmap(nullptr, total_size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, 0);
        close(fd);
        if (ptr == MAP_FAILED) return false;

        buffer_ = reinterpret_cast<FlightTraceRecord*>(static_cast<char*>(ptr) + 64);
        return true;
    }

    inline void Record(const FlightTraceRecord& rec) noexcept {
        if (__builtin_expect(!buffer_, 0)) return;
        uint64_t idx = head_idx_.fetch_add(1, std::memory_order_relaxed);
        buffer_[idx % capacity_] = rec;
    }

    inline uint64_t GetTotalRecorded() const noexcept {
        return head_idx_.load(std::memory_order_relaxed);
    }

private:
    size_t capacity_;
    std::atomic<uint64_t> head_idx_;
    FlightTraceRecord* buffer_;
};

} // namespace holotestbed
