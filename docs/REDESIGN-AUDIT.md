# Redesign audit (Phase 0)

Scope: frontend only. Read-only: all Python, routes, schemas, migrations, tests.
Skills: Impeccable and taste-skill are not installed in this environment, so the design
rules come from PRODUCT.md and the brief itself.

Note on references: docs/prototype.html does not exist in the repository. docs/inspo/ did
not exist either; the four reference images were saved there (palette, glass wellness
dashboard, mobile dark pill nav, stat tiles with capsule chart).

## 1. Inventory

### Stylesheets
- `frontend/static/css/tokens.css` : colour, radius, spacing tokens, light plus dark (three
  copies of the dark palette: `[data-theme=dark]` and the media query).
- `frontend/static/css/app.css` (977 lines) : everything else. Pinned by tests: no raw hex
  except `#fff`, no physical left/right properties, must contain `:focus-visible` and
  `outline: 2px solid var(--brand)`, `.entry-row`, `.badge--err`, and a
  `@media (pointer: coarse)` block that names `.btn--sm`.
- `frontend/static/css/theme.css` : the interim glass layer from the first pass. Replaced
  by `ui.css` in Phase 1.

### JavaScript (all progressive enhancement)
auth.js (role toggle, submit guard), profile.js (submit guard), documents.js (delete dialog),
schedule.js, timeline.js, queue.js (reloads a page that carries `data-autorefresh`).
Hooks that must survive: `.js-role`, `#doctor-fields`, `#login-sub`, `[data-autorefresh]`,
`#delete-dialog`, `data-delete-*`, `data-label` on submit buttons.

### Shared partials
topbar.html (header plus the horizontal nav), form_field.html (field and textarea macros),
doc_* partials (documents), plus the doctor and queue partials used by the dashboard.

### Pages per role
- Patient: shell (dashboard), profile, passport, access, documents (list, upload, detail,
  delete, preprocess), timeline (list, detail), booking (find, doctor, confirm, list,
  appointment, actions), queue card.
- Doctor: shell (dashboard), lookup, patient record, schedule, appointments (doctor_day).
- Clinic admin: admin dashboard (verification tabs), queue desk.
- Public: login, register, admin_register, onboarding, oauth_complete, unavailable.

### What the backend already supplies (no new context may be added)
- `nav_items` ([{href,label,current}]) is built per role by the backend: 8 patient items,
  up to 4 doctor items (3 for an unverified doctor), admin tabs plus Queue.
- Dashboard (`shell.html`): `full_name`, `initials`, `role_label`, `passport_no`, `links`,
  `crumb`, `page_title`. A verified doctor also gets `patients`, `appointments_today`
  (time, name, status, badge, cancelled) and `queue_today` (sessions with clinic name,
  hours, in-room, waiting, expected, seen).
- The patient dashboard receives NO appointments, documents or QR. Those cards cannot show
  real data (see docs/BACKEND-FOLLOWUPS.md).
- Only the patient appointment page and the documents pages auto-refresh. The doctor
  dashboard and the clinic queue desk do not, so no "Live" indicator is shown there.

## 2. Findings

### P0
- `topbar.html` + `app.css:379` : 8 patient nav items wrap to two lines on desktop and
  cannot fit a phone or Urdu. Fix: sidebar plus mobile pill nav.
- `tokens.css` : contrast of `--faint` (#93A19D) on white is about 2.7:1, used for real
  text (`app.css:168`, `app.css:322`). Fails AA.

### P1
- `shell.html:33-58` : dashboard is one flat card, the passport (the most valuable object)
  is a 12px chip (`shell.html:42`).
- `shell.html:35,47,52`, `_appointments_today.html`, `_patient_list.html`, `booking/*.html`
  : 24 inline `style=` attributes bypass the token system.
- Doctor dashboard repeats the same appointment in three places: `shell.html`,
  `_appointments_today.html`, `_doctor_queue.html`.
- `_doctor_queue.html:12-16` : awkward empty copy ("Nobody is in the room.") and a default
  browser bullet list (`<ol>`, `<ul>` unstyled).
- `app.css:163,356` : uppercase letter-spaced label on every card (`.eyebrow`,
  `.panel__label`), so nothing is a heading.
- Primary doctor task (passport lookup) is an outlined button (`shell.html:54`).

### P2
- `app.css:222-242` : flat buttons, one weight; no icon support; no active state.
- `app.css:107-116` : inputs 44px, no 48px target, no leading-icon search component.
- `app.css:440-452` : status badges are colour plus text only, no icon.
- `.empty-state` is a grey sentence with no action (`app.css:454`).
- Tables (`.patient-table`) do not collapse on small screens.
- Fonts load from Google Fonts at runtime (`base.html:10-14`): third-party request, layout
  shift, privacy cost for a health product.
- Dark theme is neutral grey-green, not derived from the brand base.

### P3
- Three duplicated dark palettes in tokens.css.
- `--font-serif` Newsreader is loaded but only used on a few titles.
- Urdu line-height 2 applies to the whole body, including mono IDs.

## 3. One-sentence design read
A competent but form-like admin tool: correct and accessible in structure, with no
hierarchy, no focal object and repeated data, so it does not yet feel like a trustworthy
health product.

## 4. Dials
- DESIGN_VARIANCE 4 : a calm, regular grid with one asymmetric hero per dashboard.
- MOTION_INTENSITY 3 : 150-220ms hover and press, one 320ms stagger on first load.
- VISUAL_DENSITY 4 patient, 6 doctor : patients get air and one action; doctors scan.

## 5. Old token to new token map
| Old | New |
|---|---|
| `--brand` #0F5C56 | `--brand-700` #2C6975 (links, emphasis) ; primary button uses `--brand-950` |
| `--brand-2` | `--brand-800` (hover) |
| `--brand-soft` | `--brand-100` / `--brand-200` |
| `--ink` #1A2422 | `--brand-950` #033236 |
| `--bg` #F6F4F0 | `--bg` #F6F6F6 over the page wash |
| `--faint` | `--text-3` (AA-safe) |
| `--r-card` 16 | `--r-5` 24 (cards), other radii 8/12/16/24/32/pill |
| `--shadow` | tinted `rgba(3,50,54,.x)` shadows |
Old names stay defined as aliases so `app.css` page rules keep working.

## 6. Implementation order
1. Foundation: tokens, fonts, atmosphere, components (`ui.css`), icon sprite, role-aware
   sidebar and mobile pill nav in `partials/topbar.html` (every page already includes it).
2. Patient dashboard.
3. Doctor dashboard, lookup, appointments, schedule, queue.
4. Passport, documents, timeline, access, find a doctor, appointments, profile, auth, errors.
5. Verification, audits, DESIGN.md.
