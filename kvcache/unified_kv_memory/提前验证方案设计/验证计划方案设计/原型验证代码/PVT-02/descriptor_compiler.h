// descriptor_compiler.h - 异构框架 Layout 描述符编译器头文件
#ifndef DESCRIPTOR_COMPILER_H
#define DESCRIPTOR_COMPILER_H

#include <cstdint>
#include <vector>
#include <string>

// 逻辑物理块描述
struct LogicalBlock {
    uint64_t block_id;
    uint64_t phys_addr;
    uint32_t size_bytes;
};

// 硬件 Scatter-Gather 描述符条目
struct HardwareSGEntry {
    uint64_t src_phys_addr;
    uint64_t dst_phys_addr;
    uint32_t len_bytes;
};

// 批量提交描述符头结构
struct BatchDescriptorHeader {
    uint32_t entry_count;
    uint32_t flags; // 0x01: ASYNC, 0x02: FENCE_BARRIER
    std::vector<HardwareSGEntry> entries;
};

class DescriptorCompiler {
public:
    DescriptorCompiler() = default;

    // 编译 Manifest 并执行相邻物理连续块的合并
    BatchDescriptorHeader compile_manifest(const std::vector<LogicalBlock>& src_blocks,
                                          const std::vector<LogicalBlock>& dst_blocks);
};

#endif // DESCRIPTOR_COMPILER_H
