"""Analyse collected state and write servers.csv, firmware.csv and report.xlsx."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from ..analysis import Analyzer
from ..models import ServerResult
from .csv_out import write_csv
from .rows import FIRMWARE_COLUMNS, SERVER_COLUMNS, firmware_rows, server_rows
from .xlsx_out import write_xlsx

log = logging.getLogger(__name__)


def build_reports(results: list[ServerResult], analyzer: Analyzer, out_dir: str | Path, meta: dict | None = None) -> dict[str, Path]:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    pairs = [(r, analyzer.server(r)) for r in results]
    srows = server_rows(pairs)
    frows = firmware_rows(pairs)
    paths = {
        "servers": out / "servers.csv",
        "firmware": out / "firmware.csv",
        "xlsx": out / "report.xlsx",
    }
    write_csv(paths["servers"], SERVER_COLUMNS, srows)
    write_csv(paths["firmware"], FIRMWARE_COLUMNS, frows)
    info = {
        "Generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "As-of date for ages": analyzer.as_of.isoformat(),
        "Servers": len(results),
        "Components": len(frows),
        "Target policy (no baseline)": analyzer.settings.target_policy,
        "Dell catalog(s)": ", ".join(f"{Path(s).name} v{v}" for s, v in zip(analyzer.dell.sources, analyzer.dell.catalog_versions)) or "none",
        "HPE reference": analyzer.hpe.path or "none",
        "HPE SPP level (reference)": analyzer.hpe.spp_level or "-",
        "Baselines": ", ".join([analyzer.baselines.path] * bool(analyzer.baselines.path)
                               + [f"{b} ({scope})" for b, scope, _ in analyzer.baselines.bundles]) or "none",
        **(meta or {}),
    }
    write_xlsx(paths["xlsx"], srows, frows, info)
    log.info("Reports written to %s", out)
    return paths
