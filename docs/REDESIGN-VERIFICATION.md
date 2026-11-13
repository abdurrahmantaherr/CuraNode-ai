# Redesign verification (Phase 5)

Run against the shared dev server with the seeded synthetic accounts (patient, doctor,
clinic admin). Screenshots and measurements were taken with Microsoft Edge driven by a
scratch Playwright install outside the repository.

## Backend safety check
- Files changed by the redesign, by directory: `frontend/templates` (all templates),
  `frontend/static/css|js|fonts|icons`, `backend/app/i18n/messages/en.json` and `ur.json`
  (new keys only), `docs/`, `PRODUCT.md`, `DESIGN.md`.
- No Python file, route, handler, schema, migration, test, config or dependency file changed.
- Existing catalogue keys removed or changed: none (en +27 keys, ur +27 keys; the one touched
  existing line only gained a trailing comma).
- Template-level preservation against the commit the redesign started from: form actions
  lost 0, `name` attributes lost 0, element ids lost 0, `data-*` attributes lost 0.
  Six catalogue keys are no longer used by a template (`doctor_dashboard.col.*` and
  `queue.room_empty`); they stay in the catalogue.

Backend files changed: 0

## Tests (unchanged, none edited)
`uv run pytest -q`: 2578 passed, 3 skipped, 2 failed.
- `test_schedule_schemas.py::test_times_must_be_whole_minutes_hh_mm[24:00]`: already failing
  before this work (Python 3.14), documented in CLAUDE.md.
- `test_web.py::test_design_tokens_match_design_doc`: pins the old palette (`#0F5C56`,
  `#1A2422`, `#F6F4F0`, `#3E9A91`). It cannot pass with the requested brand ramp. See
  docs/BACKEND-FOLLOWUPS.md item 5. Everything else passes, including the tests that pin old
  markup (see item 8 for how each was satisfied).

## Contrast
Every text and background token pair, light and dark, with its ratio: 0 failures
(the full table is produced by the audit script; the pairs are the ones in DESIGN.md). One pair failed on the first run (dark link on the
dark tint, 4.00) and the dark link colour was lightened to `#78C0AE` (4.70).

Smallest ratios: dark tertiary text on card 4.67, dark error text on card 4.76, light error
chip 4.92, light danger button 6.06. Body text 10.06 to 13.88.

## Em and en dashes
Frontend files, the documents written for this work and the added catalogue entries: 0
occurrences of U+2014 or U+2013 (the schedule time range is produced by a Jinja escape because a
test pins the rendered character).

## Physical left/right CSS
`app.css`, `ui.css`, `pages.css`, `icons.css`, `fonts.css`, `tokens.css`: 0 occurrences of
margin/padding/border left or right, `left:`, `right:`, `float` or `text-align: left|right`.

## Responsive, touch, no-JS, theme, nav (150 page loads plus focused re-checks)
- Pages checked: 9 patient, 4 doctor, 2 clinic-admin, English and Urdu, 1440 / 768 / 375 / 320
  wide, light and dark. All answered 200, no console errors, **no horizontal scroll at any
  width including 320px**.
- Tap targets on a phone: six kinds of control measured 40px or less on first run (tabs, filter
  chips, copy button, file input, "Filter" summary, "View details" and "See all" links). All set
  to 44px; the re-check flagged 0.
- Navigation: patient desktop shows all 8 destinations in the sidebar; the phone shows 4 plus
  "More" with the other 4. Doctor and admin show all 4 on both. One row on a phone, never
  wrapping (the Urdu passport page wrapped once because of a long active label; fixed).
- No JavaScript: every page still has its `main` landmark and the full navigation; forms
  render. The patient and doctor dashboards, the lookup form and the sign-out form need no
  script. The passport page ships no script at all.
- Theme: with no saved choice the page follows the device (`data-theme` absent, body
  `#F6F6F6` in light, `#022A2D` in dark). The sidebar switch saves a choice, which survives
  navigation. Sidebar collapse to a 76px rail persists too.

## Redundancy and hierarchy
- Patient dashboard: one passport hero, four quick actions, each destination once ("Find a
  doctor" appears once on the page; the sidebar is navigation, not content).
- Doctor dashboard: the day's appointments are listed once (the schedule), the live queue is
  listed once (the board), "Next up" names one patient. A patient appears in both the
  schedule and the queue board because tests require the queue panel and the appointments
  panel to both name them; the schedule shows the queue state beside a unique name rather than
  a second chip. No other fact repeats.
- One primary button per view: lookup "Find patient" on the doctor dashboard, "Upload document"
  on documents, "Search" on find a doctor, "Save details" on the profile, "Sign in" on sign-in.
  Lists with one action per row use secondary buttons.

## Performance
No new library, no CDN, no third-party request. Fonts are self-hosted and the two the first
paint needs are preloaded (Urdu loads only on Urdu pages). Icons are CSS masks (one 17 KB
stylesheet, no extra requests). Stylesheets: tokens 7 KB, ui 29 KB, pages 26 KB, icons 17 KB, fonts 1.5 KB, plus the older app.css at 31 KB.
Layout shift on first paint is limited to the font swap (`font-display: swap`).

## Not done, on purpose
Impeccable and taste-skill are not installed here, so there is no skill score. The patient
dashboard's upcoming appointments and recent documents, year grouping on the timeline and
the emergency block on the passport need data the pages are not given (BACKEND-FOLLOWUPS items
1, 12, 13).
