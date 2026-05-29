# Telegram AI Companion

A personal AI assistant that lives in Telegram and actually *remembers* you across
weeks of conversation — not just the last few messages. Built as a personal project
to explore long-term agent memory, native tool use, and proactive (agent-initiated)
messaging done in a way that isn't annoying.

It chats on **Claude Sonnet**, runs background extraction/decisions on **Claude Haiku**,
and stores long-term memory in **Postgres + pgvector** with local embeddings.

> Single-user by design. This is a "companion for one person" architecture, not a
> multi-tenant SaaS — there's an auth gate that silently ignores everyone except the
> configured user.

## What it does

- **Long-term memory (RAG).** Extracts durable facts from conversation, embeds them
  locally, and retrieves the most relevant ones on every turn — with a re-ranking pass
  (see below), not just raw cosine top-k.
- **Tasks, reminders, diary (GTD).** `/task`, `/done`, `/diary`, plus the agent can set
  real reminders itself via tool use and message you when they fire.
- **Day planning with real-world data.** Pulls weather, location, and nearby points of
  interest to suggest concrete activities.
- **Proactive check-ins that adapt.** Morning brief, evening check-in, habit nudges, and
  weekly planning — but each one passes through a gate that decides *whether now is a good
  moment* based on the user's recent pressure/engagement, so it doesn't spam.

## Architecture

```
Telegram (aiogram 3.x)
      │  single-user auth middleware
      ▼
  Context builder ──► parallel DB reads + query embedding ──► ContextBundle
      │                  (profile, tasks, recent msgs, RAG facts, diary, life-state)
      ▼
  Claude Sonnet (chat)  ◄── prompt caching on system blocks
      │  native tool-use loop
      ├─► get_weather / get_user_location / wiki_geosearch / schedule_reminder
      ▼
  Response  ──► background (Haiku): fact extraction, routine detection, task auto-close
                                                                    │
  APScheduler ──► proactive jobs ──► signals + LLM gate ──► send / soften / skip
```

### Things worth looking at in the code

- **Memory re-ranking** — [`src/core/memory.py`](src/core/memory.py) (`_rerank_facts`).
  Candidates from pgvector are re-scored by `similarity × kind-weight × confidence +
  freshness`, with **minimum quotas** per critical fact kind (health / preference / goal)
  so important context isn't crowded out by whatever happens to be semantically closest.
- **Tool-use loop** — [`src/core/llm.py`](src/core/llm.py) (`chat_with_tools`). Proper
  Anthropic native tool use: bounded iterations, full assistant/tool_result round-trips,
  a forced finalize turn if it hits the iteration cap, and token-usage logging on every
  call. System blocks use `cache_control: ephemeral` for prompt caching.
- **Context assembly** — [`src/core/memory.py`](src/core/memory.py) (`build_context`).
  The current-message embedding is computed concurrently with 4 DB queries; a second
  round fans out fact + diary similarity search. Keeps per-turn latency down.
- **Adaptive proactive gating** — [`src/core/signals.py`](src/core/signals.py) +
  [`src/core/proactive_gate.py`](src/core/proactive_gate.py). Computes a `pressure` /
  `engagement` signal, then a cheap Haiku call decides send / soften / skip before any
  proactive message goes out.
- **Tool registry & dispatch** — [`src/core/tools.py`](src/core/tools.py). Every tool
  call is timed, timeout-guarded, logged for audit, and never raises into the loop
  (errors come back as `{"error": ...}`).

## Stack

| Layer | Choice |
|---|---|
| Bot framework | Python 3.11, aiogram 3.x |
| LLM (chat) | Claude Sonnet (Anthropic native tool use + prompt caching) |
| LLM (background) | Claude Haiku (extraction, gating, routine detection) |
| Memory | Supabase Postgres + `pgvector` |
| Embeddings | `BAAI/bge-m3`, run locally (no embedding API cost) |
| Scheduling | APScheduler |
| Voice → text | OpenRouter (Gemini audio), optional |

External data comes from **free, no-key public APIs**: Open-Meteo (weather),
OpenStreetMap Overpass + Nominatim (places, geocoding), Wikipedia GeoSearch.

## Quick start (local)

1. Create a bot via [@BotFather](https://t.me/BotFather), grab `BOT_TOKEN`.
2. Get your numeric `user_id` from [@userinfobot](https://t.me/userinfobot) → `ALLOWED_USER_ID`.
3. Get an `ANTHROPIC_API_KEY` from [console.anthropic.com](https://console.anthropic.com).
4. Create a Supabase project, copy the `DATABASE_URL` (Connection string → Transaction pooler).
5. Apply migrations in order:
   ```bash
   for f in migrations/0*.sql; do psql "$DATABASE_URL" -f "$f"; done
   ```
6. Configure env:
   ```bash
   cp .env.example .env   # then fill BOT_TOKEN, ALLOWED_USER_ID, ANTHROPIC_API_KEY, DATABASE_URL
   ```
7. Run:
   ```bash
   docker compose up --build
   ```

First start downloads the `bge-m3` embedding model (~2 GB) into a cached Docker volume,
so subsequent restarts are fast.

## Bot commands

| Command | What it does |
|---|---|
| `/start` | Onboarding (FSM questionnaire) |
| `/task <text>` · `/tasks` · `/done <id>` | GTD task management |
| `/diary <text>` | Diary entry (plain text also works) |
| `/plan_day` · `/activity` | Agent plans using weather + location |
| `/where [city]` | View or set current location |
| 📎 → Location | Send coordinates for nearby-activity search |
| `/timezone` · `/pause` · `/tone` · `/usage` · `/export` | Settings & introspection |

## Notes & scope

- **Single-user.** No multi-tenancy, no per-user isolation beyond the auth gate — that
  was a deliberate constraint to keep the memory model simple.
- **Secrets** live only in `.env` (gitignored). `.env.example` ships placeholders.
- Code comments are in Russian (it started as a personal tool); the architecture above
  is the English summary.

---

*Personal project. Shared to show how I approach LLM agents: memory, tool use, and
proactive behavior that respects the user.*
