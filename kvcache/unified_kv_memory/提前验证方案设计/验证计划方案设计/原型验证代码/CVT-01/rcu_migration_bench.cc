// CVT-01 结果 Schema 脚手架。固定值仅为 DEMO；正式测试需要并发 Reader 和原始停顿样本。
#include <fstream>
#include <iostream>
#include <string>

int main(int argc, char** argv) {
    bool hardware_supported = false;
    std::string output = "res_rcu_migration.csv";
    for (int i = 1; i < argc; ++i) {
        const std::string arg = argv[i];
        if (arg == "--hardware-supported") hardware_supported = true;
        else if (arg == "--out" && i + 1 < argc) output = argv[++i];
    }
    std::ofstream stream(output);
    stream << "scheme,concurrent_readers,p99_pause_ms,p99_read_us,tpot_interference_pct,corrupted_reads,rollback_safe,evidence_level,status\n";
    stream << "STOP_THE_WORLD,32,14.5,14500,125,0,FALSE,DEMO,DEMO_ONLY\n";
    stream << "SOFTWARE_RCU_COPY_ON_MIGRATE,32,0.08,3.8,1.8,0,TRUE,DEMO,DEMO_ONLY\n";
    if (hardware_supported) stream << "HARDWARE_ATOMIC_REMAP,32,0.05,3.5,1.2,0,TRUE,DEMO,DEMO_ONLY\n";
    else stream << "HARDWARE_ATOMIC_REMAP,32,,,,,,,DEMO,NOT_SUPPORTED\n";
    std::cout << "output=" << output
              << " evidence_level=DEMO formal_gates=p99_pause_ms<1,tpot_interference_pct<3,corrupted_reads=0"
              << " hardware_status=" << (hardware_supported ? "DEMO_ONLY" : "NOT_SUPPORTED") << '\n';
    return 0;
}
