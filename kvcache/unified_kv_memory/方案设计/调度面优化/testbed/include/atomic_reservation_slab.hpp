#pragma once
/**
 * @file atomic_reservation_slab.hpp
 * @brief 带租约生命周期与僵尸熔断回收机制的原子在途配额条带 (Atomic Reservation Slab)
 * 
 * 针对专家评审提出之“漏洞一 (僵尸预扣泄漏导致通道永久坍缩)”的软硬件协同防范设计：
 * 1. 模拟 RDMA Fetch-and-Add / UBMEM ldadd 的原子在途配额预扣与实时返还；
 * 2. 引入基于硬件纳秒时钟的租约有效期 (Lease Deadline, 默认 50ms)；
 * 3. 带外守护巡检与自愈机制：当网络丢包、请求崩溃或客户端断开未发送释放时，
 *    巡检器自动识别过期条目并原子冲正对应通道的 inflight_bytes，杜绝虚假拥塞累积。
 */

#include "holo_telemetry_probe.hpp"
#include <atomic>
#include <cstdint>
#include <vector>

namespace holotestbed {

enum class ReservationStatus : uint8_t {
    FREE = 0,
    ACTIVE = 1,
    RELEASED = 2,
    EXPIRED_RECLAIMED = 3
};

/**
 * @brief 单条原子预扣租约明细项 (64B 缓存行对齐)
 */
struct alignas(64) ReservationSlot {
    std::atomic<uint32_t> req_id{0};             ///< 请求唯一编号 (0 表示空闲)
    uint32_t sccl_id{0};                        ///< 目标物理 SCCL 通道编号
    uint64_t reserved_bytes{0};                 ///< 预扣在途数据量 (Bytes)
    uint64_t lease_deadline_ns{0};              ///< 租约硬超时绝对硬件时间戳 (ns)
    std::atomic<ReservationStatus> status{ReservationStatus::FREE}; ///< 当前槽位状态
    uint8_t  padding[39]{0};                    ///< 严格补齐 64 字节
};
static_assert(sizeof(ReservationSlot) == 64, "ReservationSlot must be exactly 64 bytes");

/**
 * @brief 原子在途配额条带管理器
 */
class AtomicReservationSlab {
public:
    static constexpr size_t MAX_CONCURRENT_RESERVATIONS = 16384;
    static constexpr uint64_t DEFAULT_LEASE_TIMEOUT_NS = 50'000'000ULL; // 50ms 默认租约保护

    explicit AtomicReservationSlab(uint32_t num_sccls = 8)
        : num_sccls_(num_sccls),
          reclaimed_count_(0),
          reclaimed_bytes_(0) {
        slots_ = new ReservationSlot[MAX_CONCURRENT_RESERVATIONS];
        channel_inflight_ = new std::atomic<uint64_t>[num_sccls_];
        for (uint32_t i = 0; i < num_sccls_; ++i) {
            channel_inflight_[i].store(0, std::memory_order_relaxed);
        }
    }

    ~AtomicReservationSlab() {
        delete[] slots_;
        delete[] channel_inflight_;
    }

    /**
     * @brief 原子预扣在途配额 (对应 RDMA Fetch-and-Add / UBMEM ldadd 原语)
     * @param sccl_id 目标 SCCL 管道
     * @param req_id 请求 ID
     * @param bytes_to_pull 拟拉取字节数
     * @param lease_timeout_ns 租约超时时间 (纳秒)
     * @return 预扣前排在前面的在途字节总数 (用于闭式排队时间推导)
     */
    uint64_t AcquireReservation(uint32_t sccl_id, uint32_t req_id, uint64_t bytes_to_pull,
                                uint64_t lease_timeout_ns = DEFAULT_LEASE_TIMEOUT_NS) {
        if (sccl_id >= num_sccls_) return 0;

        // 1. 硬件原子递增信道在途未完成总字节数，获取前序排队水位
        uint64_t prev_inflight = channel_inflight_[sccl_id].fetch_add(bytes_to_pull, std::memory_order_acq_rel);

        // 2. 登记租约条目 (线性探测找寻空闲槽位)
        uint64_t now_ns = ReadHardwareCntvctNs();
        size_t start_idx = req_id % MAX_CONCURRENT_RESERVATIONS;
        for (size_t i = 0; i < 256; ++i) {
            size_t idx = (start_idx + i) % MAX_CONCURRENT_RESERVATIONS;
            ReservationStatus expected = ReservationStatus::FREE;
            if (slots_[idx].status.compare_exchange_strong(expected, ReservationStatus::ACTIVE,
                                                           std::memory_order_acquire)) {
                slots_[idx].sccl_id = sccl_id;
                slots_[idx].reserved_bytes = bytes_to_pull;
                slots_[idx].lease_deadline_ns = now_ns + lease_timeout_ns;
                slots_[idx].req_id.store(req_id, std::memory_order_release);
                break;
            }
        }
        return prev_inflight;
    }

    /**
     * @brief 正常释放在途配额 (当 DMA 完成、按层切分减免或主动回退重算时调用)
     */
    void ReleaseReservation(uint32_t sccl_id, uint32_t req_id, uint64_t bytes_released) {
        if (sccl_id >= num_sccls_ || bytes_released == 0) return;

        // 1. 硬件原子冲减对应信道水位 (防止下溢)
        uint64_t curr = channel_inflight_[sccl_id].load(std::memory_order_relaxed);
        while (curr > 0) {
            uint64_t next_val = (curr >= bytes_released) ? (curr - bytes_released) : 0;
            if (channel_inflight_[sccl_id].compare_exchange_weak(curr, next_val, std::memory_order_acq_rel)) {
                break;
            }
        }

        // 2. 将对应条目标记为已释放
        size_t start_idx = req_id % MAX_CONCURRENT_RESERVATIONS;
        for (size_t i = 0; i < 256; ++i) {
            size_t idx = (start_idx + i) % MAX_CONCURRENT_RESERVATIONS;
            if (slots_[idx].req_id.load(std::memory_order_relaxed) == req_id &&
                slots_[idx].status.load(std::memory_order_relaxed) == ReservationStatus::ACTIVE) {
                slots_[idx].status.store(ReservationStatus::FREE, std::memory_order_release);
                slots_[idx].req_id.store(0, std::memory_order_release);
                break;
            }
        }
    }

    /**
     * @brief 带外守护协程调用的僵尸条目回收与通道自愈扫描器
     * @param now_ns 当前硬件时间戳
     * @return 本轮巡检回收的僵尸请求总数
     */
    uint32_t SweepExpiredZombieReservations(uint64_t now_ns) {
        uint32_t reclaimed_in_sweep = 0;
        for (size_t i = 0; i < MAX_CONCURRENT_RESERVATIONS; ++i) {
            if (slots_[i].status.load(std::memory_order_acquire) == ReservationStatus::ACTIVE) {
                if (now_ns > slots_[i].lease_deadline_ns) {
                    // 确认超时，尝试原子抢占回收
                    ReservationStatus expected = ReservationStatus::ACTIVE;
                    if (slots_[i].status.compare_exchange_strong(expected, ReservationStatus::EXPIRED_RECLAIMED,
                                                                 std::memory_order_acq_rel)) {
                        uint32_t sccl_id = slots_[i].sccl_id;
                        uint64_t leak_bytes = slots_[i].reserved_bytes;
                        
                        // 冲减对应通道泄漏的在途字节
                        uint64_t curr = channel_inflight_[sccl_id].load(std::memory_order_relaxed);
                        while (curr > 0) {
                            uint64_t next_val = (curr >= leak_bytes) ? (curr - leak_bytes) : 0;
                            if (channel_inflight_[sccl_id].compare_exchange_weak(curr, next_val, std::memory_order_acq_rel)) {
                                break;
                            }
                        }

                        reclaimed_in_sweep++;
                        reclaimed_count_.fetch_add(1, std::memory_order_relaxed);
                        reclaimed_bytes_.fetch_add(leak_bytes, std::memory_order_relaxed);

                        // 清空条目供后续请求复用
                        slots_[i].req_id.store(0, std::memory_order_release);
                        slots_[i].status.store(ReservationStatus::FREE, std::memory_order_release);
                    }
                }
            }
        }
        return reclaimed_in_sweep;
    }

    inline uint64_t GetChannelInflightBytes(uint32_t sccl_id) const noexcept {
        if (sccl_id >= num_sccls_) return 0;
        return channel_inflight_[sccl_id].load(std::memory_order_relaxed);
    }

    inline uint64_t GetTotalReclaimedCount() const noexcept {
        return reclaimed_count_.load(std::memory_order_relaxed);
    }

    inline uint64_t GetTotalReclaimedBytes() const noexcept {
        return reclaimed_bytes_.load(std::memory_order_relaxed);
    }

private:
    uint32_t num_sccls_;
    ReservationSlot* slots_;
    std::atomic<uint64_t>* channel_inflight_;
    std::atomic<uint64_t> reclaimed_count_;
    std::atomic<uint64_t> reclaimed_bytes_;
};

} // namespace holotestbed
