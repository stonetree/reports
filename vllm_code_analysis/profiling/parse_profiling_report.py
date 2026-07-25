# parse_profiling_report.py
# 自动化解析 vLLM & vLLM-Ascend CPU Profiling 日志并生成汇总报告

import os
import re
import sys
import glob
from datetime import datetime

def parse_single_log(log_path):
    """解析单个压测日志文件，提取 Performance Breakdown"""
    if not os.path.exists(log_path):
        return None

    with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()

    # 检索是否包含 Profiling Summary 块
    if "CPU E2E Performance Breakdown" not in content:
        return None

    data = {
        "main_stages": {},
        "sub_stages": {},
        "total_ms": 0.0
    }

    # 匹配主环节行: ► Stage_1:Tokenization_and_Req_Init  | 18.5210  | 14.82%
    main_pattern = re.compile(r"►\s+(Stage_\d+:[^\s|]+)\s+\|\s+([\d\.]+)\s+\|\s+([\d\.]+)%")
    for match in main_pattern.finditer(content):
        stage_name = match.group(1).strip()
        cost_ms = float(match.group(2))
        ratio_pct = float(match.group(3))
        data["main_stages"][stage_name] = {"cost_ms": cost_ms, "ratio_pct": ratio_pct}

    # 匹配子环节行: └── Sub-2.1:Local_PrefixCache_Lookup | 14.2105 | (11.37% of Total CPU)
    sub_pattern = re.compile(r"└──\s+(Sub-[\d\.]+:[^\s|]+)\s+\|\s+([\d\.]+)\s+\|\s+\(([\d\.]+)% of Total CPU\)")
    for match in sub_pattern.finditer(content):
        sub_name = match.group(1).strip()
        cost_ms = float(match.group(2))
        ratio_pct = float(match.group(3))
        data["sub_stages"][sub_name] = {"cost_ms": cost_ms, "ratio_pct": ratio_pct}

    # 匹配总 CPU 耗时
    total_pattern = re.compile(r"TOTAL CPU TRACKED LATENCY \(100%\)\s+\|\s+([\d\.]+)")
    total_match = total_pattern.search(content)
    if total_match:
        data["total_ms"] = float(total_match.group(1))

    return data

def generate_report(log_dir, output_report_path):
    """扫描日志目录，生成 Markdown 格式的全场景矩阵对比报告"""
    print(f"=== Processing Logs in: {log_dir} ===")
    
    scenario_files = sorted(glob.glob(os.path.join(log_dir, "log_scenario*.txt")))
    if not scenario_files:
        print(f"Error: No scenario log files found in {log_dir}")
        return

    scenario_names = {
        "log_scenario1.txt": "Scenario 1: Code Completion (Low Metadata)",
        "log_scenario2.txt": "Scenario 2: Agent Tool Call (Low Metadata)",
        "log_scenario3.txt": "Scenario 3: Single-turn QA (Medium Metadata)",
        "log_scenario4.txt": "Scenario 4: Multi-turn Chat (High Prefix Cache)",
        "log_scenario5.txt": "Scenario 5: RAG Long Context (High BlockTable)",
        "log_scenario6.txt": "Scenario 6: Distributed KV Pool (High Mooncake RPC)"
    }

    results = {}
    for filepath in scenario_files:
        filename = os.path.basename(filepath)
        sname = scenario_names.get(filename, filename)
        parsed = parse_single_log(filepath)
        if parsed:
            results[sname] = parsed
        else:
            print(f"Warning: Failed to parse profiling data from {filename}")

    if not results:
        print("Error: No valid profiling data extracted.")
        return

    # 生成 Markdown 报告
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    md_lines = []
    md_lines.append("# vLLM & vLLM-Ascend CPU Profiling 全场景数据汇总分析报告\n")
    md_lines.append(f"> **生成时间**: {timestamp}")
    md_lines.append(f"> **日志路径**: `{log_dir}`\n")
    md_lines.append("---")
    md_lines.append("## 一、 6 大业务场景 Profiling 绝对耗时 (ms) 与 占比 (%) 汇总表\n")

    # 构建 Markdown 表格头
    scenarios_list = list(results.keys())
    header = "| Stage / Sub-Stage Name | " + " | ".join([f"**Scenario {i+1}**" for i in range(len(scenarios_list))]) + " |"
    divider = "| :--- | " + " | ".join([":---:" for _ in range(len(scenarios_list))]) + " |"
    md_lines.append(header)
    md_lines.append(divider)

    # 搜集所有出现的主环节和子环节
    all_main_stages = [
        "Stage_1:Tokenization_and_Req_Init",
        "Stage_2:Scheduler_and_Memory_Management",
        "Stage_3:Model_Input_Preparation_and_H2D",
        "Stage_4:Model_Forward_Launch_Overhead",
        "Stage_5:Sampling_and_Logits_PostProcessing",
        "Stage_6:Detokenization_and_HTTP_Stream"
    ]

    for stage in all_main_stages:
        # 主环节行
        cells = []
        for sname in scenarios_list:
            sdata = results[sname]["main_stages"].get(stage)
            if sdata:
                cells.append(f"{sdata['cost_ms']:.2f}ms ({sdata['ratio_pct']:.1f}%)")
            else:
                cells.append("N/A")
        md_lines.append(f"| **► {stage}** | " + " | ".join(cells) + " |")

        # 判断并添加对应的子环节
        if "Stage_2" in stage:
            for sub_name in ["Sub-2.1:Local_PrefixCache_Lookup", "Sub-2.2:Mooncake_Master_RPC_RTT"]:
                sub_cells = []
                for sname in scenarios_list:
                    sub_data = results[sname]["sub_stages"].get(sub_name)
                    if sub_data:
                        sub_cells.append(f"{sub_data['cost_ms']:.2f}ms ({sub_data['ratio_pct']:.1f}%)")
                    else:
                        sub_cells.append("-")
                md_lines.append(f"| &nbsp;&nbsp;&nbsp;&nbsp;└── *{sub_name}* | " + " | ".join(sub_cells) + " |")

        elif "Stage_3" in stage:
            for sub_name in ["Sub-3.1:BlockTable_and_SlotMapping_CPU_Build", "Sub-3.2:BlockTable_H2D_Transfer"]:
                sub_cells = []
                for sname in scenarios_list:
                    sub_data = results[sname]["sub_stages"].get(sub_name)
                    if sub_data:
                        sub_cells.append(f"{sub_data['cost_ms']:.2f}ms ({sub_data['ratio_pct']:.1f}%)")
                    else:
                        sub_cells.append("-")
                md_lines.append(f"| &nbsp;&nbsp;&nbsp;&nbsp;└── *{sub_name}* | " + " | ".join(sub_cells) + " |")

    # 总 CPU 耗时行
    total_cells = [f"**{results[sname]['total_ms']:.2f}ms (100%)**" for sname in scenarios_list]
    md_lines.append(f"| **TOTAL CPU LATENCY** | " + " | ".join(total_cells) + " |")

    # 计算元数据总占比 (Sub-2.1 + Sub-2.2 + Sub-3.1 + Sub-3.2)
    meta_cells = []
    for sname in scenarios_list:
        sub21 = results[sname]["sub_stages"].get("Sub-2.1:Local_PrefixCache_Lookup", {}).get("ratio_pct", 0.0)
        sub22 = results[sname]["sub_stages"].get("Sub-2.2:Mooncake_Master_RPC_RTT", {}).get("ratio_pct", 0.0)
        sub31 = results[sname]["sub_stages"].get("Sub-3.1:BlockTable_and_SlotMapping_CPU_Build", {}).get("ratio_pct", 0.0)
        sub32 = results[sname]["sub_stages"].get("Sub-3.2:BlockTable_H2D_Transfer", {}).get("ratio_pct", 0.0)
        total_meta_pct = sub21 + sub22 + sub31 + sub32
        meta_cells.append(f"**{total_meta_pct:.1f}%**")
    md_lines.append(f"| **元数据处理总占比 (Sub-2+3)** | " + " | ".join(meta_cells) + " |")

    md_content = "\n".join(md_lines)

    # 保存报告
    with open(output_report_path, "w", encoding="utf-8") as f:
        f.write(md_content)

    print(f"\nSuccessfully generated profiling summary report: {output_report_path}")

if __name__ == "__main__":
    if len(sys.argv) > 1:
        log_dir = sys.argv[1]
    else:
        log_dir = "."
    
    output_report_path = os.path.join(log_dir, f"profiling_report_{datetime.now().strftime('%Y%m%m_%H%M%S')}.md")
    generate_report(log_dir, output_report_path)
