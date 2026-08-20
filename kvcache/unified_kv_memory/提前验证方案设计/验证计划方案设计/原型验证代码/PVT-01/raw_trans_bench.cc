// PVT-01 传输微基准脚手架。未接入设备 DMA 的路径明确标为 DEMO_ONLY。
#include <algorithm>
#include <chrono>
#include <cstring>
#include <fstream>
#include <iostream>
#include <string>
#include <time.h>
#include <vector>

uint64_t process_cpu_us() {
    timespec ts{};
    clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &ts);
    return ts.tv_sec * 1000000ULL + ts.tv_nsec / 1000ULL;
}

int main(int argc, char** argv) {
    std::string mode = "urma_direct";
    std::string output = "res_trans.csv";
    size_t payload_bytes = 64 * 1024 * 1024;
    int queue_depth = 1;
    int loops = 10;
    for (int i = 1; i < argc; ++i) {
        std::string arg = argv[i];
        if (arg == "--mode" && i + 1 < argc) mode = argv[++i];
        else if (arg == "--payload-bytes" && i + 1 < argc) payload_bytes = std::stoull(argv[++i]);
        else if (arg == "--qd" && i + 1 < argc) queue_depth = std::stoi(argv[++i]);
        else if (arg == "--loops" && i + 1 < argc) loops = std::stoi(argv[++i]);
        else if (arg == "--out" && i + 1 < argc) output = argv[++i];
    }

    std::vector<char> source(payload_bytes, static_cast<char>(0xAB));
    std::vector<char> target(payload_bytes, 0);
    std::vector<double> latency_us;
    const bool executable_reference = mode == "host_memcpy";
    const uint64_t cpu_begin = process_cpu_us();
    const auto wall_begin = std::chrono::steady_clock::now();
    for (int loop = 0; loop < loops; ++loop) {
        const auto begin = std::chrono::steady_clock::now();
        for (int q = 0; q < queue_depth; ++q) {
            if (executable_reference) std::memcpy(target.data(), source.data(), payload_bytes);
            else asm volatile("" ::: "memory"); // 仅示范描述符提交位置，不代表数据已搬运。
        }
        const auto end = std::chrono::steady_clock::now();
        latency_us.push_back(std::chrono::duration<double, std::micro>(end - begin).count());
    }
    const auto wall_end = std::chrono::steady_clock::now();
    const uint64_t cpu_end = process_cpu_us();
    const double wall_seconds = std::chrono::duration<double>(wall_end - wall_begin).count();
    const uint64_t actual_completed_bytes = executable_reference
        ? static_cast<uint64_t>(payload_bytes) * queue_depth * loops
        : 0;
    const double bandwidth_gbps = actual_completed_bytes > 0
        ? actual_completed_bytes * 8.0 / wall_seconds / 1e9
        : 0.0;
    std::sort(latency_us.begin(), latency_us.end());
    const double cpu_pct = (cpu_end - cpu_begin) / 1e6 / std::max(wall_seconds, 0.001) * 100.0;
    const std::string status = executable_reference ? "OK" : "DEMO_ONLY";

    std::ofstream stream(output);
    stream << "path_mode,payload_bytes,queue_depth,actual_completed_bytes,bandwidth_gbps,latency_p50_us,latency_p99_us,host_cpu_pct,evidence_level,status\n";
    stream << mode << ',' << payload_bytes << ',' << queue_depth << ',' << actual_completed_bytes << ','
           << bandwidth_gbps << ',' << latency_us[latency_us.size() / 2] << ','
           << latency_us[static_cast<size_t>(latency_us.size() * 0.99)] << ',' << cpu_pct
           << ",DEMO," << status << '\n';
    std::cout << "status=" << status << " actual_completed_bytes=" << actual_completed_bytes
              << " evidence_level=DEMO output=" << output << '\n';
    return 0;
}
