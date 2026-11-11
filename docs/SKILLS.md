# AI Engineer (Platform & Product) Skills — 50 Series A+ Startups

Source analysis for the skill taxonomy (M0) and the market page (M4). Read 50 live AI Engineer postings (Platform or Product roles, one per company) at Series A, B, C and later-stage startups in November 2026, and coded every skill each asks for, required or preferred. Percentages are the share of the 50 postings that mention each skill.

**Headline:** these are software engineering jobs first. The most common requirements are shipping LLM features to production (70%), evals (64%), agents / tool calling (62%) and Python (62%). Only 12% mention ML fundamentals or PyTorch. Median stated experience is 5 years (range 2 to 10+).

## Starting taxonomy (34 skill codes)

Use these codes as the fixed vocabulary for `PostingFacts.required_skills` and `preferred_skills`. Change them deliberately (and log the change in `DECISIONS.md`) when drift watch flags something new.

| Code | Skill | Share | Tier |
|---|---|---|---|
| `LLM_PROD` | Shipping LLM features to production | 70% | 1 |
| `EVALS` | Evals and quality measurement | 64% | 1 |
| `AGENTS` | Agents and tool calling | 62% | 1 |
| `PY` | Python | 62% | 1 |
| `OWN` | Ownership in ambiguity / startup mindset | 60% | 1 |
| `TS` | TypeScript / JavaScript / Node.js | 44% | 2 |
| `CONTEXT` | Context and prompt engineering | 40% | 2 |
| `RAG` | RAG, retrieval, vector DBs, embeddings | 40% | 2 |
| `PRODUCT` | Product sense and customer focus | 38% | 2 |
| `FT` | Fine-tuning and post-training | 36% | 2 |
| `BACKEND` | Backend and API design | 34% | 2 |
| `DATA` | SQL, data modeling, pipelines | 34% | 2 |
| `CLOUD` | Cloud and DevOps (AWS, Kubernetes, Terraform, CI/CD) | 32% | 2 |
| `OBS` | LLM observability and tracing | 30% | 2 |
| `FW` | Named agent frameworks (LangGraph, LangChain, Mastra, Vercel AI SDK, LlamaIndex, PydanticAI) | 28% | 3 |
| `RELIAB` | Reliability and guardrails (structured outputs, fallbacks, retries, permissions) | 28% | 3 |
| `FULLSTACK` | Full-stack / React | 28% | 3 |
| `COMMS` | Communication | 26% | 3 |
| `DISTRIB` | Distributed systems, concurrency, durable workflows | 22% | 3 |
| `EXPER` | Experiment design, A/B tests | 22% | 3 |
| `DOMAIN` | Domain knowledge (healthcare, finance, insurance, compliance) | 20% | 3 |
| `MCP` | Model Context Protocol | 18% | 3 |
| `VOICE` | Voice and multimodal (ASR, TTS, voice agents) | 18% | 3 |
| `TRADEOFF` | Cost, latency and model routing | 16% | 3 |
| `LEAD` | Technical leadership | 16% | 3 |
| `AITOOLS` | AI-native development (Cursor, Claude Code, Copilot) | 16% | 3 |
| `MLFUND` | ML fundamentals, PyTorch | 12% | 4 |
| `PORTF` | Public portfolio of shipped work | 8% | 4 |
| `STREAM` | Streaming responses (SSE, WebSockets) | 8% | 4 |
| `BROWSER` | Browser or computer-use automation | 6% | 4 |
| `OSS` | Open-source contributions | 4% | 4 |
| `SANDBOX` | Sandboxing, isolation, permission models | 4% | 4 |
| `SYSLANG` | Rust or C++ | 4% | 4 |
| `SERVING` | Open-weight model serving and quantization | 2% | 4 |

## Platform vs. Product roles

- **Platform-leaning** (Traversal, bunch, Moment, Andera, Encharge AI, Artisan, Sekai, Bluefish Labs, Sierra, Notion): distributed systems, durable execution and retries, observability, agent runtimes and harnesses, sandboxing and permissions, developer-facing APIs/SDKs.
- **Product-leaning** (Slash, Drata, HeyGen, Unify, Omni, You.com, Juro, Topline Pro, FieldGuide, Loop): TypeScript and React, streaming UIs, structured outputs, tool calling in user-facing features, product sense, shipping speed.

## Most-named tools

- **Languages:** Python (31 postings), TypeScript (22), Go (4), Rust/C++ (2)
- **Model APIs:** OpenAI and Anthropic named together in 9; Gemini/Vertex and Bedrock a few times
- **Frameworks:** LangGraph (8), LangChain (5), Vercel AI SDK (4), Mastra (4), LlamaIndex (2), PydanticAI (2)
- **Data:** Postgres (8), Redis, pgvector, Pinecone, ClickHouse, MongoDB
- **Frontend:** React (11), Next.js
- **Cloud/infra:** AWS (11), Kubernetes (3), Terraform, Docker, Temporal
- **Eval/observability:** LangSmith, Langfuse, Braintrust, Datadog

Base salary where listed: about $120K–$400K; most US mid-to-senior ranges $150K–$300K.

## The 50 roles

| # | Company | Stage | Role | Years stated |
|---|---|---|---|---|
| 1 | Traversal | B | AI Engineer, Backend/Platform | — |
| 2 | bunch | B | Sr Applied AI Engineer, AI Platform | 5+ |
| 3 | StackBlitz (Bolt) | B | Sr Applied AI Engineer | — |
| 4 | Liberate | B | Sr AI Agent Engineer | 7+ |
| 5 | Tandem Health | B | Senior AI Engineer | 5+ |
| 6 | Reducto | B | Backend/AI Engineer | 2+ |
| 7 | Town | B | AI Engineer, Evals & Agent Quality | Senior/Staff |
| 8 | Glow | B | Senior AI Engineer | — |
| 9 | Doctronic | B | Senior AI Engineer | 3+ (adv. degree) / 7+ |
| 10 | Distyl AI | B | AI Engineer | 8+ |
| 11 | Patlytics | B | AI Engineer | — |
| 12 | Topline Pro | B | Senior AI Engineer | 5+ |
| 13 | Unify | B | Applied AI Engineer | 3+ |
| 14 | Juro | B | AI Engineer | none fixed |
| 15 | Bluefish Labs | B | Staff AI Engineer | 8+ |
| 16 | You.com | C | Sr AI Engineer, Product Engineering | 5+ |
| 17 | Omni | C | AI Engineer | 3–10+ |
| 18 | Drata | C | Sr Product Engineer 2 (AI features) | 7+ |
| 19 | Slash | C | Applied AI Engineer | — |
| 20 | Moment | C | Agent Engineer | — |
| 21 | FieldGuide | C | Sr AI Engineer, Quality | "multiple" |
| 22 | Momentic | A | AI Engineer | 3+ |
| 23 | Mastra | A | AI Engineer | — |
| 24 | Magic Patterns | A | Applied AI Engineer | — |
| 25 | Sable | A | Applied AI Engineer, Generalist | 2+ |
| 26 | Artisan AI | A | Staff AI Engineer, Agent Architecture | — |
| 27 | Arcade AI | A | Staff Applied AI Engineer | 8+ |
| 28 | Hellyeah AI | A | AI Engineer, Platform & Simulation | — |
| 29 | Andera | A | Lead AI Engineer | — |
| 30 | Notion | Late-stage | SWE, AI Connections (Developer Platform) | 10+ |
| 31 | Vooma | A | AI Engineer | — |
| 32 | Sierra | Late-stage | Software Engineer, Platform | 4+ |
| 33 | 7AI | A | Senior AI Engineer | 6+ |
| 34 | Prosper AI | A | Senior AI Engineer | 3–5 |
| 35 | FurtherAI | A | Sr/Staff AI Engineer, Evals & Model Quality | 5+ |
| 36 | Salient | A | Applied AI Engineer | — |
| 37 | Fundamental | A | Applied AI Engineer | — |
| 38 | Cambio | A | AI Engineer | — |
| 39 | TRM Labs | C | Sr SWE, AI Automation | 6+ |
| 40 | Hippocratic AI | C | AI Engineer (Sr–Principal) | 5+ |
| 41 | Atlas Card | C | Founding Applied AI Engineer | 5+ |
| 42 | Crogl | A | AI Engineer | — |
| 43 | Sekai | A | AI Agent Engineer (Coding Agent) | — |
| 44 | Gyde | A | AI Engineer | 3+ leadership |
| 45 | Jetson | A | AI Engineer | — |
| 46 | HeyGen | A | Applied AI Engineer, Video Agent | 2+ |
| 47 | Encharge AI | B | AI Software Engineer, Agent Harness | 10+ |
| 48 | Outsmart | A | Senior Applied AI Engineer | 5+ |
| 49 | Loop | C | AI Software Engineer, Backend | 2 |
| 50 | Composio | A | MTS, Product Eng (Python) | — |

**Method:** stages from Fast AI Jobs' Series A/B/C AI Engineer listings plus two later-stage companies (Notion, Sierra). Postings read from each company's Ashby, Greenhouse or Rippling page in November 2026. One role per company; forward-deployed, sales, marketing, research-only, robotics and intern roles excluded. A skill counts if it appears in required or preferred lists.
