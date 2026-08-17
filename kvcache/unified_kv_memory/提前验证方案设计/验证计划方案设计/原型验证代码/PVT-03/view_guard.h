// view_guard.h - ViewGuard 租约管理与异常崩溃隔离头文件
#ifndef VIEW_GUARD_H
#define VIEW_GUARD_H

#include <cstdint>
#include <string>
#include <chrono>
#include <atomic>
#include <csignal>

struct ViewLease {
    uint64_t object_id;
    uint64_t remote_addr;
    uint32_t size_bytes;
    std::chrono::time_point<std::chrono::steady_clock> expire_time;
    std::atomic<bool> is_valid{true};
};

class DirectViewGuard {
public:
    DirectViewGuard() = default;

    // 分配租约 (默认 50ms)
    ViewLease create_lease(uint64_t object_id, uint64_t addr, uint32_t size, uint32_t duration_ms = 50);

    // 访问前校验 (微秒级)
    bool validate_access(const ViewLease& lease);

    // 撤销租约
    void revoke_lease(ViewLease& lease);

    // 模拟捕获源节点崩溃信号并执行安全回滚 Fallback
    static bool handle_remote_crash_fallback(uint64_t object_id);
};

#endif // VIEW_GUARD_H
