import asyncio
import json
from pathlib import Path
import tempfile
import unittest

from aiohttp import web

from kvcbench.analysis import summarize
from kvcbench.replay import replay
from kvcbench.runner import schedule, calibration_identity
from kvcbench.config import load
from kvcbench.telemetry import parse_prometheus


BASE = Path(__file__).resolve().parents[1]
TMP = BASE.parents[1] / "tmp" / "kvcbench"


class Scheduling(unittest.TestCase):
    def test_normalized_qps_changes_arrival_schedule(self):
        c=load(BASE/"config.toml")
        c["trace"]["normalized_path"]="fixture"
        rows=[{"request_id":str(i),"arrival_s":i,"prompt":"a","max_tokens":2} for i in range(6)]
        a=schedule(rows,1,60,c)
        b=schedule(rows,10,60,c)
        self.assertAlmostEqual(a[1]["arrival_s"],1)
        self.assertAlmostEqual(b[1]["arrival_s"],.1)
        old=calibration_identity(c)
        c["variants"]["B0"]["hardware_id"]="other-host"
        self.assertNotEqual(old,calibration_identity(c))

    def test_metric_label_filter_and_finite_only(self):
        text='m{rank="0",role="p"} 5\nm{rank="1",role="p"} 7\nm{rank="2",role="p"} NaN\n'
        self.assertEqual(parse_prometheus(text,"m",{"rank":"1"}),[7])


class Streaming(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        TMP.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(dir=TMP)
        self.out=Path(self.tmp.name)
        app=web.Application()
        app.router.add_post('/v1/completions',self.respond)
        self.server=web.AppRunner(app)
        await self.server.setup()
        site=web.TCPSite(self.server,'127.0.0.1',0)
        await site.start()
        port=site._server.sockets[0].getsockname()[1]
        self.spec={"base_url":f"http://127.0.0.1:{port}","model":"m"}
        self.run={"duration_s":.1,"drain_s":.2,"request_timeout_s":1,"max_inflight":1,"max_pending":1}

    async def asyncTearDown(self):
        await self.server.cleanup()
        self.tmp.cleanup()

    async def respond(self,request):
        body=await request.json()
        mode=body["prompt"]
        if mode=='http_error':
            return web.Response(status=503,text="unavailable")
        response=web.StreamResponse(headers={'Content-Type':'text/event-stream'})
        await response.prepare(request)
        if mode=='slow':
            await asyncio.sleep(.05)
        count=100 if mode=='batch' else 2
        frames=[{"choices":[{"text":"a","token_ids":[1]*count if mode=='batch' else [1],"finish_reason":"stop" if mode=='batch' else None}]}]
        if mode!='batch':
            frames.append({"choices":[{"text":"b","token_ids":[2],"finish_reason":"stop"}]})
        frames.append({"choices":[],"usage":{"completion_tokens":count}})
        try:
            for frame in frames:
                encoded=("data: "+json.dumps(frame)+"\n\n").encode()
                # Fragment a single event across network writes.
                await response.write(encoded[:7])
                await response.write(encoded[7:])
                await asyncio.sleep(.001)
            if mode!='eof':
                await response.write(b'data: [DONE]\n\n')
            await response.write_eof()
        except (ConnectionResetError,RuntimeError):
            pass
        return response

    def requests(self,n,prompt):
        return [{"request_id":str(i),"session_id":str(i),"prompt":prompt,"max_tokens":100,"arrival_s":0.0} for i in range(n)]

    async def test_bounded_open_loop_keeps_drops_in_cohort(self):
        rows=await replay(self.requests(10,'slow'),self.spec,self.run,self.out/'requests.jsonl')
        self.assertEqual(len(rows),10)
        self.assertGreaterEqual(sum(r['status']=='dropped' for r in rows),8)
        s=summarize(rows,.1,1,{"ttft_ms":1000,"tpot_ms":1000,"e2e_ms":1000})
        self.assertEqual(s['cohort_requests'],10)
        self.assertLessEqual(s['cohort_compliance'],.2)

    async def test_fragmented_stream_batch_and_incomplete(self):
        for prompt,expected in [('ok','ok'),('batch','ok'),('eof','error'),('http_error','error')]:
            rows=await replay(self.requests(1,prompt),self.spec,self.run,self.out/f'{prompt}.jsonl')
            self.assertEqual(rows[0]['status'],expected)
            if prompt=='batch':
                self.assertEqual(rows[0]['timing_quality'],'batched_or_unknown')
                s=summarize(rows,.1,1,{"ttft_ms":1000,"tpot_ms":1,"e2e_ms":1000})
                self.assertEqual(s['slo_pass_in_window'],0)
            if prompt=='ok':
                self.assertEqual(rows[0]['completion_tokens'],2)
                self.assertEqual(rows[0]['timing_quality'],'one_token_per_chunk')

    async def test_timeout_retains_partial_first_token(self):
        spec=dict(self.run,request_timeout_s=.02)
        rows=await replay(self.requests(2,'slow'),self.spec,spec,self.out/'timeout.jsonl')
        self.assertEqual(len(rows),2)
        self.assertTrue(all(r['status']=='timeout' for r in rows))


if __name__=='__main__':
    unittest.main()
