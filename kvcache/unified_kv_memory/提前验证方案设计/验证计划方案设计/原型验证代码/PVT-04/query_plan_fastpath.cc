// query_plan_fastpath.cc - 动态决策引擎与成本评估实现 (支持 MLA/MHA 多模型)
#include "query_plan_fastpath.h"
#include <algorithm>

double CostEvaluator::estimate_load_cost(uint32_t tokens, double bw_gbps, double queue_ms, ModelArch model_arch) {
    double bytes_per_tok = 327680.0; // 默认 MHA Qwen 72B (320KB/tok)
    if (model_arch == ModelArch::MLA_DeepSeek) {
        bytes_per_tok = 512.0;       // DeepSeek MLA (512B/tok)
    } else if (model_arch == ModelArch::GQA_Llama) {
        bytes_per_tok = 81920.0;     // LLaMA GQA (80KB/tok)
    }

    double kv_bytes = tokens * bytes_per_tok;
    double bw_bytes_per_ms = (bw_gbps * 1e9) / (8.0 * 1000.0);
    double xfer_ms = kv_bytes / std::max(bw_bytes_per_ms, 1e5);
    double metadata_overhead_ms = 0.15; // 150us 元数据开销
    return metadata_overhead_ms + queue_ms + xfer_ms;
}

double CostEvaluator::estimate_recompute_cost(uint32_t tokens, double compute_tps) {
    return (tokens / compute_tps) * 1000.0;
}

ExecutionPlan QueryPlanFastPath::generate_plan(const AccessIntent& intent) {
    ExecutionPlan plan;

    if (!intent.is_remote_cached) {
        plan.action = PlanAction::Recompute;
        plan.estimated_cost_ms = CostEvaluator::estimate_recompute_cost(intent.prefix_tokens);
        plan.reason = "NO_CACHE_AVAILABLE";
        return plan;
    }

    double load_cost = CostEvaluator::estimate_load_cost(intent.prefix_tokens, intent.current_ewma_bw_gbps, intent.queue_delay_ms, intent.model_arch);
    double recompute_cost = CostEvaluator::estimate_recompute_cost(intent.prefix_tokens);

    // 判定条件 1: Deadline 约束
    if (intent.deadline_ms > 0 && load_cost > intent.deadline_ms && recompute_cost <= intent.deadline_ms) {
        plan.action = PlanAction::Recompute;
        plan.estimated_cost_ms = recompute_cost;
        plan.reason = "DEADLINE_CONSTRAINT_RECOMPUTE_PREFERRED";
        return plan;
    }

    // 判定条件 2: 正价值成本比对 (负收益拦截)
    if (load_cost < recompute_cost) {
        plan.action = PlanAction::Remote_Load;
        plan.estimated_cost_ms = load_cost;
        plan.reason = "LOAD_FASTER_THAN_RECOMPUTE";
    } else {
        plan.action = PlanAction::Recompute;
        plan.estimated_cost_ms = recompute_cost;
        plan.reason = "NEGATIVE_BENEFIT_LOAD_INTERCEPTED";
    }

    return plan;
}
