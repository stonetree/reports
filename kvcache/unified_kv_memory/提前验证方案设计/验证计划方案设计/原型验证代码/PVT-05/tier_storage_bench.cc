// tier_storage_bench.cc - HBM ↔ NVMe SSD 直达容量主路径微基准压测工具
// DEMO 级固定结果 Schema；开发人员需替换为 SPDK/io_uring 完成量和 Host Touch 探针。
#include <iostream>
#include <stdexcept>
#include <vector>
#include <chrono>
#include <fstream>
#include <algorithm>
#include <cstring>
#include <cstdlib>
#include <unistd.h>

size_t parse_size_bytes(const std::string& text) {
    if (text.empty()) throw std::invalid_argument("empty block size");
    const char suffix = text.back();
    size_t multiplier = 1;
    std::string number = text;
    if (suffix == 'K' || suffix == 'k') { multiplier = 1024ULL; number.pop_back(); }
    else if (suffix == 'M' || suffix == 'm') { multiplier = 1024ULL * 1024ULL; number.pop_back(); }
    else if (suffix == 'G' || suffix == 'g') { multiplier = 1024ULL * 1024ULL * 1024ULL; number.pop_back(); }
    return std::stoull(number) * multiplier;
}

struct TierResult {
    std::string path_mode; // "nvme_direct_bypass_ddr" vs "hbm_ddr_ssd_staging"
    size_t block_size;
    double throughput_gbps;
    double latency_ms;
    double host_cpu_pct;
};

int main(int argc, char** argv) {
    std::vector<size_t> block_sizes = {1048576, 4194304, 16777216, 67108864}; // 1M ~ 64M
    std::string out_file = "res_tier_storage.csv";
    std::string device = "DEMO_DEVICE";
    int queue_depth = 1;
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--device" && i + 1 < argc) device = argv[++i];
        else if (arg == "--block-size" && i + 1 < argc) block_sizes = {parse_size_bytes(argv[++i])};
        else if (arg == "--qd" && i + 1 < argc) queue_depth = std::stoi(argv[++i]);
        else if (arg == "--out" && i + 1 < argc) out_file = argv[++i];
    }

    std::cout << "=== PVT-05: HBM-SSD Direct Capacity Path Benchmark ===\n";

    std::ofstream out(out_file);
    out << "path_mode,device,block_size_bytes,queue_depth,throughput_gbps,latency_ms,host_cpu_pct,host_payload_touch_bytes,evidence_level,status\n";

    for (size_t bs : block_sizes) {
        // 1. 测试 NVMe Direct (Payload Bypass DDR)
        // 4 盘阵列顺序读峰值 28GB/s (224 Gbps)，直达达成率约 85% = 190 Gbps
        double direct_bw = 190.4;
        double direct_lat = (bs * 8.0) / (direct_bw * 1e6); // ms
        double direct_cpu = 1.2; // 极低 CPU 占用

        // 2. 测试传统 DDR 中转 (HBM -> DDR -> SSD)
        // 受限于 DDR 带宽与 CPU Memcpy 瓶颈，带宽降低至 85 Gbps，CPU 占满 65%
        double ddr_bw = 85.2;
        double ddr_lat = (bs * 8.0) / (ddr_bw * 1e6);
        double ddr_cpu = 65.4;

        std::cout << "Block: " << bs / 1024 / 1024 << " MB => "
                  << "Direct: " << direct_bw << " Gbps (CPU " << direct_cpu << "%) vs "
                  << "DDR-Staging: " << ddr_bw << " Gbps (CPU " << ddr_cpu << "%)\n";

        out << "nvme_direct_bypass_ddr," << device << ',' << bs << ',' << queue_depth << ',' << direct_bw << ',' << direct_lat << ',' << direct_cpu << ",,DEMO,DEMO_ONLY\n";
        out << "hbm_ddr_ssd_staging," << device << ',' << bs << ',' << queue_depth << ',' << ddr_bw << ',' << ddr_lat << ',' << ddr_cpu << ",,DEMO,DEMO_ONLY\n";
    }

    std::cout << "\nResults saved to " << out_file << "\n";
    return 0;
}
