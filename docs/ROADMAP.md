# 8-Week AI Engineer Roadmap

The bigger plan this project sits inside: the skills to learn and the three portfolio projects to build between Nov 9, 2026 and Jan 2, 2027 to land an Applied / AI Engineer role (Platform or Product) at a Series A+ startup.

## Where I stand

I already cover most of the top two tiers of what employers ask for: shipping LLM features to production, Python and TypeScript, React/Node, Postgres, RAG with hybrid retrieval and re-ranking, vector search (pgvector, FAISS), AWS/Azure, Docker, CI/CD, and eval suites (protocol-drift's 400-question suite, injection-pareto's 8,000-episode benchmark). **The gaps are agents in production, not retrieval.**

| Skill (share of 50 postings) | Status | What to do |
|---|---|---|
| Shipping LLM features to production (70%) | Have | Project 1: real users, SLOs and a postmortem make it visible |
| Evals (64%) | Strong | Project 1: CI release gates and online evals. Project 2: trajectory evals |
| Agents and tool calling (62%) | Partial | Main focus of Project 2: a production-grade agent with recovery and state |
| Python (62%) / TypeScript (44%) | Have | No study needed |
| Context engineering (40%) | Partial | Memory, context compaction, prompt versioning |
| RAG (40%) | Strong | Don't build another RAG project |
| Fine-tuning and post-training (36%) | Gap | Project 3: one focused SFT/LoRA project |
| LLM observability (30%) | Partial | OpenTelemetry tracing into Langfuse or LangSmith |
| Named frameworks (28%) | Partial | Learn LangGraph and Vercel AI SDK (LangChain already known) |
| Reliability and guardrails (28%) | Partial | Structured outputs, fallbacks, permission gates |
| Distributed systems and durable workflows (22%) | Partial | Temporal or a durable state machine, plus retries and idempotency |
| MCP (18%) | Gap | Build and publish an MCP server |
| Cost, latency and model routing (16%) | Partial | Prompt caching, model tiering, a router with measured savings |
| Sandboxing and permissions (4%, but high-paying platform roles) | Strong | OpenShell work is rare. Build a public analog |
| Voice (18%) | Gap | Optional stretch only |

## Learning roadmap

Production engineering and evals come first, because every later project reuses the same tracing, eval harness and release process.

| Phase | Weeks | Project | What to learn | Gate |
|---|---|---|---|---|
| 1. Production LLM engineering | 1–2 | P1 | Provider APIs (tool calling, structured outputs, streaming); versioned prompts, schemas (Zod / Pydantic), feature flags; OpenTelemetry tracing into Langfuse; cost and latency per call | Pipeline runs on real data with full traces |
| 2. Evals that gate releases | 2–3 | P1 | Golden set you label; code checks; LLM-as-judge calibrated against your labels; CI regression gate; shadow and canary releases | A bad prompt change is blocked in CI |
| 3. Reliability and cost | 3 | P1 | Retries, timeouts, provider fallback, graceful degradation; prompt caching and model tiering; rate limits; SLOs, dashboards, incident log, online evals | P1 live with real users and SLOs |
| 4. Agents and tools | 4 | P2 | Tool-call loops, planning, retry and repair; LangGraph vs raw APIs; context and memory; MCP servers and clients | Agent completes a task through MCP tools |
| 5. Durable, governed agents | 5–6 | P2 | Temporal or a Postgres state machine; idempotent tools; sandboxed execution, permission policy, human approval; trajectory evals; prompt-injection defenses | Kill it mid-run and it resumes cleanly |
| 6. Fine-tuning and routing | 7–8 | P3 | Dataset building; SFT with LoRA (TRL or Unsloth); serving with vLLM; compare against frontier models; confidence-based router; quality-vs-cost Pareto chart | Routing cuts cost at equal eval scores |

## Projects

Three core projects in roadmap order, plus two optional ones. Together with protocol-drift (RAG + evals) and injection-pareto (security benchmarking), they answer almost every requirement in the 50 postings. Build public versions only: keep IBM code, data and client details out.

### Project 1 — AI Job Radar: an LLM feature in production, run by evals

See `docs/PLAN.md` for the full build plan. Targets shipping to production (70%) and evals (64%), plus observability, reliability, cost, and the TypeScript product stack. It deliberately leaves out agents, MCP and fine-tuning, which Projects 2 and 3 cover.

### Project 2 — Governed agent runtime (flagship, platform-leaning)

**What it is:** An open-source agent harness that runs multi-step agents safely and reliably. A good demo task is a financial-compliance investigator: given an alert, the agent pulls data through MCP tools, runs analysis code in a sandbox, asks a human to approve risky actions, and writes a cited report.

**Why it stands out:** It combines the three things the highest-paying postings ask for (Artisan, Encharge AI, Moment, Andera, Sekai): durable agent execution, sandboxing and permissions, and trajectory evals. Very few candidates can show all three, and it tells the OpenShell story in a form I can share.

**Build:**
- Agent loop with tool calling, planning, retry and repair, and cancellation (LangGraph or raw APIs, and write down why)
- Durable runs with Temporal or a Postgres state machine: kill the process mid-run and resume it
- Two or three MCP servers for the tools (data lookup, document search, report writer)
- Sandboxed code execution (Docker, gVisor or Firecracker) with a permission policy and human-approval gates enforced in code, not in the prompt
- Prompt-injection defenses, reusing injection-pareto findings
- OpenTelemetry tracing into Langfuse, with cost and latency per step
- Trajectory eval suite (task success, tool-call correctness, policy violations) run in CI

**Done when:** README with an architecture diagram, eval results, a failure-and-fix writeup, and a 3-minute demo video. GitHub plus one blog post on durable agents.

### Project 3 — Fine-tune and route (model-leaning)

**What it is:** Fine-tune a small open-weight model (e.g. Qwen or Llama, 3–8B) with LoRA for one narrow task from existing work, such as classifying prompt-injection attempts from injection-pareto data, or extracting posting fields from Job Radar data. Then build a router that sends easy requests to the small model and hard ones to a frontier model.

**Why it stands out:** Fine-tuning appears in 36% of postings and model routing in 16%. Most applicants only describe these; I'll have numbers.

**Build:**
- Dataset construction and cleaning, with a held-out test set
- SFT with LoRA (TRL or Unsloth on a rented GPU; budget about $50–100)
- Serve with vLLM; measure latency, throughput and cost per 1,000 requests
- Compare against frontier models with the eval suite
- A confidence-based router, with the quality-vs-cost Pareto curve

**Done when:** a results table, a Pareto chart, the model on Hugging Face, and a writeup.

### Optional — Harden SignalPath into a shipped product

A full-stack TypeScript product with an agent at its core and real users: Next.js + Vercel AI SDK, streaming, structured outputs with Zod, tool calling against GitHub/Linear/Slack (ideally via MCP), memory across sessions, model tiering and fallback, feedback feeding an eval dataset, eval-gated deploys.

### Optional stretch — Voice agent weekend

Only if ahead of schedule. A Pipecat or LiveKit voice front end on Project 1 or 2, measuring end-to-end latency and turn-taking. Voice shows up in 18% of postings (Hippocratic, Prosper AI, Tandem Health, Salient).

## 8-week schedule

Assumes about 12–15 hours a week alongside the IBM job. Start applying in week 3.

| Week | Learn | Build | Job search |
|---|---|---|---|
| 1 (Nov 9) | Provider APIs, structured outputs, OpenTelemetry + Langfuse | P1: M0–M1 (scope, ingestion) | Update resume headline to "Applied AI Engineer"; pick 20 target companies |
| 2 (Nov 16) | Golden sets, LLM-as-judge calibration, CI eval gates | P1: M2–M3 (extraction, matching, evals) | Write the "agent I built, how I measured it, a failure I fixed" story |
| 3 (Nov 23) | Fallback, caching, model tiering, canary releases, SLOs | P1: M4–M5 (product, harden, launch) | Apply to first 10 (product and evals roles) |
| 4 (Nov 30) | Agent loops, LangGraph, MCP | P2: agent loop + MCP servers. P1: M6, fix incidents | P1 blog post; reach out to engineers at targets |
| 5 (Dec 7) | Temporal, sandboxing, permission models | P2: durable runs, sandbox, approval gates | Apply to next 10 (platform roles) |
| 6 (Dec 14) | Trajectory evals, injection defenses | P2: tracing, trajectory evals in CI, README + demo | Practice agent system-design interviews |
| 7 (Dec 21) | LoRA/SFT with TRL or Unsloth, vLLM | P3: dataset, fine-tune, serving | Light week (holidays): polish READMEs |
| 8 (Dec 28) | Model eval and routing | P3: comparison, router, Pareto chart, writeup. P1: four-week metrics + postmortem | Apply in the early-January hiring push; follow up |

## How to present it

- **Resume:** one line per project with a number ("resumes 100% of runs after a crash", "cut cost 62% at equal quality via routing", "40 weekly users").
- **Pinned GitHub repos:** Projects 1–3, protocol-drift and injection-pareto. Every README opens with what it does, a demo GIF, eval results, and a "what broke and how I fixed it" section.
- **Interview stories to prepare:** an agent failure debugged from traces; a time evals changed a decision; a cost/latency tradeoff with numbers; why a guardrail belongs in code instead of the prompt.
- **System design practice:** "Design a customer-support agent," "design an eval pipeline," "design a sandboxed code-execution service for agents."
- **Positioning:** lead with forward-deployed experience at Fortune 500 financial firms plus agent security and governance. Regulated, document-heavy domains came up in 20% of postings.

## Best-fit targets from the 50

- **Platform / agent infrastructure:** Encharge AI, Artisan, Moment, Andera, Sekai, bunch, Traversal
- **Product with regulated or document-heavy domains:** Drata, FieldGuide, Patlytics, Distyl AI, Juro, Atlas Card, TRM Labs
- **Evals-focused roles:** Town, FurtherAI, Momentic
