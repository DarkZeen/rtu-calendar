"""Turn raw RTU event records into structured events with a resolved subject.

RTU events carry no subject id. The only subject signal is ``eventTempName``,
which packs three things into one string::

    "Lekc, Pr.d. Ievads studiju nozare, M.Strautmane"
     |___________| |__________________| |___________|
       type(s)           subject          lecturer

So we strip the lecturer (we get it separately as a field), strip the leading
type tokens, and resolve what is left against the authoritative catalogue from
``getSemProgSubjects``.

The resolver matches longest-title-first on purpose. A first-year grid can hold
both "Matematika" and "Elementaras matematikas pamatnodalas"; a shortest- or
first-match strategy files the latter's events under the former, which is
exactly the subject most students want dropped.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

RIGA = ZoneInfo("Europe/Riga")

# Token -> (Latvian label, English label). Order matters: longer tokens must be
# tried first so "Pr.d" is not shadowed by "Pr". Harvested from 4558 live events
# across 31 groups.
_TYPE_TABLE: Sequence[Tuple[str, str, str]] = (
    (r"Lab\.d", "Laboratorijas darbi", "Laboratory"),
    (r"Pr\.d", "Praktiskie darbi", "Practical"),
    (r"M\.d", "Maģistra darbs", "Master's thesis"),
    (r"B\.d", "Bakalaura darbs", "Bachelor's thesis"),
    (r"Kval\.d", "Kvalifikācijas darbs", "Qualification paper"),
    (r"Kurs\.d", "Kursa darbs", "Course project"),
    (r"Lekc", "Lekcija", "Lecture"),
    (r"Eks[aā]m\w*", "Eksāmens", "Exam"),
    (r"Ieskaite", "Ieskaite", "Test"),
    (r"Iesk", "Ieskaite", "Test"),
    (r"Konsult\w*", "Konsultācija", "Consultation"),
    (r"Kons", "Konsultācija", "Consultation"),
    (r"Sem", "Seminārs", "Seminar"),
    (r"atk", "atkārtots", "resit"),
    (r"Pr", "Prakse", "Placement"),
)

_TYPE_RE = re.compile(r"(%s)\.?" % "|".join(pat for pat, _, _ in _TYPE_TABLE), re.IGNORECASE)
_TYPE_LOOKUP = [(re.compile(r"^(?:%s)$" % pat, re.IGNORECASE), lv, en) for pat, lv, en in _TYPE_TABLE]

# Fallback lecturer shape: "A.Berzins", "V.Koliskina-Zarina", "J. Pundure".
_LECTURER_TAIL_RE = re.compile(
    r",\s*(?:[^\W\d_]\.\s?)+[^\W\d_][\w\-’']*\s*$", re.UNICODE
)


def normalise(text: str) -> str:
    """Casefolded comparison key: NFC, tidy spacing, tidy bracket padding."""
    if not text:
        return ""
    text = unicodedata.normalize("NFC", text)
    text = text.replace(" ", " ").replace("–", "-").replace("—", "-")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"\(\s+", "(", text)
    text = re.sub(r"\s+\)", ")", text)
    return text.casefold()


def strip_lecturer(title: str, lecturer: Optional[str]) -> str:
    """Remove the trailing lecturer names from an event title."""
    title = title.strip()
    if lecturer:
        tail = ", " + lecturer.strip()
        if normalise(title).endswith(normalise(tail)):
            return title[: len(title) - len(tail)].strip().rstrip(",").strip()
    # Lecturer field absent or formatted differently -- peel initials-surname tails.
    previous = None
    while previous != title:
        previous = title
        title = _LECTURER_TAIL_RE.sub("", title).strip()
    return title


def split_types(title: str) -> Tuple[List[str], str]:
    """Split leading type tokens off a title.

    ``"Lekc, Pr.d. Ievads studiju nozare"`` -> ``(["Lekc", "Pr.d"], "Ievads ...")``
    Returns raw tokens plus the remaining text.
    """
    tokens: List[str] = []
    rest = title.strip()
    while True:
        match = _TYPE_RE.match(rest)
        if not match:
            break
        after = rest[match.end() :]
        if after[:1] == ",":
            tokens.append(match.group(1))
            rest = after[1:].lstrip()
            continue
        if after[:1].isspace():
            remainder = after.lstrip()
            if not remainder:
                break          # never swallow the whole title ("Iesk." alone)
            tokens.append(match.group(1))
            rest = remainder
            continue           # keep going: "Eksam. atk. Matematika 2"
        # Token is really the start of a subject name ("Prakse", "Ieskaite").
        break
    return tokens, rest


def label_types(tokens: Sequence[str], language: str) -> List[str]:
    labels: List[str] = []
    for token in tokens:
        token = token.rstrip(".")
        for pattern, lv, en in _TYPE_LOOKUP:
            if pattern.match(token):
                label = en if language == "en" else lv
                if label not in labels:
                    labels.append(label)
                break
        else:
            if token not in labels:
                labels.append(token)
    return labels


@dataclass(frozen=True)
class Subject:
    subject_id: int
    code: str
    title_lv: str
    title_en: str
    part: int

    def title(self, language: str) -> str:
        chosen = self.title_en if language == "en" else self.title_lv
        chosen = re.sub(r"\s+", " ", (chosen or "").strip())
        chosen = re.sub(r"\(\s+", "(", chosen)
        chosen = re.sub(r"\s+\)", ")", chosen)
        if self.part and self.part > 1:
            chosen = "%s %d" % (chosen, self.part)
        return chosen

    @property
    def key(self) -> str:
        """Stable identity used by the config include-list.

        Carries the part number, so parts 1 and 2 of one subject are separately
        selectable rather than collapsing onto the same config entry.
        """
        return self.title("lv")


@dataclass
class Event:
    event_date_id: int
    event_id: int
    start: dt.datetime
    end: dt.datetime
    raw_title_lv: str
    raw_title_en: str
    subject: Optional[Subject]
    subject_text: str          # what we parsed out, matched or not
    type_tokens: List[str] = field(default_factory=list)
    lecturer_lv: str = ""
    lecturer_en: str = ""
    room_short: str = ""
    room_full: str = ""
    fuzzy: bool = False        # resolved by containment, not exact title match

    @property
    def uid_seed(self) -> str:
        return "%d-%d" % (self.event_date_id, self.event_id)


class SubjectCatalog:
    """Resolves event titles to catalogue subjects, longest title first."""

    def __init__(self, records: Sequence[Dict[str, Any]]) -> None:
        self.subjects: List[Subject] = []
        for rec in records:
            if rec.get("deletedDate"):
                continue
            self.subjects.append(
                Subject(
                    subject_id=int(rec.get("subjectId") or 0),
                    code=(rec.get("code") or "").strip(),
                    title_lv=(rec.get("titleLV") or "").strip(),
                    title_en=(rec.get("titleEN") or "").strip(),
                    part=int(rec.get("part") or 1),
                )
            )
        # Longest normalised title wins, so specific beats generic.
        self._index: List[Tuple[str, Subject]] = []
        for subject in self.subjects:
            variants = [subject.title_lv, subject.title_en]
            if subject.part and subject.part > 1:
                variants += ["%s %d" % (subject.title_lv, subject.part),
                             "%s %d" % (subject.title_en, subject.part)]
            seen = set()
            for raw in variants:
                key = normalise(raw)
                if key and key not in seen:
                    seen.add(key)
                    self._index.append((key, subject))
        self._index.sort(key=lambda pair: len(pair[0]), reverse=True)

    def resolve(self, candidate: str, full_title: str) -> Tuple[Optional[Subject], bool]:
        """Return ``(subject, was_fuzzy)``. ``candidate`` is the de-prefixed title."""
        target = normalise(candidate)
        for key, subject in self._index:
            if key == target:
                return subject, False
        # A single slot can carry two subjects joined by ";". Take the first
        # segment that resolves exactly before falling back to containment.
        if ";" in candidate:
            for segment in candidate.split(";"):
                segment_key = normalise(segment)
                if not segment_key:
                    continue
                for key, subject in self._index:
                    if key == segment_key:
                        return subject, False
        haystacks = (target, normalise(full_title))
        for key, subject in self._index:      # already longest-first
            if any(key and key in hay for hay in haystacks):
                return subject, True
        return None, False


def build_event(record: Dict[str, Any], catalog: SubjectCatalog) -> Event:
    """Convert one raw API record into an :class:`Event`."""
    raw_lv = (record.get("eventTempName") or "").strip()
    raw_en = (record.get("eventTempNameEn") or "").strip()
    lecturer_lv = (record.get("lecturerInfoText") or "").strip()
    lecturer_en = (record.get("lecturerInfoTextEn") or "").strip()

    without_lecturer = strip_lecturer(raw_lv or raw_en, lecturer_lv or lecturer_en)
    tokens, subject_text = split_types(without_lecturer)
    subject, fuzzy = catalog.resolve(subject_text, raw_lv or raw_en)

    # RTU sends the event day as an epoch to be read in Europe/Riga -- the site's
    # own fullcalendar.js formats it with timeZone: 'Europe/Riga'. Match that.
    day = dt.datetime.fromtimestamp(int(record["eventDate"]) / 1000, RIGA).date()
    start_at = record.get("customStart") or {}
    end_at = record.get("customEnd") or {}
    start = dt.datetime(
        day.year, day.month, day.day,
        int(start_at.get("hour", 0)), int(start_at.get("minute", 0)), tzinfo=RIGA,
    )
    end = dt.datetime(
        day.year, day.month, day.day,
        int(end_at.get("hour", 0)), int(end_at.get("minute", 0)), tzinfo=RIGA,
    )
    if end <= start:                       # defensive: never emit a negative span
        end = start + dt.timedelta(minutes=95)

    room = record.get("room") or {}
    tidy = lambda value: re.sub(r"\s+", " ", (value or "").strip())
    return Event(
        event_date_id=int(record["eventDateId"]),
        event_id=int(record["eventId"]),
        start=start,
        end=end,
        raw_title_lv=raw_lv,
        raw_title_en=raw_en,
        subject=subject,
        subject_text=subject_text,
        type_tokens=tokens,
        lecturer_lv=lecturer_lv,
        lecturer_en=lecturer_en,
        room_short=tidy(record.get("roomInfoText")),
        room_full=tidy(room.get("roomName")),
        fuzzy=fuzzy,
    )
