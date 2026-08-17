// consume_eligibility.cc - 6 维语义校验算法实现
#include "consume_eligibility.h"

CheckResult ConsumeEligibility::evaluate(const SemanticMetadata& req, const SemanticMetadata& cached, uint64_t now_ms) {
    // 1. 模型版本强校验
    if (req.model_version != cached.model_version) {
        return CheckResult::REJECT_MODEL_MISMATCH;
    }
    // 2. Tokenizer 校验
    if (req.tokenizer_hash != cached.tokenizer_hash) {
        return CheckResult::REJECT_TOKENIZER_MISMATCH;
    }
    // 3. ChatTemplate 格式校验
    if (req.template_hash != cached.template_hash) {
        return CheckResult::REJECT_TEMPLATE_MISMATCH;
    }
    // 4. LoRA Adapter 校验
    if (req.adapter_id != cached.adapter_id) {
        return CheckResult::REJECT_ADAPTER_MISMATCH;
    }
    // 5. Ready 写入可见性屏障校验
    if (!cached.ready_bit) {
        return CheckResult::REJECT_NOT_READY;
    }
    // 6. 租约时效性校验
    if (now_ms > cached.lease_expire_ms) {
        return CheckResult::REJECT_LEASE_EXPIRED;
    }

    return CheckResult::PASS;
}

std::string ConsumeEligibility::result_to_string(CheckResult res) {
    switch (res) {
        case CheckResult::PASS: return "PASS";
        case CheckResult::REJECT_MODEL_MISMATCH: return "REJECT_MODEL_MISMATCH";
        case CheckResult::REJECT_TOKENIZER_MISMATCH: return "REJECT_TOKENIZER_MISMATCH";
        case CheckResult::REJECT_TEMPLATE_MISMATCH: return "REJECT_TEMPLATE_MISMATCH";
        case CheckResult::REJECT_ADAPTER_MISMATCH: return "REJECT_ADAPTER_MISMATCH";
        case CheckResult::REJECT_NOT_READY: return "REJECT_NOT_READY";
        case CheckResult::REJECT_LEASE_EXPIRED: return "REJECT_LEASE_EXPIRED";
        default: return "UNKNOWN";
    }
}
