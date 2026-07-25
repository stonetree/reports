# gen_matrix_datasets.py
# 自动生成 6 大典型在线推理场景的测试数据集

import json
import os

def generate_datasets():
    print("=== Generating 6-Scenario Workload Datasets ===")
    
    # 1. 代码补全数据集 (Code Completion: Short input, Short output)
    with open("dataset_scenario1_code.jsonl", "w") as f:
        for i in range(100):
            f.write(json.dumps({"prompt": f"def add_numbers(a, b):\n    # Query {i}", "id": i}) + "\n")

    # 2. 短文本 Agent 数据集 (Agent Tool Call: Medium input)
    with open("dataset_scenario2_agent.jsonl", "w") as f:
        for i in range(100):
            prompt = "You are a helpful AI Agent. Available tools: [get_weather, search_web]. Request: " + ("info " * 30) + f" Query {i}"
            f.write(json.dumps({"prompt": prompt, "id": i}) + "\n")

    # 3. 多轮对话前缀共享数据集 (Shared 8K System Prompt)
    system_prompt = "System Policy: You are a professional AI Assistant. " + ("Context Knowledge Base " * 500) # ~8k tokens
    with open("dataset_scenario4_chat.jsonl", "w") as f:
        for i in range(200):
            prompt = system_prompt + f"\nUser Query {i}: What is the summary of the above policy?"
            f.write(json.dumps({"prompt": prompt, "id": i}) + "\n")

    # 4. RAG / 长文本异构数据集 (Long Context Heterogeneous Lengths)
    long_doc = "RAG Long Document Content. " * 8000 # ~64k tokens
    short_doc = "Short Question. " * 2             # ~16 tokens
    with open("dataset_scenario5_rag.jsonl", "w") as f:
        for i in range(150):
            prompt = (long_doc if i % 10 == 0 else short_doc) + f" Query {i}"
            f.write(json.dumps({"prompt": prompt, "id": i}) + "\n")

    print("Successfully generated all dataset files!")

if __name__ == "__main__":
    generate_datasets()
