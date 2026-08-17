// consume_eligibility.h - ConsumeEligibility 6 维语义校验引擎头文件
#ifndef CONSUME_ELIGIBILITY_H
#define CONSUME_ELIGIBILITY_H

#include <cstdint>
#include <string>
#include <vector>

struct SemanticMetadata {
    std::string model_version;    // 模型架构与版本 (如 "Qwen2.5-72B-v1")
    std::string tokenizer_hash;   // Tokenizer 校验哈希
    std::string template_hash;    // ChatTemplate 格式哈希
    std::string adapter_id;       // LoRA Adapter 标识 (无则为 "base")
    uint64_t lease_expire_ms;     // 租约过期时间戳
    bool ready_bit;               // 写入就绪屏障标志位
};

enum class CheckResult {
    PASS = 0,
    REJECT_MODEL_MISMATCH = 1,
    REJECT_TOKENIZER_MISMATCH = 2,
    REJECT_TEMPLATE_MISMATCH = 3,
    REJECT_ADAPTER_MISMATCH = 4,
    REJECT_NOT_READY = 5,
    REJECT_LEASE_EXPIRED = 6
};

class ConsumeEligibility {
public:
    ConsumeEligibility() = default;

    // 6 维语义校验 (微秒级)
    CheckResult evaluate(const SemanticMetadata& req_meta, const SemanticMetadata& cached_meta, uint64_t now_ms);
    static std::string result_to_string(CheckResult res);
};

#endif // CONSUME_ELIGIBILITY_H
