# Element Workspace

A self-hosted, ChatGPT-like workspace for documents. Create projects, drop in files, then let the
AI read, search **and actually edit** them — with citations, streaming answers and one-click undo.

* **File workspace** — projects → nested folders → files, context menu, drag & drop (files *and*
  folders), ZIP download / upload / extract.
* **Chat with tools** — the model creates, reads, renames, moves, deletes files; appends Excel
  sheets and cells; writes DOCX/PDF/Markdown; translates documents. Every mutating tool call is
  recorded as a reversible operation.
* **RAG with citations** — every document is parsed (Excel cells, Word, PDF, Tesseract OCR for
  images), chunked, embedded and stored in Qdrant. Answers cite `[1] file.pdf (page:3)`.
* **Multi-provider LLM layer** — OpenAI, Anthropic, Google Gemini, DeepSeek, OpenRouter, Ollama and
  any OpenAI-compatible endpoint (Groq, Together, xAI) behind one `LLMProvider` interface, with
  automatic fallback and encrypted keys in PostgreSQL.
* **Viewers** — Excel (SheetJS, per-sheet tabs), Word (mammoth), PDF (PDF.js), images + OCR,
  Markdown/code/JSON with syntax highlighting.
* **UI** — three panes: file tree · chat · viewer (the chat/viewer splitter is draggable and the
  width is remembered), dark/light theme and a **RU/EN interface** (Russian by default, switch in
  the top bar or on the settings page).

Everything runs with `docker compose up`; a single API key in `.env` is enough, and without any key
the app still starts (degraded banner + optional local Ollama profile).

---

## Architecture

The stack is a small set of single-purpose containers behind one Nginx entry point. Next.js serves
the UI; FastAPI owns the REST API, the WebSocket chat channel and the agent loop. The agent never
touches the filesystem directly: it calls typed tools that go through the same service layer as the
UI, which keeps validation, versioning and undo in one place.

File **bytes** live in a Docker volume as opaque blobs (`/data/workspace/blobs/<random>`), while the
tree (projects, folders, names, versions) lives in PostgreSQL. Renames and moves are therefore pure
metadata operations and path traversal is impossible by construction. Parsing/chunking/embedding is
queued on Celery (Redis broker) and written into Qdrant with per-chunk payloads (`node_id`,
`project_id`, page/sheet label, text) used for citations. Provider keys entered in the UI are
encrypted with Fernet (key derived from `APP_SECRET_KEY`) and only ever returned as a mask.

```
                        ┌─────────────────────────────┐
   browser  ──────────► │  nginx :3000                │
                        │  /        → frontend        │
                        │  /api/*   → backend (REST)  │
                        │  /api/ws/*→ backend (WS)    │
                        └───────┬──────────────┬──────┘
                                │              │
                    ┌───────────▼───┐   ┌──────▼─────────┐
                    │ frontend      │   │ backend        │
                    │ Next.js 14    │   │ FastAPI        │
                    │ App Router    │   │ agent + tools  │
                    └───────────────┘   └──┬───┬───┬─────┘
                                           │   │   │
                     ┌─────────────────────┘   │   └──────────────────┐
                     │                         │                      │
              ┌──────▼───────┐        ┌────────▼────────┐    ┌────────▼────────┐
              │ postgres:16  │        │ qdrant          │    │ redis:7         │
              │ tree/versions│        │ vectors + text  │    │ celery broker   │
              │ chats/keys   │        └─────────────────┘    └────────┬────────┘
              └──────────────┘                                      │
                                                             ┌───────▼────────┐
                                                             │ worker         │
                                                             │ Celery indexer │
                                                             └────────────────┘
        optional profile local-llm:  ┌──────────────────────┐
                                     │ ollama (OpenAI API)  │
                                     └──────────────────────┘
```

Volumes: `postgres_data`, `qdrant_data`, `redis_data`, `workspace_data` (blobs + local embedding
models cache), `ollama_data`.

> **Naming:** the product is **Element Workspace**. The Compose project is intentionally still
> `ai-file-workspace` (see `name:` in `docker-compose.yml`), so existing volumes keep their names
> and no data is lost when pulling a new version. Renaming it would create new, empty volumes.

---

## Quick start

```bash
git clone <this repo> element-workspace
cd element-workspace

cp .env.example .env
# put ONE key in .env, e.g.
#   OPENAI_API_KEY=sk-...
# generate a real encryption secret:
python -c "import secrets;print(secrets.token_urlsafe(48))"   # → APP_SECRET_KEY

docker compose up -d --build      # or: make up
```

Open **http://localhost:3000**, press **+ Project**, drop a folder or files into the tree.

* Health/readiness (and the UI banner) come from `GET /api/health`.
* `docker compose ps` should show `postgres`, `qdrant` and `redis` as *healthy*; `backend` health
  turns green after migrations complete.
* Direct backend for debugging: `http://localhost:8001/api/docs` (OpenAPI).

### Local-only mode (no cloud key)

```bash
docker compose --profile local-llm up -d
docker compose exec ollama ollama pull llama3.2
# .env
LLM_PROVIDER=ollama
LLM_MODEL=llama3.2
EMBEDDING_PROVIDER=local          # sentence-transformers, CPU, downloaded on first use
```

The Ollama container exposes an OpenAI-compatible API on `http://ollama:11434/v1`, so the same
`CompatibleProvider` code path is used. Without any provider the UI shows a clear banner and the
health endpoint reports `degraded` instead of failing.

### Make targets

| command | effect |
| --- | --- |
| `make up` | build + start everything, opens `http://localhost:3000` |
| `make logs` | tail logs from all containers |
| `make down` | stop the stack, keep volumes |
| `make reset` | stop + delete **all** volumes (workspace, DB, vectors) |
| `make pull-model` | start the `local-llm` profile and pull `OLLAMA_MODEL` |
| `make backend-shell` / `make worker-shell` / `make psql` | debug shells |

### Interface language and layout

The UI ships in **Russian (default)** and **English**: toggle with the `RU/EN` button in the top bar
or pick a language under `/settings → Interface language`. The choice is stored in `localStorage`
(`afw-locale`); no backend change or restart is required. Brand names, file names, tool identifiers
and document contents are not translated.

Workspace layout is `file tree → chat → viewer`. Drag the handle between the chat and the viewer to
resize the chat (320 px … width minus 320 px for the viewer); double-click the handle to reset it to
460 px. The width is remembered per browser.


## Project layout

```
.
├── docker-compose.yml          # 7 services + optional ollama profile
├── .env.example                # every setting documented
├── Makefile                    # up / logs / reset / shells
├── nginx/                      # single entry point (REST + WS + static)
├── backend/
│   ├── Dockerfile              # multi-stage: builder → api / worker targets
│   ├── requirements.txt        # pinned runtime deps
│   ├── requirements-local.txt  # sentence-transformers (optional, build arg)
│   ├── alembic.ini
│   ├── alembic/                # async env + initial schema revision
│   └── app/
│       ├── main.py             # FastAPI app, CORS, error handlers
│       ├── entrypoint.py       # migrations → uvicorn
│       ├── config.py           # pydantic-settings (.env)
│       ├── db.py               # async engine / session scopes
│       ├── models.py           # nodes, versions, chats, operations, keys, logs
│       ├── schemas.py          # API models
│       ├── security.py         # Fernet encryption + masking
│       ├── llm/
│       │   ├── base.py         # LLMProvider protocol + capabilities
│       │   ├── schemas.py      # ChatMessage / ToolCall / stream events
│       │   ├── catalog.py      # provider metadata, fallback order
│       │   ├── openai_provider.py
│       │   ├── anthropic_provider.py
│       │   ├── gemini_provider.py
│       │   ├── compatible_provider.py  # DeepSeek/OpenRouter/Ollama/Groq/…
│       │   ├── local_embeddings.py
│       │   └── factory.py      # credentials, selection, fallback
│       ├── services/
│       │   ├── storage.py      # blob store + decoding
│       │   ├── parsing.py      # xlsx/xls/csv, docx/doc, pdf, OCR, text
│       │   ├── chunking.py     # overlap-aware chunker with labels
│       │   ├── documents.py    # xlsx/docx/pdf generators
│       │   ├── nodes.py        # tree CRUD, versions, snapshots
│       │   ├── archive.py      # ZIP in/out (zip-slip safe)
│       │   ├── vectorstore.py  # Qdrant wrapper
│       │   ├── embeddings.py   # batching + retries
│       │   ├── indexer.py      # parse → chunk → embed → upsert
│       │   ├── rag.py          # retrieval + citation objects
│       │   ├── tools.py        # 14 AI tools + undo records
│       │   ├── agent.py        # streaming + tool loop + fallback
│       │   ├── operations.py   # rollback of AI actions
│       │   └── chat_service.py # sessions/messages/LLM logs
│       ├── api/
│       │   └── health.py, files.py, chat.py, providers.py
│       └── worker/
│           ├── celery_app.py
│           └── tasks.py
└── frontend/
    ├── Dockerfile              # standalone Next.js runtime
    ├── app/                    # layout, workspace page, settings page
    ├── components/             # tree, chat, viewer, viewers/, settings, splitter
    └── lib/                    # typed API client, chat WebSocket hook, i18n (ru/en)
```

---

## Connecting cloud providers

Keys can be set **either** in `.env` **or** on the settings page (`/settings`). Values saved in the
UI win and are stored encrypted; the API only returns `sk-t…6789` style masks. After changing a
credential the provider client cache is invalidated immediately.

### OpenAI (native SDK)

```env
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o
OPENAI_API_KEY=sk-...
# OPENAI_BASE_URL=            # optional: Azure/proxy/self-hosted gateway
EMBEDDING_PROVIDER=openai
EMBEDDING_MODEL=text-embedding-3-small
```

### Anthropic

```env
LLM_PROVIDER=anthropic
LLM_MODEL=claude-sonnet-4-20250514
ANTHROPIC_API_KEY=sk-ant-...
EMBEDDING_PROVIDER=local      # Anthropic has no embeddings API
```

Tool-use blocks are mapped both ways (`tool_use` → `ToolCall`, `tool_result` → tool message), and
images become `image` blocks.

### Google Gemini

```env
LLM_PROVIDER=gemini
LLM_MODEL=gemini-2.0-flash
GOOGLE_API_KEY=AIza...
EMBEDDING_PROVIDER=gemini
EMBEDDING_MODEL=text-embedding-004
```

Function declarations are sent with `automatic_function_calling` disabled so the agent keeps control
of the loop; tool JSON-Schemas are sanitised to the subset Gemini accepts.

### DeepSeek

```env
LLM_PROVIDER=deepseek
LLM_MODEL=deepseek-flash      # or deepseek-v4-pro / deepseek-chat (depends on your account)
DEEPSEEK_API_KEY=sk-...
EMBEDDING_PROVIDER=local
```

Base URL `https://api.deepseek.com` (OpenAI-compatible). The chat model selector loads the live
model list from `/v1/models`, so newly released DeepSeek models appear automatically.

### OpenRouter

```env
LLM_PROVIDER=openrouter
LLM_MODEL=anthropic/claude-3.5-sonnet
OPENROUTER_API_KEY=sk-or-...
EMBEDDING_PROVIDER=local
```

Base URL `https://openrouter.ai/api/v1`; requests carry `HTTP-Referer`/`X-Title` headers.

### Ollama (local fallback)

```env
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_MODEL=llama3.2
LLM_PROVIDER=ollama
EMBEDDING_PROVIDER=local
```

`docker compose --profile local-llm up -d` starts the container; without the profile the URL is
unreachable and Ollama is simply skipped in the fallback chain.

### Groq / Together / xAI

```env
GROQ_API_KEY=gsk_...          # provider groq,      https://api.groq.com/openai/v1
TOGETHER_API_KEY=...          # provider together,  https://api.together.xyz/v1
XAI_API_KEY=xai-...           # provider xai,       https://api.x.ai/v1
```

Set `LLM_PROVIDER` to the matching name or pick it in the chat model selector. Catalogues and base
URLs live in `backend/app/llm/catalog.py`.

### Which provider is used?

1. Provider/model chosen explicitly in the chat selector (sent with the request).
2. Defaults saved on the settings page (DB), else `LLM_PROVIDER` / `LLM_MODEL` from `.env`.
3. If that provider has no key, or fails with 401/429/5xx **before the first token**, the agent walks
   `openai → anthropic → gemini → deepseek → openrouter → groq → together → xai → ollama` and emits a
   `provider_fallback` event that the UI shows as a notice.
4. Nothing usable → `/api/health` reports `degraded`, `/api/providers` returns
   `chat_available=false`, and the chat shows a banner linking to the settings page.

Retries (`LLM_MAX_RETRIES`, default 2) only apply until the first token is streamed; afterwards an
error is surfaced instead of silently restarting the answer.

### Embeddings

`EMBEDDING_PROVIDER` is independent from the chat provider:

| value | backend | notes |
| --- | --- | --- |
| `openai` | `text-embedding-3-small` / `-large` | needs `OPENAI_API_KEY` |
| `gemini` | `text-embedding-004`, `gemini-embedding-001` | needs `GOOGLE_API_KEY` |
| `ollama` | `nomic-embed-text`, `mxbai-embed-large` | local profile + pulled model |
| `local` | sentence-transformers (CPU) | `paraphrase-multilingual-MiniLM-L12-v2`, 384 dims |

If the configured embedding provider is unavailable the backend falls back
`configured → openai → gemini → local`. When nothing is available RAG is disabled (health warning,
`/api/rag/search` returns an error) while chat and tools keep working. Changing embedding
dimensions recreates the Qdrant collection automatically — re-index documents afterwards
(right-click a project → **Reindex**).


---

## Walkthroughs

### 1. Upload a project

1. **+ Project** in the left panel → name it `Finance 2026`.
2. Drag a folder from your desktop onto the project row (folder drag & drop is supported through
   `webkitGetAsEntry`, so nested directories are recreated), or use the context menu →
   **Upload files / Upload folder**.
3. Right-click the project → **Upload ZIP & extract** to import an archive; entries are sanitised,
   `../` paths skipped, entry count and expanded size limited.
4. Watch the coloured dot next to each file: `pending → indexing → indexed` (`⚠` = unsupported,
   red = failed with the reason in the tooltip). Parsing happens in the Celery worker; if the worker
   is not running the API indexes in-process after a 25 s watchdog.

### 2. Ask a question about a PDF (RAG + citations)

Attach `report.pdf` (📎 Attach or context menu → *Attach to chat*) and ask:

> What was the revenue growth and which risks are mentioned?

The backend retrieves the top chunks from Qdrant, injects them into the system prompt and the answer
contains `[1]`, `[2]` markers rendered as clickable chips under the message. Clicking a chip opens
the source file in the viewer. If embeddings are unavailable the answer is produced without
retrieval and the UI shows a `RAG off` chip.

### 3. Edit an Excel file through chat

> Add a sheet "Q4" to budget.xlsx and fill it with October/November/December = 1000/1200/900.

Tool flow (visible as cards in the chat):

1. `list_workspace` → finds `budget.xlsx` and its node id.
2. `excel_read` → inspects existing sheets/columns.
3. `excel_add_sheet` → new sheet + rows, saved as a **new version** of the same node.

Open the file in the viewer to see the new tab. The tool card has **↩ Undo this change**, which
restores the previous version (or deletes the file when it was created).

### 4. Translate a Word document

Attach or reference `report.docx`:

> Translate report.docx into English and save it next to the original.

`translate_file` extracts the text, calls the active provider and writes `report.english.docx`
(DOCX/PDF/CSV/Markdown depending on the source extension). The source file is never overwritten.

### 5. Download / versions / rollback

* File → **⬇ Download**, folder or project → **⬇ Download ZIP** (streamed; temp file cleaned up
  afterwards).
* **🕘 Versions** lists the last 20 snapshots per file, each restorable.
* **Undo** on a tool card reverts the recorded operation (create → delete, modify → restore bytes,
  delete → restore subtree, rename/move → inverse).

---

## API overview

REST under `/api` (OpenAPI at `/api/docs`), JSON everywhere, NDJSON/WebSocket for streaming.

| method | path | purpose |
| --- | --- | --- |
| GET | `/api/health` | db/qdrant/redis checks, provider readiness, UI banner |
| GET | `/api/tree?project_id=` | full tree or a single project |
| POST | `/api/projects`, `/api/folders` | create project / folder |
| POST | `/api/upload` | multipart `files[]` + `paths[]` (folder structure) |
| POST | `/api/nodes/{id}/extract` | upload ZIP and extract into that folder |
| PATCH | `/api/nodes/{id}` | rename and/or move (`name`, `parent_id`) |
| DELETE | `/api/nodes/{id}` | delete node + descendants |
| GET | `/api/nodes/{id}/download` | file stream or ZIP for folders |
| GET | `/api/nodes/{id}/content` | inline bytes (viewers) with ETag |
| GET | `/api/nodes/{id}/text` | extracted text/segments/meta (viewers + OCR) |
| GET | `/api/nodes/{id}/versions` · POST `…/versions/{version_id}/revert` | history |
| POST | `/api/nodes/{id}/reindex` | queue indexing for a file/folder/project |
| GET | `/api/index/status` | counts by status, chunks, vectors |
| GET/POST | `/api/sessions` | list / create chat |
| GET | `/api/sessions/{id}/messages` · DELETE `/api/sessions/{id}` | history / delete |
| POST | `/api/sessions/{id}/message` | NDJSON stream (fallback when WS is blocked) |
| POST | `/api/operations/{id}/revert` | undo an AI action |
| POST | `/api/rag/search` | retrieval only (citations, no LLM) |
| GET | `/api/providers` | catalogue + configured flags + defaults |
| POST | `/api/providers/{name}/test` | real connection check (✅/❌ + latency) |
| GET | `/api/providers/{name}/models` | dynamic model list (Ollama/OpenAI) |
| POST | `/api/embeddings/test` | embed a probe string, returns dimension |
| GET | `/api/settings` · PUT `/api/settings/credentials` · PUT `/api/settings/preferences` | encrypted keys + defaults |

### WebSocket protocol

`ws://<host>/api/ws/chat/{session_id}` (proxied by nginx, 1 h read timeout).

Client → server:

```json
{"type":"message","content":"Add a Q4 sheet to budget.xlsx","provider":"openai","model":"gpt-4o","attachments":["<node-id>"],"use_rag":true}
{"type":"ping"}
```

Server → client events: `user_saved`, `start`, `citations`, `delta`, `tool_call`, `tool_result`,
`assistant_progress`, `usage`, `provider_fallback`, `node_changed`, `assistant_done`, `error`,
`stream_end`. `tool_result` carries `operation_id` so the UI can offer **Undo**.

Each assistant step is persisted, so reloading a chat restores tool cards and citations.

---

## Security notes

* **Key storage** — provider keys saved from the UI are encrypted with Fernet
  (`SHA-256(APP_SECRET_KEY)` → urlsafe base64) before being written to PostgreSQL; the API returns
  masks only. `APP_SECRET_KEY` is validated at startup and flagged in the UI when insecure.
* **Path handling** — user names are sanitised (`<>:"/\|?*`, control chars, `..`); blobs are stored
  under random 32-hex keys, so no user input ever reaches a filesystem path. ZIP extraction rejects
  absolute paths and `..` segments.
* **Uploads** — extension allow-list, per-file size limit (`MAX_UPLOAD_MB`), archive entry/size
  limits, and magic-byte checks for images/PDF/ZIP/OOXML to catch MIME spoofing.
* **Auth** — single-user stub: open by default, optional `APP_USER_TOKEN` bearer token for REST and
  `?token=` for the WebSocket. Put the stack behind your own reverse proxy / SSO for multi-user use.
* **SQL** — all queries go through SQLAlchemy Core/ORM with bound parameters.
* **Logging** — every provider call is recorded in `llm_logs` (provider, model, tokens, duration,
  status, error) without storing prompt contents.

---

## Development

```bash
# backend (Python 3.11/3.12)
python -m venv .venv && source .venv/bin/activate
pip install -r backend/requirements.txt
export DATABASE_URL=postgresql+asyncpg://workspace:workspace@localhost:5432/workspace
export QDRANT_URL=http://localhost:6333    # or ":memory:" for embedded Qdrant (no container)
export REDIS_URL=redis://localhost:6379/0  # optional: without Redis, indexing runs in-process
export WORKSPACE_DIR=./data/workspace APP_SECRET_KEY=dev-secret-dev-secret
cd backend && alembic upgrade head && python -m app.entrypoint

# worker (second terminal)
cd backend && celery -A app.worker.celery_app:celery_app worker --loglevel=INFO

# frontend (third terminal) - talks to the backend on :8001
cd frontend
NEXT_PUBLIC_API_BASE=http://localhost:8001 npm run dev
```

`CORS_ORIGINS` must contain the frontend origin (default `http://localhost:3000`). The WebSocket URL
is derived from `NEXT_PUBLIC_API_BASE` automatically.

Checks used while building this project:

```bash
python -m compileall backend/app backend/alembic
python -c "import sys; sys.path.insert(0,'backend'); import app.main"   # schema/route smoke test
cd frontend && npx tsc --noEmit && npm run build
docker compose config > /dev/null    # compose schema check
cd backend && alembic upgrade head --sql > /tmp/schema.sql   # inspect SQL without a DB
```

An end-to-end harness (portable PostgreSQL + a mock OpenAI-compatible server) was used to verify:
file upload → parsing → indexing → Qdrant retrieval with citations, WS/NDJSON agent streaming,
AI-created XLSX with a second sheet, ZIP download/extract and operation undo.

---

## Troubleshooting

| symptom | cause / fix |
| --- | --- |
| UI banner “No LLM provider is configured” | no key in `.env`/settings and Ollama not reachable. Add a key on `/settings` or start the `local-llm` profile. |
| `/api/health` = `degraded` | inspect the `checks` object: database/qdrant/redis, missing keys, insecure secret, RAG disabled. |
| `429` in chat | the message shows a rate-limit card with **Switch provider**; the fallback chain also auto-switches before the first token. |
| Files stay `queued` | worker not running (`docker compose logs worker`) — the API watchdog indexes in-process after 25 s. |
| `index failed` on PDF | scanned PDF without a text layer; OCR currently applies to images, not PDF pages (roadmap). |
| Legacy `.doc` shows text only | the viewer falls back to extracted text (`antiword`/`catdoc`); download the original for exact layout. |
| `RAG off` chip | no embedding provider configured or Qdrant unhealthy. Configure OpenAI/Gemini or `EMBEDDING_PROVIDER=local`. |
| Keys “lost” after restart | `APP_SECRET_KEY` changed → stored ciphertext cannot be decrypted; re-enter keys. |
| Chat stops mid-answer | provider error after the first token is surfaced (retries intentionally apply before the first token only). |

---

## Limitations & post-MVP roadmap

* Scanned PDFs: page-image OCR (render → Tesseract) instead of text-layer-only extraction.
* Tool-call *deltas* are collapsed into one card per call (no partial argument view).
* Excel editing supports `.xlsx`/`.xlsm`; `.xls` is read-only (write a new `.xlsx`).
* Chat attachments are capped (8 nodes, 12k chars each) — rely on RAG for large corpora.
* Single-user stub only: no login/sessions, no per-user ACLs, no audit export.
* No global “re-index everything” button in the UI (per project/folder reindex exists); add a Celery
  `reindex_all` route + progress bar.
* Embeddings are recomputed on every save; add content hashing to skip unchanged files.
* Streaming markdown jumps slightly while a code fence is incomplete — add a buffer heuristic.
* Images are previewed and OCR-indexed, but there is no vision chat over them yet.
* Multi-user workspaces, share links, S3 blob storage, per-workspace Qdrant namespaces.


