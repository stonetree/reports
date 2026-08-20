// view_vs_copy_bench.cc - N 次重读下 Direct-View vs Copy-to-HBM 性能交叉基准
// 使用 PVT-01 实测能力输入或 DEMO 参数计算 Crossover；不预置“重读次数>2”结论。
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
    size_t payload_size = 16 * 1024 * 1024;
    std::vector<int> read_counts = {1, 2, 4, 8, 16, 32, 64, 128, 256};
    std::string out_file = "crossover_bench.csv";

    std::cout << "=== PVT-03: Direct-View vs Copy-to-HBM Crossover Benchmark ===\n";
    std::cout << "Payload Size: 16 MB\n";

    double t_dma_copy_ms = 3.20;
    double t_local_hbm_read_ms = 0.06;
    double t_remote_view_read_ms = 0.85;
    std::string evidence_level = "DEMO";
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--payload-mb" && i + 1 < argc) payload_size = std::stoull(argv[++i]) * 1024 * 1024;
        else if (arg == "--dma-copy-ms" && i + 1 < argc) t_dma_copy_ms = std::stod(argv[++i]);
        else if (arg == "--local-read-ms" && i + 1 < argc) t_local_hbm_read_ms = std::stod(argv[++i]);
        else if (arg == "--remote-read-ms" && i + 1 < argc) t_remote_view_read_ms = std::stod(argv[++i]);
        else if (arg == "--evidence-level" && i + 1 < argc) evidence_level = argv[++i];
        else if (arg == "--out" && i + 1 < argc) out_file = argv[++i];
    }

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

    std::cout << "\nCalculated Crossover Point from supplied capability values: N = " << crossover_point
              << " reads; evidence_level=" << evidence_level << "\n";

    std::ofstream out(out_file);
    out << "read_count,payload_mb,view_time_ms,copy_time_ms,better_path,dma_copy_ms,local_read_ms,remote_read_ms,evidence_level\n";
    for (const auto& r : results) {
        out << r.read_count << "," << payload_size / 1024 / 1024 << "," << r.view_time_ms << "," << r.copy_time_ms << "," << r.better_path
            << "," << t_dma_copy_ms << "," << t_local_hbm_read_ms << "," << t_remote_view_read_ms << "," << evidence_level << "\n";
    }

    std::cout << "Results saved to " << out_file << "\n";
    return 0;
}
