"""Command line entry point.

    tokentrace sessions                      list Claude Code sessions on this machine
    tokentrace report <target> [--format]    per-step attribution report for one session
    tokentrace check <target>                findings only; exit 1 on any error-level finding
    tokentrace ingest <target>... --db FILE  store events in SQLite for later cross-session queries
    tokentrace totals --db FILE              per-day, per-model totals from the database
    tokentrace watch [<target>]              follow a live session, one line per turn as it runs

<target> is a transcript path, a session directory, a session id prefix, or ``latest``.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from . import __version__, attribution, checks, report, store, watch
from .adapters import claude_code


def _load(target: str, root: str | None):
    path = claude_code.resolve_session(target, root or claude_code.DEFAULT_ROOT)
    events = claude_code.parse_session(path)
    return path, events


def cmd_sessions(args: argparse.Namespace) -> int:
    rows = claude_code.list_sessions(args.root or claude_code.DEFAULT_ROOT)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"{'session':<38} {'modified':<17} {'size':>8} {'agents':>6}  title")
    for r in rows[: args.limit]:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(r["modified"]))
        size = f"{r['bytes'] / 1e6:.1f}MB" if r["bytes"] > 1e6 else f"{r['bytes'] // 1024}KB"
        print(f"{r['session_id']:<38} {when:<17} {size:>8} {r['subagents']:>6}  {r['title'] or ''}")
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    path, events = _load(args.target, args.root)
    attr = attribution.build(events)
    th = checks.Thresholds(oversized_result_chars=args.oversized, cache_rewarm_gap_s=args.cache_gap)
    findings = checks.run_all(events, attr, th)
    if args.format == "json":
        out = report.render_json(attr, findings)
    elif args.format == "html":
        out = report.render_html(attr, findings)
    else:
        out = report.render_text(attr, findings, max_rows=args.rows)
    if args.out:
        Path(args.out).write_text(out, encoding="utf-8")
        print(f"wrote {args.out} ({len(out):,} bytes) for {path}", file=sys.stderr)
    else:
        sys.stdout.write(out)
    if args.db:
        conn = store.open_db(args.db)
        n = store.ingest(conn, events, path=str(path), title=attr.title)
        print(f"stored {n} events in {args.db}", file=sys.stderr)
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    _, events = _load(args.target, args.root)
    attr = attribution.build(events)
    findings = checks.run_all(events, attr)
    for f in findings:
        print(f"[{f.severity:<5}] {f.code:<24} {f.message}")
    return 1 if any(f.severity == "error" for f in findings) else 0


def cmd_ingest(args: argparse.Namespace) -> int:
    conn = store.open_db(args.db)
    total = 0
    for target in args.targets:
        path, events = _load(target, args.root)
        attr = attribution.build(events)
        n = store.ingest(conn, events, path=str(path), title=attr.title)
        total += n
        print(f"{path.stem}: {n} events, ${attr.total_cost_usd:,.2f}")
    print(f"stored {total} events in {args.db}")
    return 0


def cmd_totals(args: argparse.Namespace) -> int:
    conn = store.open_db(args.db)
    rows = store.daily_totals(conn)
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print(f"{'day':<11} {'model':<26} {'calls':>6} {'tokens':>14} {'cost':>10}")
    for r in rows:
        total = r["input_tokens"] + r["cache_read_tokens"] + r["cache_write_tokens"] + r["output_tokens"]
        cost = "n/a" if r["cost_usd"] is None else f"${r['cost_usd']:,.2f}"
        print(f"{r['day']:<11} {str(r['model']):<26} {r['calls']:>6} {total:>14,} {cost:>10}")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    return watch.watch(args.target, root=args.root, interval=args.interval, once=args.once)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="tokentrace", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=f"tokentrace {__version__}")
    p.add_argument("--root", help="Claude Code projects directory (default ~/.claude/projects)")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("sessions", help="list sessions")
    s.add_argument("--limit", type=int, default=40)
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_sessions)

    r = sub.add_parser("report", help="attribution report for one session")
    r.add_argument("target")
    r.add_argument("--format", choices=("text", "json", "html"), default="text")
    r.add_argument("--out", help="write to this file instead of stdout")
    r.add_argument("--rows", type=int, default=12, help="rows per table in text output")
    r.add_argument("--oversized", type=int, default=15_000, help="tool result size (chars) that counts as oversized")
    r.add_argument("--cache-gap", type=float, default=300.0, dest="cache_gap",
                   help="idle seconds after which a big cache write counts as a re-warm")
    r.add_argument("--db", help="also store the parsed events in this SQLite file")
    r.set_defaults(fn=cmd_report)

    c = sub.add_parser("check", help="print findings; exit 1 on error-level findings")
    c.add_argument("target")
    c.set_defaults(fn=cmd_check)

    i = sub.add_parser("ingest", help="store sessions in SQLite")
    i.add_argument("targets", nargs="+")
    i.add_argument("--db", required=True)
    i.set_defaults(fn=cmd_ingest)

    t = sub.add_parser("totals", help="per-day, per-model totals from the database")
    t.add_argument("--db", required=True)
    t.add_argument("--json", action="store_true")
    t.set_defaults(fn=cmd_totals)

    w = sub.add_parser("watch", help="follow a live session and print cost per turn")
    w.add_argument("target", nargs="?", default="latest")
    w.add_argument("--interval", type=float, default=2.0, help="seconds between checks for new transcript lines")
    w.add_argument("--once", action="store_true", help="print the current state and exit")
    w.set_defaults(fn=cmd_watch)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.fn(args)
    except (FileNotFoundError, ValueError) as e:
        print(f"tokentrace: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
