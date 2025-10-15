from __future__ import annotations

import json
from pathlib import Path

from scraper import fetch_best_albums


def main() -> None:
    """Fetch the latest chart and persist a trimmed payload locally."""

    entries = fetch_best_albums(2024)
    payload = [entry.to_dict() for entry in entries[:10]]

    destination = Path("data") / "best_albums_2024.json"
    destination.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote {len(payload)} entries to {destination}")


if __name__ == "__main__":
    main()
