# Postmortem: <short title>

> Copy this file to `YYYY-MM-DD-short-title.md`. Blameless: focus on how the system allowed it, not who did it. You write this one (M6, Teach mode).

**Date of incident:** · **Duration:** · **Severity:** low / medium / high · **Author:**

## Summary

_Two or three sentences: what happened, who was affected, how it was fixed._

## Impact

- Users affected:
- Bad outputs shown or sent:
- Cost impact:
- SLOs breached:

## Timeline (Eastern time)

| Time | Event |
|---|---|
| | First sign of the problem |
| | Detected (how?) |
| | Mitigated |
| | Resolved |

## Root cause

_What actually caused it? Keep asking "why" until you reach something you can change. For LLM incidents, say whether it was infrastructure (timeouts, rate limits), data (an ATS changed its format) or quality (the model's outputs drifted)._

## Detection

_How did you find out? How should you have found out sooner? Which alert, eval or check would have caught it?_

## What went well / what went badly

- Went well:
- Went badly:

## Action items

| Action | Type (prevent / detect / mitigate) | Status |
|---|---|---|
| Add the failing cases to the golden set | detect | |
| | | |

## Lessons

_One or two sentences you'd say in an interview about what this taught you._
