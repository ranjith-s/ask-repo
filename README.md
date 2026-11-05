# Ask Repo

Ask questions about any public **Python, JavaScript or TypeScript** project on GitHub and get
answers that **cite the exact file and lines**, with every citation checked against what the
model actually read. Works with Claude, Gemini or a local model through Ollama, and with a
local, Docker or hosted (Neon, Supabase) Postgres.

<!-- Add screenshots: docs/screenshots/answer.png, docs/screenshots/source-viewer.png -->

## Quick start

Requires Docker and one of: an [Anthropic API key](https://console.anthropic.com/),
a [Gemini API key](https://aistudio.google.com/apikey), or [Ollama](https://ollama.com)
running locally (see [LLM providers](#llm-providers)).

```bash
cp .env.example .env        # set LLM_PROVIDER and that provider's key
```
```bash
docker compose up --build   # first build downloads the embedding model (~130 MB)
```

Open http://localhost:8000, click **Not sure? Try an example project**, and ask a question
once the project is ready (about a minute on CPU).

No Docker, or a hosted database? See [Running without Docker](#running-without-docker) and
[Using a hosted Postgres](#using-a-hosted-postgres-neon-supabase).

## Running locally

```bash
make setup       # install backend and frontend dependencies (once)
make db          # make sure Postgres + pgvector is reachable (hosted, native, or starts Docker)
make dev         # API on 127.0.0.1:8000 (reload) and the UI on http://localhost:5173
make test        # backend tests against a real local database
make lint        # ruff, mypy, tsc
make eval        # retrieval eval, no API key needed (SET=ky or SET=express for JS/TS)
```

Backend: Python 3.12 with [uv](https://docs.astral.sh/uv/). Frontend: Node 22 (React + Vite).
One `.env` at the repo root configures both Docker Compose and the backend run directly
(`backend/.env`, if present, overrides it). All settings are listed under
[Configuration](#configuration).

`make db` reads `DATABASE_URL`. A hosted database is only checked for reachability; a local one
is used if it is already running; otherwise the Docker one is started, or, without Docker,
you get a message saying what to install.

### Running without Docker

Docker only provides Postgres. Everything else runs natively, so without Docker you need
Postgres 16 with the [pgvector](https://github.com/pgvector/pgvector) extension (or a hosted
one, below), plus Python 3.12+ with uv, Node 22, and git (used to clone the repos you index).

1. Install Postgres and pgvector.

   macOS (Homebrew):
   ```bash
   brew install postgresql@16 pgvector
   ```
   ```bash
   brew services start postgresql@16
   ```

   Ubuntu / Debian:
   ```bash
   sudo apt install postgresql-16 postgresql-16-pgvector
   ```

   Windows: use WSL2 and follow the Ubuntu steps (`make` is not available in plain PowerShell).

2. Create the user and database the app expects by default (on Linux, prefix each with
   `sudo -u postgres`):
   ```bash
   psql postgres -c "CREATE USER askrepo WITH PASSWORD 'askrepo' SUPERUSER;"
   ```
   ```bash
   psql postgres -c "CREATE DATABASE askrepo OWNER askrepo;"
   ```
   `SUPERUSER` lets the app create the `vector` extension and lets the tests create their
   own `askrepo_test` database. That's fine on your machine; don't do it on a shared server.

3. Configure, install and run:
   ```bash
   cp .env.example .env      # set LLM_PROVIDER and its key
   ```
   ```bash
   make setup
   ```
   ```bash
   make dev
   ```
   Open http://localhost:5173. If your Postgres uses another port, set it in `DATABASE_URL`;
   `make` reads the host and port from there.

Differences from the Docker setup:
- **First indexing is slower.** The embedding model (~130 MB) downloads once into a local
  cache; the Docker image ships with it.
- **Ollama** is reached at `http://localhost:11434` (the default outside Docker).
- **If pgvector is missing**, the app stops at startup with a message saying how to install it.
- **The dev UI proxies `/api` to `127.0.0.1:8000`**, not `localhost`: Node may resolve
  `localhost` to IPv6 first while the backend listens on IPv4, which would make every API
  call fail with a 502.

### Using a hosted Postgres (Neon, Supabase)

Any Postgres 16+ with pgvector works. Put the provider's connection string in `.env`
(no quotes needed):

```bash
DATABASE_URL=postgresql://USER:PASSWORD@HOST:PORT/DBNAME?sslmode=require
```

Then use the normal commands. On startup the app creates the `vector` extension and its
tables in that database. With Docker, start only the app:

```bash
docker compose up --build --no-deps app
```

Without `--no-deps`, Compose also starts its own local database (unused but harmless).

| | Neon | Supabase |
|---|---|---|
| Connection string | Project → **Connect** | Project → **Connect** |
| Which one to use | The **direct** string (host without `-pooler`). The pooled one also works. | The **session pooler** (`…pooler.supabase.com:5432`). The direct `db.<ref>.supabase.co` host is IPv6-only unless you add IPv4, which many home networks and Docker can't reach. The transaction pooler (port `6543`) also works. |
| pgvector | Created by the app on startup | Created by the app on startup, or enable **vector** under Database → Extensions |
| Keep in the URL | `sslmode=require` (and `channel_binding=require` if given) | `sslmode=require` |
| Free tier pauses | After a few idle minutes; the first query wakes it (about a second) | Projects pause after about a week without use; resume in the dashboard |

**Example: Neon + Gemini, without Docker.** `.env` at the repo root:

```bash
LLM_PROVIDER=gemini
GEMINI_API_KEY=your-gemini-key
LLM_MODEL=gemini-3.5-flash
DATABASE_URL=postgresql://USER:PASSWORD@ep-xxxx.REGION.aws.neon.tech/neondb?sslmode=require&channel_binding=require
```

Then `make setup` and `make dev`. `make dev` should print
`Using the hosted Postgres at ep-xxxx…:5432.`, and the header shows **Gemini · gemini-3.5-flash**.

The connection pool is set up for hosted databases:
- **Transaction-mode poolers** (Supabase port 6543, PgBouncer) hand each transaction to a
  different server connection, which breaks server-side prepared statements after a few
  queries ("prepared statement already exists"). The pool disables them, at negligible cost.
- **Dropped connections**: Neon closes idle connections when it suspends. The pool checks
  each connection before use and silently reconnects, so the first question after a break
  doesn't fail.

Both were reproduced (a local PgBouncer in transaction mode; killing server connections)
before being fixed, and both are covered by tests (`tests/test_config_db.py`).

Things to know:
- **Storage**: a repo the size of Starlette (117 files, ~2,300 sections) takes about 22 MB,
  mostly vectors and their index. A 0.5 GB free tier fits roughly 20 such repos.
- **Latency**: each tool call is one or two queries, so a database in a region near you keeps
  answers snappy.
- **Tests never use `DATABASE_URL`.** They create and drop their own `askrepo_test` database,
  on local Postgres by default (`TEST_DATABASE_URL`), so `make test` needs a local Postgres or Docker.
- **Credentials** stay in `.env`, which git ignores. `make db` prints only the host and port.

## What it does

1. **Add a project**: paste a GitHub link. The repo is shallow-cloned and its code is split
   along its syntax tree (see [Supported languages](#supported-languages-and-files)): each
   function, class, method, interface/type and route handler becomes its own section, plus an
   outline of every file and class. Sections are embedded on CPU in batches and stored in
   Postgres with a full-text index. Each step shows live progress: download percentage and
   size, files read (and the current file), and sections made searchable with time left.
2. **Ask** in one of two modes:
   - **Thorough** (`deep` in the API): an agent with four read-only tools (`search_code`,
     `grep`, `read_file`, `list_files`) explores until it has evidence, then answers. The
     steps it takes are shown as it works.
   - **Quick** (`quick`): one hybrid search, one model call. Cheaper and faster.
3. **Check**: every `[path:Lx-Ly]` citation becomes a chip. The app checks whether the lines
   exist and whether the model was actually shown them; chips it couldn't confirm are amber.
   Clicking one opens the file with those lines highlighted, with a link to GitHub.

The footer of each answer shows how many sources were checked, the time taken and the
approximate cost; hovering the cost shows tokens, the share reused from cache, the exact cost
and the model that answered.

## Supported languages and files

| | How it is split | Notes |
|---|---|---|
| Python (`.py`) | Syntax tree (built-in `ast`) | Functions, classes, methods, module and class outlines; long definitions split with overlap |
| JavaScript (`.js .jsx .mjs .cjs`) | Syntax tree ([tree-sitter](https://tree-sitter.github.io/)) | Also CommonJS assignments (`res.send = function send…` → `res.send`) and calls that pass a handler (`router.post('/login', …)` → `router.post /login`) |
| TypeScript (`.ts .tsx .mts .cts`) | Syntax tree (tree-sitter) | Also interfaces, type aliases, enums; `.tsx` uses the TSX grammar; decorators and JSDoc stay with their definition |
| Docs (`.md .rst .txt`) | By heading, else line windows | |
| Config (`.toml .cfg .ini .yml .yaml`, `package.json`, `tsconfig.json`, `deno.json`, `Dockerfile`, `Makefile`) | Line windows | Answers dependency questions |

Skipped: `node_modules/`, virtualenvs, build output (`dist/`, `build/`, `.next/`, `.nuxt/`,
`.svelte-kit/`, `coverage/` …), bundles and source maps (`*.min.js`, `*.bundle.js`, `*.map`),
lockfiles and other JSON data, files over 300 KB, and anything that commonly holds secrets
(`.env*`, `*.pem`, `*.key`). Tests are indexed but rank below the code they test.

## LLM providers

Pick one with `LLM_PROVIDER` in `.env`. Indexing and search are the same for all three; only
the model that reads the evidence and writes the answer changes.

| | Claude (default) | Gemini | Ollama (local) |
|---|---|---|---|
| `LLM_PROVIDER` | `anthropic` | `gemini` | `ollama` |
| Credentials | `ANTHROPIC_API_KEY` | `GEMINI_API_KEY` | none |
| Default `LLM_MODEL` | `claude-opus-5-5` | `gemini-3.5-flash` | `qwen3:8b` |
| Streaming, tool use | yes | yes | yes (model must support tools) |
| Prompt caching | explicit breakpoints (see below) | implicit, automatic | n/a (local) |
| Reasoning | adaptive thinking; `LLM_EFFORT` = low / medium / high | model default thinking | model default |
| Safety refusal | automatic fallback to another Claude model, else an error shown | error shown | n/a |
| Cost shown | built in | "cost unknown" until `PRICE_*` is set | free |

**Claude**: set `ANTHROPIC_API_KEY`. `LLM_MODEL=claude-sonnet-5-5` is about half the cost of Opus.

**Gemini**: set `LLM_PROVIDER=gemini` and `GEMINI_API_KEY`. The default model is
`gemini-3.5-flash`; Google renames and retires models often, so `LLM_MODEL` takes any current
Gemini model id with function calling. Its price is not built in (it couldn't be confirmed
from Google's pricing page when this was written), so add the rates from
[Google's pricing page](https://ai.google.dev/gemini-api/docs/pricing) to see costs:
`PRICE_INPUT_PER_MTOK`, `PRICE_OUTPUT_PER_MTOK` and `PRICE_CACHE_READ_PER_MTOK` (USD per
million tokens). `gemini-2.5-flash` and `gemini-2.5-pro` are priced in the built-in table.

**Ollama**: install Ollama, then:

```bash
ollama pull qwen3:8b
```
```bash
ollama serve
```

(Any model tagged "tools" works, e.g. `llama3.1` or `mistral-small`; skip `ollama serve` if it
is already running.) Set `LLM_PROVIDER=ollama`. In Docker the app reaches Ollama on your machine
through `host.docker.internal:11434`. `OLLAMA_NUM_CTX` (default 32768) is sent with every
request, because Ollama's default context window is too small for the system prompt plus tool
results. Expect weaker answers than the hosted models: small models call tools less reliably,
so **Quick** mode often works better locally.

The adapters live in `backend/app/agent/llm/`, one file per provider (about 100 lines each)
behind provider-neutral message types. The agent loop, tools, prompts and citation checks are
shared. Adapters are used instead of a compatibility layer so each provider keeps what makes it
work well: Claude's thinking blocks and cache breakpoints, Gemini's thought signatures between
tool calls, Ollama's context size. All three are tested offline against canned responses
(`tests/test_providers.py`).

## Cost and caching

**Estimated cost per question on Claude Opus 5.5** ($4 input / $20 output per million tokens,
cache reads $0.20, cache writes $5):

| Mode | Model calls | Estimate |
|---|---|---|
| Quick | 1 | ~$0.03–0.05 |
| Thorough, before history caching | 3–4 | ~$0.13–0.16 |
| Thorough, with history caching | 3–4 | **~$0.10–0.12** |

Indexing and the retrieval evals cost nothing (embeddings run on CPU). The answer eval
(`make eval-answers`) is roughly $2–3 per set in Thorough mode, so about $6–9 for all three.
Sonnet 5.5 halves all of these; Ollama is free.

**How history caching works.** In Thorough mode each agent step resends the whole conversation
so far: system prompt, tools, question, and every earlier tool call and result. Claude's prompt
cache is prefix-based, so the adapter marks two cache breakpoints:

1. **System prompt + tool definitions**: identical for every question on a repo.
2. **The end of the conversation**: on the newest tool result, in Thorough mode only.

The next step then reads everything before its new tool results from cache at 5% of the input
price, and pays the 1.25× write rate only for what that step added.

Worked example for a typical 4-step answer with ~4k tokens of tool output per step (system
prompt already cached from an earlier question):

| | Uncached input | Cache read | Cache write | Input cost |
|---|---|---|---|---|
| Before | ~24k tokens × $4 | ~12k × $0.20 | ~0 | ~$0.10 |
| After | ~0.1k × $4 | ~24k × $0.20 | ~12k × $5 | ~$0.065 |

That cuts input cost by about a third. Output (~1.5–3k tokens, ~$0.03–0.06) is unchanged, so
the whole question costs about 25% less. Quick mode deliberately does not cache its
conversation: it makes one call, so a cache write would be a 25% surcharge never read back.

These are estimates from token counts. To see real numbers:

```bash
docker compose exec db psql -U askrepo -c "
  select provider, model, mode, count(*) as questions,
         round(avg(cost_usd), 4) as avg_cost,
         round(100.0 * sum(cache_read_tokens)
               / nullif(sum(input_tokens + cache_read_tokens + cache_write_tokens), 0)) as pct_cached
  from query_logs where error is null
  group by 1, 2, 3 order by 1, 2, 3;"
```

In a healthy Thorough run `pct_cached` should be well above 50%. In `query_logs`,
`input_tokens` counts **uncached** input only; cached tokens are in their own columns.

## Configuration

All settings are environment variables, read from the environment, then `backend/.env`, then
`.env` at the repo root (12-factor). `.env.example` has the common ones.

| Variable | Default | What it does |
|---|---|---|
| `LLM_PROVIDER` | `anthropic` | `anthropic`, `gemini` or `ollama` |
| `LLM_MODEL` | per provider | Model id; see [LLM providers](#llm-providers) |
| `ANTHROPIC_API_KEY` / `GEMINI_API_KEY` | none | Key for the chosen provider |
| `LLM_EFFORT` | `medium` | Claude only: `low`, `medium`, `high` |
| `LLM_MAX_TOKENS` | `16000` | Maximum answer length per model call |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Compose sets `http://host.docker.internal:11434` |
| `OLLAMA_NUM_CTX` | `32768` | Context window requested from Ollama |
| `PRICE_INPUT_PER_MTOK`, `PRICE_OUTPUT_PER_MTOK`, `PRICE_CACHE_WRITE_PER_MTOK`, `PRICE_CACHE_READ_PER_MTOK` | built-in table | USD per million tokens, for models not in the table |
| `DATABASE_URL` | `postgresql://askrepo:askrepo@localhost:5432/askrepo` | Any Postgres 16+ with pgvector |
| `TEST_DATABASE_URL` | `…/askrepo_test` on localhost | Used only by `make test` |
| `EMBEDDING_BACKEND` | `fastembed` | `hash` is an offline stand-in for tests; don't judge quality with it |
| `EMBEDDING_MODEL` / `EMBEDDING_DIM` | `BAAI/bge-small-en-v1.5` / `384` | Changing the model needs a matching dimension and re-adding projects |
| `AGENT_MAX_STEPS` | `8` | Most model calls per Thorough answer; the last one must answer |
| `TOOL_RESULT_MAX_CHARS` | `8000` | Longer tool output is truncated |
| `QUESTION_MAX_CHARS` | `2000` | Longest accepted question |
| `HISTORY_MAX_TURNS` | `6` | Earlier questions and answers sent with a follow-up |
| `CLONE_TIMEOUT_S` | `120` | A clone that takes longer is stopped |
| `MAX_FILES` / `MAX_FILE_BYTES` | `3000` / `300000` | Limits per project |
| `LOG_LEVEL` / `LOG_JSON` | `INFO` / `true` | JSON logs suit containers; `false` for readable local logs |

## Architecture

```mermaid
flowchart LR
    UI["React + Vite<br/>(served by FastAPI)"] -- "REST + SSE" --> API[FastAPI]
    API -- "background task" --> ING["Ingestion<br/>clone → parse (ast / tree-sitter) → embed"]
    ING --> PG[("Postgres<br/>pgvector + full-text<br/>repos · files · chunks · query_logs")]
    API --> QA["QuestionAnswerer<br/>quick | deep"]
    QA -- "tools: search_code · grep<br/>read_file · list_files" --> PG
    QA -- "streaming, tool use" --> LLM["LLM adapter<br/>Claude · Gemini · Ollama"]
    QA --> CIT["Citation checker<br/>valid · grounded"]
```

| Path | Responsibility |
|------|----------------|
| `backend/app/ingest/` | `source.py` (URL validation, streamed clone with progress, file filter), `chunker.py` (Python `ast`), `script_chunker.py` (JS/TS tree-sitter), `pipeline.py` (phases and progress) |
| `backend/app/store.py` | All SQL: vector, keyword, hybrid (RRF), grep, file access, progress, query logs |
| `backend/app/agent/` | `runner.py` (both modes), `tools.py`, `prompts.py`, `citations.py` |
| `backend/app/agent/llm/` | `base.py` (neutral types), one adapter per provider, `pricing.py` |
| `backend/app/api.py` | HTTP routes; answers stream as server-sent events |
| `backend/app/schema.sql` | Idempotent schema, including in-place migrations for older databases |
| `backend/scripts/ensure_db.py` | What `make db` runs: check a hosted DB, use a local one, or start Docker |
| `backend/evals/` | Three pinned eval sets, retrieval eval, answer eval |
| `frontend/src/` | `App.tsx`, `components/`, `progress.ts`, `api.ts` (the only module that calls the backend) |

**Retrieval.** Hybrid search fuses pgvector cosine similarity and Postgres full-text search
with reciprocal rank fusion. In the keyword index, the words of a section's name weigh most
(camelCase and snake_case both split), then its file path, then its text. Test files rank at
half weight, so implementations come first.

## Evaluation

Three eval sets, one per language, each pinned to one commit, in five categories: concept,
lookup, location, dependencies, and negative (not answerable from the repo).

| Set | Repo | Language | Questions |
|---|---|---|---|
| `starlette` | `encode/starlette` | Python | 22 |
| `ky` | `sindresorhus/ky` | TypeScript | 18 |
| `express` | `expressjs/express` | JavaScript (CommonJS) | 19 |

- `make eval` (or `make eval SET=ky`, `SET=express`): **retrieval**. hit@5 and MRR for
  vector, keyword and hybrid search. No API key, no cost.
- `make eval-answers SET=…`: **answers**. Runs the real agent on the configured provider and
  scores whether answers cite an expected file, citation validity and grounding, declines on
  negative questions, steps, latency and cost.

Current keyword results (hit@5 / MRR):

| Set | Keyword |
|---|---|
| Starlette (Python) | 0.75 / 0.50 |
| ky (TypeScript) | 0.62 / 0.40 |
| Express (JavaScript) | 0.88 / 0.78 |

These runs used the offline `hash` embedder, so only the keyword column is a real measurement;
re-run with the default `fastembed` for vector and hybrid numbers. Every ranking change was
made one at a time and measured (on all three sets since JS/TS support was added): see
[`docs/eval-log.md`](docs/eval-log.md).

<!-- AUTHOR: paste your fastembed `make eval` and `make eval-answers` numbers here -->

## Troubleshooting

| What you see | What it means |
|---|---|
| `Cannot reach the Postgres at …` from `make dev` | The host in `DATABASE_URL` is wrong, or your network blocks port 5432 |
| "Claude/Gemini isn't connected" in the header | The provider's API key is missing from `.env` |
| "Gemini rejected the request (404)" | The model id isn't available to your key; try another `LLM_MODEL` |
| "Could not reach Ollama …" | Start it with `ollama serve`; in Docker, check `OLLAMA_BASE_URL` |
| Every API call fails with 502 in `make dev` | Something else is on port 8000, or the backend isn't running |
| "pgvector extension is not installed" at startup | Install pgvector, or use the Docker or a hosted database |
| "Setup was interrupted" on a project | The server restarted while it was being added; add it again |
| A JS/TS project only answers from its docs | It was added before JS/TS support, when code files were skipped; add it again |

## Upgrading an existing install

The database schema migrates itself on startup, keeping your data. To benefit from newer
indexing in projects you added earlier, add them again (paste the same link): this re-indexes
JavaScript/TypeScript files that older versions skipped, and applies the newer keyword ranking.

---

<!--
  The sections below are for the author's own reasoning. The notes inside each
  are raw material from the build, not final text. Rewrite in your own words.
-->

## RAG / LLM approach and decisions <!-- AUTHOR -->

> _Notes to draw on (delete when written):_ see `docs/decisions.md` for each choice and its
> alternatives, and `docs/eval-log.md` for the measurements. Points worth explaining:
> - Why split code along its syntax tree (Python `ast`, JS/TS tree-sitter), and why the
>   file/class **outlines** exist. Why CommonJS assignments and route registrations are
>   treated as definitions.
> - Why hybrid search: embeddings miss exact identifiers, keyword search misses paraphrase.
>   What the evals showed: tests crowding out implementations, the tokenizer bug, camelCase
>   names getting no boost, test titles and file-name labels outranking real code.
> - Why an agent for Thorough mode *and* Quick mode as the baseline.
> - Context management: history keeps only Q&A text, tool output is truncated, the step cap
>   and the final no-tools turn, prompt caching of tools + system prompt + conversation.
> - Provider adapters instead of a compatibility layer (Claude, Gemini, Ollama).
> - Guardrails: read-only tools, DB-only paths, URL allow-list, prompt-injection notice,
>   and the citation **grounding** check as a deterministic quality signal.
> - Observability: request IDs, JSON logs, `query_logs` with steps/tokens/cache/cost/latency.

## Key technical decisions <!-- AUTHOR -->

## Productionizing and scaling on a hyperscaler <!-- AUTHOR -->

> _Notes:_ stateless API container (Cloud Run / ECS Fargate / Container Apps); managed
> Postgres with pgvector (Cloud SQL / RDS / Azure Flexible Server, or Neon / Supabase, which
> already work: pool tuned for transaction poolers and idle disconnects); ingestion moved to a
> queue + worker (Pub/Sub, SQS) with per-repo limits; secrets in Secret Manager / Secrets
> Manager with rotation; auth (OIDC) and per-user rate limits; private repos via a GitHub App;
> incremental re-indexing on push webhooks; OpenTelemetry traces to a backend; evals in CI on
> every retrieval/prompt change; HNSW tuning and partitioning chunks by repo at scale; cost
> controls (provider and model per route, Sonnet/Haiku or Ollama for Quick mode, effort tuning,
> cache hit rate).

## Engineering standards followed (and skipped) <!-- AUTHOR -->

> _Notes:_ followed: typed Python (mypy), ruff, ~130 tests against a real Postgres (plus a
> plain Postgres in CI for the no-pgvector path), agent tests with a scripted LLM, provider
> adapters tested against canned responses, failures reproduced before fixing, 12-factor
> config with one `.env`, structured logs, small commits, CI building the Docker image,
> one-command start. Skipped: frontend tests, auth, a migrations tool (the schema is
> idempotent SQL with in-place migrations), load testing, SAST/DAST in CI.

## How I used AI tools <!-- AUTHOR -->

> _Notes:_ `CLAUDE.md` holds the conventions the assistant must follow. Examples from this
> build where review or testing mattered:
> - The assistant's first RRF test asserted an order between two items with equal scores.
> - Grounding was first too generous: a search hit on a module *outline* marked the whole file
>   as seen. Fixed so only lines actually shown count.
> - A React effect returned `scrollIntoView()`'s Promise, which newer Chromium returns, and
>   blanked the page. Found by driving the UI with Playwright, not by reading code.
> - Hosted Postgres: prepared statements breaking behind a transaction pooler, and the first
>   queries failing after Neon dropped idle connections. Both reproduced before fixing.
> - A stalled clone wasn't stopped at its timeout: git's helper processes kept the pipe open.
>   A test with a fake `git` caught it; fixed by killing the whole process group.
> - The Vite proxy to `localhost` fails where Node resolves it to IPv6; demonstrated, then
>   changed to `127.0.0.1`. The README's `docker compose up app` advice was wrong (it also
>   starts the database); a dry run showed it.
> - Ranking changes were accepted only with eval numbers, on all three languages.

## What I'd do with more time <!-- AUTHOR -->

> _Notes:_ more languages via tree-sitter (Go, Java, Rust; each is one chunker); stemming in
> keyword search ("retried" vs "retry"); a code-specific embedding model, A/B'd with the evals;
> cross-encoder reranker; call-graph tool ("who calls X"); LLM-judge for answer correctness;
> incremental indexing; citations for paths containing brackets (Next.js routes); frontend
> tests; OpenTelemetry; an MCP server exposing the same tools to Claude Code / Cursor.

## Known limitations

- Public GitHub repositories only, default branch, up to 3,000 files of 300 KB each.
- Python, JavaScript and TypeScript get structure-aware chunking; other languages (Go, Java,
  Rust …) are not indexed yet.
- Keyword search has no stemming ("retried" doesn't match "retry"); the embedding model
  covers some of that.
- Citations use `[path:Lx-Ly]`, so files whose path contains `[` or `]` (Next.js routes such
  as `app/[id]/page.tsx`) can be read and searched, but their citations won't render as links.
- The default Gemini model has no built-in price; costs show as "cost unknown" until `PRICE_*`
  is set.
- Ingestion runs in the API process. If the server restarts mid-index, the project is marked
  failed on startup and must be added again.
- Eval runs index their pinned repos into the app's database (hidden from the UI).
- Conversations live in the browser tab; reload starts fresh.
