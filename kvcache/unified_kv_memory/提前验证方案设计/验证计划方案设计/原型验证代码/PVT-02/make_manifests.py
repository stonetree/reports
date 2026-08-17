#!/usr/bin/env python3
"""
make_manifests.py - 生成模拟 vLLM / SGLang 内存池中具有不同离散碎片特征的 ExtentManifest
"""
import argparse
import json
import random

def generate_manifest(block_count: int, fragmentation: float, output_file: str):
    random.seed(42)
    blocks = []
    curr_addr = 0x200000000

    for i in range(block_count):
        b = {
            "block_id": i,
            "phys_addr": hex(curr_addr),
            "size_bytes": 65536 # 64KB per block (e.g. 16 tokens fp16)
        }
        blocks.append(b)

        if random.random() < fragmentation:
            # 离散跳跃 (模拟显存碎片)
            curr_addr += random.randint(0x100000, 0x800000)
        else:
            curr_addr += 65536

    data = {
        "block_count": block_count,
        "fragmentation_ratio": fragmentation,
        "blocks": blocks
    }

    with open(output_file, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)

    print(f"Generated manifest with {block_count} blocks (Frag: {fragmentation*100:.0f}%) -> {output_file}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--block-count", type=int, default=1024)
    parser.add_argument("--frag", type=float, default=0.5)
    parser.add_argument("--out", type=str, default="manifest_1024_frag0.5.json")
    args = parser.parse_args()

    generate_manifest(args.block_count, args.frag, args.out)
