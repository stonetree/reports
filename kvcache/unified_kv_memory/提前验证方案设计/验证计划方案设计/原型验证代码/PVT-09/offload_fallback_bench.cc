// PVT-09 双轨结果 Schema 脚手架。固定样例全部标为 DEMO，不关闭正式结论。
#include <fstream>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    bool hardware_supported = false;
    std::string output = "res_dpu_benchmark.csv";
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--hardware-supported") hardware_supported = true;
        else if (arg == "--out" && i + 1 < argc) output = argv[++i];
    }
    std::ofstream stream(output);
    stream << "mode,payload_mb,bandwidth_gbps,latency_ms,host_cpu_pct,fault_detect_us,fallback_switch_us,request_success_rate,packet_loss_count,integrity_ok,actual_path,evidence_level,status\n";
    stream << "RAW_DIRECT,64,685,0.75,0.5,,,,,,RAW_DIRECT,DEMO,DEMO_ONLY\n";
    if (hardware_supported) {
        stream << "DPU_HARDWARE_OFFLOAD,64,660,0.78,1.2,,,,,TRUE,DPU_AES_CRC,DEMO,DEMO_ONLY\n";
        stream << "DPU_FAULT_FALLBACK,64,,,,80,120,1.0,0,TRUE,RAW_DIRECT,DEMO,DEMO_ONLY\n";
    } else {
        stream << "DPU_HARDWARE_OFFLOAD,64,,,,,,,,,,DEMO,NOT_SUPPORTED\n";
        stream << "DPU_FAULT_FALLBACK,64,,,,,,,,,,DEMO,NOT_SUPPORTED\n";
    }
    std::cout << "output=" << output << " evidence_level=DEMO dpu_status="
              << (hardware_supported ? "DEMO_ONLY" : "NOT_SUPPORTED")
              << " formal_fallback_threshold_us=500\n";
    return 0;
}
