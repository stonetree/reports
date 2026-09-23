void UbWorkerPool::transferWorker(int thread_id) {
    bindToSocket(numa_socket_id_);
    const static uint64_t kWaitPeriodInNano = 100000000;  // 100ms
    uint64_t last_wait_ts = getCurrentTimeInNano();

    // ================== 【双轨载荷性能诊断打点状态机】 ==================
    static std::atomic<int> active_thread_id{-1};
    static uint64_t t0_before_post        = 0; // T0: 提交前
    static uint64_t t1_after_post         = 0; // T1: 第一次 Post 结束
    static uint64_t t1_5_after_first_poll = 0; // T1.5: 首批完成包收割结束
    static uint64_t t2_all_done           = 0; // T2: 全部完成
    static uint64_t first_poll_processed  = 0; // 首批完成的切片数

    // 真实底层字节采集变量
    static uint64_t actual_total_bytes        = 0; // 底层所有 slice->length 的真实累加总字节
    static uint64_t actual_single_slice_bytes = 0; // 采集到的单个 slice 真实字节大小

    static std::atomic<bool> has_started{false};
    static std::atomic<bool> post_completed{false};
    static std::atomic<bool> first_poll_completed{false};
    static std::atomic<bool> report_logged{false};
    // ===================================================================

    while (workers_running_.load(std::memory_order_relaxed)) {
        auto processed_slice_count =
            processed_slice_count_.load(std::memory_order_relaxed);
        auto submitted_slice_count =
            submitted_slice_count_.load(std::memory_order_relaxed);

        // 【T2 触发：全部 1024 个切片处理完成】
        if (active_thread_id.load(std::memory_order_relaxed) == thread_id &&
            first_poll_completed.load(std::memory_order_relaxed) &&
            !report_logged.load(std::memory_order_relaxed) &&
            processed_slice_count == submitted_slice_count && submitted_slice_count > 0) {
            
            t2_all_done = getCurrentTimeInNano();
            report_logged.store(true, std::memory_order_relaxed);

            // 1. 各阶段耗时换算
            double dt_post_s       = (t1_after_post - t0_before_post) / 1e9;
            double dt_startup_s    = (t1_5_after_first_poll - t1_after_post) / 1e9;
            double dt_steady_s     = (t2_all_done - t1_5_after_first_poll) / 1e9;
            double dt_e2e_s        = (t2_all_done - t0_before_post) / 1e9;

            // 2. 业务层面预期数据量 (假设 1.75 MiB = 1.75 * 1024 * 1024 字节)
            double app_expected_bytes = 1024.0 * 1.75 * 1024 * 1024;
            double app_e2e_bw         = (app_expected_bytes / 1e9) / dt_e2e_s;

            // 3. Mooncake 传输层实测切片数据量 (以实际采集的 slice->length 累加为准)
            double mc_actual_bytes    = (double)actual_total_bytes;
            double mc_startup_bytes   = (double)first_poll_processed * actual_single_slice_bytes;
            double mc_steady_bytes    = mc_actual_bytes - mc_startup_bytes;

            // 4. Mooncake 实际传输带宽 (GB/s = 10^9 Bytes/s)
            double mc_post_rate       = (mc_actual_bytes / 1e9) / dt_post_s;
            double mc_startup_bw      = (dt_startup_s > 0 && mc_startup_bytes > 0) ? ((mc_startup_bytes / 1e9) / dt_startup_s) : 0.0;
            double mc_steady_bw       = (dt_steady_s > 0 && mc_steady_bytes > 0) ? ((mc_steady_bytes / 1e9) / dt_steady_s) : 0.0;
            double mc_e2e_bw          = (mc_actual_bytes / 1e9) / dt_e2e_s;

            // 5. 传输额外负载分析 (Overhead)
            double overhead_bytes     = mc_actual_bytes - app_expected_bytes;
            double overhead_percent   = (overhead_bytes / app_expected_bytes) * 100.0;
            double bw_diff            = mc_e2e_bw - app_e2e_bw;

            LOG(INFO) << "\n==================== Mooncake Dual-Payload Benchmark ====================\n"
                      << "  [Payload Size Check] : 业务声明单包 = 1.750 MB (1,835,008 B)\n"
                      << "                         MC实际单Slice = " << (actual_single_slice_bytes / 1024.0 / 1024.0) 
                      << " MB (" << actual_single_slice_bytes << " B)\n"
                      << "  [Overhead / Padding] : 总差额 = " << (overhead_bytes / 1024.0) 
                      << " KB (额外开销占比: " << overhead_percent << " %)\n"
                      << "  ----------------------------------------------------------------------------\n"
                      << "  1. 任务提交阶段 (T0 -> T1)     : 耗时 " << (dt_post_s * 1000) << " ms\n"
                      << "     >>> 提交速率 (Post Rate)   : " << mc_post_rate << " GB/s\n"
                      << "  ----------------------------------------------------------------------------\n"
                      << "  2. 启动/注水阶段 (T1 -> T1.5)  : 耗时 " << (dt_startup_s * 1000) << " ms, 完成 " << first_poll_processed << " slices\n"
                      << "     >>> 启动带宽 (Startup BW)  : " << mc_startup_bw << " GB/s\n"
                      << "  ----------------------------------------------------------------------------\n"
                      << "  3. 纯稳态传输阶段 (T1.5 -> T2) : 耗时 " << (dt_steady_s * 1000) << " ms, 完成 " << (1024 - first_poll_processed) << " slices\n"
                      << "     >>> 真实稳态带宽 (Steady BW): " << mc_steady_bw << " GB/s (★物理网卡纯线速极限★)\n"
                      << "  ----------------------------------------------------------------------------\n"
                      << "  4. E2E 业务综合有效带宽对比 (T0 -> T2, 耗时 " << (dt_e2e_s * 1000) << " ms):\n"
                      << "     >>> 业务有效载荷带宽 (App E2E BW) : " << app_e2e_bw << " GB/s (业务看到的有效率)\n"
                      << "     >>> MC传输层载荷带宽 (MC E2E BW)  : " << mc_e2e_bw << " GB/s (物理线路实际载荷)\n"
                      << "     >>> 传输额外负载造成的带宽差额     : " << bw_diff << " GB/s\n"
                      << "==============================================================================";
        }

        if (processed_slice_count == submitted_slice_count) {
            uint64_t curr_wait_ts = getCurrentTimeInNano();
            if (curr_wait_ts - last_wait_ts > kWaitPeriodInNano) {
                std::unique_lock<std::mutex> lock(cond_mutex_);
                suspended_flag_.fetch_add(1);
                cond_var_.wait_for(lock, std::chrono::seconds(1));
                suspended_flag_.fetch_sub(1);
                last_wait_ts = curr_wait_ts;
            }
            continue;
        }

        // 动态探测：检查当前线程负责的 Shard 中是否有待发切片
        bool has_pending_slices = false;
        if (!has_started.load(std::memory_order_relaxed)) {
            for (int s = thread_id; s < kShardCount; s += kTransferWorkerCount) {
                if (slice_queue_count_[s].load(std::memory_order_relaxed) > 0) {
                    has_pending_slices = true;
                    break;
                }
            }
        }

        // 【T0 标记：在首次 Post 前，精准统计切片的实际字节大小】
        if (has_pending_slices && !has_started.exchange(true)) {
            active_thread_id.store(thread_id, std::memory_order_relaxed);

            // 直接遍历当前命中的 Shard，提取每个 slice->length 的真实大小
            uint64_t collected_bytes = 0;
            for (int s = thread_id; s < kShardCount; s += kTransferWorkerCount) {
                if (slice_queue_count_[s].load(std::memory_order_relaxed) > 0) {
                    slice_queue_lock_[s].lock();
                    for (auto& pair : slice_queue_[s]) {
                        for (auto* slice : pair.second) {
                            if (slice) {
                                collected_bytes += slice->length;
                                actual_single_slice_bytes = slice->length; // 记录单个切片实际大小
                            }
                        }
                    }
                    slice_queue_lock_[s].unlock();
                }
            }
            actual_total_bytes = collected_bytes; // 记录 1024 个切片的真实总大小
            t0_before_post = getCurrentTimeInNano();
        }

        performPostSend(thread_id);

        // 【T1 标记：第一次 Post 刚刚结束】
        if (active_thread_id.load(std::memory_order_relaxed) == thread_id &&
            !post_completed.exchange(true)) {
            t1_after_post = getCurrentTimeInNano();
        }

#ifndef USE_FAKE_POST_SEND
        performPoll(thread_id);

        // 【修正后的 T1.5】：只有当硬件真正收割到了第一批切片 (processed > 0) 时才触发打点！
        if (active_thread_id.load(std::memory_order_relaxed) == thread_id &&
            post_completed.load(std::memory_order_relaxed) &&
            processed_slice_count_.load(std::memory_order_relaxed) > 0 &&
            !first_poll_completed.exchange(true)) {
            t1_5_after_first_poll = getCurrentTimeInNano();
            first_poll_processed = processed_slice_count_.load(std::memory_order_relaxed);
        }
#endif
    }
}