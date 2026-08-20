// PVT-08 DEMO 级分发拓扑计算器。开发人员需将公式替换为发送/完成事件实测。
#include <algorithm>
#include <cmath>
#include <fstream>
#include <iostream>
#include <sstream>
#include <stdexcept>
#include <string>
#include <vector>

std::vector<size_t> parse_sizes_mb(const std::string& text) {
    std::vector<size_t> values;
    std::stringstream stream(text);
    std::string item;
    while (std::getline(stream, item, ',')) {
        if (!item.empty() && (item.back() == 'M' || item.back() == 'm')) item.pop_back();
        if (item.empty()) throw std::invalid_argument("empty size in --sizes");
        values.push_back(std::stoull(item));
    }
    if (values.empty()) throw std::invalid_argument("--sizes requires at least one value");
    return values;
}

int main(int argc, char** argv) {
    int nodes = 8;
    std::vector<size_t> payload_sizes_mb = {16};
    bool slow_node = false, hardware_supported = false;
    std::string output = "res_multicast_summary.csv";
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--nodes" && i + 1 < argc) nodes = std::stoi(argv[++i]);
        else if (arg == "--payload-mb" && i + 1 < argc) payload_sizes_mb = {std::stoull(argv[++i])};
        else if (arg == "--sizes" && i + 1 < argc) payload_sizes_mb = parse_sizes_mb(argv[++i]);
        else if (arg == "--slow-node") slow_node = true;
        else if (arg == "--hardware-supported") hardware_supported = true;
        else if (arg == "--out" && i + 1 < argc) output = argv[++i];
    }
    std::ofstream stream(output);
    stream << "mode,payload_mb,nodes,slow_node,source_egress_bytes,normal_nodes_p99_ms,all_nodes_done_ms,retries,evidence_level,status\n";
    for (size_t payload_mb : payload_sizes_mb) {
        const uint64_t payload_bytes = payload_mb * 1024ULL * 1024ULL;
        const uint64_t unicast_egress = payload_bytes * nodes;
        const uint64_t fanout_egress = payload_bytes * std::min(nodes, 2);
        const uint64_t hardware_egress = payload_bytes;
        const double unit_ms = payload_mb / 8.0;
        const double unicast_ms = unit_ms * nodes;
        const double fanout_ms = unit_ms * std::ceil(std::log2(std::max(nodes, 1)));
        const double hardware_ms = unit_ms;
        stream << "N_UNICAST," << payload_mb << ',' << nodes << ',' << slow_node << ',' << unicast_egress << ',' << unicast_ms << ',' << (unicast_ms + (slow_node ? 10 : 0)) << ",0,DEMO,DEMO_ONLY\n";
        stream << "SOFTWARE_STAGING_FANOUT," << payload_mb << ',' << nodes << ',' << slow_node << ',' << fanout_egress << ',' << fanout_ms << ',' << (fanout_ms + (slow_node ? 10 : 0)) << ",0,DEMO,DEMO_ONLY\n";
        stream << "HARDWARE_MULTICAST," << payload_mb << ',' << nodes << ',' << slow_node << ','
               << (hardware_supported ? std::to_string(hardware_egress) : "") << ','
               << (hardware_supported ? std::to_string(hardware_ms) : "") << ','
               << (hardware_supported ? std::to_string(hardware_ms + (slow_node ? 10 : 0)) : "")
               << ",0,DEMO," << (hardware_supported ? "DEMO_ONLY" : "NOT_SUPPORTED") << '\n';
    }
    std::cout << "output=" << output << " evidence_level=DEMO hardware_status="
              << (hardware_supported ? "DEMO_ONLY" : "NOT_SUPPORTED") << '\n';
    return 0;
}
