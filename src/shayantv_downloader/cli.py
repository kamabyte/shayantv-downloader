"""Command line entry point: `shayantv-dl run | daemon | status | health`."""

from __future__ import annotations

import argparse
import fcntl
import logging
import signal
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta

from shayantv_downloader import health
from shayantv_downloader.config import Config
from shayantv_downloader.state import State
from shayantv_downloader.sync import STOP, run_sync

log = logging.getLogger("shayantv_downloader")


@contextmanager
def single_instance(cfg: Config):
    """Prevent overlapping runs (e.g. a manual run while the daemon is syncing)."""
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    with open(cfg.state_dir / ".lock", "w") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("another sync is already running") from None
        yield


def _sync_once(cfg: Config, args: argparse.Namespace) -> int:
    only = set(args.only) if args.only else None
    with single_instance(cfg):
        health.beat(cfg)
        try:
            result = run_sync(cfg, only=only, limit=args.limit, dry_run=args.dry_run)
        except Exception as exc:
            health.sync_finished(cfg, f"{type(exc).__name__}: {exc}")
            raise
        all_failed = bool(result.failed) and not result.downloaded
        health.sync_finished(cfg, f"all {len(result.failed)} downloads failed" if all_failed else None)
    log.info(
        "Sync finished: %d shows, %d new episodes, %d downloaded, %d failed",
        result.shows, result.new_episodes, result.downloaded, len(result.failed),
    )
    return 1 if result.failed else 0


def cmd_run(cfg: Config, args: argparse.Namespace) -> int:
    return _sync_once(cfg, args)


def cmd_daemon(cfg: Config, args: argparse.Namespace) -> int:
    stop = False

    def handle_signal(signum, _frame):
        nonlocal stop
        stop = True
        STOP.set()
        log.info("Received signal %s, exiting after the current step", signum)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    interval = timedelta(hours=cfg.interval_hours)
    log.info("Daemon started; syncing every %s", interval)
    while not stop:
        try:
            _sync_once(cfg, args)
        except SystemExit as exc:
            log.warning("%s", exc)
        except Exception:
            log.exception("Sync crashed; will retry next cycle")
        next_run = datetime.now() + interval
        log.info("Next sync at %s", next_run.strftime("%Y-%m-%d %H:%M"))
        while not stop and datetime.now() < next_run:
            health.beat(cfg)
            for _ in range(12):
                if stop:
                    break
                time.sleep(5)
    return 0


def cmd_status(cfg: Config, _args: argparse.Namespace) -> int:
    if not cfg.db_path.exists():
        print("No state yet - run a sync first.")
        return 0
    state = State(cfg.db_path)
    try:
        rows = state.summary()
        width = max((len(r["title"]) for r in rows), default=10)
        print(f"{'Show':<{width}}  {'done':>5} {'todo':>5} {'fail':>5}")
        totals = [0, 0, 0]
        for r in rows:
            done, pending, failed = r["done"] or 0, r["pending"] or 0, r["failed"] or 0
            totals = [totals[0] + done, totals[1] + pending, totals[2] + failed]
            print(f"{r['title']:<{width}}  {done:>5} {pending:>5} {failed:>5}")
        print(f"{'TOTAL':<{width}}  {totals[0]:>5} {totals[1]:>5} {totals[2]:>5}")
        for f in state.failures():
            print(f"FAILED {f['show_slug']} S{f['season']:02d}E{f['episode']:02d} "
                  f"(attempts: {f['attempts']}): {(f['last_error'] or '').splitlines()[-1:]}")
    finally:
        state.close()
    return 0


def cmd_health(cfg: Config, _args: argparse.Namespace) -> int:
    ok, message = health.check(cfg)
    print(message)
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="shayantv-dl", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_sync_args(p: argparse.ArgumentParser) -> None:
        p.add_argument("--only", nargs="+", metavar="SLUG", help="limit to these show slugs, e.g. luntik fiksiki")
        p.add_argument("--limit", type=int, help="download at most N episodes this run")
        p.add_argument("--dry-run", action="store_true",
                       help="discover and write metadata, but do not download video")

    add_sync_args(sub.add_parser("run", help="sync once and exit"))
    add_sync_args(sub.add_parser("daemon", help="sync now, then every INTERVAL_HOURS"))
    sub.add_parser("status", help="show per-show download progress")
    sub.add_parser("health", help="exit 0 if the daemon is alive and the last sync worked (for healthchecks)")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        stream=sys.stdout,
    )
    cfg = Config.from_env()
    handlers = {"run": cmd_run, "daemon": cmd_daemon, "status": cmd_status, "health": cmd_health}
    return handlers[args.command](cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
