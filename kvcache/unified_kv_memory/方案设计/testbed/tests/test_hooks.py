from __future__ import annotations
import asyncio, json, os, sys, tempfile, time, unittest
from pathlib import Path
from kvcbench.hooks_runtime import RuntimeHooks, _Finder, doctor_sources

ROOT = Path(__file__).resolve().parents[3]

# The desktop runner can have an inherited Proactor self-pipe that blocks
# loop construction.  This is only a test harness choice, not production code.
if hasattr(asyncio, "WindowsSelectorEventLoopPolicy"):
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

class HooksTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT / "tmp")
        self.base = Path(self.tmp.name); self.event_dir = self.base / "events"; self.config = self.base / "control.json"
        self.mod = self.base / "fake_connector.py"
        self.mod.write_text("""import asyncio\nclass Connector:\n def get_num_new_matched_tokens(self, request=None): return (7, True)\n def boom(self): raise RuntimeError('boom')\n async def load(self): await asyncio.sleep(0); return 'ok'\n""", encoding="utf-8")
        sys.path.insert(0, str(self.base)); sys.modules.pop("fake_connector", None)
        self.write("observe")
        self.hooks = RuntimeHooks(self.config, self.event_dir); self.finder = _Finder(self.hooks); sys.meta_path.insert(0, self.finder)
    def tearDown(self):
        if self.finder in sys.meta_path: sys.meta_path.remove(self.finder)
        sys.path.remove(str(self.base)); sys.modules.pop("fake_connector", None); self.tmp.cleanup()
    def write(self, action, delay=0, probability=1):
        payload = {"case_id":"C", "enabled":True, "targets":[{"module":"fake_connector","class":"Connector","methods":["get_num_new_matched_tokens","boom","load"]}], "rules":[{"module":"fake_connector","class":"Connector","method":"get_num_new_matched_tokens","action":action,"delay_ms":delay,"probability":probability},{"module":"fake_connector","class":"Connector","method":"load","action":"delay","delay_ms":delay}]}
        tmp = self.config.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, self.config)
    def events(self):
        return [json.loads(x) for p in self.event_dir.glob("events-*.jsonl") for x in p.read_text().splitlines()]
    def test_import_then_dynamic_miss_and_delay(self):
        import fake_connector
        c=fake_connector.Connector(); self.assertEqual(c.get_num_new_matched_tokens(), (7,True))
        time.sleep(.003); self.write("miss")
        self.assertEqual(c.get_num_new_matched_tokens(), (0,False))
        self.write("delay", 8)
        t=time.perf_counter(); c.get_num_new_matched_tokens(); self.assertGreaterEqual(time.perf_counter()-t,.006)
        delay_event = [x for x in self.events() if x.get("method") == "get_num_new_matched_tokens"][-1]
        self.assertEqual(delay_event["requested_delay_ns"], 8_000_000)
        self.assertGreater(delay_event["injected_delay_ns"], 0)
        self.assertTrue(any(x.get("lookup_async") is False and x.get("matched_tokens") == 0 for x in self.events()))

    def test_probability_zero_never_bypasses(self):
        import fake_connector
        c=fake_connector.Connector(); self.write("miss", probability=0)
        self.assertEqual(c.get_num_new_matched_tokens(), (7, True))
        lookup = [x for x in self.events() if x.get("method") == "get_num_new_matched_tokens"][-1]
        self.assertEqual(lookup["action"], "miss"); self.assertFalse(lookup["applied"])

    def test_disabled_still_observes_without_injection(self):
        import fake_connector
        payload = json.loads(self.config.read_text()); payload["enabled"] = False
        tmp = self.config.with_suffix(".tmp"); tmp.write_text(json.dumps(payload), encoding="utf-8"); os.replace(tmp, self.config)
        self.assertEqual(fake_connector.Connector().get_num_new_matched_tokens(), (7, True))
        event = [x for x in self.events() if x.get("method") == "get_num_new_matched_tokens"][-1]
        self.assertEqual(event["action"], "observe"); self.assertFalse(event["applied"])

    def test_ast_doctor_does_not_import_target(self):
        result = doctor_sources({"targets":[{"module":"never.import.me","class":"Connector","methods":["load", "missing"],"source_path":str(self.mod)}]})
        self.assertFalse(result["ok"]); self.assertEqual(result["targets"][0]["missing"], ["missing"])
        self.assertNotIn("never.import.me", sys.modules)
    def test_error_and_async_and_no_double_wrap(self):
        import fake_connector
        c=fake_connector.Connector()
        with self.assertRaisesRegex(RuntimeError,"boom"): c.boom()
        # This sandbox's Windows event-loop bootstrap can block in socketpair.
        # Assert that the hook retained coroutine semantics; normal CI executes
        # the coroutine against the platform event loop.
        coro = c.load(); self.assertTrue(asyncio.iscoroutine(coro))
        # asyncio.sleep(0) yields None once; manually drive the coroutine to
        # cover the async wrapper without creating this sandbox's blocked loop.
        yielded = coro.send(None); self.assertIsNone(yielded)
        done = None
        for _ in range(4):
            try: yielded = coro.send(None)
            except StopIteration as stop: done = stop.value; break
        self.assertEqual(done, "ok")
        first = fake_connector.Connector.load; self.hooks.install_module("fake_connector",fake_connector)
        self.assertIs(first, fake_connector.Connector.load)
        self.assertTrue(any(x.get("raised")=="RuntimeError" for x in self.events()))

if __name__ == "__main__": unittest.main()
