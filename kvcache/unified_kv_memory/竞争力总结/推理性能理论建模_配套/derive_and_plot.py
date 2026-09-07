"""复算理论条件并生成参数曲线；不读取或生成硬件性能样本。

依赖：Python 3.10+、NumPy、Matplotlib。
输出只位于本脚本目录：四幅 PNG、verification.json。
"""
from __future__ import annotations

import hashlib
import json
import math
import random
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib import font_manager
from matplotlib.ticker import PercentFormatter
import numpy as np

ROOT = Path(__file__).resolve().parent
PARAMETERS = json.loads((ROOT / "parameters.json").read_text(encoding="utf-8"))
P = PARAMETERS["illustration_parameters"]
COLORS = ["#1D4ED8", "#64748B", "#0F172A"]
STYLES = ["-", "--", ":"]


def pipeline_recurrence(transfer: list[float], compute: list[float]) -> float:
    """独立按数据到齐和前一层完成事件推进。"""
    if len(transfer) != len(compute) or not transfer:
        raise ValueError("阶段数组必须非空且等长")
    arrival = finish = 0.0
    for x, c in zip(transfer, compute):
        if x < 0 or c < 0:
            raise ValueError("时长不能为负")
        arrival += x
        finish = max(finish, arrival) + c
    return finish


def pipeline_closed(stages: int, x: float, c: float) -> float:
    return x + c + (stages - 1) * max(x, c)


def dag_finish(nodes: list[tuple[str, float, list[str]]]) -> float:
    """输入为拓扑顺序，资源约束也必须显式加入前置关系。"""
    finish: dict[str, float] = {}
    for name, duration, predecessors in nodes:
        finish[name] = duration + max((finish[p] for p in predecessors), default=0.0)
    return finish[nodes[-1][0]]


def check_math() -> dict:
    checks = []
    def check(name: str, condition: bool, cases: int, scope: str) -> None:
        if not condition:
            raise AssertionError(name)
        checks.append({"name": name, "result": "VERIFIED_MATH", "cases": cases, "scope": scope})

    rng = random.Random(20260907)
    cases = []
    for _ in range(600):
        stages = rng.randint(1, 128)
        x, c = 10 ** rng.uniform(-4, 3), 10 ** rng.uniform(-4, 3)
        recursive = pipeline_recurrence([x] * stages, [c] * stages)
        formula = pipeline_closed(stages, x, c)
        cases.append(math.isclose(recursive, formula, rel_tol=2e-12))
    check("流水闭式与独立事件递推", all(cases), len(cases), "恒定两阶段耗时，计算传输可并行")
    check("一层流水无重叠收益", pipeline_closed(1, 4, 7) == 11, 1, "结构边界")
    check("流水无额外干扰时不慢于串行", all(pipeline_closed(l, x, c) <= l * (x + c) + 1e-9
        for l in (1, 2, 8, 32) for x in (0, .01, 1, 100) for c in (0, .01, 1, 100)), 64, "含零时间与不平衡边界")
    # 反例：并行导致共享资源变慢，收益并非无条件。
    check("共享资源减速可使流水更慢", pipeline_closed(32, 3, 3) > 32 * (1 + 1), 1, "反例，不是硬件观测")
    original = [("start", 0, []), ("short", 2, ["start"]), ("long", 10, ["start"]), ("end", 1, ["short", "long"])]
    changed = [("start", 0, []), ("short", .1, ["start"]), ("long", 10, ["start"]), ("end", 1, ["short", "long"])]
    check("缩短非关键节点不必改善总时延", dag_finish(original) == dag_finish(changed), 1, "关键路径反例")

    conditions = []
    for _ in range(500):
        u, q = rng.uniform(1, 1000), rng.uniform(.001, 10)
        reduction = rng.uniform(.3, .4)
        t0, t1 = u + q, u + q * (1 - reduction)
        conditions.append(math.isclose((t0 - t1) / t0, reduction * q / t0, rel_tol=1e-8))
    check("查询直接收益由总占比折算", all(conditions), len(conditions), "查询串行且其他环节不变")

    eps = P["tpot_interference_target"]
    thresholds = []
    for w in P["decode_memory_time_fractions"]:
        beta = eps / (w + eps)
        tfix, memory = 1 - w, w
        delta = tfix + memory / (1 - beta) - 1
        thresholds.append({"memory_fraction": w, "max_effective_bandwidth_loss": beta})
        check(f"TPOT 阈值代回原始耗时式 w={w}", math.isclose(delta, eps, abs_tol=1e-12), 1, "带宽受限暴露时间模型")

    conditions = []
    for _ in range(100):
        n = rng.randint(1, 200)
        p = rng.randint(0, n)
        a, b = rng.uniform(1, 10), rng.uniform(1, 10)
        full = a * n + b * sum(range(1, n + 1))
        suffix = a * (n - p) + b * sum(range(p + 1, n + 1))
        conditions.append(math.isclose(full - suffix, a * p + b * p * (p + 1) / 2, rel_tol=1e-10, abs_tol=1e-9))
    check("因果注意力运算量与逐位置求和", all(conditions), len(conditions), "固定稠密结构，非运行时间模型")
    check("缓存字节量单位", 2 * 32 * 8192 * 8 * 128 * 2 == 2 ** 30, 1, "纯结构示例 1 GiB；非指定业务模型")

    # 网络演算水平偏移条件，以与闭式不同的区间不等式核验。
    conditions = []
    for _ in range(100):
        rate = rng.uniform(1, 100)
        arrival_rate = rng.uniform(.01, .95) * rate
        burst, latency = rng.uniform(.1, 50), rng.uniform(0, 5)
        delay = latency + burst / rate
        u = np.linspace(0, 100, 501)
        rhs = rate * np.maximum(u + delay - latency, 0)
        conditions.append(bool(np.all(burst + arrival_rate * u <= rhs + 1e-9)))
        # 缩小界后，起始突发无法全部按期服务。
        conditions.append(burst > rate * max(delay - 1e-6 - latency, 0))
    check("到达服务曲线时延界", all(conditions), 100, "速率时延与仿射到达；不检验实际设备保证")

    conditions = []
    for _ in range(100):
        capacity = rng.uniform(1, 100)
        bg_rate = .4 * capacity
        t0, burst = rng.uniform(0, 3), rng.uniform(.1, 5)
        rate = capacity - bg_rate
        latency = (capacity * t0 + burst) / rate
        u = np.linspace(0, 30, 301)
        residual = np.maximum(capacity * np.maximum(u - t0, 0) - burst - bg_rate * u, 0)
        curve = rate * np.maximum(u - latency, 0)
        conditions.append(bool(np.allclose(residual, curve, rtol=1e-10, atol=1e-9)))
    check("剩余服务率时延表达", all(conditions), 100, "严格总服务与受约束后台")

    coflow = []
    for members in (1, 2, 4, 8, 16):
        p_ind = .99 ** (1 / members)
        p_union = 1 - .01 / members
        check(f"组完成覆盖率 k={members}", math.isclose(p_ind ** members, .99, abs_tol=1e-12)
              and members * (1 - p_union) <= .01 + 1e-12, 2, "独立同分布与无独立假设的并集条件分别计算")
        coflow.append({"members": members, "iid_member_quantile": p_ind, "union_member_coverage": p_union})

    ratios = []
    for rho in (.05, .5, .85, .95):
        for k in (.7, .85, .95):
            service = np.array([.2, .5, 1, 2])
            mean, second = float(service.mean()), float((service ** 2).mean())
            rate = rho / mean
            old_wait = rate * second / (2 * (1 - rate * mean))
            new_wait = rate * (k * k * second) / (2 * (1 - rate * k * mean))
            ratios.append(math.isclose(new_wait / old_wait, k * k * (1 - rho) / (1 - k * rho), rel_tol=1e-12))
    check("平均排队缩放与两次原式计算", all(ratios), len(ratios), "M/G/1 平均等待；不推断 P99")

    for alpha in (.21, .3, .6, .9):
        for eta in (0, .01, .03):
            r = 1 - (.2 + eta) / alpha
            check(f"TTFT 反推阈值 alpha={alpha} eta={eta}", math.isclose(alpha * (1-r)-eta, .2, abs_tol=1e-12), 1,
                  "负 r 表示物理上无法满足该单项预算")
    return {
        "evidence_type": "THEORETICAL_PARAMETER_SWEEP",
        "status": "MATH_CHECKS_PASSED",
        "measurement_claim": "无硬件实测或项目验收结论",
        "checks": checks,
        "illustrative_results": {
            "query_share_needed_for_20pct_ttft_reduction": [.2 / .4, .2 / .3],
            "balanced_32_stage_subpath_reduction": 31 / 64,
            "tpot_thresholds": thresholds,
            "coflow_member_requirements": coflow,
        },
        "parameters_sha256": hashlib.sha256((ROOT / "parameters.json").read_bytes()).hexdigest(),
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
    }


def style_setup() -> None:
    font = Path("C:/Windows/Fonts/msyh.ttc")
    if font.exists():
        font_manager.fontManager.addfont(str(font))
        plt.rcParams["font.family"] = font_manager.FontProperties(fname=str(font)).get_name()
    else:
        plt.rcParams["font.family"] = ["Noto Sans CJK SC", "DejaVu Sans"]
    plt.rcParams.update({"axes.unicode_minus": False, "font.size": 11,
        "axes.spines.top": False, "axes.spines.right": False,
        "axes.labelcolor": "#0F172A", "text.color": "#0F172A",
        "xtick.color": "#475569", "ytick.color": "#475569", "savefig.dpi": 220})


def new_chart(title: str, subtitle: str):
    fig, ax = plt.subplots(figsize=(10.8, 6.6))
    fig.subplots_adjust(left=.10, right=.96, bottom=.20, top=.76)
    fig.text(.10, .935, title, fontsize=19, weight="bold")
    fig.text(.10, .87, subtitle, fontsize=11, color="#475569")
    fig.text(.10, .065, "理论参数曲线 · 未使用硬件实测值 · 假设与公式见配套论证稿", fontsize=10, color="#475569")
    ax.grid(axis="y", color="#E2E8F0", linewidth=.7)
    ax.set_axisbelow(True)
    return fig, ax


def save(fig, filename: str):
    fig.savefig(ROOT / filename, facecolor="white")
    plt.close(fig)


def make_plots():
    style_setup()
    fig, ax = new_chart("较大的端到端改善需要足够大的原始瓶颈", "目标：TTFT 降低 20%；仅改变暴露加载时间，其余时间不变")
    alpha = np.linspace(.001, 1, 700)
    for eta, color, line in zip(P["added_overhead_fractions"], COLORS, STYLES):
        remaining = 1 - (P["ttft_reduction_target"] + eta) / alpha
        remaining[remaining < 0] = np.nan
        ax.plot(alpha, remaining, color=color, linestyle=line, linewidth=2.4, label=f"新增开销占比 {eta:.0%}")
    ax.set(xlim=(0, 1), ylim=(0, .85), xlabel="原始暴露加载时间 / 基线 TTFT（α）", ylabel="增强后加载时间 / 原加载时间（r）的上限")
    ax.xaxis.set_major_formatter(PercentFormatter(1)); ax.yaxis.set_major_formatter(PercentFormatter(1))
    ax.legend(loc="lower right", frameon=False)
    ax.text(.03, .72, "曲线以下满足本模型目标\n若要求 r < 0，则该单项无法达标", color="#334155")
    save(fig, "01_增量收益条件.png")

    fig, ax = new_chart("理想流水收益取决于传输与计算的匹配", "只计算加载与计算子路径；两者可并行且并行不使单阶段变慢")
    ratio = np.unique(np.append(np.logspace(-2, 2, 600), 1.0))
    for layers, color, line in zip(P["pipeline_stage_counts"], COLORS, STYLES):
        gain = (layers - 1) * np.minimum(ratio, 1) / (layers * (ratio + 1))
        ax.plot(ratio, gain, color=color, linestyle=line, linewidth=2.4, label=f"{layers} 层抽象模型")
    ax.set(xscale="log", xlim=(.01, 100), ylim=(0, .55), xlabel="单层传输时间 / 单层计算时间（x/c）", ylabel="子路径相对串行执行的时延降幅")
    ax.yaxis.set_major_formatter(PercentFormatter(1))
    ax.axvline(1, color="#94A3B8", linewidth=1)
    ax.legend(loc="upper left", frameon=False)
    ax.text(2, .44, "32 层且 x=c 时：48.44%\n端到端降幅还需乘子路径占比", fontsize=10)
    save(fig, "02_流水收益边界.png")

    fig, ax = new_chart("前台带宽损失与生成时延干扰呈非线性关系", "固定模型、批量及上下文；β 表示有效显存服务损失，不是后台网络带宽比例")
    beta = np.linspace(0, .3, 500)
    for w, color, line in zip(P["decode_memory_time_fractions"], COLORS, STYLES):
        ax.plot(beta, w * beta / (1 - beta), color=color, linestyle=line, linewidth=2.4, label=f"显存服务时间占比 w={w:.0%}")
    ax.axhline(P["tpot_interference_target"], color="#475569", linewidth=1, linestyle="-.")
    ax.text(.185, .036, "3% 干扰预算", fontsize=10)
    ax.set(xlim=(0, .3), ylim=(0, .45), xlabel="前台有效显存服务速率损失（β）", ylabel="生成时延相对纯前台的增加比例")
    ax.xaxis.set_major_formatter(PercentFormatter(1)); ax.yaxis.set_major_formatter(PercentFormatter(1))
    ax.legend(loc="upper left", frameon=False)
    save(fig, "03_TPOT干扰预算.png")

    fig, ax = new_chart("高负载下，服务缩短可以放大平均排队收益", "M/G/1：泊松到达、单服务台、先来先服务；服务时间均缩为原来的 k，固定到达率")
    rho = np.linspace(.01, .98, 600)
    for k, color, line in zip(P["queue_service_scaling_factors"], COLORS, STYLES):
        ax.plot(rho, k * k * (1-rho) / (1-k*rho), color=color, linestyle=line, linewidth=2.4, label=f"服务时间缩为原来的 {k:.0%}")
    ax.set(xlim=(0, 1), ylim=(0, 1), xlabel="基线服务台负载率（ρ）", ylabel="优化后平均排队 / 基线平均排队")
    ax.xaxis.set_major_formatter(PercentFormatter(1)); ax.yaxis.set_major_formatter(PercentFormatter(1))
    ax.legend(loc="lower left", frameon=False)
    ax.text(.49, .94, "均值模型，不能解释为 P99 预测", fontsize=10)
    save(fig, "04_平均排队敏感度.png")


if __name__ == "__main__":
    result = check_math()
    make_plots()
    result["figures"] = [{"name": p.name, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                         for p in sorted(ROOT.glob("0[1-4]_*.png"))]
    (ROOT / "verification.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": result["status"], "check_groups": len(result["checks"]),
                      "evaluated_cases": sum(c["cases"] for c in result["checks"]),
                      "figures": len(result["figures"])}, ensure_ascii=False))
