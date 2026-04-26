from __future__ import annotations

import sys
from pathlib import Path


def bootstrap_vendor() -> None:
    vendor_dir = Path(__file__).resolve().parent.parent / ".vendor"
    if vendor_dir.exists():
        sys.path.insert(0, str(vendor_dir))
