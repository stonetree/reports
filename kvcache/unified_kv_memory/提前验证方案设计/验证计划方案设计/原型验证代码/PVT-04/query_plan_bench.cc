// query_plan_bench.cc - QueryPlan 决策引擎 100K QPS 压测与反事实验证 Harness
#include "query_plan_fastpath.h"
#include <iostream>
#include <vector>
#include <chrono>
#include <fstream>
#include <algorithm>
#include <cmath>

int main(int argc, char** argv) {
    int qps_loops = 500000;
    std::string out_file = "res_plan_accuracy.csv";

    std::cout << "=== PVT-04: QueryPlan Dynamic Decision & CostEvaluator Benchmark ===\n";

    QueryPlanFastPath engine;

    // 1. 极限单次决策延迟压测 (100K+ QPS 模拟)
    std::vector<double> latencies_us;
    latencies_us.reserve(qps_loops);

    AccessIntent test_intent = {1001, 16384, 50, 600.0, 0.5, true};

    auto t0 = std::chrono::high_resolution_clock::now();
    for (int i = 0; i < qps_loops; ++i) {
        auto d0 = std::chrono::high_resolution_clock::now();
        auto plan = engine.generate_plan(test_intent);
        auto d1 = std::chrono::high_resolution_clock::now();
        latencies_us.push_back(std::chrono::duration<double, std::micro>(d1 - d0).count());
    }
    auto t1 = std::chrono::high_resolution_clock::now();

    double total_sec = std::chrono::duration<double>(t1 - t0).count();
    double actual_qps = qps_loops / total_sec;

    std::sort(latencies_us.begin(), latencies_us.end());
    double p50 = latencies_us[latencies_us.size() * 0.50];
    double p90 = latencies_us[latencies_us.size() * 0.90];
    double p99 = latencies_us[latencies_us.size() * 0.99];

    std::cout << "1. Decision Throughput & Latency:\n"
              << "   - Measured QPS: " << actual_qps << " req/s\n"
              << "   - P50 Latency: " << p50 << " us\n"
              << "   - P90 Latency: " << p90 << " us\n"
              << "   - P99 Latency: " << p99 << " us (Goal: < 5.0 us)\n";

    // 2. 多场景决策与反事实验证 (长前缀、短前缀、拥塞网络)
    std::vector<AccessIntent> scenarios = {
        {1, 32768, 100, 750.0, 0.1, true}, // 场景1: 空闲长前缀 -> 应 Load
        {2, 24, 100, 750.0, 0.1, true},    // 场景2: 极短前缀 -> 应 Recompute (拦截小IO)
        {3, 8192, 100, 1.0, 20.0, true},   // 场景3: 严重拥塞 -> 应 Recompute (拦截慢加载)
        {4, 16384, 5, 400.0, 10.0, true}   // 场景4: 紧迫 Deadline -> 应 Recompute
    };

    std::ofstream out(out_file);
    out << "req_id,prefix_tokens,bw_gbps,queue_ms,action,est_cost_ms,reason\n";

    std::cout << "\n2. Scenario Validation:\n";
    for (const auto& sc : scenarios) {
        auto plan = engine.generate_plan(sc);
        std::string action_str = (plan.action == PlanAction::Remote_Load) ? "Remote_Load" : "Recompute";
        std::cout << "   - Req " << sc.request_id << " (Tokens: " << sc.prefix_tokens
                  << ", BW: " << sc.current_ewma_bw_gbps << " Gbps) => Action: "
                  << action_str << " (Est Cost: " << plan.estimated_cost_ms << " ms, Reason: " << plan.reason << ")\n";

        out << sc.request_id << "," << sc.prefix_tokens << "," << sc.current_ewma_bw_gbps << ","
            << sc.queue_delay_ms << "," << action_str << "," << plan.estimated_cost_ms << "," << plan.reason << "\n";
    }

    std::cout << "\nResults saved to " << out_file << "\n";
    return 0;
}
