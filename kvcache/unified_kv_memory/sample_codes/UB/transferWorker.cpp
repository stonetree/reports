void UbWorkerPool::transferWorker(int thread_id) {
    bindToSocket(numa_socket_id_);
    const static uint64_t kWaitPeriodInNano = 100000000;  // 100ms
    uint64_t last_wait_ts = getCurrentTimeInNano();

    // ================== 【自适应 Worker 打点状态机】 ==================
    static std::atomic<int> active_thread_id{-1}; // 动态记录真正承载流量的线程号
    static uint64_t t0_before_post = 0;           // T0: 第一次 Post 前
    static uint64_t t1_after_post  = 0;           // T1: 第一次 Post 结束
    static uint64_t t2_all_done    = 0;           // T2: 全部完成
    static std::atomic<bool> has_started{false};
    static std::atomic<bool> post_completed{false};
    static std::atomic<bool> report_logged{false};
    // =================================================================

    while (workers_running_.load(std::memory_order_relaxed)) {
        auto processed_slice_count =
            processed_slice_count_.load(std::memory_order_relaxed);
        auto submitted_slice_count =
            submitted_slice_count_.load(std::memory_order_relaxed);

        // 【打点 3：全部 1024 个切片处理完成 (T2)】
        // 只有此前真正承载流量的 active 线程负责记录和输出
        if (active_thread_id.load(std::memory_order_relaxed) == thread_id &&
            post_completed.load(std::memory_order_relaxed) &&
            !report_logged.load(std::memory_order_relaxed) &&
            processed_slice_count == submitted_slice_count && submitted_slice_count > 0) {
            
            t2_all_done = getCurrentTimeInNano();
            report_logged.store(true, std::memory_order_relaxed);

            double post_duration_ms = (t1_after_post - t0_before_post) / 1e6;
            double poll_duration_ms = (t2_all_done - t1_after_post) / 1e6;
            double total_duration_s = (t2_all_done - t0_before_post) / 1e9;

            double total_data_gb = (1024.0 * 1.75) / 1024.0; // 1.75 GB
            double real_bandwidth = total_data_gb / total_duration_s;

            // 通过 vLLM LOG(INFO) 打印完整报告，并显示被选中的是哪个 Worker 线程
            LOG(INFO) << "\n==================== Mooncake Transfer Benchmark ====================\n"
                      << "  Active Worker TID : Thread " << thread_id << " (承载了全部 1024 切片)\n"
                      << "  Total Slices      : 1024 (" << total_data_gb << " GB)\n"
                      << "  T0 -> T1 [Post]   : " << post_duration_ms << " ms (CPU 组包敲门铃耗时)\n"
                      << "  T1 -> T2 [Poll]   : " << poll_duration_ms << " ms (网卡纯线速在途与收割耗时)\n"
                      << "  T0 -> T2 [Total]  : " << total_duration_s << " s (端到端全流程总耗时)\n"
                      << "  >>> Real Wire Bandwidth: " << real_bandwidth << " GB/s <<<\n"
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

        // 动态探测：检查当前线程负责的 Shard 中是否有待发数据
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
        // 发现数据命中自己负责的分片，原子性夺得 active_thread 归属权并打点
        if (has_pending_slices && !has_started.exchange(true)) {
            active_thread_id.store(thread_id, std::memory_order_relaxed);
            t0_before_post = getCurrentTimeInNano();
        }

        performPostSend(thread_id);

        // 【打点 2：第一次 Post 刚刚结束 (T1)】
        if (active_thread_id.load(std::memory_order_relaxed) == thread_id &&
            !post_completed.exchange(true)) {
            t1_after_post = getCurrentTimeInNano();
        }

#ifndef USE_FAKE_POST_SEND
        performPoll(thread_id);
#endif
    }
}