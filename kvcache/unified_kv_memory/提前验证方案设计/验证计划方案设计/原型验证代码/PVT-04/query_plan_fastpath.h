// query_plan_fastpath.h - QueryPlan 微秒级决策引擎与 CostEvaluator 头文件 (支持 MLA/MHA/GQA 多模型架构)
#ifndef QUERY_PLAN_FASTPATH_H
#define QUERY_PLAN_FASTPATH_H

#include <cstdint>
#include <string>
#include <vector>

enum class PlanAction {
    Local_Load = 0,
    Remote_Load = 1,
    SSD_Restore = 2,
    Recompute = 3
};

enum class ModelArch {
    MHA_Dense = 0,    // Qwen2.5-72B (320KB/tok)
    MLA_DeepSeek = 1, // DeepSeek-V3/R1 (512B/tok)
    GQA_Llama = 2     // LLaMA-3.1-70B (80KB/tok)
};

struct AccessIntent {
    uint64_t request_id;
    uint32_t prefix_tokens;
    uint32_t deadline_ms;
    double current_ewma_bw_gbps;
    double queue_delay_ms;
    bool is_remote_cached;
    ModelArch model_arch = ModelArch::MHA_Dense;
};

struct ExecutionPlan {
    PlanAction action;
    double estimated_cost_ms;
    std::string reason;
};

class CostEvaluator {
public:
    static double estimate_load_cost(uint32_t tokens, double bw_gbps, double queue_ms, ModelArch model_arch = ModelArch::MHA_Dense);
    static double estimate_recompute_cost(uint32_t tokens, double compute_tps = 8000.0);
};

class QueryPlanFastPath {
public:
    QueryPlanFastPath() = default;

    // 微秒级核心决策生成 (< 5us)
    ExecutionPlan generate_plan(const AccessIntent& intent);
};

#endif // QUERY_PLAN_FASTPATH_H
