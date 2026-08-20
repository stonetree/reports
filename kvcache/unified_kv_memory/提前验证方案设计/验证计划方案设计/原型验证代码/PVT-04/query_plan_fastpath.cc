#include "query_plan_fastpath.h"
#include <algorithm>

double CostEvaluator::estimate_load_cost(const AccessIntent& intent) {
    const double kv_bytes = static_cast<double>(intent.prefix_tokens) * intent.kv_bytes_per_token;
    const double bytes_per_ms = intent.current_ewma_bw_gbps * 1e9 / 8.0 / 1000.0;
    return intent.metadata_overhead_ms + intent.queue_delay_ms + kv_bytes / std::max(bytes_per_ms, 1.0);
}

double CostEvaluator::estimate_recompute_cost(const AccessIntent& intent) {
    return intent.prefix_tokens / std::max(intent.compute_tokens_per_second, 1.0) * 1000.0;
}

ExecutionPlan QueryPlanFastPath::generate_plan(const AccessIntent& intent) const {
    const double recompute = CostEvaluator::estimate_recompute_cost(intent);
    const double load = intent.is_remote_cached ? CostEvaluator::estimate_load_cost(intent) : 1e300;
    if (!intent.is_remote_cached || load >= recompute || (intent.deadline_ms > 0 && load > intent.deadline_ms && recompute <= intent.deadline_ms)) {
        return {PlanAction::Recompute, load, recompute, recompute,
                intent.is_remote_cached ? "NEGATIVE_BENEFIT_OR_DEADLINE" : "NO_REMOTE_CACHE"};
    }
    return {PlanAction::Remote_Load, load, recompute, load, "LOAD_PREDICTED_FASTER"};
}

const char* action_name(PlanAction action) {
    return action == PlanAction::Remote_Load ? "REMOTE_LOAD" : "RECOMPUTE";
}
