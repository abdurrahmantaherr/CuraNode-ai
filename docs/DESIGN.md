# CuraNode Design System

> **Updated for the October 2026 UI redesign.** The palette, shell and component notes below now
> describe the redesigned app (teal brand ramp, glass shell, sidebar and pill navigation). The
> repo-root `DESIGN.md` is the working reference (rules, components, file map);
> `tests/test_web.py::test_design_tokens_match_design_doc` pins the token values in this file.
> The original warm-paper palette (`#0F5C56`, `#1A2422`, `#F6F4F0`) was replaced on purpose.

## 1. Design Overview

CuraNode-AI is a calm, premium, clinical-record product. The look is **a mint-to-white atmosphere + a deep teal brand ramp + a distinct violet reserved exclusively for AI**. The navigation shell is frosted glass; data surfaces (tables, forms, lists) are solid white cards with hairline borders and soft teal-tinted shadows. Gradients are used only for the atmosphere, the passport hero and the brand badges.

Guiding ideas visible throughout the artifact:

- **Trust over flash.** Clinical copy is plain; ornament is minimal.
- **AI is always visually marked.** Anything machine-generated uses the `--ai` violet (border, soft fill, chip, or pulse) so it can never be mistaken for a verified clinical fact.
- **Provenance is a UI element.** AI answers and extracted data carry mono-font `src: …` citation chips.
- **Three typefaces with fixed jobs.** Serif (Newsreader) for page titles and names, sans (Public Sans) for UI, mono (IBM Plex Mono) for identifiers, times and passport numbers. All three are self-hosted.
- **Density follows the person.** Patient pages are airy (one action per screen, 16px body); doctor pages are denser and scannable.

## 2. Design Tokens

All tokens are CSS custom properties in `frontend/static/css/tokens.css` (the only file that may hold a raw colour). Light is `:root`; dark is `[data-theme="dark"]` and, when no theme was chosen, the device's `prefers-color-scheme`.

### Colors

| Token | Light | Dark | Use |
|---|---|---|---|
| `--brand-950` | `#033236` | `#033236` | Ink, primary button, nav pill (constant) |
| `--brand-700` | `#2C6975` | `#2C6975` | Links, secondary emphasis, chart main (constant) |
| `--brand-500` | `#68B2A0` | `#68B2A0` | Accents, fills, progress. Never text on white, never under white text |
| `--brand-200` | `#CDE0C9` | `#CDE0C9` | Selected fills |
| `--brand-100` | `#E0ECDE` | `#E0ECDE` | Tinted surfaces and the page wash |
| `--link` | `#2C6975` | `#78C0AE` | Links and brand text (`--brand` is an alias) |
| `--primary-bg` / `--primary-ink` | `#033236` / `#FFFFFF` | `#68B2A0` / `#033236` | Primary button |
| `--ai` | `#6B5FD9` | `#9B90F2` | AI accent: fills, rules, icons (text uses `--ai-ink`) |
| `--ai-soft` | `#EDEBFB` | `#2A2650` | AI panels and chips |
| `--ok` / `--ok-soft` | `#1B6F42` / `#DDEFE3` | `#5FC48A` / `#123F33` | Confirmed, in-range, success |
| `--warn` / `--warn-soft` | `#8A5A00` / `#F8EDD3` | `#E3B04B` / `#3A2F10` | Pending review, borderline values |
| `--err` / `--err-soft` | `#B3361F` / `#F8E3DE` | `#F08A76` / `#44221B` | Abnormal, allergy, revoke |

Status colours always pair with an icon and a word. Never hardcode the hex values outside `tokens.css`.

### Text, surface & line

| Token | Light | Dark | Use |
|---|---|---|---|
| `--text-1` (`--ink`) | `#033236` | `#E6F1EE` | Primary text |
| `--text-2` (`--muted`) | `#3F5F62` | `#A9C4C0` | Secondary text, descriptions |
| `--text-3` (`--faint`) | `#4A6667` | `#8DAAA6` | Tertiary text, mono metadata (still 4.5:1) |
| `--surface` | `#FFFFFF` | `#0A3F44` | Cards |
| `--surface-2` | `#F6F6F6` | `#08373C` | Inputs, inset rows, subtle fills |
| `--bg` | `#F6F6F6` | `#022A2D` | Page background (under the wash) |
| `--line` | `rgba(3,50,54,.10)` | `rgba(224,236,222,.12)` | Card & row borders |
| `--line-2` | `rgba(3,50,54,.22)` | `rgba(224,236,222,.26)` | Input borders, secondary button borders |

### Navigation (own scale, near-constant across themes)

| Token | Light | Dark |
|---|---|---|
| `--nav` | `#033236` | `#021F22` |
| `--nav-2` | `#0A4A50` | `#0A4A50` |
| `--nav-ink` | `#E0ECDE` | `#E0ECDE` |
| `--nav-muted` | `#A9CBC4` | `#A9CBC4` |
| `--nav-active` | `#FFFFFF` | `#68B2A0` |

### Typography

| Role | Family | Spec |
|---|---|---|
| Display | Newsreader (serif) | `400 34px/1.1` — design-system hero only |
| Page title | Newsreader | `500 28px/1.15` (22px on a phone) |
| Greeting / next step | Newsreader | `500 22–36px` |
| Section / card heading | Public Sans | `600 18px/1.3`, sentence case |
| Body | Public Sans | `16px/1.55`; `14px` in dense rows |
| Label / row title | Public Sans | `600 14px` |
| Small / meta | Public Sans | `12–14px` |
| Table column head | Public Sans | `600 12px`, uppercase, `letter-spacing:.06em` (the only uppercase label) |
| Mono | IBM Plex Mono | `400–500 14–36px`: IDs, times, passport numbers, values in tables |

Type scale: 12 / 14 / 16 / 18 / 22 / 28 / 36. Urdu uses Noto Nastaliq Urdu with line height 2.

Weights in use: **400, 500, 600, 700**. Sans weights are effectively 400/500/600 (700 only for table column heads). Body line-height 1.5–1.65; headings 1.1–1.2.

### Spacing, radius, shadow

| Token | Values |
|---|---|
| Spacing scale | 4px scale: `4 · 8 · 12 · 16 · 20 · 24 · 32 · 40 · 48 · 64px` (`--s-1` to `--s-10`) |
| Card padding | `24px` (desktop), `16–20px` on a phone |
| Radius | `8 · 12 · 16 · 24 · 32px` and pill. Cards `24px`, hero `32px`, controls `12px`, buttons and chips pill |
| Shadows | Tinted with the ink colour: `--shadow-1`, `--shadow-2`, `--shadow-3` (dark theme uses deeper teal-black) |

### Motion

Hover and press: 150 to 220 ms ease-out. One 320 ms entrance stagger on the dashboards. Keyframes: `cnSpin` (spinner) and `rise` (entrance). Nothing loops, and nothing animates under `prefers-reduced-motion`.

## 3. Layout

- **Shell:** a CSS grid: floating glass sidebar + fluid main column, `min-height:100dvh`.
- **Sidebar:** `248px` (a `76px` icon rail between 721 and 1023px, and collapsible by the person), sticky, own scroll. Groups for a patient: Overview, My record, Privacy, Care; a doctor and a clinic admin get one flat list.
- **Phone (up to 720px):** the sidebar is replaced by a floating dark pill at the bottom with the main destinations (a patient also gets a "More" sheet). The active item is a white pill with its label.
- **Page header:** role label and a Newsreader title, no bar.
- **Content area:** centred, `padding:24px 32px 64px`, on the atmosphere (soft mint-to-white wash).
- **Content max width:** `1200px` desktop.
- **Reading width:** long-form paragraphs capped at `56ch`–`80ch` (commonly `64ch`).
- **Responsive grid:** multi-column regions use CSS grid with a few breakpoints (1100px: doctor side column drops below; 900px: patient home blocks stack; 720px: phone layout; 320px is the smallest tested width, with no horizontal scroll).
- **Mobile stage:** rounded `30px` frame, `1px solid var(--line-2)`, padding `16px 14px 22px`, with a simulated status bar, a compact app bar (logo + screen title + avatar), and a sticky bottom tab bar.
- **Wide tables/grids** are wrapped in `overflow-x:auto` with a `min-width` (e.g. the schedule grid at `min-width:660px`).

## 4. Theme

- The theme follows the device (`prefers-color-scheme`) until the person picks one with the sidebar switch; the choice is saved in `localStorage` and applied by `theme-init.js` before first paint (`data-theme` on `<html>`). **Only custom properties are redefined** — no component styles branch on theme.
- Light: a mint-to-`#F6F6F6` wash with white cards. Dark: derived from the brand base `#033236` (`#022A2D` page, `#0A3F44` cards), not neutral grey.
- Brand text and status hues **lighten** in dark mode (e.g. link `#2C6975` → `#78C0AE`) to hold 4.5:1; their `-soft` companions become deep tints. The primary button flips to `#68B2A0` with `#033236` text.
- Borders switch from `rgba(3,50,54,…)` to `rgba(224,236,222,…)`.
- The navigation pill is dark in both themes; its active item is white in light and `#68B2A0` in dark.

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
