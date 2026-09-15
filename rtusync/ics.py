"""iCalendar output.

Two things here are load-bearing:

UID stability. Each VEVENT is keyed on RTU's own ``eventDateId``/``eventId``
rather than a hash of the event's contents. Both are deterministic, but a
content hash changes when a room moves, which retires the old event and creates
a new one; the RTU ids survive edits, so a room change updates the event in
place and keeps any alarm you set on it.

Idempotent writes. Only DTSTAMP moves between two runs over unchanged data, so
we compare with DTSTAMP masked and leave the file untouched when nothing really
changed. That keeps the daily commit history honest -- a commit means the
timetable moved.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from typing import Iterable, List, Optional, Sequence

from .parse import Event, label_types, normalise

PRODID = "-//rtu-calendar-sync//RTU nodarbibas timetable//LV"
UID_DOMAIN = "nodarbibas.rtu.lv"
SOURCE_URL = "https://nodarbibas.rtu.lv/"


def escape_text(value: str) -> str:
    """RFC 5545 TEXT escaping. Colons are *not* escaped in TEXT values."""
    if value is None:
        return ""
    value = value.replace("\\", "\\\\")
    value = value.replace(";", "\;").replace(",", "\\,")
    value = value.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")
    return value


def fold(line: str) -> str:
    """Fold to <=75 octets per RFC 5545 without splitting a UTF-8 character."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    chunks: List[bytes] = []
    start, limit = 0, 75
    while start < len(raw):
        end = min(start + limit, len(raw))
        if end < len(raw):
            # Never cut mid-character: back off over continuation bytes.
            while end > start and (raw[end] & 0xC0) == 0x80:
                end -= 1
            if end == start:                   # pathological; take the whole char
                end = min(start + limit, len(raw))
        chunks.append(raw[start:end])
        start = end
        limit = 74                             # continuation lines carry a leading space
    head = chunks[0].decode("utf-8")
    tail = "".join("\r\n " + chunk.decode("utf-8") for chunk in chunks[1:])
    return head + tail


def _prop(name: str, value: str) -> str:
    return fold("%s:%s" % (name, value))


def _utc(moment: dt.datetime) -> str:
    return moment.astimezone(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _location(event: Event, style: str) -> str:
    if style == "short":
        return event.room_short or event.room_full
    return event.room_full or event.room_short


def _render_template(event: Event, cfg, template: str) -> str:
    subject = event.subject.title(cfg.language) if event.subject else event.subject_text
    types = label_types(event.type_tokens, cfg.language)
    lecturer = (event.lecturer_en if cfg.language == "en" else event.lecturer_lv) or event.lecturer_lv
    text = template.format(
        subject=subject,
        types=", ".join(types),
        type_short=", ".join(t.rstrip(".") + "." for t in event.type_tokens),
        room=event.room_short,
        room_full=event.room_full,
        lecturer=lecturer,
        code=event.subject.code if event.subject else "",
    )
    text = re.sub(r"\s*·\s*", " · ", text)            # normalise separators
    text = re.sub(r"^\s*·\s*|\s*·\s*$", "", text).strip()
    return re.sub(r"\s{2,}", " ", text)


def _summary(event: Event, cfg) -> str:
    return _render_template(event, cfg, cfg.summary_format)


def _alarm_text(event: Event, cfg) -> str:
    """Notification text. Keeps the room even when the title does not -- an
    alert 15 minutes out is exactly when the room is the useful part."""
    return _render_template(event, cfg, cfg.alarm_format)


def _description(event: Event, cfg, record: Optional[dict] = None) -> str:
    lines: List[str] = []
    record = record or {}
    previous_room = record.get("prev_room")
    if previous_room:
        # Survives a slow-polling client: the event itself says it moved.
        moved_on = record.get("changed") or ""
        if cfg.language == "en":
            note = "! Room changed%s \u2014 was: %s"
        else:
            note = "! Telpa main\u012bta%s \u2014 bija: %s"
        lines.append(note % ((" " + moved_on) if moved_on else "", previous_room))
    types = label_types(event.type_tokens, cfg.language)
    if types:
        lines.append(("Type: " if cfg.language == "en" else "Veids: ") + ", ".join(types))
    lecturer = (event.lecturer_en if cfg.language == "en" else event.lecturer_lv) or event.lecturer_lv
    if cfg.include_lecturer and lecturer:
        lines.append(("Lecturer: " if cfg.language == "en" else "Pasniedzējs: ") + lecturer)
    # LOCATION already carries the primary form; show only the complementary one
    # so "Kipsalas 6A - 428 (D4.2)" is not echoed by "(Kip. 6A-428 (D4.2))".
    primary = _location(event, cfg.location_style)
    alternate = (event.room_short if cfg.location_style == "full" else event.room_full) or ""
    if alternate and normalise(alternate) != normalise(primary):
        lines.append(("Room: " if cfg.language == "en" else "Telpa: ") + alternate)
    elif primary and not alternate:
        lines.append(("Room: " if cfg.language == "en" else "Telpa: ") + primary)
    if event.subject:
        lines.append(("Code: " if cfg.language == "en" else "Kods: ") + event.subject.code)
        other = event.subject.title(cfg.other_language)
        if other and other != event.subject.title(cfg.language):
            lines.append(other)
    lines.append(SOURCE_URL)
    return "\n".join(lines)


def render(events: Sequence[Event], cfg, generated_at: Optional[dt.datetime] = None,
           records: Optional[dict] = None) -> str:
    generated_at = generated_at or dt.datetime.now(dt.timezone.utc)
    stamp = _utc(generated_at)
    records = records or {}

    name = cfg.calendar_name
    desc_bits = [b for b in (cfg.semester_label, cfg.group_label) if b]
    out: List[str] = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        _prop("PRODID", PRODID),
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        _prop("X-WR-CALNAME", escape_text(name)),
        _prop("NAME", escape_text(name)),
        _prop("X-WR-CALDESC", escape_text(" — ".join(desc_bits))),
        _prop("DESCRIPTION", escape_text(" — ".join(desc_bits))),
        "X-WR-TIMEZONE:Europe/Riga",
        "REFRESH-INTERVAL;VALUE=DURATION:PT1H",
        "X-PUBLISHED-TTL:PT1H",
        _prop("SOURCE;VALUE=URI", SOURCE_URL),
    ]

    # Sorted so the file is byte-stable regardless of the order months came back.
    for event in sorted(events, key=lambda e: (e.start, e.uid_seed)):
        out.append("BEGIN:VEVENT")
        out.append(_prop("UID", "%s@%s" % (event.uid_seed, UID_DOMAIN)))
        out.append(_prop("DTSTAMP", stamp))
        out.append(_prop("DTSTART", _utc(event.start)))
        out.append(_prop("DTEND", _utc(event.end)))
        out.append(_prop("SUMMARY", escape_text(_summary(event, cfg))))
        location = _location(event, cfg.location_style)
        if location:
            out.append(_prop("LOCATION", escape_text(location)))
        record = records.get(event.uid_seed) or {}
        out.append(_prop("DESCRIPTION", escape_text(_description(event, cfg, record))))
        sequence = int(record.get("seq", 0) or 0)
        if sequence:
            out.append("SEQUENCE:%d" % sequence)
        if event.subject:
            out.append(_prop("CATEGORIES", escape_text(event.subject.title(cfg.language))))
        out.append(_prop("URL;VALUE=URI", SOURCE_URL))
        out.append("TRANSP:OPAQUE")
        if cfg.alarm_minutes > 0:
            # A subscribed calendar is read-only, so the feed is the only thing
            # that can remind you. Carry the summary -- it holds the room, which
            # is the part worth knowing 15 minutes out.
            out.append("BEGIN:VALARM")
            out.append("ACTION:DISPLAY")
            out.append("TRIGGER:-PT%dM" % cfg.alarm_minutes)
            out.append(_prop("DESCRIPTION", escape_text(_alarm_text(event, cfg))))
            out.append("END:VALARM")
        out.append("END:VEVENT")

    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"


_DTSTAMP_LINE = re.compile(r"^DTSTAMP:.*$", re.M)


def _comparable(text: str) -> str:
    return _DTSTAMP_LINE.sub("DTSTAMP:—", text)


def write_if_changed(path: str, text: str) -> bool:
    """Write only when something other than DTSTAMP moved. Returns True if written."""
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8", newline="") as handle:
            if _comparable(handle.read()) == _comparable(text):
                return False
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    return True
