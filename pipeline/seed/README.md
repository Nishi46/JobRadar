# Seed list of job boards

`companies.csv` tells the poller which boards to fetch. One row per company.
Only rows with `verified=true` are safe to poll.

| Column | Meaning |
|---|---|
| `company` | Company name |
| `ats` | `greenhouse`, `ashby`, `lever`, or another ATS name (out of scope for the poller) |
| `board_token` | Exact token used in the API URL (case matters on Ashby); empty if unknown |
| `stage` | Funding stage: from `docs/SKILLS.md` for the 50, otherwise from the source in `notes` |
| `in_skills_analysis` | `true` for the 50 companies in `docs/SKILLS.md` |
| `verified` | `true` only if the check below passed on `checked_on` |
| `jobs_at_check` | Number of jobs the endpoint returned at the check |
| `checked_on` | Date of the check (YYYY-MM-DD) |
| `notes` | Why unverified, name collisions, stage source, other boards |

**Verification rule.** The public, keyless endpoint returned HTTP 200 with a well-formed job list, and the content belongs to the company (a job ID from the company's own listing is on the board, or the board's company name or job URLs match).
- Greenhouse: `https://boards-api.greenhouse.io/v1/boards/<token>/jobs`
- Ashby: `https://api.ashbyhq.com/posting-api/job-board/<name>`
- Lever: `https://api.lever.co/v0/postings/<site>?mode=json` (docs: https://github.com/lever/postings-api)

**Adding a company.** Find its careers page, read the token from the board URL, call the endpoint once, confirm the jobs are that company's, then add a row (sorted by name, case-insensitive) with today's date. If you cannot confirm it, add the row with `verified=false` and say why in `notes`.
