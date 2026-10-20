#include "control_plane_probe.h"
#include <atomic>
#include <cstdio>
#include <cstring>
#include <chrono>

#if defined(__aarch64__)
static inline uint64_t read_hardware_counter() {
    uint64_t val;
    asm volatile("mrs %0, cntvct_el0" : "=r"(val));
    return val;
}

static inline uint64_t read_hardware_frequency() {
    uint64_t freq;
    asm volatile("mrs %0, cntfrq_el0" : "=r"(freq));
    return freq;
}
#elif defined(_MSC_VER) || defined(__x86_64__) || defined(_M_X64)
#include <chrono>
static inline uint64_t read_hardware_counter() {
    auto now = std::chrono::steady_clock::now().time_since_epoch();
    return std::chrono::duration_cast<std::chrono::nanoseconds>(now).count();
}

static inline uint64_t read_hardware_frequency() {
    return 1000000000ULL; // 1 GHz 虚拟时钟
}
#else
static inline uint64_t read_hardware_counter() {
    auto now = std::chrono::steady_clock::now().time_since_epoch();
    return std::chrono::duration_cast<std::chrono::nanoseconds>(now).count();
}

static inline uint64_t read_hardware_frequency() {
    return 1000000000ULL;
}
#endif

// 环形缓冲区配置：400 万槽位 (4194304)
constexpr size_t RING_BUFFER_SIZE = 1024 * 1024 * 4;
constexpr size_t RING_MASK = RING_BUFFER_SIZE - 1;

static TraceEventRecord g_event_ring[RING_BUFFER_SIZE];
static std::atomic<size_t> g_ring_head{0};
static uint64_t g_timer_freq = 0;

// 各阶段可编程注入延迟配置 (毫秒)
static std::atomic<double> g_stage_delays[8];

extern "C" {

void init_probe_subsystem(void) {
    g_timer_freq = read_hardware_frequency();
    if (g_timer_freq == 0) {
        g_timer_freq = 100000000ULL; // 默认 100MHz
    }
    for (int i = 0; i < 8; ++i) {
        g_stage_delays[i].store(0.0, std::memory_order_relaxed);
    }
    g_ring_head.store(0, std::memory_order_relaxed);
    std::memset(g_event_ring, 0, sizeof(g_event_ring));
}

void inject_delay_ns(uint64_t target_delay_ns) {
    if (target_delay_ns == 0) return;
    if (g_timer_freq == 0) {
        g_timer_freq = read_hardware_frequency();
    }
    uint64_t start_cycles = read_hardware_counter();
    uint64_t target_cycles = (target_delay_ns * g_timer_freq) / 1000000000ULL;
    if (target_cycles == 0) target_cycles = 1;

    while ((read_hardware_counter() - start_cycles) < target_cycles) {
#if defined(__aarch64__)
        asm volatile("yield" ::: "memory");
#endif
    }
}

void set_stage_delay(int stage_id, double delay_ms) {
    if (stage_id >= 0 && stage_id < 8) {
        g_stage_delays[stage_id].store(delay_ms, std::memory_order_relaxed);
    }
}

double get_stage_delay(int stage_id) {
    if (stage_id >= 0 && stage_id < 8) {
        return g_stage_delays[stage_id].load(std::memory_order_relaxed);
    }
    return 0.0;
}

void inject_stage_delay(int stage_id) {
    if (stage_id >= 0 && stage_id < 8) {
        double d_ms = g_stage_delays[stage_id].load(std::memory_order_relaxed);
        if (d_ms > 0.0001) {
            inject_delay_ns(static_cast<uint64_t>(d_ms * 1e6));
        }
    }
}

void record_probe_event(uint32_t req_id, uint16_t stage, uint16_t flags, float val, uint32_t bytes) {
    uint64_t t = read_hardware_counter();
    size_t idx = g_ring_head.fetch_add(1, std::memory_order_relaxed) & RING_MASK;
    g_event_ring[idx] = {t, req_id, stage, flags, val, bytes};
}

size_t get_ring_buffer_head(void) {
    return g_ring_head.load(std::memory_order_relaxed);
}

int dump_ring_buffer(const char* filepath) {
    FILE* fp = std::fopen(filepath, "wb");
    if (!fp) return -1;

    size_t head = g_ring_head.load(std::memory_order_relaxed);
    size_t total_records = (head > RING_BUFFER_SIZE) ? RING_BUFFER_SIZE : head;
    size_t start_idx = (head > RING_BUFFER_SIZE) ? (head & RING_MASK) : 0;

    // 写入文件头：[Magic 4B: "KVEV"][TotalCount 8B][TimerFreq 8B]
    const char magic[4] = {'K', 'V', 'E', 'V'};
    uint64_t count_u64 = static_cast<uint64_t>(total_records);
    uint64_t freq_u64 = g_timer_freq;

    std::fwrite(magic, 1, 4, fp);
    std::fwrite(&count_u64, sizeof(uint64_t), 1, fp);
    std::fwrite(&freq_u64, sizeof(uint64_t), 1, fp);

    if (head <= RING_BUFFER_SIZE) {
        std::fwrite(g_event_ring, sizeof(TraceEventRecord), total_records, fp);
    } else {
        size_t first_part = RING_BUFFER_SIZE - start_idx;
        std::fwrite(&g_event_ring[start_idx], sizeof(TraceEventRecord), first_part, fp);
        if (start_idx > 0) {
            std::fwrite(&g_event_ring[0], sizeof(TraceEventRecord), start_idx, fp);
        }
    }

    std::fclose(fp);
    return 0;
}

void reset_ring_buffer(void) {
    g_ring_head.store(0, std::memory_order_relaxed);
    std::memset(g_event_ring, 0, sizeof(g_event_ring));
}

} // extern "C"
