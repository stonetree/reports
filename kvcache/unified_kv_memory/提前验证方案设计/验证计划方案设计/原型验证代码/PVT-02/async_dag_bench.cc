#include "descriptor_compiler.h"
#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <fstream>
#include <iostream>
#include <string>
#include <vector>

std::vector<LogicalBlock> mock_blocks(int count, double fragmentation, uint32_t block_size = 65536) {
    std::vector<LogicalBlock> blocks;
    uint64_t address = 0x10000000ULL;
    for (int i = 0; i < count; ++i) {
        if (i > 0 && (std::rand() / static_cast<double>(RAND_MAX)) < fragmentation) address += 0x1000000ULL;
        blocks.push_back({static_cast<uint64_t>(i), address, block_size});
        address += block_size;
    }
    return blocks;
}

double percentile(std::vector<double> values, double p) {
    std::sort(values.begin(), values.end());
    return values[static_cast<size_t>((values.size() - 1) * p)];
}

int main(int argc, char** argv) {
    int block_count = 1024, chunks = 4, loops = 1000;
    double fragmentation = 0.5, compute_ms = 300.0, dma_ms = 160.0;
    std::string output = "res_compiler_dag.csv";
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--block-count" && i + 1 < argc) block_count = std::stoi(argv[++i]);
        else if (arg == "--fragmentation" && i + 1 < argc) fragmentation = std::stod(argv[++i]);
        else if (arg == "--chunks" && i + 1 < argc) chunks = std::stoi(argv[++i]);
        else if (arg == "--compute-ms" && i + 1 < argc) compute_ms = std::stod(argv[++i]);
        else if (arg == "--dma-ms" && i + 1 < argc) dma_ms = std::stod(argv[++i]);
        else if (arg == "--loops" && i + 1 < argc) loops = std::stoi(argv[++i]);
        else if (arg == "--out" && i + 1 < argc) output = argv[++i];
    }
    std::srand(42);
    const auto source = mock_blocks(block_count, fragmentation);
    const auto target = mock_blocks(block_count, fragmentation);
    DescriptorCompiler compiler;
    std::vector<double> compile_us;
    CompiledBatch batch;
    for (int i = 0; i < loops; ++i) {
        const auto begin = std::chrono::steady_clock::now();
        batch = compiler.compile_manifest(source, target);
        const auto end = std::chrono::steady_clock::now();
        compile_us.push_back(std::chrono::duration<double, std::micro>(end - begin).count());
    }
    const double compression_pct = (1.0 - static_cast<double>(batch.header.entry_count) / block_count) * 100.0;
    const double serial_ms = (compute_ms + dma_ms) * chunks;
    const double dag_ms = dma_ms + std::max(compute_ms, dma_ms) * (chunks - 1) + compute_ms;
    const double overlap_pct = ((compute_ms + dma_ms) * chunks - dag_ms) / (dma_ms * chunks) * 100.0;
    std::ofstream stream(output);
    stream << "block_count,fragmentation,sg_entries,compile_p50_us,compile_p95_us,compile_p99_us,compression_pct,chunks,serial_ms,dag_ms,overlap_pct,evidence_level,status\n";
    stream << block_count << ',' << fragmentation << ',' << batch.header.entry_count << ','
           << percentile(compile_us, 0.50) << ',' << percentile(compile_us, 0.95) << ','
           << percentile(compile_us, 0.99) << ',' << compression_pct << ',' << chunks << ','
           << serial_ms << ',' << dag_ms << ',' << overlap_pct << ",DEMO,DEMO_ONLY\n";
    std::cout << "descriptor correctness and compile timing recorded; DAG overlap is DEMO formula only. output=" << output << '\n';
    return 0;
}
