# AI Job Radar — Design Doc

**Status:** Draft · **Author:** Nishi · **Last updated:** 2026-11-11

## 1. Problem

Most AI roles at Series A+ startups are posted first on the company's own Greenhouse, Ashby or Lever board. Aggregators pick them up late or not at all. Keyword alerts miss many of them, because titles vary a lot ("Applied AI Engineer," "Agent Engineer," "Member of Technical Staff," "Senior Product Engineer, AI features"). What actually decides fit is in the posting's text: the track, the real seniority, the skills required versus preferred, and pay (when it's published at all). So today, finding the right 5 roles a week means reading dozens of postings across 100+ career pages by hand.

## 2. User and the moment that matters

**Primary user:** a software engineer with 2–8 years of experience moving into Applied / AI Engineer roles (platform, product or forward-deployed) at Series A+ startups, in NYC, SF or remote in the US. I'm the first user. Next come friends and Cornell alumni with similar profiles.

**The moment:** Monday morning, an email with 5–10 roles posted in the past week, ranked for me. Each one shows the company and its stage, the track, seniority, pay range if published, remote policy, one sentence on why it fits (citing the posting), and the skills I'd need. I open the 1–3 worth applying to and mark the rest. Success means I trust the list enough that I stop checking career pages myself.

## 3. What the system does

- **Input:** about 150 company job boards, read through their public APIs every 6 hours; a user profile built from a pasted resume plus filters (track, location, remote, minimum pay)
- **Output:** a weekly email digest, a web feed updated within hours, and a market page showing which skills are in demand
- **Frequency:** ingestion and extraction every 6 hours; matching whenever there are new postings or a profile changes; the digest weekly
- **Why an LLM:** keyword filters can't read the real track, seniority or required skills from messy prose, can't tell required skills from preferred ones, and can't explain a match. A small model handles those few judgment calls, and code handles everything else.

## 4. What the LLM does vs. what code does

| Step | LLM or code? | Why |
|---|---|---|
| Fetching postings | Code | Deterministic API calls |
| Detecting new / edited / closed postings | Code | Content hashes and board membership are exact |
| Extracting fields (track, seniority, skills, pay, remote) | LLM (small model) | Requires judgment over unstructured text |
| Validating fields | Code | Schema, taxonomy, evidence IDs and salary numbers can all be checked exactly |
| Filtering matches | Code | Location, seniority, track and pay are hard constraints |
| Scoring skill overlap | Code | Set arithmetic over taxonomy codes |
| Re-ranking the top 20 and explaining matches | LLM (stronger model) | Weighing tradeoffs and writing a grounded explanation |
| Extracting a profile from a resume | LLM, then the user edits it | Messy text; the user's correction is ground truth |

**Permissive filters, strict ranking.** The hard filters lean toward letting postings through, because a posting that is wrongly filtered out is a miss nobody ever sees, while a bad match that gets through is visible and produces feedback. Precision is enforced downstream: the re-ranker has to demote borderline postings, and precision@5 measures whether it does. In practice:

- A field extracted as `unknown` passes the filter for that field.
- Track filter: a posting passes if *any* of its tracks matches one of the user's. `unknown` passes; `other` (a real non-target role) does not.
- Pay filter: if the user's location matches a salary range's location, compare the user's minimum against that range. If no range matches, compare against the highest range.
- If the user has more than one acceptable location, the posting passes when *any* matching range meets the minimum.
- Pay types are compared as annual amounts: hourly × 2,080 (40 h × 52 weeks); OTE is compared against the minimum as is.

## 5. Extracted fields (`PostingFacts`)

"Unknown" or "unstated" is always an allowed answer. The model must never guess.

| Field | Type / allowed values | Evidence required? | How to check it |
|---|---|---|---|
| `track` | non-empty list of: platform, product, forward_deployed, research, other, unknown | Yes | Golden-set label (scored with Jaccard) |
| `seniority` | junior, mid, senior, staff_plus, manager, unknown | Yes | Golden-set label |
| `years_min` | int or null | Yes, if not null | Number appears in the cited paragraph |
| `salary_ranges` | list (possibly empty) of {`location`, `min`, `max`, `currency`, `pay_type`}; `pay_type` is annual_base, ote or hourly | Yes: one paragraph citation covers the whole list | Every `location` appears in `locations`; every number appears in the cited paragraph; cross-checked against ATS compensation when present (below) |
| `pay_source` | ats, text, none | No | Set by code, not the model |
| `locations` | list of normalized cities or "remote" | Yes | ATS location fields plus golden-set label |
| `remote_policy` | onsite, hybrid, remote, unknown | Yes | ATS workplace type when present; golden-set label |
| `visa_sponsorship` | yes, no, unstated | Yes, if not unstated | Golden-set label |
| `required_skills` | list of taxonomy codes (`docs/SKILLS.md`) | One paragraph per skill | Precision and recall against labels; codes must be in the taxonomy |
| `preferred_skills` | list of taxonomy codes | One paragraph per skill | Same as above |

**ATS cross-check for pay.** When the ATS publishes compensation, code compares it with the LLM's `salary_ranges`. On a disagreement, retry the extraction once; if it still disagrees, display the ATS value and log the disagreement. The LLM's raw output is always stored separately from the displayed value, so ATS compensation stays usable as an eval label in M3.

**Labeling rules** (the golden set and the prompt both follow these)

- **Track:** a posting can have several tracks; a generalist role spanning platform and product is labeled `[platform, product]`. `other` means a real non-target role; `unknown` means the posting doesn't say enough to tell.
- **Seniority:** decided by the posting, not only the title. A "Lead" role that manages people is `manager`; otherwise "Lead" is `staff_plus`.
- **`years_min`:** take the lowest stated number (Doctronic's "3+ with an advanced degree / 7+" → 3; Omni's "3–10+" → 3).

## 6. Success metrics (proposed targets)

| Metric | Target | Why this target |
|---|---|---|
| Track mean Jaccard | Target TBD | Track is a list, so partial credit is fairer than exact match; a wrong track can wrongly filter a posting out |
| Seniority accuracy | ≥ 85% | Often implied rather than stated, so it's harder |
| Remote policy accuracy | ≥ 95% | Usually stated, and often in ATS fields |
| Salary correct when stated (within $5K) | ≥ 95% of ranges | Pay is the field users check first. Scored per range, so a wrong range for one city can't hide behind a right one; missing and extra ranges are counted too (precision/recall over ranges) |
| Invented salary (stated when the posting has none) | 0 observed in the golden set, plus a code check that blocks it | ~100 no-salary postings can only bound the true rate below ~3% (rule of three), so the guarantee comes from the code check; reports include the 95% upper bound |
| `unknown` rate per field | Target TBD | `unknown` passes the filters, so the model must not be able to look safe by abstaining; scored against labels where the true value is known |
| Required-skill precision / recall | ≥ 0.85 / ≥ 0.75 | Precision matters more: a false "requires X" misleads the gap analysis |
| Match precision@5 (on my labeled profiles) | ≥ 0.6 | At least 3 of the top 5 are worth opening |
| Match NDCG@10 | Set a baseline in M3, then improve on it | No prior number to anchor to |
| Judge agreement with my labels (Cohen's kappa) | ≥ 0.6 before the judge gates CI | Below that, the judge is too unreliable to block releases |
| p95 time from posting to feed | ≤ 8 hours | 6-hour poll plus processing |
| Extraction cost per posting | ≤ $0.01 | Keeps a full re-run of the backfill cheap |
| Re-rank cost per user per week | ≤ $0.05 | Keeps 20 users well under $5 a month |

**Which error is worse:** a wrong answer is worse than a missing one. A bad match in the top 5, or an invented salary, teaches users to ignore the digest. A missed match costs one role. So I optimize precision (precision@5, skill precision, zero invented salaries) and accept some loss of recall.

## 7. Out of scope (for now)

- Applying on the user's behalf, or writing cover letters and resumes
- Roles outside AI engineering (other engineering, PM, sales, research-only)
- Companies without a public Greenhouse, Ashby or Lever board (Workday, custom career sites), and LinkedIn scraping
- Predicting pay for postings that don't publish it
- Company research beyond stage and name (funding news, Glassdoor)
- Mobile app, payments, teams or shared accounts
- Republishing full posting text: every card links to the original

## 8. Risks and open questions

**Risks**
- **Coverage:** some target companies don't use Greenhouse, Ashby or Lever. Mitigation: track the coverage rate in the seed list, and accept the gap for now.
- **Self-bias:** I'm both the labeler and the main user, so the evals may fit my taste. Mitigation: get relevance labels from at least 4 other people's profiles in M3.
- **Small eval set:** about 200 labeled postings means wide confidence intervals. Mitigation: bootstrap intervals on every report, and add cases from feedback over time.
- **Taxonomy drift:** new frameworks and tool names appear all the time. Mitigation: drift watch in M6 and deliberate taxonomy updates.
- **Cost runaway:** retry loops or a routing bug could get expensive. Mitigation: a daily budget cap and an alert (M5).
- **Resume privacy:** profiles are personal data. Mitigation: store only the extracted profile plus the raw text the user can delete, keep it out of traces, and use synthetic profiles in tests.
- **Terms of use:** the content belongs to employers. Mitigation: public endpoints only, low request rates, link back to the original, non-commercial.
- **Engagement:** friends may sign up but not give feedback. Mitigation: one-click feedback in the email, and personally ask the first 10 users for a 10-minute call.

**Open questions**
- Lever's public postings endpoint and fields: confirm in M1
- Funding stage data: start with a hand-tagged seed list. Is there a reliable source to update it?
- What counts as "edited": any content-hash change, or only changes to extracted fields?
- Should closed postings stay visible, marked as closed, for the market page's history?
- Ranges split across paragraphs (e.g. a remote range in a footnote): allow a second citation, or force one?
- The `unknown` rate target and how it's scored (an error, or tracked separately?)
- Track mean Jaccard target (90% accuracy and 0.90 mean Jaccard are not the same bar)

## 9. Milestone plan

See `docs/PLAN.md`.