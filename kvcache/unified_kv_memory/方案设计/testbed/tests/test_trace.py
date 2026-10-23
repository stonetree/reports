from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from kvcbench.trace import prepare


def _workspace_temp():
    root = Path.cwd() / "tmp"
    root.mkdir(exist_ok=True)
    return tempfile.TemporaryDirectory(dir=root)


class TracePrepareTests(unittest.TestCase):
    def test_task_corpus_multiturn_salt_and_manifest(self):
        with _workspace_temp() as temp:
            root = Path(temp) / "trace"
            task = root / "deep-swe" / "tasks" / "one"
            task.mkdir(parents=True)
            (task / "instruction.md").write_text("inspect a repository", encoding="utf-8")
            out = Path(temp) / "out"
            requests = prepare({"root": str(root), "datasets": ["deep-swe"], "turns": 2,
                                "replicas": 2, "salt_mode": "session", "salt_transport": "prefix",
                                "seed": 9, "output_tokens": 3}, out)
            self.assertEqual(4, len(requests))
            self.assertEqual(requests[0]["reusable_tokens"], 0)
            self.assertGreater(requests[1]["reusable_tokens"], 0)
            self.assertLess(requests[1]["reusable_tokens"], requests[1]["input_tokens"])
            self.assertNotEqual(requests[0]["prompt"].split("\n")[0], requests[2]["prompt"].split("\n")[0])
            manifest = json.loads((out / "trace_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("synthetic_schedule_from_task_corpus", manifest["classification"])
            self.assertEqual(1, manifest["loaded_prompt_count_by_dataset"]["deep-swe"])

    def test_normalized_preserves_prompt_and_rejects_duplicate(self):
        with _workspace_temp() as temp:
            raw = Path(temp) / "work.jsonl"
            raw.write_text(json.dumps({"request_id": "a", "session_id": "s", "arrival_s": 2, "prompt": [1, 2], "max_tokens": 3}) + "\n", encoding="utf-8")
            requests = prepare({"normalized_path": str(raw)}, Path(temp) / "out")
            self.assertEqual([1, 2], requests[0]["prompt"])
            self.assertNotIn("token_ids", requests[0])
            manifest = json.loads((Path(temp) / "out" / "trace_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual("unknown", manifest["timing_provenance"])
            raw.write_text('{"request_id":"a","prompt":"x"}\n{"request_id":"a","prompt":"y"}\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                prepare({"normalized_path": str(raw)}, Path(temp) / "again")
            raw.write_text('{"request_id":"a","arrival_s":-1,"prompt":"x"}\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                prepare({"normalized_path": str(raw)}, Path(temp) / "negative")
            raw.write_text('{"request_id":"a","arrival_s":0,"prompt":[1,true],"max_tokens":1}\n', encoding="utf-8")
            with self.assertRaises(ValueError):
                prepare({"normalized_path": str(raw)}, Path(temp) / "bad-tokens")

    def test_request_salt_removes_reuse(self):
        with _workspace_temp() as temp:
            root = Path(temp) / "trace"
            task = root / "deep-swe" / "tasks" / "one"
            task.mkdir(parents=True)
            (task / "instruction.md").write_text("test", encoding="utf-8")
            requests = prepare({"root": str(root), "datasets": ["deep-swe"], "turns": 2,
                                "salt_mode": "request"}, Path(temp) / "out")
            self.assertEqual(0, requests[1]["reusable_tokens"])

    def test_context_keeps_source_text_and_replica_salts_are_stable(self):
        with _workspace_temp() as temp:
            root = Path(temp) / "trace"
            task = root / "deep-swe" / "tasks" / "one"
            task.mkdir(parents=True)
            (task / "instruction.md").write_text("SOURCE-CONTENT", encoding="utf-8")
            config = {"root": str(root), "datasets": ["deep-swe"], "replicas": 2,
                      "context_tokens": 3, "salt_mode": "session", "seed": 4}
            first = prepare(config, Path(temp) / "first")
            second = prepare(config, Path(temp) / "second")
            self.assertIn("SOURCE-CONTENT", first[0]["prompt"])
            self.assertIn("SYNTHETIC CONTEXT", first[0]["prompt"])
            self.assertEqual(first[0]["prompt"], second[0]["prompt"])
            self.assertNotEqual(first[0]["prompt"].split("\n")[0], first[1]["prompt"].split("\n")[0])
            manifest = json.loads((Path(temp) / "first" / "trace_manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(3, manifest["context_padding_requested_tokens"])
            self.assertGreater(manifest["context_padding_observed_tokens"], 0)

    def test_configured_tokenizer_counts_text_exactly(self):
        class StubTokenizer:
            def encode(self, text, add_special_tokens=False):
                return list(range(len(text.split())))

        with _workspace_temp() as temp, patch("kvcbench.trace._get_tokenizer", return_value=StubTokenizer()):
            root = Path(temp) / "trace"
            task = root / "deep-swe" / "tasks" / "one"
            task.mkdir(parents=True)
            (task / "instruction.md").write_text("three exact words", encoding="utf-8")
            request = prepare({"root": str(root), "datasets": ["deep-swe"], "tokenizer_path": "stub"}, Path(temp) / "out")[0]
            self.assertIsInstance(request["prompt"], str)
            self.assertEqual("tokenizer", request["token_count_kind"])
            self.assertEqual(len(request["prompt"].split()), request["input_tokens"])


if __name__ == "__main__":
    unittest.main()
