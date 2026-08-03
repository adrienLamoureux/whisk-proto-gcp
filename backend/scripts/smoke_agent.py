"""Drive the real ADK agent loop offline. Run: python -m scripts.smoke_agent"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agent.whisk_runner import AgentService  # noqa: E402
from app.config import Settings  # noqa: E402
from app.deps import build_deps  # noqa: E402
from app.rag.corpus import ingest_directory  # noqa: E402

PROMPTS = [
    "what can you do?",
    "draw a red fox asleep in the snow, 16:9",
    "show me my recent creations",
    "how does the cost model work?",
    "critique: a cat on a roof",
    "switch to the glacier theme",
]


async def main() -> int:
    settings = Settings(mode="local", local_media_dir=".localdata/smoke-media")
    deps = build_deps(settings)
    await ingest_directory(
        deps.store, deps.embeddings, directory=Path(__file__).parents[2] / "docs"
    )

    service = AgentService(deps)
    uid = "smoke-user"

    for prompt in PROMPTS:
        print(f"\n\033[1m> {prompt}\033[0m")
        result = await service.run_turn(uid=uid, message=prompt, session_id="smoke")
        for call in result.tool_calls:
            status = f"error={call.error}" if call.error else "ok"
            print(f"  [tool:{call.kind}] {call.name}({call.args}) -> {status}")
            if call.result and not call.error:
                preview = {k: v for k, v in call.result.items() if k != "items"}
                print(f"           {str(preview)[:160]}")
        print(f"  \033[36m{result.text}\033[0m")
        print(f"  ({result.latency_ms}ms, {result.input_tokens}+{result.output_tokens} tok)")

    usage = await deps.guardrails.usage_snapshot(uid)
    print(f"\nusage: {usage}")

    history = await service.history(uid, "smoke")
    print(f"persisted transcript entries: {len(history)}")
    await deps.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
