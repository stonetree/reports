#include "descriptor_compiler.h"

CompiledBatch DescriptorCompiler::compile_manifest(const std::vector<LogicalBlock>& source,
                                                    const std::vector<LogicalBlock>& target) const {
    CompiledBatch batch;
    batch.header.version = 1;
    batch.header.flags = 0x03;
    if (source.empty() || source.size() != target.size()) return batch;
    batch.entries.reserve(source.size());
    HardwareSGEntry current{source[0].phys_addr, target[0].phys_addr, source[0].size_bytes, 0};
    uint64_t total_bytes = source[0].size_bytes;
    for (size_t i = 1; i < source.size(); ++i) {
        const bool contiguous = source[i].phys_addr == current.src_phys_addr + current.len_bytes
            && target[i].phys_addr == current.dst_phys_addr + current.len_bytes;
        if (contiguous) current.len_bytes += source[i].size_bytes;
        else {
            batch.entries.push_back(current);
            current = {source[i].phys_addr, target[i].phys_addr, source[i].size_bytes, 0};
        }
        total_bytes += source[i].size_bytes;
    }
    batch.entries.push_back(current);
    batch.header.entry_count = static_cast<uint32_t>(batch.entries.size());
    batch.header.total_bytes = total_bytes;
    return batch;
}
