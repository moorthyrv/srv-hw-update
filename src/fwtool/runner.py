"""Run collection across many BMCs with a thread pool, resume and progress."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable

from .collector import CollectOptions, collect_server
from .credentials import CredentialProvider
from .inputs import ServerRecord
from .models import FAILED, OK, PARTIAL, ServerResult

log = logging.getLogger(__name__)


def safe_name(ip: str) -> str:
    return re.sub(r"[^\w.-]", "_", ip)


class RunState:
    """Per-run directory: one JSON file per collected server, written atomically."""

    def __init__(self, run_dir: str | Path):
        self.dir = Path(run_dir)
        self.state_dir = self.dir / "state"
        self.raw_dir = self.dir / "raw"
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def path(self, ip: str) -> Path:
        return self.state_dir / f"{safe_name(ip)}.json"

    def load(self, ip: str) -> ServerResult | None:
        p = self.path(ip)
        if not p.exists():
            return None
        try:
            return ServerResult.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except (ValueError, TypeError) as e:
            log.warning("Ignoring unreadable state file %s: %s", p, e)
            return None

    def save(self, result: ServerResult, raw: dict | None = None) -> None:
        p = self.path(result.bmc_ip)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps(result.to_dict(), indent=1, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
        if raw:
            self.raw_dir.mkdir(exist_ok=True)
            rp = self.raw_dir / f"{safe_name(result.bmc_ip)}.json"
            rp.write_text(json.dumps(raw, indent=1, ensure_ascii=False, sort_keys=True), encoding="utf-8")

    def all_results(self) -> list[ServerResult]:
        out = []
        for p in sorted(self.state_dir.glob("*.json")):
            try:
                out.append(ServerResult.from_dict(json.loads(p.read_text(encoding="utf-8"))))
            except (ValueError, TypeError) as e:
                log.warning("Ignoring unreadable state file %s: %s", p, e)
        return out


def run_collection(
    records: list[ServerRecord],
    provider: CredentialProvider,
    opts: CollectOptions,
    state: RunState,
    *,
    workers: int = 25,
    retry_failed: bool = True,
    progress: bool = True,
    collect: Callable[..., tuple[ServerResult, dict]] = collect_server,
) -> dict[str, int]:
    """Collect every record not already in the run state. Returns counts."""
    todo: list[ServerRecord] = []
    skipped = 0
    for r in records:
        prev = state.load(r.bmc_ip)
        if prev and (prev.collection_status in (OK, PARTIAL) or (prev.collection_status == FAILED and not retry_failed)):
            skipped += 1
            continue
        todo.append(r)
    counts = {OK: 0, PARTIAL: 0, FAILED: 0, "skipped": skipped}
    if skipped:
        log.warning("Resuming: %d server(s) already collected in this run are skipped", skipped)
    if not todo:
        return counts

    def work(rec: ServerRecord) -> ServerResult:
        result, raw = collect(rec, provider, opts)
        state.save(result, raw if opts.save_raw else None)
        return result

    pool = ThreadPoolExecutor(max_workers=max(1, workers), thread_name_prefix="bmc")
    try:
        with _progress(progress, len(todo)) as advance:
            futures = {pool.submit(work, r): r for r in todo}
            for fut in as_completed(futures):
                rec = futures[fut]
                try:
                    res = fut.result()
                    counts[res.collection_status] = counts.get(res.collection_status, 0) + 1
                    log.info("%s %s %s %s", rec.bmc_ip, rec.name, res.collection_status, res.error_class)
                except Exception:  # noqa: BLE001 - defensive; collect_server should not raise
                    counts[FAILED] += 1
                    log.exception("worker crashed for %s", rec.bmc_ip)
                advance(counts)
    except KeyboardInterrupt:
        # Drop queued servers; in-flight ones finish and are saved, so --resume continues cleanly.
        pool.shutdown(wait=True, cancel_futures=True)
        raise
    pool.shutdown(wait=True)
    return counts


class _progress:
    """Rich progress bar if available and enabled; otherwise periodic log lines."""

    def __init__(self, enabled: bool, total: int):
        self.enabled = enabled
        self.total = total
        self._p = None
        self._task = None
        self._done = 0

    def __enter__(self) -> Callable[[dict], None]:
        if self.enabled:
            try:
                from rich.progress import BarColumn, MofNCompleteColumn, Progress, TextColumn, TimeElapsedColumn, TimeRemainingColumn

                self._p = Progress(
                    TextColumn("[bold]BMCs"), BarColumn(), MofNCompleteColumn(),
                    TextColumn("{task.fields[status]}"), TimeElapsedColumn(), TimeRemainingColumn(),
                )
                self._p.start()
                self._task = self._p.add_task("collect", total=self.total, status="")
            except ImportError:  # pragma: no cover
                self._p = None
        return self.advance

    def advance(self, counts: dict) -> None:
        self._done += 1
        status = f"ok={counts.get(OK, 0)} partial={counts.get(PARTIAL, 0)} failed={counts.get(FAILED, 0)}"
        if self._p is not None:
            self._p.update(self._task, advance=1, status=status)
        elif self.enabled and (self._done % 50 == 0 or self._done == self.total):
            print(f"{self._done}/{self.total} {status}", flush=True)

    def __exit__(self, *exc: object) -> None:
        if self._p is not None:
            self._p.stop()


def iter_limit(records: Iterable[ServerRecord], limit: int | None) -> list[ServerRecord]:
    records = list(records)
    return records[:limit] if limit else records
