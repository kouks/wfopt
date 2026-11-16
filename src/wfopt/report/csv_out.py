from __future__ import annotations

import csv
from pathlib import Path

from wfopt.report.table import COLUMNS, Report, row_values


def write_csv(report: Report, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    # utf-8-sig so Excel opens it with the right encoding
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow([h for h, _, _ in COLUMNS])
        for r in report.rows:
            w.writerow(row_values(r))
    return path
