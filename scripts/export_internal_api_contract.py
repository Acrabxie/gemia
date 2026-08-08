#!/usr/bin/env python3
"""Export the generated first-party API contract to one or more JSON files."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from gemia.api_v1_routes import contract_document  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("targets", nargs="+", type=Path)
    args = parser.parse_args()
    payload = (
        json.dumps(
            contract_document(),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )
    for target in args.targets:
        resolved = target.expanduser().resolve()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(payload, encoding="utf-8")
        print(f"wrote {resolved}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
