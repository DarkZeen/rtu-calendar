# RTU → Calendar Sync — Build Spec

Handoff doc. Goal: get my RTU timetable into Apple/Google Calendar automatically, with control over which subjects appear.

---

## 1. Context

- Student at RTU, first-cycle bachelor, autumn 2026 semester.
- Timetable lives at `https://nodarbibas.rtu.lv/` (Nodarbību grafiki).
- RTU has **no official ICS export** and **no per-student filtering** — the site only shows schedules **per study-programme group**.
- Consequence: my group's grid contains subjects I'm not actually enrolled in (e.g. Elementary Maths). Those must be filterable out.

## 2. What's already been ruled out

| Option | Status |
|---|---|
| Official RTU ICS button | Does not exist |
| `https://rtu.ciska.lv/` — third-party ICS generator | **Broken.** Page shell loads, but period/course/group dropdowns hang on "Notiek datu ielāde..." forever. `?lang=en` redirects back to LV. Its RTU scrape is dead. |
| Manual recurring events in Apple Calendar | Viable if the grid is stable weekly. ~20 min, zero maintenance. **Fallback if this build stalls.** |
| Build own sync | **Chosen path.** |

## 3. Reference implementation

`https://github.com/MomoAy/rtu_planning` — starting point, not gospel.

- Branch is **`master`**, not `main` (README is wrong about this).
- Files: `sync_calendar.py`, `requirements.txt`, `.github/workflows/`, `update-calendar.yml` (also at root), `planning.ics`.
- Console output is in French (`Matières ignorées` = the list of dropped subjects).
- 0 stars, single author — treat as a working sketch to adapt, verify every assumption against live API responses.

### How it works

`nodarbibas.rtu.lv` exposes a small internal JSON API. No auth, no session state.

1. Resolve `semesterId` → `programId` → `semesterProgramId` for my programme/group.
2. Fetch all events for the **whole group** across the semester (month by month).
3. Filter to subjects in a whitelist.
4. Emit `.ics` with a **stable UID per event** — a hash of event content, not a random UUID. *Critical: random UIDs cause duplicates on every daily regeneration instead of updates.*
5. GitHub Actions re-runs daily, commits `planning.ics`, raw URL is subscribed to.

### Known API surface

From the `rtu-nodarbibas-api` npm package (TypeScript wrapper over the same endpoints) — POST endpoints:

```
fetchSemesterProgramEvents({ semesterProgramId, year, month })
fetchSemesterProgramSubjects(semesterProgramId)
checkSemesterProgramPublished(semesterProgramId)
findGroupsByCourse({ courseId, semesterId, programId })
findCoursesByProgram({ semesterId, programId })
```

`fetchSemesterProgramSubjects` is the key one for requirement #4 below — it returns the subject list without needing to walk every event.

## 4. Requirements

### Must have

1. Pull the full semester timetable for my programme group from the RTU JSON API.
2. Generate a valid `.ics` with stable per-event UIDs.
3. Include: subject title, start/end datetime (Europe/Riga), room/location, lecturer if available, event type (lekcija / praktiskie darbi / laboratorijas darbi).
4. **Subject selection.** I must be able to see every subject in the group's schedule and pick which ones land in the calendar. Reference repo does this via a hardcoded `MY_COURSES` list of exact-match strings — *workable but brittle and invisible.* Better:
   - A one-shot command that calls `fetchSemesterProgramSubjects` (or derives distinct titles from the event feed) and prints/writes every subject found.
   - Selection persisted in a separate `config.json` / `config.yaml` — **not** buried in the script.
   - Explicit **include-list** semantics (opt in), not exclude-list — a new subject appearing mid-semester should be silent, not surprise-injected.
   - On every run, log any schedule subject **not** in the config, so I notice new or renamed subjects instead of silently losing lectures.
5. Daily republish via GitHub Actions → public repo → subscribe to `https://raw.githubusercontent.com/<user>/<repo>/master/planning.ics`.

### Nice to have

- Run locally with one command to produce the `.ics` without any GitHub involvement.
- Matching that survives minor title changes (normalise whitespace/case; warn on near-misses rather than dropping).
- `SEMESTER_LABEL` as config, so next semester is a one-line change.

### Out of scope

- Any UI beyond a CLI. No web app. No hosting.
- Real-time sync. Clients poll every few hours; that's accepted.

## 5. Verification (do not skip)

1. Run locally **before** wiring up Actions.
2. Cross-check the generated `.ics` against `nodarbibas.rtu.lv` for one real week — count of events, times, rooms.
3. Confirm the dropped-subjects log contains only things I genuinely don't attend.
4. Run twice in a row, confirm the second run produces **no duplicate events** in the calendar (UID stability check).

## 6. Subscribe

- **Google Calendar (web):** `+` next to *Other calendars* → *From URL* → paste raw URL.
- **iOS:** Settings → Calendar → Accounts → Add Account → Other → *Add Subscribed Calendar* → paste raw URL.
- Subscribe, never import. Import = dead snapshot.

## 7. Known limitations to accept up front

- Raw GitHub caches ~5 min; calendar clients poll every few hours. Useless for same-day room changes — check the site for those.
- Group-level data only. The include-list is the only thing making it mine.
- If RTU changes its internal API, this breaks with no warning. Fallback is manual entry.

## 8. Reality check

If my timetable turns out to be a stable weekly grid, **this whole build is unnecessary** — 8 recurring events typed by hand beats a scraper with a maintenance tail. Check three weeks of the real schedule first. Only build if it's genuinely irregular.
