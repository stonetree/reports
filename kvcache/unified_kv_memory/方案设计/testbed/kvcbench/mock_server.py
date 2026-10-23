"""Local protocol fixture. These numbers never constitute hardware evidence."""
from __future__ import annotations

import asyncio
import json
from aiohttp import web


async def models(request):
    return web.json_response({"data":[{"id":"mock-model"}]})


async def metrics(request):
    return web.Response(text="# Mock protocol fixture; no hardware counters\nmock_up 1\n")


async def completions(request):
    body = await request.json()
    count = min(int(body.get("max_tokens",8)),8)
    response = web.StreamResponse(headers={"Content-Type":"text/event-stream"})
    await response.prepare(request)
    await asyncio.sleep(.012 if request.match_info["variant"] == "B0" else .003)
    for i in range(count):
        data = {"choices":[{"text":" t","token_ids":[42],"index":0,"finish_reason":"length" if i == count-1 else None}]}
        await response.write(("data: " + json.dumps(data) + "\n\n").encode())
        await asyncio.sleep(.001)
    await response.write(("data: " + json.dumps({"choices":[],"usage":{"completion_tokens":count,"prompt_tokens":32}}) + "\n\n").encode())
    await response.write(b"data: [DONE]\n\n")
    await response.write_eof()
    return response


async def start():
    app = web.Application()
    app.router.add_get("/{variant}/v1/models",models)
    app.router.add_get("/{variant}/metrics",metrics)
    app.router.add_post("/{variant}/v1/completions",completions)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner,"127.0.0.1",0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    return runner,port
