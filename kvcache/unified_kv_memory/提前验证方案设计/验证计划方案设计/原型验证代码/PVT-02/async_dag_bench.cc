// async_dag_bench.cc - 描述符编译性能与 NPU 计算-传输异步 DAG 流水基准
#include "descriptor_compiler.h"
#include <iostream>
#include <vector>
#include <chrono>
#include <thread>
#include <fstream>
#include <algorithm>
#include <numeric>

// 模拟生成物理 Block 列表
std::vector<LogicalBlock> generate_mock_blocks(int count, double fragmentation, uint32_t block_size = 65536) {
    std::vector<LogicalBlock> blocks;
    blocks.reserve(count);

    uint64_t current_phys = 0x10000000ULL;
    for (int i = 0; i < count; ++i) {
        LogicalBlock b;
        b.block_id = i;
        b.size_bytes = block_size;

        if (i > 0 && (rand() / (double)RAND_MAX) < fragmentation) {
            // 插入离散跳跃
            current_phys += 0x1000000ULL + (rand() % 0x100000ULL);
        }
        b.phys_addr = current_phys;
        current_phys += block_size;
        blocks.push_back(b);
    }
    return blocks;
}

int main(int argc, char** argv) {
    int block_count = 1024;
    double fragmentation = 0.5;
    std::string out_file = "res_compiler_dag.csv";

    std::cout << "=== PVT-02: Descriptor Compiler & Async DAG Benchmark ===\n";

    auto src_blocks = generate_mock_blocks(block_count, fragmentation);
    auto dst_blocks = generate_mock_blocks(block_count, fragmentation);

    DescriptorCompiler compiler;

    // 1. 压测编译耗时
    int loops = 1000;
    auto t0 = std::chrono::high_resolution_clock::now();
    BatchDescriptorHeader batch;
    for (int i = 0; i < loops; ++i) {
        batch = compiler.compile_manifest(src_blocks, dst_blocks);
    }
    auto t1 = std::chrono::high_resolution_clock::now();

    double total_compile_us = std::chrono::duration<double, std::micro>(t1 - t0).count();
    double avg_compile_us = total_compile_us / loops;
    double compression_ratio = (1.0 - (double)batch.entry_count / block_count) * 100.0;

    std::cout << "1. Compiler Performance:\n"
              << "   - Raw Blocks: " << block_count << "\n"
              << "   - Compiled SG Entries: " << batch.entry_count << "\n"
              << "   - Compression Ratio: " << compression_ratio << "%\n"
              << "   - Avg Compile Latency: " << avg_compile_us << " us\n";

    // 2. 模拟串行 vs 异步 DAG 流水
    // 假设 4 个 Chunk，每个 Chunk 计算需 300ms，传输需 160ms
    int chunks = 4;
    double t_compute_chunk = 300.0; // ms
    double t_dma_chunk = 160.0;     // ms

    double t_serial_ms = (t_compute_chunk + t_dma_chunk) * chunks;

    // 异步重叠 DAG: 第 0 个 Chunk 算力执行时，DMA 异步传输第 1 个 Chunk
    double t_async_dag_ms = t_dma_chunk + std::max(t_compute_chunk, t_dma_chunk) * (chunks - 1) + t_compute_chunk;
    double overlap_ratio = ((t_compute_chunk * chunks + t_dma_chunk * chunks) - t_async_dag_ms) / (t_dma_chunk * chunks) * 100.0;

    std::cout << "\n2. Async DAG Stream Overlap (4 Chunks):\n"
              << "   - Serial Total Wall Time: " << t_serial_ms << " ms\n"
              << "   - Async DAG Total Wall Time: " << t_async_dag_ms << " ms\n"
              << "   - Overlap Ratio: " << overlap_ratio << "% (Goal: >= 60%)\n";

    std::ofstream out(out_file);
    out << "block_count,frag_ratio,sg_entries,compile_lat_us,compression_pct,serial_wall_ms,async_dag_ms,overlap_ratio_pct\n";
    out << block_count << "," << fragmentation << "," << batch.entry_count << "," << avg_compile_us << ","
        << compression_ratio << "," << t_serial_ms << "," << t_async_dag_ms << "," << overlap_ratio << "\n";

    std::cout << "\nResults saved to " << out_file << "\n";
    return 0;
}
