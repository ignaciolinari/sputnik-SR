from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from datetime import datetime
from datetime import timezone
from typing import Iterable
from typing import List

import requests

from .charts import ChartEntry
from .charts import fetch_best_albums


def _iter_years(years: Iterable[int]) -> List[int]:
    return sorted(set(years))


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Sputnikmusic best albums charts.")
    parser.add_argument(
        "--year",
        dest="years",
        type=int,
        action="append",
        help="Year to fetch (can be specified multiple times). Defaults to current year.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Optional maximum number of entries to keep per year.",
    )
    parser.add_argument(
        "--pretty",
        action="store_true",
        help="Pretty-print the resulting JSON payload.",
    )
    args = parser.parse_args()

    logger = logging.getLogger(__name__)

    try:
        target_years = _iter_years(args.years or [datetime.now(timezone.utc).year])
        all_entries: List[ChartEntry] = []

        for year in target_years:
            logger.info("Fetching chart for %s", year)
            entries = fetch_best_albums(year)
            if args.limit is not None:
                entries = entries[: args.limit]
            all_entries.extend(entries)

        payload = [asdict(entry) for entry in all_entries]
        json.dump(payload, sys.stdout, ensure_ascii=True, indent=2 if args.pretty else None)
        sys.stdout.write("\n")
    except (requests.RequestException, ValueError, AttributeError) as e:
        logger.exception("Failed to fetch charts: %s", type(e).__name__)
        sys.exit(1)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    main()
