void UbWorkerPool::transferWorker(int thread_id) {
    // ... 原有初始化代码不变 ...
    bindToSocket(numa_socket_id_);
    const static uint64_t kWaitPeriodInNano = 100000000;  // 100ms
    uint64_t last_wait_ts = getCurrentTimeInNano();

    // ================== 【新增：单次压测打点状态机】 ==================
    static uint64_t t0_before_post = 0; // 第一次 Post 前的时间戳
    static uint64_t t1_after_post  = 0; // 第一次 Post 后的时间戳
    static uint64_t t2_all_done    = 0; // 全部 1024 个切片完成的时间戳
    static bool has_started    = false; // 标记是否已经启动
    static bool post_completed = false; // 标记第一次 Post 是否已经结束
    static bool report_printed = false; // 标记是否已经打印过最终报告
    // ==================================================================

    while (workers_running_.load(std::memory_order_relaxed)) {
        auto processed_slice_count =
            processed_slice_count_.load(std::memory_order_relaxed);
        auto submitted_slice_count =
            submitted_slice_count_.load(std::memory_order_relaxed);

        // 【打点 3：全部处理完成的时刻 T2】
        // 条件：之前已经 Post 完，当前已处理数追平提交数，且只打印一次报告
        if (thread_id == 0 && post_completed && !report_printed &&
            processed_slice_count == submitted_slice_count && submitted_slice_count > 0) {
            
            t2_all_done = getCurrentTimeInNano();
            report_printed = true;

            // 耗时计算 (毫秒 / 秒)
            double post_duration_ms = (t1_after_post - t0_before_post) / 1e6;
            double poll_duration_ms = (t2_all_done - t1_after_post) / 1e6;
            double total_duration_s = (t2_all_done - t0_before_post) / 1e9;

            // 数据量计算：1024 个切片，每个 1.75 MB
            double total_data_gb = (1024.0 * 1.75) / 1024.0; // 1.75 GB
            double real_bandwidth = total_data_gb / total_duration_s;

            // 一次性打印完整的性能报告（对传输过程 0 干扰）
            printf("\n==================== Mooncake Transfer Benchmark ====================\n");
            printf("Total Slices      : 1024 (%.2f GB)\n", total_data_gb);
            printf("T0 -> T1 [Post]   : %.3f ms (CPU 组织描述符与敲门铃开销)\n", post_duration_ms);
            printf("T1 -> T2 [Poll]   : %.3f ms (物理网卡纯在途传输与收割耗时)\n", poll_duration_ms);
            printf("T0 -> T2 [Total]  : %.4f s (端到端总时间)\n", total_duration_s);
            printf(">>> Real Wire Bandwidth: %.2f GB/s <<<\n", real_bandwidth);
            printf("=====================================================================\n\n");
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
        // 条件：上层刚刚提交了切片 (submitted > 0)，且从未启动过
        if (thread_id == 0 && !has_started && submitted_slice_count > 0) {
            t0_before_post = getCurrentTimeInNano();
            has_started = true;
        }

        performPostSend(thread_id);

        // 【打点 2：第一次 Post 刚刚结束返回的时刻 T1】
        // 条件：已经启动，且尚未标记 Post 结束
        if (thread_id == 0 && has_started && !post_completed) {
            t1_after_post = getCurrentTimeInNano();
            post_completed = true;
        }

#ifndef USE_FAKE_POST_SEND
        performPoll(thread_id);
#endif
    }
}