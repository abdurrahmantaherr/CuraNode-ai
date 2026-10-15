# DESIGN

Calm, premium, clinical but warm. Mint and teal atmosphere, frosted glass shell, soft rounded
forms, tall capsule shapes, circular gradient badges, a dark floating pill navigation with a
white active pill. Supersedes docs/DESIGN.md for anything visual. Product context: PRODUCT.md.

Dials: DESIGN_VARIANCE 4, MOTION_INTENSITY 3, VISUAL_DENSITY 4 (patient) and 6 (doctor).

## Files
| File | Owns |
|---|---|
| `static/css/tokens.css` | Every raw colour, both themes, the radius and spacing scales, shadows, type scale |
| `static/css/fonts.css` | Self-hosted @font-face (Public Sans, Newsreader, IBM Plex Mono, Noto Nastaliq Urdu) |
| `static/css/icons.css` | The icon set as CSS masks (`.icon.i-name`, `--ic-name`) |
| `static/css/app.css` | Older page rules (forms, review screen, timeline, schedule). Tests pin it: no raw hex but `#fff`, no physical left/right, `:focus-visible` ring |
| `static/css/ui.css` | Atmosphere, shell, sidebar, pill nav, buttons, inputs, cards, badges, tables, empty state, stat tile |
| `static/css/pages.css` | Dashboards, quick tiles, hero, tabs and chips, doctor cards, passport, sign-in |
| `static/js/theme-init.js` | Saved theme and sidebar state before first paint |
| `static/js/ui.js` | Theme switch, collapsible sidebar, copy button, date and greeting. Enhancement only |

Load order in `base.html`: fonts, tokens, app, icons, ui, pages.

## Tokens
Brand ramp (constants): brand-950 `#033236` ink, primary button, nav pill. brand-700 `#2C6975`
links and chart main. brand-500 `#68B2A0` accents, fills, progress (never text, never under
white text). brand-200 `#CDE0C9` selected fills. brand-100 `#E0ECDE` tinted surfaces and the
page wash. Surfaces `#FFFFFF` and `#F6F6F6`. Violet `#6B5FD9` is AI-read content only; its text
colour is `--ai-ink`.

Semantic (themed): `--text-1/2/3`, `--surface`, `--surface-2`, `--bg`, `--tint`, `--tint-2`,
`--line`, `--line-2`, `--link`, `--primary-bg`, `--primary-ink`, `--ok/warn/err` with `-soft`,
`--nav` family, `--glass` family. Older names (`--brand`, `--ink`, `--muted`, `--faint`,
`--brand-soft`) remain as aliases so old rules keep working.

Dark theme is derived from `#033236`: page `#022A2D`, surface `#0A3F44`, primary fill
`#68B2A0` with `#033236` text. It follows the device and can be overridden with the sidebar
switch (saved in `localStorage`). Every text and background pair is verified (see the Phase 5
report): body 4.5:1, large text and UI 3:1.

Radius 8, 12, 16, 24, 32, pill. Spacing 4px scale (`--s-1` to `--s-10`). Shadows are tinted
with the ink colour, never black (dark theme uses a deeper teal). Type 12/14/16/18/22/28/36;
Newsreader for page titles and names, Public Sans for UI, IBM Plex Mono for ids, times and
passport numbers; `tabular-nums` throughout. Urdu uses Noto Nastaliq Urdu with line height 2.

## Rules
- One focal element per page. At most one primary button per view.
- Each fact appears once on a screen. Never repeat the same data in several cards.
- Never invent data: if a design needs data the page is not given, hide that element and note
  it in docs/BACKEND-FOLLOWUPS.md.
- Glass (blur) only on the sidebar, mobile pill nav, passport hero, sign-in card and the doctor
  "Next up" card. Data surfaces (tables, forms, lists, queue) are solid.
- One level of card: anything boxed inside a panel is a plain row with a divider.
- Status is icon plus words, never colour alone (`.badge`, `.ai-state`).
- Section titles are sentence case 16 to 18px semibold. Small uppercase letter-spaced text is
  for table column headers only.
- Colour only through tokens. Logical properties only (no left/right) so Urdu needs no second
  sheet. No em-dashes or en-dashes in code, templates, copy or comments.
- Motion 150 to 220ms on hover and press, one 320ms entrance stagger, nothing looping, and
  nothing at all under `prefers-reduced-motion`. Blur is dropped under
  `prefers-reduced-transparency` and where `backdrop-filter` is missing.

## Components
- **Buttons**: `.btn--primary` (dark fill), `--secondary` (white, brand-200 border), `--ghost`,
  `--danger`, plus `--on-dark` and `--outline-dark` for the hero. 44px minimum, icon plus label,
  active scale .98, visible focus ring.
- **Inputs**: 48px, visible labels, helper text, error with icon and text. `.search` is a
  pill with a leading icon and an inline submit.
- **Badges**: `.badge--ok/warn/err/neutral/ai`, each with a mask icon. `.ai-state` for the
  document pipeline (violet only for "being read").
- **Stat tile** `.stat`: label, circular icon, large tabular number.
- **Empty state** `.empty`: icon orb, one line, optional single action.
- **Tabs** `.tabs/.tab` and chips `.chips/.fchip`: pill groups, selected is the dark pill.
- **Table** `.table`: sticky header, 56px rows, mono time, stacks under 720px.
- **Orb** `.orb`: circular icon badge. **Avatar** `.avatar`: gradient circle with initials.

## Navigation (role aware, one partial)
`partials/topbar.html` is included by every signed-in page. It reads the destinations the
backend already sends (`nav_items`) and decides nothing about roles itself.
- Desktop: 248px glass sidebar (rail at 721 to 1023px, collapsible by the user), brand and role
  on top, grouped links, then name, language, theme, collapse, sign out.
  Patient groups: Overview, My record, Privacy, Care. Doctor and admin: one flat list.
- The sidebar links keep the exact markup old tests pin (`<a class="topnav__link" href ...>`
  with a bare label); their icons are drawn by CSS from the link address.
- Phone (up to 720px): floating dark pill at the bottom. Doctor: all four destinations. Patient:
  Dashboard, Documents, Passport, Appointments plus "More" (a `<details>` sheet). The active item
  is a white pill with a label; inactive items are icons with hidden labels. Safe-area aware.
- Skip link, `aria-current="page"`, landmarks (`aside`, `header`, `nav`, `main#main`).

## Patterns
- **Patient dashboard**: greeting, one passport hero (ID in mono, copy, Show QR, Manage access),
  four quick actions, privacy note. A brand-new account still looks intentional.
- **Doctor dashboard**: lookup first (large search, posts to the existing route), four stat
  tiles, Next up (glass), one schedule list with the queue state beside a patient when the
  name is unique, the live queue board, My patients with a Manage schedule shortcut.
- **Queue desk**: large rows, one segmented control of actions per row.
- **Passport**: large printable card, QR on white, print layout.
- **Sign-in**: centred glass card, language switch in the corner.

## Known limits (see docs/BACKEND-FOLLOWUPS.md)
Patient dashboard has no upcoming appointments or recent documents (no data supplied). The
palette test pins the old colours. Two pinned legacy strings are kept out of sight.
