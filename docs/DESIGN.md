# AI Job Radar — Design Doc

> **You write this one (M0, Teach mode).** Keep it to one page. Fill in each section in your own words; Claude reviews it but doesn't draft it. The prompts in italics are there to delete as you go.

**Status:** Draft · **Author:** _you_ · **Last updated:** _date_

## 1. Problem

_Who is struggling with what today? One paragraph. Why do existing job boards and LinkedIn alerts fall short for someone looking for AI Engineer roles at Series A+ startups?_

## 2. User and the moment that matters

_Describe the primary user in one sentence. Then describe the single moment the product exists for (e.g. "Monday morning, she opens an email with 5 roles that fit and one sentence on why each fits"). What does success feel like to them?_

## 3. What the system does

_In 4–6 bullets: what goes in, what comes out, how often._

- Input:
- Output:
- Frequency:

## 4. What the LLM does vs. what code does

| Step | LLM or code? | Why |
|---|---|---|
| Fetching postings | | |
| Detecting new / edited / closed | | |
| Extracting fields | | |
| Validating fields | | |
| Filtering matches | | |
| Ranking and explaining matches | | |

_The rule of thumb: use code wherever the answer is deterministic and checkable. Use the LLM only where judgment over messy text is needed._

## 5. Extracted fields

_List every field in `PostingFacts`, its type, allowed values, and how you'd know if it's wrong._

| Field | Type / allowed values | Evidence required? | How to check it |
|---|---|---|---|
| | | | |

## 6. Success metrics

_Set a target for each before building. You'll measure against these in M3 and M5. Pick numbers you can defend, not round numbers._

| Metric | Target | Why this target |
|---|---|---|
| Seniority / track accuracy | | |
| Salary extraction accuracy (within tolerance) | | |
| Required-skill precision / recall | | |
| Match quality (NDCG@10 or precision@5) | | |
| Judge agreement with my labels | | |
| p95 time from posting to user | | |
| Cost per posting | | |

**Which error is worse:** _a missed good match, or a bad match shown to the user? Write one sentence. This decides which metric you optimize._

## 7. Out of scope (for now)

_At least 5 things you are deliberately not building. Examples to consider: applying on the user's behalf, non-AI roles, scraping sites without public APIs, resume rewriting, salary prediction._

## 8. Risks and open questions

_What could make this fail? (Data quality, ATS coverage, cost, users not engaging, terms of use.) What do you not know yet?_

## 9. Milestone plan

See `docs/PLAN.md`.
