// descriptor_compiler.cc - 描述符合并与编译实现
#include "descriptor_compiler.h"
#include <algorithm>
#include <iostream>

BatchDescriptorHeader DescriptorCompiler::compile_manifest(const std::vector<LogicalBlock>& src_blocks,
                                                          const std::vector<LogicalBlock>& dst_blocks) {
    BatchDescriptorHeader batch;
    batch.flags = 0x03; // ASYNC | FENCE_BARRIER
    if (src_blocks.empty() || dst_blocks.empty() || src_blocks.size() != dst_blocks.size()) {
        batch.entry_count = 0;
        return batch;
    }

    size_t n = src_blocks.size();
    batch.entries.reserve(n);

    // 初始第一个条目
    HardwareSGEntry current;
    current.src_phys_addr = src_blocks[0].phys_addr;
    current.dst_phys_addr = dst_blocks[0].phys_addr;
    current.len_bytes = src_blocks[0].size_bytes;

    for (size_t i = 1; i < n; ++i) {
        bool src_contiguous = (src_blocks[i].phys_addr == (current.src_phys_addr + current.len_bytes));
        bool dst_contiguous = (dst_blocks[i].phys_addr == (current.dst_phys_addr + current.len_bytes));

        if (src_contiguous && dst_contiguous) {
            // 物理地址连续，直接合并延长
            current.len_bytes += src_blocks[i].size_bytes;
        } else {
            // 发生断点，推入当前条目，开启新段
            batch.entries.push_back(current);
            current.src_phys_addr = src_blocks[i].phys_addr;
            current.dst_phys_addr = dst_blocks[i].phys_addr;
            current.len_bytes = src_blocks[i].size_bytes;
        }
    }
    batch.entries.push_back(current);
    batch.entry_count = static_cast<uint32_t>(batch.entries.size());

    return batch;
}
