// view_guard.cc - ViewGuard 租约校验与故障隔离实现
#include "view_guard.h"
#include <iostream>

ViewLease DirectViewGuard::create_lease(uint64_t object_id, uint64_t addr, uint32_t size, uint32_t duration_ms) {
    ViewLease lease;
    lease.object_id = object_id;
    lease.remote_addr = addr;
    lease.size_bytes = size;
    lease.expire_time = std::chrono::steady_clock::now() + std::chrono::milliseconds(duration_ms);
    lease.is_valid.store(true);
    return lease;
}

bool DirectViewGuard::validate_access(const ViewLease& lease) {
    if (!lease.is_valid.load(std::memory_order_relaxed)) {
        return false;
    }
    if (std::chrono::steady_clock::now() > lease.expire_time) {
        return false;
    }
    return true;
}

void DirectViewGuard::revoke_lease(ViewLease& lease) {
    lease.is_valid.store(false, std::memory_order_release);
}

bool DirectViewGuard::handle_remote_crash_fallback(uint64_t object_id) {
    // DEMO 接口占位：尚未安装 SIGBUS handler，也未与推理服务回退事件闭环。
    std::cout << "[ViewGuard][DEMO_ONLY] fallback hook requested for object " << object_id
              << "; no SIGBUS capture evidence was produced.\n";
    return false;
}
