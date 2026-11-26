"""Run the mail outbox service until the process receives a shutdown signal."""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import signal

from .outbox import OutboxSender


async def run(dsn: str, interval: float) -> None:
    sender = OutboxSender(dsn)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    await sender.run_forever(interval=interval, stop=stop)


def main() -> None:
    parser = argparse.ArgumentParser(description="Отправка писем из очереди службы качества")
    parser.add_argument("--interval", type=float, default=5.0, help="пауза между проверками очереди, с")
    opts = parser.parse_args()
    if not math.isfinite(opts.interval) or opts.interval <= 0:
        parser.error("--interval задаёт положительное конечное число секунд")
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        parser.error("Задайте DATABASE_URL")
    logging.basicConfig(level=logging.INFO)
    asyncio.run(run(dsn, opts.interval))


if __name__ == "__main__":
    main()
