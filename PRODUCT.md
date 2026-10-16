# PRODUCT

## Purpose
CuraNode-AI is a cross-hospital health record platform for patients and doctors in Pakistan.
A patient holds one record (documents, profile, medical passport with QR code) and decides
which clinicians may see which sections. A doctor looks a patient up by passport number and
reads only what the patient shared.

Register: product, not marketing. The interface is a tool people use every day, so it is
calm, plain and fast to scan.

## Roles

### 1. Patient
- Non-technical, often on a phone, often reading Urdu (right to left).
- Holds documents, a medical passport with QR code, consent controls ("Who can see my
  record"), a medical timeline, doctor search and appointments.
- Needs: one obvious next step per screen, short plain copy, large touch targets,
  a record that feels safe.

### 2. Doctor and clinic staff
- Time-pressed, mostly on a laptop or tablet in a clinic, glancing at the screen between
  patients.
- Needs: today's schedule, the live queue, fast patient lookup by passport number, the list
  of patients who shared records, schedule management.
- Needs density and scannability more than comfort.

### 3. Clinic admin (secondary)
- Runs the front-desk queue and verifies doctors. Uses the same components as the doctor.

## Meaning of colour
- Teal ramp (#033236, #2C6975, #68B2A0, #CDE0C9, #E0ECDE) is the brand.
- Violet (#6B5FD9) is reserved for AI-read content only and keeps that single meaning.
- Green, amber, red are semantic status colours and are never decorative.

## Accessibility
- WCAG AA: 4.5:1 for body text, 3:1 for large text and UI components.
- Status is never colour alone: always icon plus text.
- 44px minimum touch targets, visible focus ring, skip link, landmarks, keyboard reachable.
- Every page works without JavaScript. JavaScript is enhancement only.
- English and Urdu (RTL) are both first class. Logical CSS properties only.
- Respect prefers-reduced-motion, prefers-reduced-transparency and prefers-color-scheme.

## Principles
1. One focal element per screen, at most one primary button per view.
2. Say each fact once. Never repeat the same data in several cards.
3. Never invent data. If a design needs data the page does not receive, hide that element.
4. Data surfaces are solid. Glass is for the shell and a few hero objects only.
5. Plain words for patients, not engineers.
6. The backend is read-only to the design work. Gaps go to docs/BACKEND-FOLLOWUPS.md.
