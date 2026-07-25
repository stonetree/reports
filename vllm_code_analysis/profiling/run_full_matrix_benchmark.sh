#!/bin/bash
# run_full_matrix_benchmark.sh
# 全自动化运行 6 大场景 Profiling 压测，持久化数据日志，并自动生成结构化分析报告

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
HOSTNAME=$(hostname)
LOG_DIR="logs_profiling_${HOSTNAME}_${TIMESTAMP}"

mkdir -p ${LOG_DIR}

echo "=========================================================================="
echo "    Running Full Business Workload CPU Profiling Matrix                  "
echo "    Log Directory: ${LOG_DIR}                                            "
echo "=========================================================================="

# 1. 生成各场景测试数据集
python3 gen_matrix_datasets.py

SERVER_URL="http://localhost:8000"

# 场景 1: 代码补全 (Code Completion)
echo -e "\n[1/6] Benchmarking Scenario 1: Code Completion (Low Metadata)..."
python3 benchmarks/benchmark_serving.py \
    --backend vllm \
    --dataset-path ./dataset_scenario1_code.jsonl \
    --num-prompts 50 \
    --request-rate 2 > ${LOG_DIR}/log_scenario1.txt 2>&1

# 场景 2: 短文本 Agent (Tool Call)
echo -e "\n[2/6] Benchmarking Scenario 2: Agent Tool Call (Low Metadata)..."
python3 benchmarks/benchmark_serving.py \
    --backend vllm \
    --dataset-path ./dataset_scenario2_agent.jsonl \
    --num-prompts 50 \
    --request-rate 5 > ${LOG_DIR}/log_scenario2.txt 2>&1

# 场景 3: 传统单轮问答 (Single-turn QA)
echo -e "\n[3/6] Benchmarking Scenario 3: Single-turn QA (Medium Metadata)..."
python3 benchmarks/benchmark_serving.py \
    --backend vllm \
    --dataset-name ShareGPT \
    --num-prompts 50 \
    --request-rate 10 > ${LOG_DIR}/log_scenario3.txt 2>&1

# 场景 4: 多轮 Chat (High Prefix Caching)
echo -e "\n[4/6] Benchmarking Scenario 4: Multi-turn Chat (High Prefix Cache)..."
python3 benchmarks/benchmark_serving.py \
    --backend vllm \
    --dataset-path ./dataset_scenario4_chat.jsonl \
    --num-prompts 100 \
    --request-rate 30 > ${LOG_DIR}/log_scenario4.txt 2>&1

# 场景 5: RAG / 长文本异构 (High BlockTable Overhead)
echo -e "\n[5/6] Benchmarking Scenario 5: RAG Long Context (High BlockTable Metadata)..."
python3 benchmarks/benchmark_serving.py \
    --backend vllm \
    --dataset-path ./dataset_scenario5_rag.jsonl \
    --num-prompts 50 \
    --request-rate 10 > ${LOG_DIR}/log_scenario5.txt 2>&1

echo -e "\n=== All Benchmarks Completed! Generating Markdown Analysis Report ==="
# 自动调用报告生成代码，汇总 LogDir 中的数据
python3 parse_profiling_report.py ${LOG_DIR}

echo -e "\nProfiling pipeline finished. Logs & Reports saved in: ${LOG_DIR}"
