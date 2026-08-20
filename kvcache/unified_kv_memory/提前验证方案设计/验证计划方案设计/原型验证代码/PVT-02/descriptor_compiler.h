#ifndef DESCRIPTOR_COMPILER_H
#define DESCRIPTOR_COMPILER_H

#include <cstdint>
#include <vector>

struct LogicalBlock {
    uint64_t block_id;
    uint64_t phys_addr;
    uint32_t size_bytes;
};

struct HardwareSGEntry {
    uint64_t src_phys_addr;
    uint64_t dst_phys_addr;
    uint32_t len_bytes;
    uint32_t flags;
};

// 仅该固定字段 wire header 声明为 64B POD；动态 entries 存放在宿主容器中。
struct WireBatchDescriptorHeader {
    uint32_t version;
    uint32_t flags;
    uint32_t entry_count;
    uint32_t reserved0;
    uint64_t total_bytes;
    uint64_t manifest_id;
    uint8_t reserved1[32];
};
static_assert(sizeof(WireBatchDescriptorHeader) == 64, "wire header must be 64 bytes");

struct CompiledBatch {
    WireBatchDescriptorHeader header{};
    std::vector<HardwareSGEntry> entries;
};

class DescriptorCompiler {
public:
    CompiledBatch compile_manifest(const std::vector<LogicalBlock>& source,
                                   const std::vector<LogicalBlock>& target) const;
};

#endif
