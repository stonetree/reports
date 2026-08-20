// proto_bench.cc - URMA/UBMEM 结果流程脚手架。当前实际执行本地 memcpy，只能标为 DEMO。
#include <iostream>
#include <vector>
#include <string>
#include <chrono>
#include <thread>
#include <atomic>
#include <fstream>
#include <cstring>
#include <cstdlib>
#include <algorithm>

struct BenchConfig {
    std::string protocol = "urma"; // "urma" or "ubmem"
    std::vector<size_t> payload_sizes = {4096, 65536, 262144, 1048576, 4194304, 16777216, 67108864};
    std::vector<int> thread_counts = {1, 4, 16, 32, 64};
    int duration_sec = 10;
    uint64_t iterations_per_thread = 0;
    std::string output_file = "proto_benchmark_results.csv";
};

struct ThreadResult {
    uint64_t total_bytes = 0;
    uint64_t total_ops = 0;
    std::vector<double> latencies_us;
};

// 模拟 UBMEM 单边 Direct 访问 (微秒级) vs URMA 异步 RDMA 传输
void worker_transfer(const std::string& protocol, size_t size, uint64_t iterations,
                     std::atomic<bool>& running, ThreadResult& result) {
    char* src_buf = (char*)malloc(size);
    char* dst_buf = (char*)malloc(size);
    memset(src_buf, 0x5A, size);
    memset(dst_buf, 0x00, size);

    result.latencies_us.reserve(100000);

    while ((iterations > 0 && result.total_ops < iterations) ||
           (iterations == 0 && running.load(std::memory_order_relaxed))) {
        auto t0 = std::chrono::high_resolution_clock::now();

        if (protocol == "ubmem") {
            // UBMEM 硬件单边直达映射 / 共享显存模拟
            memcpy(dst_buf, src_buf, size);
        } else {
            // URMA RDMA Direct 传输模拟 (包含描述符排队与网卡 DMA 传输)
            memcpy(dst_buf, src_buf, size);
            // 模拟 URMA 网络小延迟 (模拟 800G 线速与 1.5us 基础传输开销)
            for (volatile int i = 0; i < 50; ++i);
        }

        auto t1 = std::chrono::high_resolution_clock::now();
        double lat = std::chrono::duration<double, std::micro>(t1 - t0).count();

        result.latencies_us.push_back(lat);
        result.total_bytes += size;
        result.total_ops++;
    }

    free(src_buf);
    free(dst_buf);
}

void run_benchmark_case(const std::string& protocol, size_t size, int num_threads,
                        int duration, uint64_t iterations, std::ofstream& out) {
    std::atomic<bool> running(true);
    std::vector<std::thread> threads;
    std::vector<ThreadResult> results(num_threads);
    const auto case_start = std::chrono::steady_clock::now();

    for (int i = 0; i < num_threads; ++i) {
        threads.emplace_back(worker_transfer, protocol, size, iterations,
                             std::ref(running), std::ref(results[i]));
    }

    if (iterations == 0) {
        std::this_thread::sleep_for(std::chrono::seconds(duration));
        running.store(false, std::memory_order_relaxed);
    }

    for (auto& t : threads) {
        t.join();
    }

    uint64_t total_bytes = 0;
    uint64_t total_ops = 0;
    std::vector<double> all_lats;

    for (const auto& res : results) {
        total_bytes += res.total_bytes;
        total_ops += res.total_ops;
        all_lats.insert(all_lats.end(), res.latencies_us.begin(), res.latencies_us.end());
    }

    const auto case_end = std::chrono::steady_clock::now();
    double total_sec = std::chrono::duration<double>(case_end - case_start).count();
    double bw_gbps = (total_bytes * 8.0) / (total_sec * 1e9);

    std::sort(all_lats.begin(), all_lats.end());
    double lat_avg = 0;
    for (double l : all_lats) lat_avg += l;
    if (!all_lats.empty()) lat_avg /= all_lats.size();

    double lat_p50 = all_lats.empty() ? 0 : all_lats[all_lats.size() * 0.50];
    double lat_p99 = all_lats.empty() ? 0 : all_lats[all_lats.size() * 0.99];

    std::cout << "[" << protocol << "] Size: " << size / 1024 << " KB, Threads: " << num_threads
              << " => BW: " << bw_gbps << " Gbps, Lat Avg: " << lat_avg << " us, P99: " << lat_p99 << " us\n";

    out << protocol << "," << size << "," << num_threads << ",write,"
        << bw_gbps << "," << lat_avg << "," << lat_p50 << "," << lat_p99 << ",DEMO,LOCAL_MEMCPY_ONLY\n";
    out.flush();
}

int main(int argc, char** argv) {
    BenchConfig cfg;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--protocol" && i + 1 < argc) cfg.protocol = argv[++i];
        else if (arg == "--payload-bytes" && i + 1 < argc) cfg.payload_sizes = {std::stoull(argv[++i])};
        else if (arg == "--concurrency" && i + 1 < argc) cfg.thread_counts = {std::stoi(argv[++i])};
        else if (arg == "--duration-sec" && i + 1 < argc) cfg.duration_sec = std::stoi(argv[++i]);
        else if (arg == "--iters" && i + 1 < argc) cfg.iterations_per_thread = std::stoull(argv[++i]);
        else if (arg == "--out" && i + 1 < argc) cfg.output_file = argv[++i];
    }

    std::ofstream out(cfg.output_file);
    out << "protocol,payload_bytes,concurrency,direction,bandwidth_gbps,latency_avg_us,latency_p50_us,latency_p99_us,evidence_level,status\n";

    for (size_t size : cfg.payload_sizes) {
        for (int threads : cfg.thread_counts) {
            run_benchmark_case(cfg.protocol, size, threads, cfg.duration_sec,
                               cfg.iterations_per_thread, out);
        }
    }

    std::cout << "DEMO local memcpy workflow complete; no URMA/UBMEM performance conclusion. output=" << cfg.output_file << "\n";
    return 0;
}
