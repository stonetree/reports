void UbWorkerPool::transferWorker(int thread_id) {
    // ... 原有初始化与绑核代码不变 ...
    bindToSocket(numa_socket_id_);
    const static uint64_t kWaitPeriodInNano = 100000000;  // 100ms
    uint64_t last_wait_ts = getCurrentTimeInNano();

    // ================== 【单次传输打点状态机】 ==================
    static uint64_t t0_before_post = 0; // T0: 第一次 Post 前的时间戳
    static uint64_t t1_after_post  = 0; // T1: 第一次 Post 后的时间戳
    static uint64_t t2_all_done    = 0; // T2: 全部切片完成的时间戳
    static bool has_started    = false; // 是否已经启动
    static bool post_completed = false; // 第一次 Post 是否已完成
    static bool report_logged  = false; // 是否已打印过日志
    // ==========================================================

    while (workers_running_.load(std::memory_order_relaxed)) {
        auto processed_slice_count =
            processed_slice_count_.load(std::memory_order_relaxed);
        auto submitted_slice_count =
            submitted_slice_count_.load(std::memory_order_relaxed);

        // 【打点 3：全部 1024 个切片处理完成的瞬间 T2】
        if (thread_id == 0 && post_completed && !report_logged &&
            processed_slice_count == submitted_slice_count && submitted_slice_count > 0) {
            
            t2_all_done = getCurrentTimeInNano();
            report_logged = true;

            // 耗时换算 (毫秒 / 秒)
            double post_duration_ms = (t1_after_post - t0_before_post) / 1e6;
            double poll_duration_ms = (t2_all_done - t1_after_post) / 1e6;
            double total_duration_s = (t2_all_done - t0_before_post) / 1e9;

            // 数据量计算：1024 个切片，每个 1.75 MB
            double total_data_gb = (1024.0 * 1.75) / 1024.0; // 1.75 GB
            double real_bandwidth = total_data_gb / total_duration_s;

            // 使用 vLLM / Mooncake 原生 LOG(INFO) 输出
            LOG(INFO) << "\n==================== Mooncake Transfer Benchmark ====================\n"
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

        // 【打点 1：第一次 Post 刚刚开始之前的时刻 T0】
        if (thread_id == 0 && !has_started && submitted_slice_count > 0) {
            t0_before_post = getCurrentTimeInNano();
            has_started = true;
        }

        performPostSend(thread_id);

        // 【打点 2：第一次 Post 刚刚执行结束的时刻 T1】
        if (thread_id == 0 && has_started && !post_completed) {
            t1_after_post = getCurrentTimeInNano();
            post_completed = true;
        }

#ifndef USE_FAKE_POST_SEND
        performPoll(thread_id);
#endif
    }
}