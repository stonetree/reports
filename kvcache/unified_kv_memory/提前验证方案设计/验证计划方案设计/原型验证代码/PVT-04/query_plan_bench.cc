#include "query_plan_fastpath.h"
#include <algorithm>
#include <chrono>
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

struct Scenario {
    AccessIntent intent;
    double actual_load_ms;
    double actual_recompute_ms;
};

std::vector<std::string> split(const std::string& line) {
    std::vector<std::string> parts;
    std::stringstream stream(line);
    std::string part;
    while (std::getline(stream, part, ',')) parts.push_back(part);
    return parts;
}

std::vector<Scenario> load_scenarios(const std::string& path) {
    std::ifstream input(path);
    std::string line;
    std::getline(input, line); // header
    std::vector<Scenario> scenarios;
    while (std::getline(input, line)) {
        const auto f = split(line);
        if (f.size() != 11) throw std::runtime_error("scenario CSV requires 11 columns");
        scenarios.push_back({
            {std::stoull(f[0]), static_cast<uint32_t>(std::stoul(f[1])), static_cast<uint32_t>(std::stoul(f[2])),
             std::stod(f[3]), std::stod(f[4]), f[5] == "1", std::stoull(f[6]), std::stod(f[7]), std::stod(f[8])},
            std::stod(f[9]), std::stod(f[10])});
    }
    return scenarios;
}

int main(int argc, char** argv) {
    std::string scenario_csv, output = "query_plan_results.csv", evidence_level = "DEMO";
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--scenario-csv" && i + 1 < argc) scenario_csv = argv[++i];
        else if (arg == "--out" && i + 1 < argc) output = argv[++i];
        else if (arg == "--evidence-level" && i + 1 < argc) evidence_level = argv[++i];
    }
    if (scenario_csv.empty()) {
        std::cerr << "--scenario-csv is required. It must contain request_id,prefix_tokens,deadline_ms,bw_gbps,queue_ms,is_cached,kv_bytes_per_token,compute_tps,metadata_ms,actual_load_ms,actual_recompute_ms\n";
        return 2;
    }
    const auto scenarios = load_scenarios(scenario_csv);
    QueryPlanFastPath planner;
    std::ofstream stream(output);
    stream << "request_id,kv_bytes_per_token,predicted_path,actual_path,predicted_load_ms,predicted_recompute_ms,actual_load_ms,actual_recompute_ms,regret_ms,is_correct,decision_ns,evidence_level\n";
    for (const auto& scenario : scenarios) {
        const auto begin = std::chrono::steady_clock::now();
        const auto plan = planner.generate_plan(scenario.intent);
        const auto end = std::chrono::steady_clock::now();
        const PlanAction optimal = scenario.actual_load_ms < scenario.actual_recompute_ms ? PlanAction::Remote_Load : PlanAction::Recompute;
        const double selected_actual = plan.action == PlanAction::Remote_Load ? scenario.actual_load_ms : scenario.actual_recompute_ms;
        const double optimal_actual = std::min(scenario.actual_load_ms, scenario.actual_recompute_ms);
        stream << scenario.intent.request_id << ',' << scenario.intent.kv_bytes_per_token << ',' << action_name(plan.action) << ','
               << action_name(optimal) << ',' << plan.predicted_load_ms << ',' << plan.predicted_recompute_ms << ','
               << scenario.actual_load_ms << ',' << scenario.actual_recompute_ms << ',' << selected_actual - optimal_actual << ','
               << (plan.action == optimal ? "TRUE" : "FALSE") << ','
               << std::chrono::duration_cast<std::chrono::nanoseconds>(end - begin).count() << ',' << evidence_level << '\n';
    }
    std::cout << "results=" << output << " scenarios=" << scenarios.size() << '\n';
    return 0;
}
