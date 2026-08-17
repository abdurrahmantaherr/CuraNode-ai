# CuraNode-AI — Product Requirements Document

| | |
|---|---|
| **Document** | PRD.md |
| **Version** | 1.0 (post-panel scope revision) |
| **Project** | CuraNode-AI — Cross-Hospital Health Management Platform |
| **Institution** | PUCIT, University of the Punjab — BS Data Science Final Year Project |
| **Advisor** | Dr. Syed Muhammad Ali |
| **Team** | Four members, split across four workstreams (see Section 8) |
| **Method** | Agile, two-week sprints, tracked in Jira, versioned in GitHub |
| **Status** | Draft for advisor and panel review |

---

> **Note for implementers (human or AI).**
> This document specifies *what* the product must do. It deliberately makes no technical, architectural, or data-model decisions — those live in the technical design document.
>
> **Where this document is silent on behaviour, stop and ask. Do not assume, infer, or fill the gap with a reasonable-sounding default.** A silent assumption here becomes an undocumented product decision that nobody on the team reviewed. If a requirement can be read two ways, treat that as a blocking question, not a judgement call.
>
> Requirement IDs (FR1–FR37, NFR1–NFR27) are stable. Reference them by ID in commits, branches, Jira tickets, and tests. Do not renumber them.

---

## 1. Overview / Purpose

In Pakistan's private healthcare sector, a patient's medical history lives on paper. Prescriptions, lab reports, and discharge notes are handed to the patient in a plastic bag, and it becomes the patient's job to carry, store, and produce them at the next visit. When those papers are lost — which is often — the history is simply gone. Doctors then prescribe based on what the patient can remember, and patients repeat tests they have already paid for.

CuraNode-AI addresses this by giving every patient a single, portable digital health identity, referred to throughout this document as a **Medical Passport**. The Medical Passport holds the patient's profile, visit history, prescriptions, and reports in one place. The patient owns it. Any doctor or clinic the patient authorises can see it instantly, from any participating facility.

The platform has three faces:

- **For patients** — a web and mobile-accessible app to view their own history, upload photographs of paper prescriptions and reports for automatic reading, book appointments and watch their place in the queue, get lab results explained in plain language, and ask questions in Urdu or English.
- **For doctors** — a dashboard showing a verified profile, the patient's cross-clinic history at the point of care, a short summary of what has changed since the patient's last visit, and basic analytics on their own practice.
- **For clinics** — tools to manage doctor schedules and the front-desk queue, translate documents between Urdu and English, and control who inside the facility can see what.

Behind all three sits an **AI Orchestrator** — a coordinating agent that decides which specialist capability a given request needs (reading a prescription image, retrieving relevant history, translating a document, answering a question) and routes it accordingly.

**Purpose of this document.** This PRD defines *what* CuraNode-AI must do and how success will be judged. It deliberately makes no technical or implementation decisions — no choice of frameworks, models, databases, or libraries appears here. Those belong in the technical design document that follows.

**A note on scope.** The original project proposal described roughly forty features and twenty sub-agents. The review panel judged this unimplementable within a Final Year Project timeline, and they were right. This document describes a narrowed, buildable product: one orchestrator, four specialist agents, three user roles, and a feature set sized to what four students can build, test, and demonstrate in the remaining academic year. Section 6 records what was cut and why, so the ambition is documented rather than lost.

---

## 2. Goals & Objectives

Each objective below is stated so that it can be measured at the end of the project. Where a baseline figure is marked *(to be confirmed)*, it must be established during requirements validation with a partner clinic before the target is treated as binding.

### G1 — Make a patient's history available at the point of care
**Objective 1.1** — A doctor with patient consent can retrieve that patient's complete cross-clinic history in **under 30 seconds** from the moment they open the patient record, compared with a current baseline of several minutes of verbal questioning and paper-shuffling *(to be confirmed)*.
**Objective 1.2** — At least **90%** of visits recorded through the platform during pilot testing are retrievable at a different participating facility without the patient producing any paper.

### G2 — Remove manual re-typing of paper records
**Objective 2.1** — The system correctly extracts medicine name, dosage, and frequency from **at least 85%** of legible printed prescriptions, and **at least 60%** of legible handwritten prescriptions, measured against a manually labelled test set of at least 200 documents.
**Objective 2.2** — A patient can go from photographing a prescription to having a reviewed, saved entry in their Medical Passport in **under 2 minutes**, including the time taken to correct extraction errors.

### G3 — Reduce front-desk and scheduling workload
**Objective 3.1** — At least **70%** of appointments during the pilot are booked by patients themselves, without a phone call to the clinic.
**Objective 3.2** — Patients can see their live position in the queue, with the displayed position accurate to within **one place** of the actual order.

### G4 — Serve patients in the language they actually speak
**Objective 4.1** — All patient-facing screens and notifications are fully available in both Urdu and English.
**Objective 4.2** — The support chatbot answers **at least 80%** of in-scope questions in the language the question was asked in, without needing a human to step in.

### G5 — Keep patients in control of their own records
**Objective 5.1** — No doctor or clinic can view a patient's history without an access grant the patient can see and withdraw.
**Objective 5.2** — **100%** of record accesses are logged and visible to the patient.

### G6 — Deliver a demonstrable, defensible final-year project
**Objective 6.1** — All Must-have requirements in Section 4 are complete, tested, and demonstrable at the final panel.
**Objective 6.2** — The system runs end to end in a live demonstration with at least **three simultaneous simulated users** across the three roles.

---

## 3. Target Users

### 3.1 Primary user groups

**Patients (urban, private-sector)**
Adults in Lahore, Karachi, and Islamabad who use private clinics and hospitals. They have a smartphone and intermittent but generally workable internet. Digital literacy ranges widely — some are comfortable with banking apps, others have only ever used WhatsApp. Many read Urdu more comfortably than English, and some read neither well. They currently have no copy of their own health record beyond whatever paper they have managed to keep.

*What they need:* to stop losing their records, to avoid repeating tests, to understand what a lab report means, and to book an appointment without a phone call.

**Doctors (general practitioners and specialists)**
Working in high-volume private clinics, seeing 50–100 patients a day, which leaves roughly five to eight minutes per patient. They are time-poor and will abandon any tool that adds clicks. They frequently see a patient for the second or third time with no record of what they themselves prescribed previously, and almost never have visibility into what a doctor at another facility did.

*What they need:* the patient's history in one glance, a fast answer to "what has changed since I last saw this person," and no data entry that they were not already doing.

**Clinic and hospital administrators**
Front-desk staff and practice managers who currently run scheduling on paper registers, whiteboards, and phone calls. They handle patient check-in, manage the waiting room, and field questions about wait times all day.

*What they need:* one place to manage doctor schedules and the day's queue, and fewer interruptions from patients asking how long the wait is.

### 3.2 Secondary considerations

- **Family caregivers** frequently accompany elderly patients and may operate the app on their behalf. The design must not assume the phone holder is the patient.
- **Low-connectivity users** will experience slow or dropped connections. The product assumes internet access is required, but must fail gracefully rather than losing a patient's uploaded data.

### 3.3 Explicitly not target users in this version

Government hospital staff, insurance providers, pharmacies, laboratory technicians, and pharmaceutical companies. Their needs are real and are noted in Section 6 as future scope.

---

## 4. Functional Requirements

Requirements are prioritised as:
**M (Must)** — required for the project to be considered complete.
**S (Should)** — built if Must-have work finishes on schedule.
**C (Could)** — built only if time genuinely allows; safe to drop.

Owner codes refer to workstreams: **AI** (orchestration/agentic core), **PAT** (patient platform), **DOC** (doctor platform), **INF** (clinic/admin, multilingual, deployment).

### 4.1 Accounts, identity, and consent

| ID | Requirement | Priority | Owner |
|---|---|---|---|
| **FR1** | A patient can create an account and is issued one Medical Passport that stays with them permanently, regardless of which clinics they visit. | M | PAT |
| **FR2** | A patient can record and edit their own profile: name, age, gender, contact details, blood group, known allergies, chronic conditions, and current medications. | M | PAT |
| **FR3** | A clinic administrator can register doctors belonging to their facility and mark them as verified practitioners. A doctor cannot access any patient record until verified. | M | INF |
| **FR4** | A patient can grant a named doctor or clinic access to their Medical Passport, view all currently active grants, and withdraw any grant at any time. | M | PAT |
| **FR5** | Every access to a patient record is recorded with who accessed it, when, and from which facility. The patient can view this list in full. | M | INF |
| **FR6** | The system supports three distinct roles — patient, doctor, clinic administrator — and each role sees only the functions and data permitted to it. | M | INF |
| **FR7** | A patient can grant a family caregiver delegated access to operate their account on their behalf. | C | PAT |

### 4.2 Medical Passport and history

| ID | Requirement | Priority | Owner |
|---|---|---|---|
| **FR8** | A patient can view their complete medical history as a single chronological timeline, showing visits, prescriptions, lab reports, and diagnoses across all participating facilities. | M | PAT |
| **FR9** | A patient can filter the timeline by date range, facility, doctor, or record type. | S | PAT |
| **FR10** | A patient can upload a photograph or scan of a paper prescription, lab report, or discharge note and attach it to their timeline. | M | PAT |
| **FR11** | The system automatically reads uploaded prescriptions and reports and proposes structured entries — medicine names, dosages, frequencies, test names, result values. | M | AI |
| **FR12** | Every automatically extracted entry is shown to the patient for confirmation or correction before it is saved. Nothing extracted is stored as fact without human confirmation. | M | AI |
| **FR13** | The system flags any extraction it is not confident about, so the user knows which fields to check most carefully. | S | AI |
| **FR14** | A patient can download or share a summary of their Medical Passport as a document. | S | PAT |

### 4.3 Appointments and queue

| ID | Requirement | Priority | Owner |
|---|---|---|---|
| **FR15** | A patient can search for a doctor by name, specialty, or clinic, and see that doctor's available appointment slots. | M | PAT |
| **FR16** | A patient can book, reschedule, and cancel an appointment. | M | PAT |
| **FR17** | A patient with an appointment on the current day can see their live position in the doctor's queue and an estimated waiting time. | M | PAT |
| **FR18** | A clinic administrator can define and edit doctor schedules, working hours, slot lengths, and days off. | M | INF |
| **FR19** | A clinic administrator can check patients in on arrival, mark them as seen, and reorder the queue when a case needs to be moved forward. | M | INF |
| **FR20** | Patients receive notifications for appointment confirmation, reminders, and cancellations. | S | PAT |

### 4.4 Doctor-facing capabilities

| ID | Requirement | Priority | Owner |
|---|---|---|---|
| **FR21** | A verified doctor can look up any patient who has granted them access and view that patient's full cross-clinic history. | M | DOC |
| **FR22** | The system presents a doctor with a short "what changed" summary for a returning patient — new diagnoses, medication changes, new test results, and new allergies recorded since that doctor last saw them. | M | DOC |
| **FR23** | A doctor can record a visit: presenting complaint, diagnosis, notes, and prescription. The record is written to the patient's Medical Passport immediately. | M | DOC |
| **FR24** | A doctor can view their own appointment list and queue for the day. | M | DOC |
| **FR25** | A doctor can see basic analytics on their own practice — patient volume over time, most common diagnoses, most prescribed medicines. | S | DOC |
| **FR26** | The system orders a doctor's waiting list to highlight patients whose recorded information suggests they should be seen sooner. Any such ordering is a suggestion only, always visible as such, and always overridable. | C | DOC |
| **FR27** | A doctor can configure a set of standard answers to routine patient questions (pre-visit instructions, fasting requirements, follow-up guidance) which the platform serves to their patients on the doctor's behalf. | C | AI |

### 4.5 Language, explanation, and support

| ID | Requirement | Priority | Owner |
|---|---|---|---|
| **FR28** | The entire patient-facing interface is available in Urdu and English, switchable at any time without losing the user's place. | M | INF |
| **FR29** | A patient can ask questions about using the platform through a chatbot, in Urdu or English, and receive an answer in the same language. | M | AI |
| **FR30** | The system produces a plain-language explanation of an uploaded lab report, stating which values fall outside normal ranges and what they generally relate to. | M | AI |
| **FR31** | Every AI-produced explanation carries a clear, unmissable statement that it is informational only, is not a diagnosis, and does not replace the treating doctor. The system never states a diagnosis, never recommends or changes a medication or dosage, and never advises a patient to stop or delay seeking care. | M | AI |
| **FR32** | A clinic administrator can submit a document and receive a translation between Urdu and English. | M | INF |
| **FR33** | Where a question falls outside what the chatbot can safely answer — including anything that reads as a medical emergency or as personal distress — it says so plainly and directs the person to their doctor or to emergency care, rather than attempting an answer. | M | AI |

### 4.6 AI orchestration

| ID | Requirement | Priority | Owner |
|---|---|---|---|
| **FR34** | A central orchestrator receives every AI request, determines which specialist capability is needed, routes the request, and returns the result to the interface that asked. | M | AI |
| **FR35** | The platform provides exactly four specialist capabilities in this version: document reading and extraction, history retrieval and summarisation, translation, and conversational support. | M | AI |
| **FR36** | When a specialist capability fails or is unavailable, the orchestrator returns a clear message to the user and the rest of the platform continues to function. | M | AI |
| **FR37** | Every AI request and its outcome is logged so that behaviour can be reviewed and evaluated. | S | AI |

---

## 5. Non-Functional Requirements

### 5.1 Performance and responsiveness
- **NFR1** — Any screen a doctor uses during a consultation loads within 3 seconds on a standard clinic internet connection.
- **NFR2** — A patient's full history loads within 5 seconds for a record containing up to 100 entries.
- **NFR3** — Automatic reading of an uploaded document returns a proposed result within 30 seconds.
- **NFR4** — The chatbot begins responding within 5 seconds of a question being sent.

### 5.2 Scale
- **NFR5** — The system supports at least 50 concurrent users and at least 1,000 registered patients without degradation, which is the realistic pilot scale for this project.
- **NFR6** — Design decisions must not create obstacles to later growth, but building for national scale is explicitly not a requirement of this version.

### 5.3 Availability and resilience
- **NFR7** — The platform is available during clinic operating hours, targeting 95% uptime across the pilot period.
- **NFR8** — An interrupted upload or a dropped connection must never result in silent data loss; the user is told clearly what did and did not save.
- **NFR9** — Internet connectivity is required. Offline operation is not supported in this version.

### 5.4 Usability and accessibility
- **NFR10** — A first-time patient can complete registration and view their profile without written instructions.
- **NFR11** — A doctor can retrieve a patient's history in no more than three interactions from their dashboard.
- **NFR12** — All patient-facing screens work on both desktop and mobile browsers without requiring an app installation.
- **NFR13** — Urdu text renders correctly, including right-to-left layout, across all supported screens.
- **NFR14** — Text is legible and controls are usable for older patients, with adequate contrast and touch target sizes.

### 5.5 Security and privacy
- **NFR15** — All data in transit and at rest is protected against unauthorised access.
- **NFR16** — Patient consent is the only basis on which a doctor or clinic may view a record. There is no administrative override in this version.
- **NFR17** — Access logs cannot be edited or deleted by any user role.
- **NFR18** — The platform aligns with Pakistan's applicable data protection and electronic crimes legislation, and follows the principle of collecting only data the product actually needs.
- **NFR19** — Test and demonstration data must be synthetic. No real patient data is used at any point during development.

### 5.6 AI quality and safety
- **NFR20** — No automatically extracted clinical information enters a patient's permanent record without a human confirming it.
- **NFR21** — AI outputs are informational. The system does not diagnose, does not prescribe, and does not alter clinical decisions.
- **NFR22** — Extraction accuracy is measured against a labelled test set and reported honestly, including failure cases, in the final project documentation.
- **NFR23** — Where the system is uncertain, it says so rather than guessing confidently.

### 5.7 Deployment and maintainability
- **NFR24** — The system is deployed in containers, as agreed in the project brief, so that the full platform can be brought up reproducibly on a fresh machine.
- **NFR25** — The entire system can be set up and run by a team member from documentation alone in under one hour.
- **NFR26** — All code is version-controlled in GitHub, with work tracked in Jira against the sprint plan in Section 8.

### 5.8 Documentation
- **NFR27** — The project delivers a technical design document, a user guide for each of the three roles, and an evaluation report covering AI accuracy results.

---

## 6. Out of Scope

The following are explicitly **not** part of this version. They are recorded here so the panel can see they were considered and consciously excluded, not overlooked.

### 6.1 Cut during post-panel scope reduction
- **Additional specialist agents.** The original twenty sub-agents are reduced to four (FR35). Agents for drug interaction checking, symptom analysis, diagnosis suggestion, insurance processing, medical coding, imaging analysis, treatment planning, and similar are all deferred.
- **Additional user roles.** Pharmacist, lab technician, insurance officer, and platform super-administrator roles are removed. Doctor verification sits with clinic administrators (FR3).
- **Deep clinical analytics.** Population health dashboards, outbreak detection, and cross-facility epidemiological reporting are out. Doctor analytics is limited to FR25.

### 6.2 Not attempted at all in this version
- **Any diagnostic or prescriptive function.** The platform does not diagnose conditions, suggest treatments, check drug interactions, or recommend dosages. This is a firm product boundary, not a timeline compromise.
- **Medical imaging.** X-ray, ultrasound, CT, and MRI interpretation.
- **Payments.** Consultation fees, insurance claims, billing, and pharmacy transactions.
- **Pharmacy and laboratory integration.** Sending prescriptions to pharmacies or receiving results directly from labs. All reports enter the system by patient upload.
- **Integration with existing hospital systems.** No connection to incumbent hospital information systems, and no compliance with international health data exchange standards in this version.
- **Video consultation and telemedicine.**
- **Native mobile applications.** Access is through a mobile-responsive web application only.
- **Offline use.**
- **Wearable and device data.**
- **Emergency and ambulance services.** The platform is not a route to emergency care and must never present itself as one.
- **Government sector deployment.** Private clinics only.
- **Languages beyond Urdu and English.**
- **Voice input and voice output.**

### 6.3 Deliberately deferred to future work
Multi-branch hospital hierarchies, doctor-to-doctor referrals, clinical decision support, insurance integration, a public API for third parties, and national-scale infrastructure. These are the natural next steps if the project continues past the FYP.

---

## 7. Success Metrics

### 7.1 Product metrics

| Metric | Target | How measured |
|---|---|---|
| Registered pilot patients | ≥ 100 | Platform records |
| Patients returning within 30 days of registering | ≥ 40% | Platform records |
| Doctors completing at least 10 consultations on the platform | ≥ 5 | Platform records |
| Appointments self-booked by patients | ≥ 70% | Booking source |
| Documents uploaded and confirmed into a Medical Passport | ≥ 300 | Platform records |
| Records retrieved at a facility other than where created | ≥ 50 instances | Access logs |

### 7.2 AI quality metrics

| Metric | Target | How measured |
|---|---|---|
| Field-level extraction accuracy, printed prescriptions | ≥ 85% | Labelled test set of ≥ 200 documents |
| Field-level extraction accuracy, handwritten prescriptions | ≥ 60% | Same test set |
| Corrections required per extracted document | ≤ 2 fields | Correction logs |
| Chatbot questions answered without escalation | ≥ 80% | Conversation logs |
| Translation rated acceptable by native Urdu speakers | ≥ 85% | Human review of ≥ 100 samples |
| Confident but wrong outputs (the critical failure mode) | Tracked and reported, target ≤ 5% | Manual review |

### 7.3 Efficiency metrics

| Metric | Baseline | Target |
|---|---|---|
| Doctor time to retrieve prior history | Several minutes *(to be confirmed)* | < 30 seconds |
| Patient time from photo to saved record | Not currently possible | < 2 minutes |
| Front-desk time spent on scheduling per day | *(to be confirmed)* | Reduced by ≥ 30% |

### 7.4 Satisfaction metrics

| Metric | Target |
|---|---|
| Patient usability score (standard usability questionnaire) | ≥ 70 |
| Doctors reporting the tool did not slow their consultation | ≥ 80% |
| Administrators preferring the platform to their current process | ≥ 70% |

### 7.5 Academic metrics
- All Must-have requirements complete, tested, and demonstrated at the final panel.
- Documentation deliverables in NFR27 submitted.
- Sprint velocity and burndown evidenced in Jira across all sprints.
- An honest evaluation report, including where the system underperformed.

---

## 8. Timeline / Milestones

The plan below is expressed in two-week sprints. Calendar months are indicative and **must be pinned to PUCIT's actual FYP submission and panel dates** before the first sprint begins. Total: nine sprints across roughly eighteen weeks of build, plus buffer.

### 8.1 Workstream ownership

| Workstream | Scope | Requirements |
|---|---|---|
| **AI** | Orchestrator and the four specialist capabilities | FR11–FR13, FR22, FR27, FR29–FR31, FR33–FR37 |
| **PAT** | Patient web/mobile experience | FR1, FR2, FR4, FR7–FR10, FR14–FR17, FR20 |
| **DOC** | Doctor dashboard | FR21, FR23–FR26 |
| **INF** | Clinic/admin tools, multilingual support, access control, deployment *(requester's workstream)* | FR3, FR5, FR6, FR18, FR19, FR28, FR32, and NFR24–NFR26 |

### 8.2 Milestones

| # | Milestone | Sprint | Exit criteria |
|---|---|---|---|
| **M0** | Requirements locked | Sprint 0 (2 wks) | This PRD approved by advisor. Clinic partner engaged. Roles, consent model, and scope boundary agreed. Jira backlog populated. |
| **M1** | Foundation | Sprints 1–2 (4 wks) | Three roles can register and sign in. Consent grant and withdrawal working (FR1–FR6). Patient profile and empty timeline visible. Containerised environment running for all four members. |
| **M2** | Records flowing | Sprints 3–4 (4 wks) | Document upload works. Automatic extraction returns proposals with human confirmation (FR10–FR12). Timeline populated and filterable. Doctor can look up a consented patient and record a visit (FR21, FR23). |
| **M3** | Mid-project review | End of Sprint 4 | Working demonstration to advisor: a patient uploads a prescription, a doctor at a different clinic retrieves it. Scope re-checked against remaining time; Should-have items confirmed or dropped. |
| **M4** | Scheduling live | Sprints 5–6 (4 wks) | Booking, rescheduling, cancellation, live queue position (FR15–FR17). Admin schedule and check-in tools (FR18, FR19). |
| **M5** | Language and intelligence | Sprints 7–8 (4 wks) | Full Urdu/English interface (FR28). Chatbot in both languages (FR29). Lab report explanation with safety statement (FR30, FR31, FR33). Document translation (FR32). "What changed" summary (FR22). |
| **M6** | Hardening and evaluation | Sprint 9 (2 wks) | Integration testing across all three roles. AI accuracy measured against the labelled test set. Usability testing with real patients and at least one practising doctor. Performance targets in Section 5 verified. |
| **M7** | Final delivery | Final 2 wks | Final report, user guides, evaluation report. Panel demonstration rehearsed end to end with three simultaneous users. Repository and documentation handed over. |

### 8.3 Risks to the schedule

| Risk | Impact | Response |
|---|---|---|
| Handwritten prescription extraction underperforms | High — a headline feature misses target | Treat printed prescriptions as the committed target; report handwritten performance honestly as a research finding rather than a failure. Human confirmation (FR12) means poor extraction degrades gracefully. |
| No partner clinic secured | High — no real validation | Begin outreach in Sprint 0. Fallback: recruit practising doctors individually for usability testing and use synthetic clinic data. |
| Urdu rendering and right-to-left layout proves costly | Medium | Build language support into every screen from Sprint 1 rather than retrofitting at M5. |
| Scope creeps back toward the original proposal | High | Section 6 is binding. Any addition requires an explicit removal of equivalent size, agreed with the advisor. |
| A team member becomes unavailable | High | No requirement is owned by exactly one person without a documented second reader. Weekly cross-workstream walkthroughs. |
| Examination periods compress available time | Medium | Sprints 3 and 7 planned at reduced capacity. Buffer held before M7. |

---

## Appendix A — Assumptions and open questions

**Assumptions**
1. Patients have a smartphone with a camera and workable internet access.
2. Clinics will permit doctors to use a new tool during consultations, at least on a trial basis.
3. Synthetic data is acceptable to the advisor and panel for demonstration purposes.
4. Doctor verification by clinic administrators is sufficient for a pilot; no regulatory registry check is required.
5. Patients are willing to grant record access digitally, given a clear and reversible consent mechanism.

**Decisions taken (binding on implementation — do not revisit without advisor sign-off)**

These two were previously open. They are settled here because both determine how records and permissions are structured, and leaving them open would mean an implementer choosing silently.

- **D1 — Contradictory records are never reconciled.** Where two facilities record conflicting information about the same patient, both entries are kept, each attributed to its source facility, doctor, and timestamp. The system never merges, overwrites, or hides an earlier entry, and never presents one as more correct than another. Contradictions are surfaced to the doctor as two entries side by side; resolving them is a clinical judgement, not a software function. Automated conflict resolution is out of scope (see Section 6.2).
  *Consequence:* records are append-only. Corrections are added as new entries that reference the original, not as edits to it.

- **D2 — Records the patient has not shared are entirely invisible.** A doctor without an access grant sees nothing — not the record, and not the fact that a record exists. There is no "locked record" indicator.
  *Rationale:* the alternative leaks the existence of sensitive care (mental health, reproductive health, HIV) to a doctor the patient chose not to share it with, which would undermine the consent model in FR4 and NFR16.
  *Acknowledged trade-off:* a doctor may therefore prescribe without knowing a relevant record exists. This is a deliberate choice of patient control over clinical completeness, appropriate for a pilot with no emergency-care role (Section 6.2), and should be stated openly in the final report rather than glossed over.

**Open questions to resolve before Sprint 1** *(all require a human answer — an implementer encountering these must stop and ask, per the note at the top of this document)*
1. What are PUCIT's exact FYP-II milestone and submission dates, and does the nine-sprint plan fit inside them?
2. Which clinic, if any, will partner for validation, and what will they permit?
3. Where is the line between a support chatbot and a health question the system must refuse? This must be written down as an explicit list before FR29 and FR33 are built. Until it exists, the chatbot's refusal behaviour is undefined and the feature must not be implemented.

---

*This document defines requirements only. All architectural, framework, model, and data storage decisions are deferred to the technical design document.*
