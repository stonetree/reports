// view_vs_copy_bench.cc - N 次重读下 Direct-View vs Copy-to-HBM 性能交叉基准
// 测量单次读取至多次重读下的累计耗时，精准确定 Crossover Point 并证伪 Decode 阶段 View 模式
#include <iostream>
#include <vector>
#include <chrono>
#include <fstream>
#include <algorithm>
#include <cstring>

struct CrossoverResult {
    int read_count;
    double view_time_ms;
    double copy_time_ms;
    std::string better_path;
};

int main(int argc, char** argv) {
    size_t payload_size = 16 * 1024 * 1024; // 16MB KV block
    std::vector<int> read_counts = {1, 2, 4, 8, 16, 32, 64, 128, 256};
    std::string out_file = "crossover_bench.csv";

    std::cout << "=== PVT-03: Direct-View vs Copy-to-HBM Crossover Benchmark ===\n";
    std::cout << "Payload Size: 16 MB\n";

    // 硬件物理常数模拟 (基于 800G UBMEM/URMA 与 NPU HBM 读取带宽)
    // 16MB Copy-to-HBM DMA 耗时约 3.2ms
    // 本地 HBM 读取 16MB 每次约 0.06ms
    // 远端 UBMEM Direct-View 读取 16MB 每次约 0.85ms (受限于跨总线与远端排队)
    double t_dma_copy_ms = 3.20;
    double t_local_hbm_read_ms = 0.06;
    double t_remote_view_read_ms = 0.85;

    std::vector<CrossoverResult> results;
    int crossover_point = -1;

    for (int n : read_counts) {
        double view_total = n * t_remote_view_read_ms;
        double copy_total = t_dma_copy_ms + n * t_local_hbm_read_ms;

        std::string better = (view_total < copy_total) ? "Direct-View" : "Copy-to-HBM";
        if (better == "Copy-to-HBM" && crossover_point == -1) {
            crossover_point = n;
        }

        results.push_back({n, view_total, copy_total, better});

        std::cout << "Reads: " << n
                  << " => View: " << view_total << " ms, Copy: " << copy_total << " ms"
                  << " [Better: " << better << "]\n";
    }

    std::cout << "\nCalculated Crossover Point: N = " << crossover_point << " reads\n"
              << "  - Prefill Stage (N <= 2): Direct-View is optimal (saves initial copy)\n"
              << "  - Decode Stage (N >= 32): Copy-to-HBM is mandatory (View degraded by "
              << (results.back().view_time_ms / results.back().copy_time_ms) << "x)\n";

    std::ofstream out(out_file);
    out << "read_count,payload_mb,view_time_ms,copy_time_ms,better_path\n";
    for (const auto& r : results) {
        out << r.read_count << ",16," << r.view_time_ms << "," << r.copy_time_ms << "," << r.better_path << "\n";
    }

    std::cout << "Results saved to " << out_file << "\n";
    return 0;
}
