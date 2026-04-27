"""One-shot daily-digest trigger — what `praxdaily run-now` invokes.

Drives the native ``pipeline.run()`` directly: HN/B站 scrapers →
keyword filter → markdown digest → wechat push. No LLM in the
critical path — that's what made cron flaky historically (autocli
dependencies, circuit breakers, recursive self-invocation). This is
also what the LaunchAgent (installed by ``scheduler.install``) calls
on schedule, so manual and scheduled runs share the same code path.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path


def run_once(*, cwd: Path) -> int:
    """Run the native daily pipeline once. Returns shell exit code:

    - ``0`` — pipeline ran AND wechat push succeeded
    - ``1`` — pipeline ran but push failed (per-source failures don't
      flip this; only a notify failure does, since "we couldn't tell
      anyone about today's news" is the user-visible failure)
    - ``2`` — fatal error before scrape (e.g. couldn't load sources.yaml)
    """
    from . import pipeline

    result = asyncio.run(pipeline.run(cwd=cwd))

    # Human-readable summary first (what the LaunchAgent log captures).
    print(f"started: {result.started_at} → {result.finished_at}")
    print(f"digest: {result.digest_chars} chars → {result.digest_path}")
    for sr in result.sources:
        if not sr.enabled:
            print(f"  - {sr.id}: disabled (skipped)")
        elif sr.error:
            print(f"  ✗ {sr.id}: {sr.error}")
        else:
            print(f"  ✓ {sr.id}: fetched={sr.fetched} kept={sr.kept}")
    print(f"notify: {json.dumps(result.notify, ensure_ascii=False)}")
    if result.fatal_error:
        print(f"FATAL: {result.fatal_error}")

    if result.fatal_error:
        return 2
    if not result.notify.get("sent"):
        return 1
    return 0
