import asyncio
import json

import httpx
import pytest

from reglament_agent.model import (
    GenerationSlot, JsonlSink, MemorySink, ModelOutputError, ModelSettings,
    ModelUnavailable, OllamaClient,
)

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}}}


def vec(x=0.1):
    return [x] * 1024


def chat_body(content='{"answer": "ok"}', **kw):
    b = {
        "model": "qwen3.5:9b-q4_K_M",
        "message": {"role": "assistant", "content": content},
        "done": True, "done_reason": "stop",
        "total_duration": 2_500_000_000, "load_duration": 100_000_000,
        "prompt_eval_count": 120, "prompt_eval_duration": 400_000_000,
        "eval_count": 15, "eval_duration": 1_500_000_000,
    }
    b.update(kw)
    return b


def make(handler, **kw):
    sink = MemorySink()
    c = OllamaClient(ModelSettings(embed_batch=2), sink=sink,
                     transport=httpx.MockTransport(handler), **kw)
    return c, sink


async def test_embed_batches_and_body():
    bodies = []

    def h(req):
        b = json.loads(req.content)
        bodies.append(b)
        return httpx.Response(200, json={"embeddings": [vec() for _ in b["input"]],
                                         "prompt_eval_count": 3})

    c, sink = make(h)
    out = await c.embed(["a", "b", "c"])
    assert len(out) == 3 and [len(b["input"]) for b in bodies] == [2, 1]
    assert bodies[0]["model"] == "bge-m3" and bodies[0]["options"] == {"num_gpu": 0}
    assert bodies[0]["keep_alive"] == "30m"
    assert sink.records[0].kind == "embed" and sink.records[0].prompt_tokens == 6


@pytest.mark.parametrize("bad", [[0.1] * 10, [float("nan")] + [0.1] * 1023, ["x"] * 1024])
async def test_embed_validates(bad):
    def h(req):
        # json.dumps emits NaN literal, which httpx can parse back
        return httpx.Response(200, content=json.dumps({"embeddings": [bad]}),
                              headers={"content-type": "application/json"})

    c, sink = make(h)
    with pytest.raises(ModelOutputError):
        await c.embed(["a"])
    assert sink.records[0].ok is False


async def test_chat_request_and_mapping():
    seen = {}

    def h(req):
        seen.update(json.loads(req.content))
        return httpx.Response(200, json=chat_body())

    c, sink = make(h)
    r = await c.chat_json([{"role": "user", "content": "hi"}], SCHEMA,
                          step="answer", request_id="r1", options={"num_predict": 64})
    assert seen["think"] is False and seen["stream"] is False and seen["format"] == SCHEMA
    assert seen["options"] == {"temperature": 0, "num_ctx": 8192, "num_predict": 64, "seed": 42}
    assert r.data == {"answer": "ok"}
    assert (r.prompt_tokens, r.completion_tokens) == (120, 15)
    assert (r.total_ms, r.load_ms, r.prompt_ms, r.gen_ms) == (2500, 100, 400, 1500)
    rec = sink.records[0]
    assert rec.request_id == "r1" and rec.step == "answer" and rec.kind == "chat"
    assert rec.ok and rec.prompt_tokens == 120 and rec.completion_tokens == 15 and rec.load_ms == 100


async def test_bad_json_and_truncation():
    bodies = iter([chat_body("not json"), chat_body('{"answer": "x', done_reason="length")])
    c, sink = make(lambda req: httpx.Response(200, json=next(bodies)))
    with pytest.raises(ModelOutputError) as e:
        await c.chat_json([], SCHEMA, step="s")
    assert e.value.raw == "not json"
    with pytest.raises(ModelOutputError, match="truncated") as e:
        await c.chat_json([], SCHEMA, step="s")
    assert e.value.raw == '{"answer": "x'
    assert [r.ok for r in sink.records] == [False, False]
    assert sink.records[1].completion_tokens == 15


async def test_connect_error_and_5xx_logged():
    def boom(req):
        raise httpx.ConnectError("refused")

    c, sink = make(boom)
    with pytest.raises(ModelUnavailable):
        await c.chat_json([], SCHEMA, step="s", request_id="r")
    with pytest.raises(ModelUnavailable):
        await c.embed(["a"])
    assert [(r.kind, r.ok) for r in sink.records] == [("chat", False), ("embed", False)]
    assert "ConnectError" in sink.records[0].error

    c2, sink2 = make(lambda req: httpx.Response(503, text="busy"))
    with pytest.raises(ModelUnavailable):
        await c2.chat_json([], SCHEMA, step="s")
    assert sink2.records[0].ok is False


async def test_queue_and_embed_not_blocked():
    gate = asyncio.Event()
    started = asyncio.Event()

    async def h(req):
        if req.url.path == "/api/embed":
            return httpx.Response(200, json={"embeddings": [vec()]})
        started.set()
        await gate.wait()
        return httpx.Response(200, json=chat_body())

    slot = GenerationSlot(1)
    c, sink = make(h, slot=slot)
    t1 = asyncio.create_task(c.chat_json([], SCHEMA, step="a"))
    await started.wait()
    positions = []
    t2 = asyncio.create_task(c.chat_json([], SCHEMA, step="b", on_position=positions.append))
    while slot.waiting < 1:
        await asyncio.sleep(0)
    assert slot.waiting == 1 and positions == [1]
    # embed passes while the slot is held
    assert len(await asyncio.wait_for(c.embed(["x"]), 1)) == 1
    await asyncio.sleep(0.02)
    gate.set()
    r1, r2 = await asyncio.gather(t1, t2)
    assert r1.queue_ms < 5 and r2.queue_ms >= 15
    assert slot.waiting == 0 and slot.active == 0
    chats = [r for r in sink.records if r.kind == "chat"]
    assert {r.step: r.queue_ms > 0 for r in chats}["b"]


async def test_slot_positions_update():
    slot = GenerationSlot(1)
    pos = {"b": [], "c": []}
    release = asyncio.Event()

    async def hold():
        async with slot.acquire():
            await release.wait()

    async def waiter(n):
        async with slot.acquire(on_position=pos[n].append):
            pass

    t0 = asyncio.create_task(hold())
    await asyncio.sleep(0)
    tb = asyncio.create_task(waiter("b"))
    await asyncio.sleep(0)
    tc = asyncio.create_task(waiter("c"))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(t0, tb, tc)
    assert pos["b"] == [1] and pos["c"] == [2, 1]


async def test_health():
    def h(req):
        if req.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "bge-m3:latest"}, {"name": "qwen3.5:9b-q4_K_M"}]})
        return httpx.Response(200, json={"models": [{"name": "bge-m3:latest"}]})

    c, _ = make(h)
    r = await c.health()
    assert r["chat_present"] and not r["chat_loaded"] and r["embed_present"] and r["embed_loaded"]


def test_jsonl_sink(tmp_path):
    from reglament_agent.model import CallRecord
    p = tmp_path / "sub" / "calls.jsonl"
    s = JsonlSink(p)
    s.record(CallRecord("r", "answer", "chat", "m", prompt_tokens=5))
    s.record(CallRecord(None, "e", "embed", "bge-m3", ok=False, error="x"))
    lines = p.read_text().splitlines()
    assert len(lines) == 2
    d = json.loads(lines[0])
    assert d["request_id"] == "r" and d["prompt_tokens"] == 5 and "started_at" in d


async def test_failing_position_callback_does_not_stall_slot():
    from reglament_agent.model.queue import GenerationSlot

    slot = GenerationSlot(1)

    def broken(_position):
        raise RuntimeError("UI gone")

    async with slot.acquire():
        waiter = asyncio.create_task(_hold_briefly(slot, broken))
        await asyncio.sleep(0.01)
        assert slot.waiting == 1
    await asyncio.wait_for(waiter, 1)
    assert slot.active == 0 and slot.waiting == 0


async def _hold_briefly(slot, callback):
    async with slot.acquire(on_position=callback):
        await asyncio.sleep(0)


def test_client_ignores_system_proxy(monkeypatch):
    from reglament_agent.model.ollama import OllamaClient

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("ALL_PROXY", "socks5://proxy.invalid:1080")
    client = OllamaClient()
    assert client._http._trust_env is False
