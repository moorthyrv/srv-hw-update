"""CSV outputs."""

from __future__ import annotations

import csv
from pathlib import Path


def write_csv(path: str | Path, columns: list[str], rows: list[dict]) -> None:
    # utf-8-sig so Excel on Windows opens it with the right encoding.
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow({k: ("" if row.get(k) is None else row.get(k)) for k in columns})
