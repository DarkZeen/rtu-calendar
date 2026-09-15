"""Config loading.

The file is JSON with comments allowed (``//`` and ``#``), because the part you
edit most -- the subject include-list -- is much more useful when you can write
down *why* a subject is excluded. Comments are stripped by a string-aware
scanner before parsing, so a ``#`` inside a subject title is left alone.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

DEFAULT_CONFIG_PATH = "config.json"


class ConfigError(RuntimeError):
    pass


def strip_json_comments(text: str) -> str:
    """Remove // and # line comments and /* */ blocks outside of strings."""
    out: List[str] = []
    i, n = 0, len(text)
    in_string = False
    quote = ""
    while i < n:
        ch = text[i]
        if in_string:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if ch == quote:
                in_string = False
            i += 1
            continue
        if ch in "\"'":
            in_string, quote = True, ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "#":
            while i < n and text[i] != "\n":
                i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            i = n if end == -1 else end + 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


@dataclass
class Config:
    semester_id: int
    semester_label: str
    semester_program_id: int
    include: List[str]
    language: str = "lv"
    output_path: str = "planning.ics"
    calendar_name: str = "RTU"
    summary_format: str = "{subject} · {types}"
    location_style: str = "full"          # full | short
    include_lecturer: bool = True
    alarm_minutes: int = 0
    alarm_format: str = "{subject} · {room}"
    state_path: str = "state.json"
    changelog_path: str = "changes.md"
    github: Dict[str, str] = field(default_factory=dict)
    raw_text: str = ""
    path: Optional[str] = None
    group_label: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def other_language(self) -> str:
        return "en" if self.language == "lv" else "lv"


def load(path: str = DEFAULT_CONFIG_PATH) -> Config:
    if not os.path.exists(path):
        raise ConfigError(
            "no config at %r.\n"
            "Run:  python3 -m rtusync discover   (to find your group)\n"
            "then: python3 -m rtusync subjects --write-config" % path
        )
    with open(path, "r", encoding="utf-8") as handle:
        text = handle.read()
    try:
        data = json.loads(strip_json_comments(text))
    except ValueError as exc:
        raise ConfigError("%s is not valid JSON: %s" % (path, exc)) from exc

    semester = data.get("semester") or {}
    group = data.get("group") or {}
    subjects = data.get("subjects") or {}
    output = data.get("output") or {}

    missing = []
    if not semester.get("id"):
        missing.append("semester.id")
    if not group.get("semester_program_id"):
        missing.append("group.semester_program_id")
    if missing:
        raise ConfigError("%s is missing required key(s): %s" % (path, ", ".join(missing)))

    if "include" not in subjects:
        raise ConfigError(
            "%s has no subjects.include list.\n"
            "Include-list semantics are deliberate: nothing reaches your calendar "
            "unless you opt it in.\n"
            "Run: python3 -m rtusync subjects   to see every subject in the grid." % path
        )
    include = [str(s).strip() for s in subjects.get("include") or [] if str(s).strip()]

    language = str(data.get("language", "lv")).lower()
    if language not in ("lv", "en"):
        raise ConfigError("language must be 'lv' or 'en', got %r" % language)

    try:
        alarm_minutes = int(output.get("alarm_minutes", 0) or 0)
    except (TypeError, ValueError):
        raise ConfigError("output.alarm_minutes must be a whole number of minutes")
    if alarm_minutes < 0:
        raise ConfigError("output.alarm_minutes cannot be negative")

    location_style = str(output.get("location_style", "full")).lower()
    if location_style not in ("full", "short"):
        raise ConfigError("output.location_style must be 'full' or 'short'")

    label_bits = [
        str(group.get("program_code") or "").strip(),
        ("%s. kurss" % group["course"]) if group.get("course") else "",
        ("%s. grupa" % group["group"]) if group.get("group") else "",
    ]

    github = {k: str(v) for k, v in (data.get("github") or {}).items() if v is not None}

    return Config(
        semester_id=int(semester["id"]),
        semester_label=str(semester.get("label") or ""),
        semester_program_id=int(group["semester_program_id"]),
        include=include,
        language=language,
        output_path=str(output.get("path") or "planning.ics"),
        calendar_name=str(output.get("calendar_name") or "RTU"),
        summary_format=str(output.get("summary_format") or "{subject} · {types}"),
        location_style=location_style,
        include_lecturer=bool(output.get("include_lecturer", True)),
        alarm_minutes=alarm_minutes,
        alarm_format=str(output.get("alarm_format") or "{subject} \u00b7 {room}"),
        state_path=str(output.get("state_path") or "state.json"),
        changelog_path=str(output.get("changelog_path") or "changes.md"),
        github=github,
        raw_text=text,
        path=path,
        group_label=" ".join(b for b in label_bits if b),
        raw=data,
    )
