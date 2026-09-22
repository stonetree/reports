void UbWorkerPool::transferWorker(int thread_id) {
    bindToSocket(numa_socket_id_);
    const static uint64_t kWaitPeriodInNano = 100000000;  // 100ms
    uint64_t last_wait_ts = getCurrentTimeInNano();

    // ================== 【增强版打点状态机】 ==================
    static std::atomic<int> active_thread_id{-1};
    static uint64_t t0_before_post        = 0; // T0: 第一次 Post 之前
    static uint64_t t1_after_post         = 0; // T1: 第一次 Post 结束
    static uint64_t t1_5_after_first_poll = 0; // T1.5: 第一次 Poll 结束
    static uint64_t t2_all_done           = 0; // T2: 全部 1024 切片完成
    static uint64_t first_poll_processed  = 0; // 第一次 Poll 结束后已完成的切片数

    static std::atomic<bool> has_started{false};
    static std::atomic<bool> post_completed{false};
    static std::atomic<bool> first_poll_completed{false};
    static std::atomic<bool> report_logged{false};
    // =========================================================

    while (workers_running_.load(std::memory_order_relaxed)) {
        auto processed_slice_count =
            processed_slice_count_.load(std::memory_order_relaxed);
        auto submitted_slice_count =
            submitted_slice_count_.load(std::memory_order_relaxed);

        // 【打点 4：全部 1024 个切片处理完成 (T2)】
        if (active_thread_id.load(std::memory_order_relaxed) == thread_id &&
            first_poll_completed.load(std::memory_order_relaxed) &&
            !report_logged.load(std::memory_order_relaxed) &&
            processed_slice_count == submitted_slice_count && submitted_slice_count > 0) {
            
            t2_all_done = getCurrentTimeInNano();
            report_logged.store(true, std::memory_order_relaxed);

            // 1. 各阶段耗时换算
            double post_duration_ms   = (t1_after_post - t0_before_post) / 1e6;
            double first_poll_ms      = (t1_5_after_first_poll - t1_after_post) / 1e6;
            double steady_poll_ms     = (t2_all_done - t1_5_after_first_poll) / 1e6;
            double total_duration_s   = (t2_all_done - t0_before_post) / 1e9;
            double steady_duration_s  = (t2_all_done - t1_5_after_first_poll) / 1e9;

            // 2. 总数据量与端到端全量带宽
            double total_data_gb = (1024.0 * 1.75) / 1024.0; // 1.75 GB
            double overall_bandwidth = total_data_gb / total_duration_s;

            // 3. 稳态传输数据量与纯稳态线速带宽 (排除首批启动延迟)
            uint64_t steady_slices = (1024 > first_poll_processed) ? (1024 - first_poll_processed) : 0;
            double steady_data_gb = (steady_slices * 1.75) / 1024.0;
            double steady_bandwidth = (steady_duration_s > 0) ? (steady_data_gb / steady_duration_s) : 0.0;

            // 格式化输出最终测试报表
            LOG(INFO) << "\n==================== Mooncake Transfer Benchmark ====================\n"
                      << "  Active Worker TID     : Thread " << thread_id << " (承载全量流量)\n"
                      << "  Total Slices          : 1024 (" << total_data_gb << " GB)\n"
                      << "  First Poll Completed  : " << first_poll_processed << " slices (首次收割量)\n"
                      << "  Steady Remaining      : " << steady_slices << " slices (" << steady_data_gb << " GB)\n"
                      << "  -------------------------------------------------------------------\n"
                      << "  T0   -> T1   [Post]   : " << post_duration_ms << " ms (CPU 组包敲门铃耗时)\n"
                      << "  T1   -> T1.5 [1stPoll]: " << first_poll_ms << " ms (首次收割及启动耗时)\n"
                      << "  T1.5 -> T2   [Steady] : " << steady_poll_ms << " ms (后续稳态纯传输耗时)\n"
                      << "  T0   -> T2   [Total]  : " << total_duration_s << " s (全流程总耗时)\n"
                      << "  -------------------------------------------------------------------\n"
                      << "  >>> Overall Bandwidth (T0   -> T2) : " << overall_bandwidth << " GB/s <<<\n"
                      << "  >>> Steady  Bandwidth (T1.5 -> T2) : " << steady_bandwidth << " GB/s <<<\n"
                      << "=====================================================================";
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

        // 动态探测命中的 Shard 属于哪个线程
        bool has_pending_slices = false;
        if (!has_started.load(std::memory_order_relaxed)) {
            for (int s = thread_id; s < kShardCount; s += kTransferWorkerCount) {
                if (slice_queue_count_[s].load(std::memory_order_relaxed) > 0) {
                    has_pending_slices = true;
                    break;
                }
            }
        }

        // 【打点 1：第一次 Post 之前 (T0)】
        if (has_pending_slices && !has_started.exchange(true)) {
            active_thread_id.store(thread_id, std::memory_order_relaxed);
            t0_before_post = getCurrentTimeInNano();
        }

        performPostSend(thread_id);

        // 【打点 2：第一次 Post 刚结束 (T1)】
        if (active_thread_id.load(std::memory_order_relaxed) == thread_id &&
            !post_completed.exchange(true)) {
            t1_after_post = getCurrentTimeInNano();
        }

#ifndef USE_FAKE_POST_SEND
        performPoll(thread_id);

        // 【打点 3：第一次 Poll 刚结束 (T1.5) 及读取当时的 processed_slice_num】
        if (active_thread_id.load(std::memory_order_relaxed) == thread_id &&
            post_completed.load(std::memory_order_relaxed) &&
            !first_poll_completed.exchange(true)) {
            t1_5_after_first_poll = getCurrentTimeInNano();
            first_poll_processed = processed_slice_count_.load(std::memory_order_relaxed);
        }
#endif
    }
}