"""Entry point. `ctfquant` console script wires everything together."""

from __future__ import annotations

import asyncio
import logging
import signal
import sys
from pathlib import Path

from dotenv import load_dotenv

from . import config
from .orchestrator import run_loop
from .state import Journal
from .venue_htx import HTXClient


def _setup_logging(level: str) -> None:
    logging.basicConfig(
        level=level.upper(),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


async def _amain() -> None:
    load_dotenv()
    app, risk, venue = config.load()
    _setup_logging(app.log_level)
    log = logging.getLogger("ctfquant.main")

    log.info(
        "starting tier=%s nav=$%.2f dry_run=%s db=%s",
        app.tier, risk.nav_usd, app.dry_run, app.db_path,
    )

    journal = Journal(Path(app.db_path))
    htx = HTXClient(venue, app.htx_access_key, app.htx_secret_key)

    stop = asyncio.Event()

    def _handle_signal() -> None:
        log.info("shutdown signal received")
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            asyncio.get_running_loop().add_signal_handler(sig, _handle_signal)
        except NotImplementedError:
            # Windows fallback; rely on KeyboardInterrupt
            pass

    runner = asyncio.create_task(run_loop(app, risk, venue, journal, htx))
    waiter = asyncio.create_task(stop.wait())

    done, pending = await asyncio.wait(
        {runner, waiter}, return_when=asyncio.FIRST_COMPLETED
    )
    for t in pending:
        t.cancel()
    for t in done:
        try:
            t.result()
        except asyncio.CancelledError:
            pass
        except Exception:
            log.exception("run_loop crashed")
            raise

    await htx.aclose()
    journal.close()
    log.info("clean shutdown")


def run() -> None:
    try:
        asyncio.run(_amain())
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    run()
