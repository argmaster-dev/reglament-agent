"""Manual smoke test for the laptop with Ollama. Settings come from MODEL_* env vars.

    uv run python scripts/model_smoke.py
"""
import asyncio

from reglament_agent.model import MemorySink, ModelSettings, OllamaClient

SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "status": {"type": "string", "enum": ["answered", "insufficient"]},
    },
    "required": ["answer", "status"],
}
QUESTIONS = [
    "Сколько дней отпуска положено сотруднику?",
    "Как оформить командировку?",
    "Кто согласует договор на этапе пресейла?",
]


async def main() -> None:
    settings = ModelSettings()
    sink = MemorySink()
    async with OllamaClient(settings, sink=sink) as client:
        print("health:", await client.health())
        vecs = await client.embed(QUESTIONS, step="smoke_embed", request_id="smoke")
        print(f"embedded {len(vecs)} vectors, dim={len(vecs[0])}")

        def ask(i: int, q: str):
            msgs = [
                {"role": "system", "content": "Ответь кратко. Если не знаешь, status=insufficient."},
                {"role": "user", "content": q},
            ]
            return client.chat_json(
                msgs, SCHEMA, step=f"smoke_chat_{i}", request_id="smoke",
                on_position=lambda p, i=i: print(f"  chat {i}: в очереди: {p}"),
            )

        results = await asyncio.gather(ask(1, QUESTIONS[0]), ask(2, QUESTIONS[1]))
        for r in results:
            print(r.data)

    hdr = f"{'step':<16}{'kind':<6}{'ok':<6}{'prompt':>7}{'compl':>7}{'queue_ms':>10}{'total_ms':>10}{'load_ms':>9}"
    print(hdr)
    for c in sink.records:
        print(f"{c.step:<16}{c.kind:<6}{c.ok!s:<6}{c.prompt_tokens:>7}{c.completion_tokens:>7}"
              f"{c.queue_ms:>10.0f}{c.total_ms:>10.0f}{c.load_ms:>9.0f}")


if __name__ == "__main__":
    asyncio.run(main())
