#ifndef QUERY_PLAN_FASTPATH_H
#define QUERY_PLAN_FASTPATH_H

#include <cstdint>
#include <string>

enum class PlanAction { Remote_Load, Recompute };

struct AccessIntent {
    uint64_t request_id;
    uint32_t prefix_tokens;
    uint32_t deadline_ms;
    double current_ewma_bw_gbps;
    double queue_delay_ms;
    bool is_remote_cached;
    uint64_t kv_bytes_per_token; // 由运行时布局清单导出，不按模型名称硬编码。
    double compute_tokens_per_second;
    double metadata_overhead_ms;
};

struct ExecutionPlan {
    PlanAction action;
    double predicted_load_ms;
    double predicted_recompute_ms;
    double predicted_selected_ms;
    std::string reason;
};

class CostEvaluator {
public:
    static double estimate_load_cost(const AccessIntent& intent);
    static double estimate_recompute_cost(const AccessIntent& intent);
};

class QueryPlanFastPath {
public:
    ExecutionPlan generate_plan(const AccessIntent& intent) const;
};

const char* action_name(PlanAction action);

#endif
