"""Load named queries from .sql files (blocks start with `-- name: <id>`).

Keeping SQL in .sql files means it can be read, reviewed and run in any
SQL client on its own, without digging through Python strings."""

from __future__ import annotations

import re
from functools import lru_cache

from .config import ROOT

_NAME = re.compile(r"^--\s*name:\s*(\w+)\s*$", re.MULTILINE)


@lru_cache
def load(filename: str) -> dict[str, str]:
    text = (ROOT / "sql" / filename).read_text(encoding="utf-8")
    parts = _NAME.split(text)
    # parts = [preamble, name1, body1, name2, body2, ...]
    return {parts[i]: parts[i + 1].strip().rstrip(";") for i in range(1, len(parts), 2)}
