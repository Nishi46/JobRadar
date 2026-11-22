"""The fixed skill vocabulary that extraction, matching and evals all share.

Why a fixed list instead of free-text skills: skill precision/recall (DESIGN §6) and the
profile-vs-posting overlap score are set arithmetic, which only works when both sides use
the same codes. Free text would turn "LangGraph", "langgraph" and "agent frameworks" into
three different skills.

The source of truth is the "Starting taxonomy" table in docs/SKILLS.md; `test_taxonomy`
fails if this dict drifts from it. Change both together, deliberately, and log the change
in docs/DECISIONS.md (a renamed code silently breaks every stored extraction and label).
Descriptions are copied verbatim because the extraction prompt shows them to the model.
"""

SKILLS: dict[str, str] = {
    "LLM_PROD": "Shipping LLM features to production",
    "EVALS": "Evals and quality measurement",
    "AGENTS": "Agents and tool calling",
    "PY": "Python",
    "OWN": "Ownership in ambiguity / startup mindset",
    "TS": "TypeScript / JavaScript / Node.js",
    "CONTEXT": "Context and prompt engineering",
    "RAG": "RAG, retrieval, vector DBs, embeddings",
    "PRODUCT": "Product sense and customer focus",
    "FT": "Fine-tuning and post-training",
    "BACKEND": "Backend and API design",
    "DATA": "SQL, data modeling, pipelines",
    "CLOUD": "Cloud and DevOps (AWS, Kubernetes, Terraform, CI/CD)",
    "OBS": "LLM observability and tracing",
    "FW": (
        "Named agent frameworks (LangGraph, LangChain, Mastra, Vercel AI SDK, LlamaIndex, "
        "PydanticAI)"
    ),
    "RELIAB": "Reliability and guardrails (structured outputs, fallbacks, retries, permissions)",
    "FULLSTACK": "Full-stack / React",
    "COMMS": "Communication",
    "DISTRIB": "Distributed systems, concurrency, durable workflows",
    "EXPER": "Experiment design, A/B tests",
    "DOMAIN": "Domain knowledge (healthcare, finance, insurance, compliance)",
    "MCP": "Model Context Protocol",
    "VOICE": "Voice and multimodal (ASR, TTS, voice agents)",
    "TRADEOFF": "Cost, latency and model routing",
    "LEAD": "Technical leadership",
    "AITOOLS": "AI-native development (Cursor, Claude Code, Copilot)",
    "MLFUND": "ML fundamentals, PyTorch",
    "PORTF": "Public portfolio of shipped work",
    "STREAM": "Streaming responses (SSE, WebSockets)",
    "BROWSER": "Browser or computer-use automation",
    "OSS": "Open-source contributions",
    "SANDBOX": "Sandboxing, isolation, permission models",
    "SYSLANG": "Rust or C++",
    "SERVING": "Open-weight model serving and quantization",
}

# Membership checks (validation, dropping unknown profile skills) use this.
SKILL_CODES: frozenset[str] = frozenset(SKILLS)
