"""Command line entry points."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import config as config_mod
from .api import RtuApi, RtuApiError
from .config import Config, ConfigError
from .ics import render, write_if_changed
from .picker import render_picker
from . import state as state_mod
from .parse import RIGA, Event, SubjectCatalog, build_event, label_types, normalise


def _err(message: str) -> None:
    sys.stderr.write(message.rstrip() + "\n")


def _months(start: dt.date, end: dt.date) -> List[Tuple[int, int]]:
    out, year, month = [], start.year, start.month
    while (year, month) <= (end.year, end.month):
        out.append((year, month))
        month += 1
        if month > 12:
            year, month = year + 1, 1
    return out


def _semester_span(api: RtuApi, semester_id: int) -> Tuple[dt.date, dt.date, Dict[str, Any]]:
    info = api.semester_dates(semester_id)
    start = dt.datetime.fromtimestamp(info["startDate"] / 1000, RIGA).date()
    end = dt.datetime.fromtimestamp(info["endDate"] / 1000, RIGA).date()
    return start, end, info


def fetch_events(api: RtuApi, cfg: Config, verbose: bool = True) -> Tuple[List[Event], SubjectCatalog]:
    start, end, info = _semester_span(api, cfg.semester_id)
    if verbose:
        _err("semester : %s  (%s -> %s)" % (info.get("titleLV", cfg.semester_id), start, end))
    if not api.is_published(cfg.semester_program_id):
        _err("WARNING: semesterProgramId %d is not published yet; the grid may be empty."
             % cfg.semester_program_id)

    catalog = SubjectCatalog(api.subjects(cfg.semester_program_id))
    records: List[Dict[str, Any]] = []
    for year, month in _months(start, end):
        chunk = api.events(cfg.semester_program_id, year, month)
        records.extend(chunk)
        if verbose:
            _err("  %04d-%02d  %3d events" % (year, month, len(chunk)))

    seen, events = set(), []
    for record in records:
        key = (record.get("eventDateId"), record.get("eventId"))
        if key in seen:                      # month boundaries can repeat a record
            continue
        seen.add(key)
        events.append(build_event(record, catalog))
    return events, catalog


# --------------------------------------------------------------------- discover

def cmd_discover(args: argparse.Namespace) -> int:
    api = RtuApi()
    semesters = api.semesters()

    if args.semester is None:
        print("Semesters:")
        for item in semesters:
            print("  %4d  %s" % (item["semesterId"], item["title"]))
        print("\nNext: python3 -m rtusync discover --semester <id> --search <programme name or code>")
        return 0

    semester_id = args.semester
    departments = api.programs(semester_id)

    needle = (args.search or "").strip().casefold()
    matches = []
    for dept in departments:
        for program in dept.get("program") or []:
            haystack = "%s %s %s" % (
                program.get("code", ""), program.get("titleLV", ""), program.get("titleEN", "")
            )
            if not needle or needle in haystack.casefold():
                matches.append((dept.get("titleLV", ""), program))

    if not matches:
        _err("no programme matched %r in semester %d" % (args.search, semester_id))
        return 1

    if not args.expand and len(matches) > 12:
        print("%d programmes matched %r:" % (len(matches), args.search))
        for dept_title, program in matches:
            print("  %-7s %-52s [%s]" % (program.get("code", ""), program.get("titleLV", ""), dept_title))
        print("\nNarrow with --search, or add --expand to list groups for all of them.")
        return 0

    for dept_title, program in matches:
        print("\n%s  %s   [%s]" % (program.get("code", ""), program.get("titleLV", ""), dept_title))
        try:
            courses = api.courses(semester_id, program["programId"])
        except RtuApiError as exc:
            _err("  (courses unavailable: %s)" % exc)
            continue
        for course in courses:
            try:
                groups = api.groups(semester_id, program["programId"], course)
            except RtuApiError:
                continue
            for group in groups:
                print("    course %s  group %-4s  semesterProgramId=%d"
                      % (course, group.get("group"), group["semesterProgramId"]))
    print("\nPut the semesterProgramId you want into config.json, then run:")
    print("  python3 -m rtusync subjects --write-config")
    return 0


# --------------------------------------------------------------------- subjects

def cmd_subjects(args: argparse.Namespace) -> int:
    api = RtuApi()
    if args.semester_program_id:
        semester_program_id = args.semester_program_id
        semester_id = args.semester or 0
        cfg: Optional[Config] = None
    else:
        cfg = config_mod.load(args.config)
        semester_program_id, semester_id = cfg.semester_program_id, cfg.semester_id

    catalog = SubjectCatalog(api.subjects(semester_program_id))

    # Count events per subject so you can tell a real course from a one-off.
    counts: Dict[str, int] = {}
    if semester_id and not args.no_counts:
        start, end, _ = _semester_span(api, semester_id)
        for year, month in _months(start, end):
            for record in api.events(semester_program_id, year, month):
                event = build_event(record, catalog)
                key = event.subject.key if event.subject else "?? %s" % event.subject_text
                counts[key] = counts.get(key, 0) + 1

    print("Subjects in semesterProgramId %d — %d total\n" % (semester_program_id, len(catalog.subjects)))
    print("  %-8s %-52s %s" % ("CODE", "TITLE (LV)", "EVENTS"))
    for subject in sorted(catalog.subjects, key=lambda s: s.key.casefold()):
        print("  %-8s %-52s %s" % (subject.code, subject.key[:52], counts.get(subject.key, "-")))

    orphan = {k: v for k, v in counts.items() if k.startswith("?? ")}
    if orphan:
        print("\n  Event titles that matched NO catalogue subject:")
        for key, count in sorted(orphan.items(), key=lambda kv: -kv[1]):
            print("    %4d  %s" % (count, key[3:]))

    current = set(normalise(s) for s in (cfg.include if cfg else []))
    print("\n" + "-" * 72)
    print('Paste into config.json under "subjects" (delete the ones you do not attend):\n')
    print('  "include": [')
    ordered = sorted(catalog.subjects, key=lambda s: s.key.casefold())
    for index, subject in enumerate(ordered):
        comma = "," if index < len(ordered) - 1 else ""
        mark = "" if not cfg else ("" if normalise(subject.key) in current else "   // NEW")
        print('    %-56s%s%s' % (json.dumps(subject.key, ensure_ascii=False) + comma, "", mark))
    print("  ]")

    if args.write_config:
        return _write_config(args, api, semester_id, semester_program_id, catalog)
    return 0


def _write_config(args, api, semester_id, semester_program_id, catalog) -> int:
    import os

    if not semester_id:
        _err("--write-config also needs --semester (otherwise semester.id would be 0).")
        _err("Run: python3 -m rtusync discover   to see the semester ids.")
        return 2

    path = args.config
    if os.path.exists(path) and not args.force:
        _err("\n%s already exists; refusing to overwrite (use --force)." % path)
        return 1

    label = ""
    try:
        for item in api.semesters():
            if item["semesterId"] == semester_id:
                label = item["title"]
    except RtuApiError:
        pass

    lines = [
        "{",
        '  // RTU -> Calendar sync. Comments (// and #) are allowed in this file.',
        '  "semester": {',
        '    "id": %d,' % semester_id,
        '    "label": %s' % json.dumps(label, ensure_ascii=False),
        "  },",
        '  "group": {',
        '    "semester_program_id": %d' % semester_program_id,
        "  },",
        '  "language": "lv",            // "lv" or "en"; the other one goes in the description',
        '  "output": {',
        '    "path": "planning.ics",',
        '    "calendar_name": "RTU",',
        '    "summary_format": "{subject} \\u00b7 {types}",',
        '    "location_style": "full",  // "full" = Zunda krastmala 8 - 302, "short" = Zun. 8-302',
        '    "include_lecturer": true',
        "  },",
        "",
        "  // INCLUDE-LIST: only these subjects reach your calendar.",
        "  // Anything new that appears mid-semester stays out until you add it here,",
        "  // but every run logs what it dropped so you never lose one silently.",
        '  "subjects": {',
        '    "include": [',
    ]
    ordered = sorted(catalog.subjects, key=lambda s: s.key.casefold())
    for index, subject in enumerate(ordered):
        comma = "," if index < len(ordered) - 1 else ""
        lines.append("      %s%s" % (json.dumps(subject.key, ensure_ascii=False), comma))
    lines += ["    ]", "  }", "}", ""]

    with open(path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines))
    print("\nWrote %s with all %d subjects included." % (path, len(ordered)))
    print("Delete the lines you do not attend, then: python3 -m rtusync build")
    return 0


# ------------------------------------------------------------------------ build

def _identities(subject) -> set:
    """Every spelling the include-list may legitimately use for one subject."""
    names = {subject.key, subject.title("lv"), subject.title("en"), subject.code}
    return {normalise(n) for n in names if n and normalise(n)}


def _partition(events: Sequence[Event], cfg: Config):
    wanted = {normalise(s) for s in cfg.include}
    kept, dropped, unmatched = [], {}, {}
    for event in events:
        if event.subject is None:
            unmatched.setdefault(event.subject_text, 0)
            unmatched[event.subject_text] += 1
            continue
        if _identities(event.subject) & wanted:
            kept.append(event)
        else:
            dropped.setdefault(event.subject.key, 0)
            dropped[event.subject.key] += 1
    return kept, dropped, unmatched


def cmd_build(args: argparse.Namespace) -> int:
    cfg = config_mod.load(args.config)
    api = RtuApi()
    events, catalog = fetch_events(api, cfg)
    kept, dropped, unmatched = _partition(events, cfg)

    catalog_keys = set()
    for subject in catalog.subjects:
        catalog_keys |= _identities(subject)
    stale = [s for s in cfg.include if normalise(s) not in catalog_keys]

    _err("")
    _err("included : %d events across %d subject(s)" % (len(kept), len({e.subject.key for e in kept})))
    if dropped:
        _err("dropped  : %d events not in the include-list" % sum(dropped.values()))
        for key in sorted(dropped, key=lambda k: -dropped[k]):
            _err("           %4d  %s" % (dropped[key], key))
    if unmatched:
        _err("")
        _err("WARNING: %d event(s) matched no catalogue subject — these are invisible"
             % sum(unmatched.values()))
        _err("         to the include-list and were NOT added. Check for a renamed subject:")
        for key in sorted(unmatched, key=lambda k: -unmatched[k]):
            _err("           %4d  %r" % (unmatched[key], key))
    if stale:
        _err("")
        _err("WARNING: %d include-list entr(ies) match no subject in this semester's grid." % len(stale))
        _err("         Renamed, or left over from last semester:")
        for entry in stale:
            close = _closest(entry, [s.key for s in catalog.subjects])
            hint = ("  — did you mean %r?" % close) if close else ""
            _err("           %r%s" % (entry, hint))
    fuzzy = [e for e in kept if e.fuzzy]
    if fuzzy:
        _err("")
        _err("NOTE: %d event(s) matched by containment rather than an exact title." % len(fuzzy))
        for event in fuzzy[:5]:
            _err("        %r -> %r" % (event.raw_title_lv, event.subject.key))

    if not kept:
        _err("")
        _err("Nothing to write: the include-list selected no events.")
        _err("Run: python3 -m rtusync subjects   to see what is actually in the grid.")
        return 1

    # Reconcile against the previous run so SEQUENCE advances and moves are logged.
    previous = state_mod.load(cfg.state_path)
    result = state_mod.reconcile(kept, cfg, previous)

    if result.first_run:
        _err("")
        _err("state    : first run — recording %d events as the baseline." % len(kept))
    elif result.include_changed:
        _err("")
        _err("state    : include-list changed; not reporting the resulting adds/removes")
        _err("           as schedule changes.")
    elif result.changes:
        _err("")
        _err("CHANGES SINCE LAST RUN — %d:" % len(result.changes))
        for change in result.changes[:40]:
            _err("           %s" % change.line())
        if len(result.changes) > 40:
            _err("           ... and %d more (see %s)" % (len(result.changes) - 40, cfg.changelog_path))

    text = render(kept, cfg, records=result.records)
    if args.stdout:
        sys.stdout.write(text)
        return 0
    changed = write_if_changed(cfg.output_path, text)
    state_mod.save(cfg.state_path, result, state_mod.include_fingerprint(cfg.include))
    if result.changes and not result.first_run and not result.include_changed:
        state_mod.write_log(cfg.changelog_path, result.changes,
                            dt.datetime.now(dt.timezone.utc))
    _err("")
    _err("%s %s  (%d events, %d bytes)"
         % ("wrote" if changed else "unchanged:", cfg.output_path, len(kept), len(text.encode())))
    if not changed:
        _err("           timetable identical to last run; file left alone.")
    return 0


def _closest(needle: str, haystack: Sequence[str]) -> Optional[str]:
    import difflib

    hits = difflib.get_close_matches(normalise(needle), [normalise(h) for h in haystack], n=1, cutoff=0.7)
    if not hits:
        return None
    for candidate in haystack:
        if normalise(candidate) == hits[0]:
            return candidate
    return None


# ----------------------------------------------------------------------- picker

def cmd_picker(args: argparse.Namespace) -> int:
    """Generate picker.html -- a standalone page for choosing subjects."""
    cfg = config_mod.load(args.config)
    api = RtuApi()
    events, catalog = fetch_events(api, cfg)

    wanted = {normalise(s) for s in cfg.include}
    stats: Dict[int, Dict[str, Any]] = {}
    month_keys: List[str] = []
    for event in events:
        if event.subject is None:
            continue
        entry = stats.setdefault(event.subject.subject_id, {
            "subject": event.subject, "count": 0, "types": [], "months": {},
        })
        entry["count"] += 1
        for label in label_types(event.type_tokens, cfg.language):
            if label not in entry["types"]:
                entry["types"].append(label)
        month = event.start.strftime("%Y-%m")
        entry["months"][month] = entry["months"].get(month, 0) + 1
        if month not in month_keys:
            month_keys.append(month)
    month_keys.sort()

    subjects = []
    for subject in sorted(catalog.subjects, key=lambda s: s.key.casefold()):
        entry = stats.get(subject.subject_id) or {"count": 0, "types": [], "months": {}}
        subjects.append({
            "key": subject.key,
            "titleEn": subject.title("en"),
            "code": subject.code,
            "count": entry["count"],
            "types": entry["types"],
            "months": [entry["months"].get(m, 0) for m in month_keys],
            "included": bool(_identities(subject) & wanted),
        })

    data = {
        "semesterProgramId": cfg.semester_program_id,
        "generated": dt.datetime.now(RIGA).strftime("%Y-%m-%d %H:%M"),
        "months": month_keys,
        "subjects": subjects,
        "config": cfg.raw,
        "rawConfig": cfg.raw_text,      # lets the page edit in place, keeping comments
        "github": cfg.github,
    }

    html = render_picker(data, cfg)
    path = args.output
    changed = _write_text_if_changed(path, html)
    _err("")
    _err("%s %s  (%d subjects, %d events)"
         % ("wrote" if changed else "unchanged:", path, len(subjects), sum(s["count"] for s in subjects)))
    if changed:
        _err("           open it in a browser: file://%s" % os.path.abspath(path))
    return 0


def _write_text_if_changed(path: str, text: str) -> bool:
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            existing = handle.read()
        # The generated-at stamp alone must not count as a change.
        strip = lambda t: re.sub(r"Ģenerēts [0-9-]+ [0-9:]+", "Ģenerēts —", t)
        strip2 = lambda t: re.sub(r'"generated":"[^"]*"', '"generated":"—"', strip(t))
        if strip2(existing) == strip2(text):
            return False
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    return True


# ----------------------------------------------------------------------- verify

def cmd_verify(args: argparse.Namespace) -> int:
    """Print one week as a table, to eyeball against nodarbibas.rtu.lv."""
    cfg = config_mod.load(args.config)
    api = RtuApi()

    if args.week:
        try:
            anchor = dt.date.fromisoformat(args.week)
        except ValueError:
            _err("--week needs YYYY-MM-DD, got %r" % args.week)
            return 1
    else:
        anchor = dt.datetime.now(RIGA).date()
    monday = anchor - dt.timedelta(days=anchor.weekday())
    sunday = monday + dt.timedelta(days=6)

    catalog = SubjectCatalog(api.subjects(cfg.semester_program_id))
    records: List[Dict[str, Any]] = []
    for year, month in _months(monday, sunday):
        records.extend(api.events(cfg.semester_program_id, year, month))
    events = [build_event(r, catalog) for r in records]
    week = sorted([e for e in events if monday <= e.start.date() <= sunday], key=lambda e: e.start)

    kept, _, _ = _partition(week, cfg)
    kept_ids = {e.uid_seed for e in kept}

    days_lv = ["Pirmdiena", "Otrdiena", "Trešdiena", "Ceturtdiena", "Piektdiena", "Sestdiena", "Svētdiena"]
    print("Week %s .. %s  —  semesterProgramId %d" % (monday, sunday, cfg.semester_program_id))
    print("'+' = in your calendar, '-' = dropped by the include-list\n")
    current = None
    for event in week:
        if event.start.date() != current:
            current = event.start.date()
            print("\n%s  %s" % (current, days_lv[current.weekday()]))
        mark = "+" if event.uid_seed in kept_ids else "-"
        print("  %s %s-%s  %-46s %-22s %s" % (
            mark,
            event.start.strftime("%H:%M"), event.end.strftime("%H:%M"),
            (event.subject.key if event.subject else "?? " + event.subject_text)[:46],
            (event.room_short or "")[:22],
            ", ".join(label_types(event.type_tokens, cfg.language)),
        ))
    print("\n%d events in this week on the RTU site; %d of them in your calendar."
          % (len(week), len(kept)))
    return 0


# -------------------------------------------------------------------- arg parse

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m rtusync",
        description="Export an RTU timetable to a subscribable .ics file.",
    )
    parser.add_argument("-c", "--config", default=config_mod.DEFAULT_CONFIG_PATH,
                        help="config file (default: config.json)")

    # Same flag on every subcommand, so `build -c other.json` works as naturally
    # as `-c other.json build`. SUPPRESS keeps the subparser from clobbering a
    # value given before the subcommand.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("-c", "--config", default=argparse.SUPPRESS,
                        help="config file (default: config.json)")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("discover", help="find your semesterProgramId", parents=[common])
    p.add_argument("--semester", type=int, help="semester id; omit to list semesters")
    p.add_argument("--search", help="substring of programme code or title")
    p.add_argument("--expand", action="store_true", help="list groups even for many matches")
    p.set_defaults(func=cmd_discover)

    p = sub.add_parser("subjects", help="list every subject in the group's grid", parents=[common])
    p.add_argument("--semester-program-id", type=int, help="inspect a group without a config")
    p.add_argument("--semester", type=int, help="semester id (with --semester-program-id)")
    p.add_argument("--no-counts", action="store_true", help="skip per-subject event counts")
    p.add_argument("--write-config", action="store_true", help="scaffold config.json")
    p.add_argument("--force", action="store_true", help="overwrite an existing config")
    p.set_defaults(func=cmd_subjects)

    p = sub.add_parser("build", help="generate the .ics", parents=[common])
    p.add_argument("--stdout", action="store_true", help="print instead of writing the file")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("picker", help="generate picker.html for choosing subjects", parents=[common])
    p.add_argument("-o", "--output", default="picker.html", help="output path (default: picker.html)")
    p.set_defaults(func=cmd_picker)

    p = sub.add_parser("verify", help="print one week to cross-check against the site", parents=[common])
    p.add_argument("--week", help="any date in the week, YYYY-MM-DD (default: this week)")
    p.set_defaults(func=cmd_verify)
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        _err("config error: %s" % exc)
        return 2
    except RtuApiError as exc:
        _err("RTU API error: %s" % exc)
        _err("")
        _err("Nothing was written — the existing calendar is untouched, so it is")
        _err("stale rather than wrong. If this is a timeout or a 5xx, it is RTU's")
        _err("side and the next scheduled run will pick it up. Check the site:")
        _err("  https://nodarbibas.rtu.lv/")
        return 3
    except KeyboardInterrupt:
        return 130
