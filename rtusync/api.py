"""Client for the internal JSON API behind https://nodarbibas.rtu.lv/.

The endpoints are undocumented and unauthenticated. Names were read off the
site's own ``/resources/assets/js/fullcalendar.js`` -- note they differ from
the ``rtu-nodarbibas-api`` npm wrapper (e.g. ``findCourseByProgramId``, not
``findCoursesByProgram``). Everything is POST with form-encoded parameters.
"""

from __future__ import annotations

import html
import json
import re
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

BASE_URL = "https://nodarbibas.rtu.lv"
USER_AGENT = "rtu-calendar-sync/1.0 (personal timetable export)"


class RtuApiError(RuntimeError):
    """Raised when the RTU API cannot be reached or returns junk."""


class RtuApi:
    def __init__(
        self,
        base_url: str = BASE_URL,
        timeout: float = 45.0,
        retries: int = 4,
        backoff: float = 2.0,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self.retries = retries
        self.backoff = backoff

    # ------------------------------------------------------------------ http

    def _request(self, path: str, data: Optional[bytes]) -> bytes:
        url = "%s/%s" % (self.base_url, path.lstrip("/"))
        headers = {
            "User-Agent": USER_AGENT,
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
        if data is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"

        last: Optional[Exception] = None
        for attempt in range(self.retries):
            if attempt:
                time.sleep(self.backoff ** attempt)
            try:
                req = urllib.request.Request(url, data=data, headers=headers)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code < 500:  # 4xx will not fix itself
                    break
                # 502/503/504/524 are RTU's own backend or its Cloudflare front
                # giving up. Worth retrying, but it is their outage, not ours.
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc
        detail = str(last)
        if isinstance(last, urllib.error.HTTPError) and last.code >= 500:
            detail = "HTTP %d from RTU (their server, not this tool)" % last.code
        elif isinstance(last, (TimeoutError, socket.timeout)) or "timed out" in detail:
            # RTU's Cloudflare front does not surface its 524 until ~125s, well
            # past any sane client timeout -- so a stalled backend reaches us as
            # a socket timeout, not as the 5xx it really is. Say so.
            detail = ("no response within %gs (RTU's server is stalling, not this tool)"
                      % self.timeout)
        raise RtuApiError("%s failed after %d attempt(s): %s" % (url, self.retries, detail))

    def _post(self, path: str, **params: Any) -> Any:
        body = urllib.parse.urlencode(params).encode("utf-8")
        raw = self._request(path, body)
        try:
            return json.loads(raw.decode("utf-8"))
        except ValueError as exc:
            raise RtuApiError("%s returned non-JSON (%d bytes)" % (path, len(raw))) from exc

    # ------------------------------------------------------------- endpoints

    def semesters(self) -> List[Dict[str, Any]]:
        """Semester list. Scraped from the homepage -- there is no JSON endpoint."""
        page = self._request("/", None).decode("utf-8", "replace")
        match = re.search(r'<select[^>]*id="semester-id".*?</select>', page, re.S)
        if not match:
            raise RtuApiError("semester dropdown not found; the site layout changed")
        out = []
        for value, label in re.findall(
            r'<option[^>]*value=["\']?(\d+)["\']?[^>]*>(.*?)</option>', match.group(0), re.S
        ):
            out.append(
                {
                    "semesterId": int(value),
                    "title": html.unescape(re.sub(r"\s+", " ", label)).strip(),
                }
            )
        if not out:
            raise RtuApiError("semester dropdown was empty")
        return out

    def semester_dates(self, semester_id: int) -> Dict[str, Any]:
        return self._post("getChousenSemesterStartEndDate", semesterId=semester_id)

    def programs(self, semester_id: int) -> List[Dict[str, Any]]:
        """Departments, each with a nested ``program`` list."""
        return self._post("findProgramsBySemesterId", semesterId=semester_id)

    def courses(self, semester_id: int, program_id: int) -> List[int]:
        """Study years offered, e.g. ``[1, 2, 3, 4, 5]``."""
        return self._post(
            "findCourseByProgramId", semesterId=semester_id, programId=program_id
        )

    def groups(self, semester_id: int, program_id: int, course_id: int) -> List[Dict[str, Any]]:
        return self._post(
            "findGroupByCourseId",
            semesterId=semester_id,
            programId=program_id,
            courseId=course_id,
        )

    def is_published(self, semester_program_id: int) -> bool:
        return bool(self._post("isSemesterProgramPublished", semesterProgramId=semester_program_id))

    def subjects(self, semester_program_id: int) -> List[Dict[str, Any]]:
        """Authoritative subject catalogue for a group (titleLV/titleEN/code/part)."""
        return self._post("getSemProgSubjects", semesterProgramId=semester_program_id)

    def events(self, semester_program_id: int, year: int, month: int) -> List[Dict[str, Any]]:
        """One calendar month of events. The API has no whole-semester call."""
        return self._post(
            "getSemesterProgEventList",
            semesterProgramId=semester_program_id,
            year=year,
            month=month,
        )
