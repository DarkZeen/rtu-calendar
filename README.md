# RTU → Calendar Sync

Turns an RTU timetable from <https://nodarbibas.rtu.lv/> into a subscribable
`.ics`, with an explicit include-list so only the subjects you actually attend
reach your calendar.

Built against the spec in [`rtu-calendar-sync-spec.md`](rtu-calendar-sync-spec.md).

**No dependencies.** Python 3.9+ and the standard library. No `pip install`,
no virtualenv.

---

## Quick start

```bash
python3 -m rtusync build
```

That reads `config.json`, pulls the whole semester, and writes `planning.ics`.

---

## Commands

| Command | What it does |
|---|---|
| `python3 -m rtusync discover` | List semesters. |
| `python3 -m rtusync discover --semester 31 --search autotransporta` | Find your `semesterProgramId`. |
| `python3 -m rtusync subjects` | Every subject in your group's grid, with event counts. |
| `python3 -m rtusync subjects --write-config` | Scaffold a `config.json` with all subjects included. |
| `python3 -m rtusync build` | Generate the `.ics`. |
| `python3 -m rtusync picker` | Regenerate `picker.html`, the subject-picking page. |
| `python3 -m rtusync verify --week 2026-10-13` | Print one week to eyeball against the site. |

`build` writes to stderr and the calendar to `planning.ics`, so `--stdout`
pipes clean iCalendar if you want it.

---

## Configuring

`config.json` accepts `//` and `#` comments, so you can record *why* a subject
is excluded instead of just deleting the line:

```jsonc
"include": [
  "Matemātika",
  // "Elementārās matemātikas pamatnodaļas",   <- not enrolled
  "Angļu valoda"
]
```

Each entry may be the Latvian title, the English title, or the subject code
(`"DE0421"`) — whichever you find most stable. Anything that matches nothing is
reported rather than silently ignored.

### Include-list, not exclude-list

Nothing reaches your calendar unless you opt it in. A subject that appears
mid-semester stays out rather than surprise-injecting itself into your week.

That trade is only safe because **every run tells you what it dropped**:

```
included : 226 events across 10 subject(s)
dropped  : 15 events not in the include-list
             15  Elementārās matemātikas pamatnodaļas
```

Three further warnings fire when something moves underneath you:

- **Unmatched titles** — an event whose subject matches nothing in RTU's own
  catalogue. Invisible to the include-list, so it is called out loudly.
- **Stale include entries** — a configured subject that no longer exists in the
  grid, with a "did you mean …?" suggestion. Catches renames and leftovers
  from last semester.
- **Containment matches** — resolved by substring rather than an exact title.
  Usually harmless, worth a glance.

### Other keys

| Key | Meaning |
|---|---|
| `language` | `"lv"` or `"en"`. The other language goes into the event description, so both are always present. |
| `output.summary_format` | Template. Placeholders: `{subject}` `{types}` `{type_short}` `{room}` `{room_full}` `{lecturer}` `{code}`. |
| `output.alarm_minutes` | Minutes before each class to fire a reminder. `15` by default here; `0` emits no `VALARM` at all. |
| `output.alarm_format` | Notification text. Separate from the title on purpose, so the alert can name the room the title leaves out. |
| `output.location_style` | `"full"` → `Ķīpsalas 6A - 428 (D4.2)` (better for Maps). `"short"` → `Ķīp. 6A-428 (D4.2)` (what's on the door). |
| `semester.id` | Bump this and `group.semester_program_id` for next semester. |

---

## Next semester

```bash
python3 -m rtusync discover                                   # new semester id
python3 -m rtusync discover --semester <id> --search <code>   # new semesterProgramId
```

Update those two numbers and `semester.label` in `config.json`, then run
`subjects` to see the new grid. Your include-list carries over; anything
renamed shows up as a stale-entry warning rather than vanishing.

---

## Subscribing

Subscribe, never import — an import is a dead snapshot that never updates.

- **Google Calendar (web):** `+` beside *Other calendars* → *From URL*.
- **iOS:** Settings → Apps → Calendar → Accounts → Add Account → Other →
  *Add Subscribed Calendar*.
- **macOS Calendar:** File → New Calendar Subscription.

A local file has no URL, so for now open `planning.ics` directly (double-click)
— note that this **imports** rather than subscribes. To get a real subscription
you need the file at a URL; see *Daily republish* below.

### Daily republish

[`.github/workflows/sync.yml`](.github/workflows/sync.yml) rebuilds the calendar
**hourly at :17**, on every push that touches `config.json` or `rtusync/`, and
on demand from the Actions tab. It commits **only when the timetable actually
moved**, so a commit in the history means something changed.

There is no `setup-python` step and nothing to install — being stdlib-only means
the workflow has no dependency that can rot.

Once pushed, subscribe to:

```
https://raw.githubusercontent.com/DarkZeen/rtu-calendar/main/planning.ics
```

Two things to know about scheduled workflows:

- GitHub **disables cron workflows after 60 days** with no repo activity. Since
  the job commits nothing when nothing changed, a stable timetable would drift
  into exactly that silence. A *Monthly heartbeat* step therefore rewrites
  `.sync-alive` when the month rolls over — at most one commit a month, enough
  to keep the schedule enabled without reintroducing noise.
- The repo must be **public** for the raw URL to be subscribable without a
  token — which means your programme, group and weekly whereabouts are readable
  by anyone with the URL. Normally a fine trade for a timetable; worth making
  deliberately.

---

## picker.html — choosing subjects

`python3 -m rtusync picker` generates a standalone page listing every subject in
your grid with its event count, types, and a per-month sparkline. Tick what you
attend; the totals update live.

Everything is baked in at generation time — no fetches, no CDN, no build step —
so it behaves identically opened from disk (`file://`) or served from GitHub
Pages. The daily workflow regenerates it, so the subject list cannot drift from
the live grid.

### Publishing straight from the page

The page can commit your selection to GitHub itself, which is what lets you
change the calendar without a terminal. Fill in user / repo / branch / file,
paste a token, press **Saglabāt un publicēt**. The push triggers the workflow,
which rebuilds `planning.ics` within a few minutes.

The edit is surgical: the `include` array is rewritten **inside your original
file text**, so every comment outside that array survives. The result is parsed
and verified before it is ever sent; if verification fails it falls back to a
clean re-serialisation rather than pushing something broken. Comments *inside*
the include array are replaced, since the picker owns that list.

**About the token.** Use a **fine-grained** token, scoped to this one repository,
with only `Contents: Read and write`, and give it an expiry. The page holds a
credential, so it is built accordingly:

- A `Content-Security-Policy` pins the only reachable origin to
  `api.github.com` and forbids every external script, style, font and image.
- The page is entirely self-contained — no CDN, no web fonts, nothing
  third-party can execute in the origin that holds the token.
- The token is **not stored** unless you tick *Atcerēties šajā ierīcē*; by
  default it lives in memory and is gone when you close the tab. A **Aizmirst
  pilnvaru** button clears a stored one.
- It is never placed in a URL, a query string, or the commit payload.

If you would rather not hold a token at all, **Kopēt include** (keeps your
comments) and **Lejupielādēt config.json** (clean file) still work, and you
commit by hand.

It is live at **<https://darkzeen.github.io/rtu-calendar/picker.html>** — the
GitHub fields come pre-filled, so from a phone it is: tick, paste token, publish.

---

## Tracking schedule changes

RTU edits published timetables in place — a lecture moves room, a slot shifts,
a session is dropped. Regenerating the `.ics` each run makes those edits
invisible: the file simply differs, and you find out by walking to the wrong
building.

So `build` keeps [`state.json`](state.json) — a content hash per event — and
compares each run against the last:

```
CHANGES SINCE LAST RUN — 2:
           ~ 2026-11-03 10:15  Matemātika  -- Ķīp. 6A-428 -> Ķīp. 6-225
           - 2026-12-01 10:15  Ievads fizikā
```

Three things come out of that:

- **`SEQUENCE` advances** only on a real content change, which is what RFC 5545
  wants and what makes a client treat an event as *updated* rather than new.
- **`changes.md`** accumulates every move, newest first. After a fortnight you
  will know how volatile your grid actually is instead of guessing.
- **The event says so itself** — a moved lecture carries
  `! Telpa mainīta 2026-11-02 — bija: Ķīp. 6A-428` in its description until it
  has happened. That survives a slow-polling client, which matters because the
  client, not this tool, is the real limit on how fast a room change reaches
  you. Google refreshes external feeds on its own schedule; iOS lets you set a
  subscribed calendar to refresh every 15 minutes, which is the better option
  if room changes are frequent.

The room is also in the event **title** (`Matemātika · Ķīp. 6A-428`), so a
change is visible in month view without opening anything.

---

## How it works

RTU has no ICS export and no per-student filtering, but the site runs on a small
unauthenticated JSON API. Endpoint names were read from the site's own
`fullcalendar.js` — note they differ from the `rtu-nodarbibas-api` npm wrapper.

| Endpoint | Params |
|---|---|
| `getChousenSemesterStartEndDate` | `semesterId` |
| `findProgramsBySemesterId` | `semesterId` |
| `findCourseByProgramId` | `semesterId`, `programId` |
| `findGroupByCourseId` | `semesterId`, `programId`, `courseId` |
| `isSemesterProgramPublished` | `semesterProgramId` |
| `getSemProgSubjects` | `semesterProgramId` |
| `getSemesterProgEventList` | `semesterProgramId`, `year`, `month` |

All POST, form-encoded. There is no whole-semester call, so `build` walks the
semester month by month.

### Subject matching

Events carry **no subject id**. Everything is packed into one string:

```
"Lekc, Pr.d. Ievads studiju nozarē, M.Strautmane"
 └─ types ─┘ └──── subject ─────┘ └─ lecturer ─┘
```

So the lecturer is stripped using the separate `lecturerInfoText` field, the
leading type tokens are stripped, and the remainder is resolved against the
authoritative catalogue from `getSemProgSubjects`.

**Longest title wins.** A first-year grid holds both `Matemātika` and
`Elementārās matemātikas pamatnodaļas`; shortest- or first-match files the
latter's events under the former — which is exactly the subject most people
want dropped. Part numbers are indexed too, so `Vides tehnoloģijas 2` resolves
to the `part: 2` catalogue row rather than to part 1.

Type tokens stack, so `Eksām. atk. Matemātika 2` reads as an exam resit rather
than a subject called "atk. Matemātika 2" — but a token is only consumed when
real text follows it, so `Iesk. Ieskaite` keeps its subject. One slot can also
carry two subjects joined by `;`, in which case the first that resolves wins.

Known tokens: `Lekc.` `Pr.d.` `Lab.d.` `Eksām.` `Iesk.` `Kons.` `Sem.` `Pr.`
`M.d.` `B.d.` `Kval.d.` `Kurs.d.` `atk.`, alone or comma-joined (`Lekc, Pr.d.`).

This was checked against **7,632 events across 49 RTU groups**, not just this
one timetable.

### UID stability

Each `VEVENT` is keyed on RTU's own `eventDateId`/`eventId`, not a hash of the
event's contents. Both are deterministic, but a content hash changes when a room
moves — retiring the old event and creating a new one, losing any alarm you set.
The RTU ids survive edits, so a room change updates the event in place.

Runs are idempotent: only `DTSTAMP` differs between two runs over unchanged
data, and the writer compares with `DTSTAMP` masked, so the file is left
untouched when nothing really changed.

### Timezone

RTU sends the event day as an epoch meant to be read in `Europe/Riga` — the
site's own JS formats it with `timeZone: 'Europe/Riga'`. We do the same, then
emit UTC. Verified across the 2026-10-25 DST boundary: a 10:15 Riga class is
`07:15Z` in October and `08:15Z` in November.

---

## Reminders

Each event carries a `VALARM` firing `alarm_minutes` before it starts. Its text
comes from `alarm_format`, which is deliberately **not** the event title: the
title stays clean (`Matemātika · Lekcija`) and leaves the room to `LOCATION`,
while the notification says `Matemātika · Ķīp. 6A-428` — because a room is
clutter in a month grid and the whole point of an alert fifteen minutes out.

This matters more than it looks: a subscribed calendar is **read-only**, so you
cannot attach your own reminders to these events. The feed is the only thing
that can alert you.

On iOS the *Remove Alarms* switch on the subscription must be **off**, or the
device strips them on arrival.

---

## Limitations

- Raw GitHub caches ~5 min; calendar clients poll every few hours. Useless for
  same-day room changes — check the site for those.
- Group-level data only. The include-list is the only thing making it yours.
- If RTU changes its internal API this breaks with no warning. The failure is
  loud (non-zero exit, message naming the endpoint), not silent.
