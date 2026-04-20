#!/usr/bin/env python3
"""Extract cell data from a human-edited final PDF using pdftotext output.

Given the layout: [그룹] / 사건번호 / 물건번호 / 소재지 / 용도 / 감정·최저 / 비고,
we produce a best-effort dict: {(case_num, item): {'group':..., 'locations':[...], 'usages':[...], 'note':'...'}}
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: extract_human_cells.py <pdf>", file=sys.stderr)
        return 1
    pdf = Path(sys.argv[1])
    res = subprocess.run(
        ["pdftotext", "-layout", str(pdf), "-"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    text = res.stdout
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
