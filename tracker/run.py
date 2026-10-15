"""Daily refresh: pull every source, store a snapshot, export CSVs.

    python -m tracker.run            # all sources
    python -m tracker.run --only polls
"""
import argparse
import sys
import traceback

from . import db
from .common import now_et, today_et
from .export import export_all
from . import site
from . import reference
from .sources import nc_voter_file, uf_early_vote, votehub_polls

SOURCES = {
    "early_vote": uf_early_vote.collect,
    "polls": votehub_polls.collect,
    "nc_pace": nc_voter_file.collect,
}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=SOURCES)
    ap.add_argument("--site", action="store_true", help="also build site/ and append data/history (CI)")
    args = ap.parse_args(argv)

    day = today_et()
    con = db.connect()
    reference.load(con)
    failures = 0
    print(f"[{now_et()}] refresh for {day}")
    for name, collect in SOURCES.items():
        if args.only and name != args.only:
            continue
        started = now_et()
        try:
            stats = collect(con, day)
            db.log_run(con, started, name, "ok", sum(v for v in stats.values() if isinstance(v, int)), str(stats))
            print(f"  {name}: ok {stats}")
        except Exception as e:
            failures += 1
            db.log_run(con, started, name, "error", None, f"{e}\n{traceback.format_exc()}")
            print(f"  {name}: ERROR {e}", file=sys.stderr)

    for fname, n in export_all(con).items():
        print(f"  wrote data/latest/{fname} ({n} rows)")
    if args.site:
        print(f"  site: {site.build(con)}")
    con.close()
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
