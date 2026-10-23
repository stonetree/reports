import json
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path

from kvcbench.analysis import summarize, generate_report, _knee, _telemetry_value


class AnalysisTest(unittest.TestCase):
    def test_window_failures_and_drain(self):
        rows=[
          {"arrival_s":.1,"first_token_s":.2,"finish_s":.8,"completion_tokens":3,"token_times_s":[.2,.4,.6],"timing_quality":"server_token_timestamps","status":"ok"},
          {"arrival_s":.2,"first_token_s":.3,"finish_s":1.4,"completion_tokens":3,"token_times_s":[.3,.6,1.2],"timing_quality":"server_token_timestamps","status":"ok"},
          {"arrival_s":.3,"status":"dropped"},
        ]
        s=summarize(rows,1,1,{"ttft_ms":200,"tpot_ms":300})
        self.assertEqual(s["cohort_requests"],3); self.assertEqual(s["slo_pass_in_window"],1)
        self.assertAlmostEqual(s["slo_goodput_rps"],1); self.assertGreater(s["drain_s"],0)

    def test_batched_sse_is_not_tokens_and_single_token_allowed(self):
        rows=[{"arrival_s":0,"first_token_s":.01,"finish_s":.02,"completion_tokens":1,"token_times_s":[.01],"timing_quality":"one_token_per_chunk","chunk_token_counts":[1],"status":"ok","emission_kind":"sse"},
              {"arrival_s":.1,"first_token_s":.2,"finish_s":.3,"token_times_s":[.2,.25,.3],"timing_quality":"batched_or_unknown","status":"ok","emission_kind":"sse"}]
        s=summarize(rows,1,1,{"tpot_ms":1})
        self.assertEqual(s["slo_pass_in_window"],1)
        self.assertEqual(s["missing"]["unreliable_token_count"],1)

    def test_report_does_not_pair_mismatched_workload(self):
        test_tmp = Path(__file__).resolve().parents[1] / "tmp" / ("analysis-" + uuid.uuid4().hex)
        test_tmp.mkdir(parents=True)
        try:
            d = str(test_tmp)
            root=Path(d); (root/"cases").mkdir(); (root/"manifest.json").write_text(json.dumps({"evidence_class":"MEASURED"}))
            for name, variant, workload in (("a","B0","x"),("b","B1","y")):
                q=root/"cases"/name; q.mkdir(); (q/"case.json").write_text(json.dumps({"case_id":name,"variant":variant,"repeat":1,"cell_id":"c","workload_id":workload,"measured_axes":{"P_HBM":1,"L":.8}})); (q/"summary.json").write_text(json.dumps({"slo_goodput_per_accelerator_rps":2,"latency_ms":{"ttft_p99":1},"missing":{}}))
            out=generate_report(root)
            self.assertEqual(out["attribution"][0]["paired_repeats"],0)
            for name in ("knees.csv", "regions.csv", "diagnostic_surfaces.svg", "region_readme.md"):
                self.assertTrue((root / "reports" / name).exists())
        finally:
            shutil.rmtree(test_tmp)

    def test_zero_token_is_never_slo_pass_and_failed_drain_excluded(self):
        rows=[{"arrival_s":.1,"first_token_s":.2,"finish_s":.8,"completion_tokens":0,"token_times_s":[],"status":"ok"},
              {"arrival_s":.2,"finish_s":1.5,"completion_tokens":9,"status":"error"},
              {"arrival_s":.3,"first_token_s":.4,"finish_s":1.6,"completion_tokens":2,"token_times_s":[.4,.6],"timing_quality":"server_token_timestamps","status":"ok"}]
        s=summarize(rows,1,1,{})
        self.assertEqual(s["slo_pass_in_window"],0)
        self.assertEqual(s["drain_completed_requests"],1)
        self.assertAlmostEqual(s["drain_throughput_rps"],1/.6)

    def test_knee_does_not_fabricate_from_mixed_curve(self):
        # A linear curve has enough points but fails the material slope-change test.
        linear=[{"load":x,"ttft_p99":10+3*x} for x in range(6)]
        self.assertEqual(_knee(linear)["status"],"no_supported_knee")
        # Callers group slices first; independently testing each makes the
        # accidental cross-variant pooling failure visible.
        a=[{"load":x,"ttft_p99":10+x} for x in range(6)]
        b=[{"load":x,"ttft_p99":80-x} for x in range(6)]
        self.assertEqual(_knee(a)["status"],"no_supported_knee")
        self.assertEqual(_knee(b)["status"],"no_supported_knee")

    def test_batched_sse_cannot_fake_tpot(self):
        base={"arrival_s":0,"first_token_s":.1,"finish_s":.2,"completion_tokens":100,"token_times_s":[.1],"status":"ok"}
        s=summarize([{**base,"timing_quality":"batched_or_unknown","chunk_token_counts":[100]}],1,1,{"tpot_ms":1})
        self.assertEqual(s["slo_pass_in_window"],0)
        strict={**base,"token_times_s":[.1+i*.001 for i in range(100)],"timing_quality":"one_token_per_chunk","chunk_token_counts":[1]*100}
        s=summarize([strict],1,1,{"tpot_ms":2})
        self.assertEqual(s["slo_pass_in_window"],1)

    def test_telemetry_stats_mean_and_knee_ignores_workload_hash(self):
        self.assertEqual(_telemetry_value({"ddr_bytes_per_s":{"mean":123,"p95":150}},"ddr_bytes_per_s"),123)
        # Six loads with deliberately distinct workload hashes remain one knee
        # slice because a hash varies naturally when scheduled load changes.
        with tempfile.TemporaryDirectory(dir=str(Path(__file__).resolve().parents[1] / "tmp")) as d:
            root=Path(d); (root/"cases").mkdir(); (root/"manifest.json").write_text(json.dumps({"evidence_class":"HARDWARE_MEASURED"}))
            for load in range(6):
                q=root/"cases"/str(load); q.mkdir()
                case={"case_id":str(load),"variant":"B0","repeat":0,"cell_id":"c","workload_id":f"hash-{load}","pairing_signature":"same-env","knobs":{"context_tokens":8,"gap_s":0,"control_profile":"none","load":load},"measured_axes":{"L":load,"E_reuse":2},"baseline_declaration":{"model":"m","hardware_id":"h"}}
                (q/"case.json").write_text(json.dumps(case)); (q/"summary.json").write_text(json.dumps({"slo_goodput_per_accelerator_rps":1,"latency_ms":{"ttft_p99":10+load},"missing":{}}))
            generate_report(root)
            knees=json.loads((root/"reports"/"refinement.json").read_text())["knees"]
            self.assertEqual(knees[0]["result"]["observed"] if knees[0]["result"]["status"]=="insufficient_unique_load_points" else 6,6)


if __name__ == "__main__": unittest.main()
