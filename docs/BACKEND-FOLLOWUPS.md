# Backend follow-ups (found during the UI redesign, NOT changed)

The redesign is frontend only. Everything here needs a backend change and was left alone.
Where a design needs this data, the UI hides that element instead of inventing it.

1. **Patient dashboard has no appointments or documents data.** `patient_dashboard` passes
   only name, initials, passport number and links. To show "Upcoming appointments" and
   "Recent documents" the handler would need to pass the next appointments and the latest
   saved documents (both already available from `booking.service` and
   `documents.service`). Until then those two cards are not rendered.
2. **Doctor dashboard has no "next patient" id.** `DayRowOut` carries time, name, status,
   badge, cancelled only, with no appointment id, so the "Next up" card links to the
   appointments page, not to one visit. Adding the appointment id to `DayRowOut` would allow
   a direct link, and merging queue state into the schedule list without matching on names.
3. **Doctor dashboard has no date or hospital variable.** The date is filled in by a small
   script; the hospital and hours are derived from the queue sessions. Passing `today` and
   the doctor's clinic names would remove the script and cover days with no sessions.
4. **No time-of-day greeting server side.** The greeting "Good evening" is a script
   enhancement; with JavaScript off the page says "Welcome back".
5. **Palette test pins the old colours.** `tests/test_web.py::test_design_tokens_match_design_doc`
   asserts `--brand: #0F5C56`, `--ink: #1A2422`, `--bg: #F6F4F0` and `#3E9A91` in
   tokens.css. The requested brand ramp (#033236, #2C6975, #68B2A0, #CDE0C9, #E0ECDE) cannot
   satisfy it. Tests are read-only for this task, so that one test fails by design until its
   owner updates the expected values.
6. **Existing known failure, unrelated:** `tests/test_schedule_schemas.py` `24:00` case fails
   on Python 3.14 (documented in CLAUDE.md).
7. **Clinic queue desk and doctor dashboard do not refresh themselves.** Only the patient
   appointment page and the documents pages carry `data-autorefresh`. A "Live" indicator is
   therefore not shown for the doctor.
