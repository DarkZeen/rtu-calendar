"""Run-to-run state, so schedule changes are visible instead of silent.

RTU edits a published timetable in place: a lecture moves room, a slot shifts an
hour, a session is dropped. Regenerating the .ics from scratch each run makes
those edits invisible -- the file simply differs, and you find out when you walk
to the wrong building.

So we keep ``state.json``: one record per event, carrying a content hash, the
iCalendar SEQUENCE, and the previous room when one changed. That buys three
things:

* **SEQUENCE** increments only on real content change, which is what RFC 5545
  wants and what makes a client treat an event as *updated* rather than new.
* **A change log** you can read, so after a fortnight you know how volatile your
  grid actually is rather than guessing.
* **An in-event warning** -- a moved lecture says so in its own description
  until it has happened, which survives even a slow-polling client.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

STATE_VERSION = 1

_SEP = "\x1f"


def _hash(*parts: str) -> str:
    joined = _SEP.join(p or "" for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:16]


def event_fingerprint(event, cfg) -> str:
    """Content hash. Anything in here counts as a 'real' change."""
    from .parse import label_types

    return _hash(
        event.subject.key if event.subject else event.subject_text,
        event.start.isoformat(),
        event.end.isoformat(),
        event.room_full,
        event.room_short,
        event.lecturer_lv,
        ",".join(label_types(event.type_tokens, cfg.language)),
    )


@dataclass
class Change:
    kind: str                 # added | removed | room | time | other
    uid: str
    subject: str
    when: str
    detail: str = ""

    def line(self) -> str:
        icon = {"added": "+", "removed": "-", "room": "~", "time": "~", "other": "~"}[self.kind]
        text = "%s %s  %s" % (icon, self.when, self.subject)
        return text + ("  -- " + self.detail if self.detail else "")


@dataclass
class StateResult:
    records: Dict[str, Dict[str, Any]]
    changes: List[Change] = field(default_factory=list)
    include_changed: bool = False
    first_run: bool = False


def _empty() -> Dict[str, Any]:
    return {"version": STATE_VERSION, "include_hash": "", "events": {}}


def load(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return _empty()
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (ValueError, OSError):
        return _empty()
    if not isinstance(data, dict) or not isinstance(data.get("events"), dict):
        return _empty()
    return data


def save(path: str, result: StateResult, include_hash: str) -> None:
    payload = {
        "version": STATE_VERSION,
        "include_hash": include_hash,
        "updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "events": result.records,
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=1, sort_keys=True)
        handle.write("\n")


def include_fingerprint(include: Sequence[str]) -> str:
    return _hash(*sorted(include))


def reconcile(events: Sequence, cfg, previous: Dict[str, Any],
              today: Optional[dt.date] = None) -> StateResult:
    """Compare this run's events against the last, and roll SEQUENCE forward."""
    today = today or dt.datetime.now(dt.timezone.utc).date()
    old_events: Dict[str, Any] = previous.get("events") or {}
    include_hash = include_fingerprint(cfg.include)
    include_changed = bool(old_events) and previous.get("include_hash", "") != include_hash
    first_run = not old_events

    records: Dict[str, Dict[str, Any]] = {}
    changes: List[Change] = []

    for event in events:
        uid = event.uid_seed
        subject = event.subject.key if event.subject else event.subject_text
        room = event.room_full or event.room_short
        fingerprint = event_fingerprint(event, cfg)
        old = old_events.get(uid)

        record: Dict[str, Any] = {
            "h": fingerprint,
            "seq": 0,
            "start": event.start.isoformat(),
            "end": event.end.isoformat(),
            "room": room,
            "subj": subject,
        }

        if old is None:
            # A brand-new include-list entry floods this; do not call that a change.
            if not first_run and not include_changed:
                changes.append(Change("added", uid, subject,
                                      event.start.strftime("%Y-%m-%d %H:%M")))
        elif old.get("h") != fingerprint:
            record["seq"] = int(old.get("seq", 0)) + 1
            old_room = old.get("room") or ""
            old_start = str(old.get("start") or "")
            if old_room and old_room != room:
                record["prev_room"] = old_room
                record["changed"] = today.isoformat()
                changes.append(Change("room", uid, subject,
                                      event.start.strftime("%Y-%m-%d %H:%M"),
                                      "%s -> %s" % (old_room, room)))
            elif old_start and old_start != record["start"]:
                changes.append(Change("time", uid, subject,
                                      event.start.strftime("%Y-%m-%d %H:%M"),
                                      "was %s" % old_start[11:16]))
            else:
                changes.append(Change("other", uid, subject,
                                      event.start.strftime("%Y-%m-%d %H:%M")))
        else:
            record["seq"] = int(old.get("seq", 0))
            # Carry a still-relevant room warning forward untouched, so the note
            # does not flicker on and off between runs.
            if old.get("prev_room") and event.start.date() >= today:
                record["prev_room"] = old["prev_room"]
                record["changed"] = old.get("changed", "")

        records[uid] = record

    if not first_run and not include_changed:
        live = {e.uid_seed for e in events}
        for uid, old in old_events.items():
            if uid in live:
                continue
            start = str(old.get("start") or "")
            # A dropped past event is noise; a dropped future one is a cancellation.
            if start[:10] >= today.isoformat():
                changes.append(Change("removed", uid, str(old.get("subj") or "?"),
                                      start[:16].replace("T", " ")))

    changes.sort(key=lambda c: (c.when, c.subject))
    return StateResult(records=records, changes=changes,
                       include_changed=include_changed, first_run=first_run)


_HEADER = (
    "# Schedule changes\n\n"
    "Every room move, time shift, addition and cancellation RTU has made since\n"
    "this calendar started syncing. Newest first.\n\n<!--log-->\n"
)


def write_log(path: str, changes: Sequence[Change], when: dt.datetime, keep: int = 250) -> None:
    """Prepend this run's changes to a human-readable log."""
    if not changes:
        return
    block = ["## %s" % when.strftime("%Y-%m-%d %H:%M UTC"), ""]
    block += ["- `%s`" % change.line() for change in changes]
    block.append("")

    body = ""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as handle:
            existing = handle.read()
        if "<!--log-->" in existing:
            body = existing.split("<!--log-->", 1)[1].lstrip("\n")

    entries = [chunk for chunk in body.split("\n## ") if chunk.strip()]
    kept = entries[: max(0, keep - 1)]
    tail = ""
    if kept:
        tail = "## " + "\n## ".join(part.lstrip("# ") if i == 0 else part
                                    for i, part in enumerate(kept))

    with open(path, "w", encoding="utf-8") as handle:
        handle.write(_HEADER + "\n".join(block) + ("\n" + tail if tail else ""))
