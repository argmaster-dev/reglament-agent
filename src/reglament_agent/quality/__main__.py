"""Run the mail outbox service until the process receives a shutdown signal."""
from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import signal
import sys

from .mail import MailSettings
from .outbox import OutboxSender
from .replies import ReplyPoller


async def run(dsn: str, interval: float) -> None:
    sender = OutboxSender(dsn)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    tasks = [sender.run_forever(interval=interval, stop=stop)]
    settings = MailSettings()
    if settings.reply_enabled:  # expert answers by e-mail (MAIL_REPLY_ENABLED=true)
        tasks.append(ReplyPoller(dsn, settings).run_forever(stop=stop))
    await asyncio.gather(*tasks)


def poll_replies(argv: list[str]) -> None:
    """One-shot: read the mailbox once and publish new expert replies (demo, tests)."""
    parser = argparse.ArgumentParser(prog="quality poll-replies",
                                     description="Разобрать ответы экспертов в почтовом ящике службы качества")
    parser.parse_args(argv)
    dsn = os.environ.get("DATABASE_URL")
    if not dsn:
        parser.error("Задайте DATABASE_URL")
    counts = asyncio.run(ReplyPoller(dsn).run_once())
    print(", ".join(f"{k}: {v}" for k, v in sorted(counts.items())) or "новых писем нет")


def main() -> None:
    if sys.argv[1:2] == ["poll-replies"]:
        return poll_replies(sys.argv[2:])
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
