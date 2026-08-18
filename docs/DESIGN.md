# CuraNode Design System

## 1. Design Overview

CuraNode-AI is a calm, editorial, clinical-record product. The look is **warm off-white paper + deep teal brand + a distinct violet reserved exclusively for AI**. Surfaces are flat cards on a tinted background, separated by hairline borders and a very soft shadow — no gradients, no heavy elevation, no decorative imagery.

Guiding ideas visible throughout the artifact:

- **Trust over flash.** Clinical copy is plain; ornament is minimal.
- **AI is always visually marked.** Anything machine-generated uses the `--ai` violet (border, soft fill, chip, or pulse) so it can never be mistaken for a verified clinical fact.
- **Provenance is a UI element.** AI answers and extracted data carry mono-font `src: …` citation chips.
- **Three typefaces with fixed jobs.** Serif for headings, sans for UI, mono for identifiers/timestamps/metadata.
- **Density is compact.** Body text sits at 12.5–13.5px; the interface is information-dense but airy through generous card padding.

## 2. Design Tokens

All tokens are CSS custom properties on `:root`, overridden on `body[data-theme="dark"]`.

### Colors

| Token | Light | Dark | Use |
|---|---|---|---|
| `--brand` | `#0F5C56` | `#3E9A91` | Primary actions, links, active nav dot |
| `--brand-2` | `#0B4741` | `#2E7D75` | Primary hover / second data series |
| `--brand-soft` | `#E3EEEC` | `#12302D` | Brand chips, avatars, selected role tiles |
| `--ai` | `#6B5FD9` | `#9B90F2` | AI accent — Medico, OCR, generated content |
| `--ai-soft` | `#EDEBFB` | `#231F3D` | AI message bubbles, AI panels |
| `--ok` / `--ok-soft` | `#2F8F5B` / `#E4F1E9` | `#4FB37B` / `#132B1E` | Confirmed, in-range, success |
| `--warn` / `--warn-soft` | `#C68A1F` / `#F8F0DE` | `#DFAA45` / `#33280F` | Pending review, borderline values |
| `--err` / `--err-soft` | `#C0432E` / `#F8E7E3` | `#E06751` / `#341C17` | Abnormal, allergy, revoke |

Both `--brand` and `--ai` are exposed as editable component props (`accent`, `aiAccent`) and written back onto `documentElement` at runtime, so **never hardcode the hex values**.

### Text, surface & line

| Token | Light | Dark | Use |
|---|---|---|---|
| `--ink` | `#1A2422` | `#E9F1EE` | Primary text |
| `--muted` | `#6B7A76` | `#94A5A1` | Secondary text, descriptions |
| `--faint` | `#93A19D` | `#6E8280` | Eyebrow labels, mono metadata |
| `--surface` | `#FFFFFF` | `#141E1C` | Cards, header |
| `--surface-2` | `#FBFAF7` | `#101917` | Inputs, inset rows, nested tiles |
| `--bg` | `#F6F4F0` | `#0B1211` | Page background |
| `--line` | `rgba(26,36,34,.10)` | `rgba(255,255,255,.10)` | Card & row borders |
| `--line-2` | `rgba(26,36,34,.16)` | `rgba(255,255,255,.18)` | Input borders, secondary button borders, timeline rails |

### Navigation (own scale, near-constant across themes)

| Token | Light | Dark |
|---|---|---|
| `--nav` | `#0A1B19` | `#060F0E` |
| `--nav-2` | `#081614` | `#050C0B` |
| `--nav-ink` | `#DCE8E5` | `#DCE8E5` |
| `--nav-muted` | `#7C948F` | `#7C948F` |
| `--nav-active` | `#12433D` | `#123B36` |

### Typography

| Role | Family | Spec |
|---|---|---|
| Display | Newsreader (serif) | `400 34px/1.1` — design-system hero only |
| Page title | Newsreader | `400 26px/1.15` (most screens), `400 30px/1.15` (auth & dashboards) |
| Section / card heading | Newsreader | `500 22–25px` |
| Header title | Newsreader | `500 19px/1.1` |
| Eyebrow / card label | Public Sans | `600 10–10.5px`, uppercase, `letter-spacing:.14em` |
| Body | Public Sans | `14.5px/1.6` long-form; `13–13.5px` in cards |
| Label / row title | Public Sans | `600 12.5–13.5px` |
| Small / meta | Public Sans | `11.5–12.5px` |
| Mono | IBM Plex Mono | `400 10–12.5px` — IDs, dates, file sizes, citations, values in tables |

Weights in use: **400, 500, 600, 700**. Sans weights are effectively 400/500/600 (700 only for table column heads). Body line-height 1.5–1.65; headings 1.1–1.2.

### Spacing, radius, shadow

| Token | Values |
|---|---|
| Spacing scale (gaps) | `7 · 8 · 9 · 10 · 12 · 14 · 16 · 18px` — `18px` between page sections, `14px` between stat cards, `8–10px` between list rows |
| Card padding | `20–22px` (desktop), `16–19px` (compact cards) |
| Inset row padding | `10–12px 12px` |
| Radius | `999px` pills/chips/toggles · `16px` cards · `14px` compact cards & avatars · `10px` buttons & inputs · `11px` list rows · `6px` mono chips · `50%` dots/avatars |
| `--shadow` | Light: `0 1px 2px rgba(26,36,34,.05), 0 6px 18px rgba(26,36,34,.04)` · Dark: `0 1px 2px rgba(0,0,0,.4)` |
| Mobile stage shadow | `0 20px 50px rgba(26,36,34,.16)` |

### Motion

Three keyframes only: `cnPulse` (1.1–1.2s AI activity dot), `cnBar` (1.5–1.6s indeterminate progress fill), `cnSpin`. No CSS `transition` is declared anywhere.

## 3. Layout

- **Shell:** fixed-width sidebar + fluid main column, `min-height:100vh`.
- **Sidebar:** `262px`, `flex:none`, `position:sticky; top:0`, `height:100vh`, own scroll.
- **Header:** sticky (`z-index:20`), `padding:14px 30px`, `--surface` background, `1px solid var(--line)` bottom border.
- **Content area:** centred, `padding:26px 30px 60px` on `--bg`.
- **Content max width:** `1240px` desktop, `412px` mobile.
- **Reading width:** long-form paragraphs capped at `56ch`–`80ch` (commonly `64ch`).
- **Responsive grid:** every multi-column region is `repeat(auto-fit, minmax(Xpx, 1fr))` — `300px` for main panel pairs, `280–320px` for card grids, `160–180px` for stat/KPI tiles, `110–130px` for micro-tiles. There are **no `@media` queries**; reflow is entirely intrinsic (`auto-fit` + `flex-wrap` + `min-width:0`).
- **Mobile stage:** rounded `30px` frame, `1px solid var(--line-2)`, padding `16px 14px 22px`, with a simulated status bar, a compact app bar (logo + screen title + avatar), and a sticky bottom tab bar.
- **Wide tables/grids** are wrapped in `overflow-x:auto` with a `min-width` (e.g. the schedule grid at `min-width:660px`).

## 4. Theme

- Light is the default; dark is applied by setting `data-theme="dark"` on `<body>`. **Only custom properties are redefined** — no component styles branch on theme.
- Light: warm neutrals (`#F6F4F0` paper, white cards). Dark: cool desaturated greens (`#0B1211` page, `#141E1C` cards).
- Brand and status hues **lighten** in dark mode (e.g. `#0F5C56` → `#3E9A91`) to hold contrast; their `-soft` companions **invert** from pale tints to deep tints.
- Borders switch from `rgba(26,36,34,…)` to `rgba(255,255,255,…)` at the same alphas.
- Shadow collapses to a single tight dark shadow in dark mode.
- The navigation column is near-identical in both themes — it is always dark. Chip/button text on brand fills stays literal `#fff` in both themes.

## 5. Core Components

- **Sidebar nav** — dark column, brand-square logo + wordmark + mono subtitle, then uppercase group labels (`--nav-muted`, `.16em` tracking) with rows: `8px 10px`, `9px` radius, a 5px status dot, active = `--nav-active` background + `#FFF` text + weight 600 + brand dot; hover = `rgba(255,255,255,.06)`.
- **Header** — mono uppercase breadcrumb over a serif screen title, with pill segmented controls on the right (Desktop/Mobile, Light/Dark): `999px` track, `3px` padding, `--surface-2` fill, active segment = `--brand` fill + white text.
- **Buttons** — `10px 18px` (standard) / `9–10px 14–15px` (inline), `10px` radius, `12.5–13px`, weight 600. Variants: **primary** (`--brand`, white, hover `--brand-2`), **secondary** (`--surface` + `--line-2` border), **AI** (`--ai`, white), **destructive** (transparent + `--err` border/text), **disabled** (`--surface-2`, `--faint`, `--line` border).
- **Card** — `--surface`, `1px solid var(--line)`, `16px` radius, `20–22px` padding, `var(--shadow)`. Standard head = uppercase eyebrow left, status badge or text link right. Compact stat cards use `14px` radius and `16–17px` padding.
- **Inputs** — `11px 13px`, `10px` radius, `1px solid var(--line-2)`, `--surface-2` fill, `13.5px`, `outline:none`. Labels are `600 12px` above the field. Checkboxes use `accent-color:var(--brand)`. Selects share the input styling.
- **Badges / chips** — `4–5px 10–11px`, `999px`, `11–11.5px`, weight 600, always a `-soft` background with its matching solid foreground (`Confirmed`, `Pending review`, `Abnormal`, `Verified record`, `AI generated`).
- **Mono citation chip** — `3px 8–9px`, `6px` radius, `--surface-2` + `--line` border (or `--brand-soft`), IBM Plex Mono `10.5px`; used for `src: …` provenance.
- **List row** — `11px 12px`, `11px` radius, `--surface-2` fill, `--line` border, an optional `6–7px`-wide coloured status bar on the left, flexible title/subtitle block, right-aligned mono meta.
- **Table** — built from flex rows, not `<table>`: a `--surface-2` header strip (`700 10.5px`, uppercase, `.1em`) with fixed-width columns, then rows separated by `1px solid var(--line)` with a tinted `rowBg` for out-of-range values and a status chip in the last column.
- **Timeline** — a column of `10–11px` dots colour-coded by source (`--brand` clinical, `--ai` machine, `--warn` abnormal) joined by a `1px` `--line-2` rail; content block carries a title plus mono metadata.
- **Toggle** — `36×20px` `999px` track (`--brand` on, `--line-2` off), `16px` white knob at `left:2px` / `left:18px`, small drop shadow.
- **Stat / KPI tile** — uppercase `--faint` label, serif value at `500 24–25px` with a small unit, then a delta line coloured by tone.
- **AI panel** — `1px solid var(--ai)` on `--ai-soft`, pulsing dot + label, and a `4–5px` indeterminate progress bar (`rgba(107,95,217,.2)` track, `--ai` fill).
- **Chat bubbles** — user: `--brand` fill, white, `14px 14px 4px 14px`, max-width 78%. Medico: `--ai-soft` + `rgba(107,95,217,.25)` border, `--ink`, `14px 14px 14px 4px`, max-width 88%, followed by citation chips.
- **Dropzone** — `1.5px dashed var(--line-2)`, `12–14px` radius, `--surface-2`, centred filename + mono constraints line.
- **Avatar** — square-rounded (`13–18px` radius) or circular, `--brand-soft` fill with `--brand` serif initials; AI avatar is a solid `--ai` square.

## 6. Screen Patterns

The 30 screens reduce to six repeated layouts:

1. **Auth** (`login`, `register`, `forgot`) — centred single card, `430–470px`, brand logo mark, serif heading, stacked labelled fields, full-width primary button, footer switch link. Inline success uses an `--ok-soft` callout.
2. **Dashboard** (`pdash`, `ddash`, `danalytics`) — eyebrow + greeting + primary action pair, then a KPI tile row, then 2-up panel grids (`minmax(300–310px,1fr)`).
3. **List / index** (`pappts`, `prx`, `plabs`, `pnotif`, `dappts`, `dsearch`, `paccesshist`) — filter pill row and/or search input above a vertical stack of list rows or cards, each with badge + mono meta + inline actions.
4. **Detail** (`prxdetail`, `plabdetail`, `pdoctor`, `pconfirm`, `dpatient`) — identity header card (avatar, serif name, mono ID line, status chips, primary action), then a full-width detail card, then supporting side cards.
5. **Workflow / stepper** (`pocr`, `dconsult`, `drxsent`) — pill step indicator, source pane beside a result pane, AI processing state → reviewable extracted rows → confirm action.
6. **Conversational & control** (`pmedico`, `dassist`, `paccessctl`, `pprofile`, `dsched`) — tall scrolling panel with sticky composer beside a context sidebar, or grids of grant/permission cards with toggles.

## 7. Interaction Patterns

- **Navigation** is single-state: `go(screen)` swaps the screen and scrolls to top. Sidebar (desktop) and 5-item bottom tab bar (mobile) drive the same state; there is no router or history.
- **Active states** are expressed by three simultaneous changes — background fill, foreground colour, and font weight (400 → 600) — plus a colour change on the row's dot.
- **Segmented pills** (device, theme, OCR steps, patient-diff tabs, filters) share one shape: `999px`, active = `--brand` fill + white text (or brand border + `--brand-soft` fill for outlined variants), inactive = transparent/`--surface-2` with `--muted`/`--faint` text.
- **Hover** is declared only where it changes meaning: primary button → `--brand-2`, sidebar row → `rgba(255,255,255,.06)`, secondary/suggestion chips → `border-color:var(--brand)` or `--ai`, list cards → `background:var(--surface-2)`.
- **Forms** use inline optimistic feedback rather than validation UI — e.g. "Send reset link" reveals an `--ok-soft` confirmation block in place.
- **Selection** (role picker, appointment slot, booking type) is a tile that switches to `--brand` border + `--brand-soft` fill + `--brand` text.
- **Toggles** flip instantly with no confirmation; copy states that changes take effect immediately.
- **AI actions** run a three-stage sequence: idle → pulsing "reading/extracting…" panel with indeterminate bar → result with citation chips, driven by a `setTimeout` of ~1.8s.
- **Responsive** switching is a manual Desktop/Mobile control on the header; mobile replaces the sidebar with the bottom tab bar and reframes the stage.

## 8. Design Rules

1. Use the CSS variables — never a raw hex. `--brand` and `--ai` are user-configurable at runtime, so hardcoding them breaks theming.
2. Any AI-produced content must be visibly marked with `--ai` (chip, border, or soft fill) and, where it asserts a clinical fact, carry mono `src:` citation chips.
3. Status colour is semantic: `--ok` confirmed/in-range, `--warn` pending/borderline, `--err` abnormal/destructive, `--brand` verified clinical record. Do not reuse them decoratively.
4. Reach for the existing card, list row, badge, pill, or toggle before inventing a shape. New radii, shadows, or button variants need a reason.
5. Keep responsiveness intrinsic: `auto-fit`/`minmax`, `flex-wrap`, `min-width:0`. Do not introduce media queries or fixed pixel columns for a new section.
6. Every new colour must be defined for both themes as a `--token` / `--token-soft` pair; never branch component styles on theme.
7. Respect the three-typeface contract: Newsreader for headings and numeric values, Public Sans for UI text, IBM Plex Mono for IDs, dates, sizes and citations.
8. Stay on the spacing rhythm — `18px` between sections, `14px` between tiles, `8–10px` between rows, `20–22px` card padding.
9. Preserve the dark navigation column: it does not follow `--surface`.
10. Content stays within `1240px` (desktop) / `412px` (mobile), with prose capped around `64ch`.

## 9. Design Reference

- **Claude Design source:** https://claude.ai/design/p/9dee5556-21d2-48ab-876f-05b9380a181b?file=CuraNode-AI.dc.html&via=share
- **Local artifact used to derive this document:** `C:\Users\Cp9-30\Desktop\design\CuraNode-AI.dc.html` (all tokens, layouts and components above are read from it).
- `support.js` in the same folder is the generated `dc-runtime` renderer, and `deck-stage.js` is an unused starter scaffold — neither contributes design tokens.
- No design-token, component-inventory, or screen-mapping files exist elsewhere in this repository; `docs/PRD.md` and `docs/TDD.md` cover product and technical scope only.

## 10. Design Gaps

- **No focus styling.** Inputs set `outline:none` with no replacement focus ring — a keyboard-accessibility gap that must be resolved before implementation.
- **No modal, dialog, drawer, toast, or tooltip exists** in the artifact (zero `position:fixed` overlays). Any of these will be a new pattern with no precedent to copy.
- **No transitions defined.** Hover and active changes are instantaneous; a standard duration/easing token needs to be chosen.
- **Loose radius scale.** 19 distinct radii appear (`2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,18,20,30,999`). Consolidation to roughly `6 / 10 / 14 / 16 / 999` is implied but not stated.
- **Loose type scale.** Font sizes run in 0.5px steps (`10, 10.5, 11, 11.5, 12, 12.5, 13, 13.5, 14.5…`) and serif headings appear at 14 distinct sizes. Formalising a scale is needed.
- **Spacing is ad-hoc**, not a multiple-based scale (`7, 9, 11, 13px` all occur); no named spacing tokens exist.
- **`--nav-2` is defined but never used**, and `cnSpin` is declared but never applied.
- **No error/validation state** for inputs is designed (no error border, helper text, or field-level message).
- **No empty, loading-skeleton, or offline states** are shown for lists — only AI processing has a loading treatment.
- **Breakpoints are unspecified.** Desktop/mobile is a manual toggle in the prototype, so the real viewport threshold for switching the sidebar to the bottom tab bar is an open decision.
