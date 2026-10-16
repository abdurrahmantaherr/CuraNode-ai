# Backend follow-ups (found during the UI redesign, NOT changed)

The redesign is frontend only. Everything here needs a backend change and was left alone.
Where a design needs this data, the UI hides that element instead of inventing it.

> Update after merging `feature/ux-improvements`: items 1, 2, 3 and 4 below are resolved by that
> branch (`web/dashboard.py`, `patient_home`, `doctor_home`). The patient dashboard now shows the
> next step, upcoming appointments and unfinished documents, and the doctor dashboard uses the
> real next step and counts. The placeholder pins in item 8 were replaced by that branch's tests,
> so the meta-description shim is gone. Items 5 to 13 still apply.

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
8. **Tests that pin the old UI, found while redesigning.** None was edited. Each is satisfied in
   the least harmful way, but the owner may want to update them:
   - `test_patient_dashboard_keeps_its_placeholder` and `test_pending_dashboard_has_only_the_schedule_link`
     pin the sentence "This area arrives with a later feature". It is kept in the patient page's
     `<meta name="description">` (not visible) and shown to a pending doctor, whose page has no
     other content. The catalogue key `dashboard.placeholder` could be reworded or the tests dropped.
   - `test_t18_urdu_renders_rtl` expects the string `Noto+Nastaliq+Urdu` on Urdu pages only (it
     was a Google Fonts URL). Fonts are now self-hosted; the Urdu font preload carries
     `data-family="Noto+Nastaliq+Urdu"` so the check still reads true.
   - `test_t8_qr_encodes_passport_no_alone` takes the first `<svg>` on the passport page, and
     `test_t14_qr_is_inline_svg_without_script` forbids any `<script` on it. So icons are CSS masks
     (no inline `<svg>` before the QR), and the passport page ships no script; its Print button
     is an inline `onclick` that is hidden when scripting is off.
   - Two tests pin the exact sidebar link markup `<a class="topnav__link" href=... aria-current="page">Label</a>`
     (profile and timeline). The sidebar keeps that markup and draws icons from the address in CSS.
   - `tests/test_schedule_web.py` pins an en dash in `09:00-13:00`. The template writes it as the
     escape for code point U+2013, so no dash character lives in the source.
   - `tests/test_booking_i18n.py` scans `doctor/_appointments_today.html`. The dashboard no
     longer includes it; the file is kept, with a comment, so the scan still finds it.
9. **Catalogue changes are loaded once at start-up.** New keys appear only after the server is
   restarted (a dev server with `--reload` does not watch `.json`).
10. **Clinic colours on the schedule used violet (`--ai`).** Violet is reserved for AI-read
    content, so `pages.css` remaps `.clinic-c0` to `.clinic-c3` to non-violet tokens. The class
    names come from `web/router.py` (`_CLINIC_COLOURS = 4`) and were left alone.
11. **Doctor "Next up" and the schedule cannot link to one visit** (no appointment id in
    `DayRowOut`), so Next up links nowhere specific and the queue state is matched to a row by
    patient name only when that name is unique on both sides.
12. **Year grouping on the timeline** needs the event year as a field; the template only gets a
    formatted date string, so the timeline stays a flat list.
13. **Emergency information on the passport page** (allergies, blood group, emergency contact)
    is not passed to that page, so it is not drawn.
