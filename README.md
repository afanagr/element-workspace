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
* **Multi-provider AI (DeepSeek + Qwen)** — the chat layer speaks the OpenAI dialect to the DeepSeek
  cloud API, Alibaba Cloud DashScope (Qwen) and any local Qwen server (Ollama/vLLM/LM Studio), with
  tool calling, streaming and automatic failover. Embeddings run locally (sentence-transformers), so
  no extra embedding vendor is involved.
* **Accounts, roles and isolation** — an administrator creates users, assigns each one a DeepSeek
  and/or Qwen key and can see every project. Users only ever see their own projects; a chat is bound
  to a single project, so retrieval and the AI tools can never reach outside it.
* **Viewers** — Excel (SheetJS, per-sheet tabs), Word (mammoth), PDF (PDF.js), images + OCR,
  Markdown/code/JSON with syntax highlighting.
* **UI** — three panes: file tree · chat · viewer (the chat/viewer splitter is draggable and the
  width is remembered), dark/light theme and a **RU/EN interface** (Russian by default, switch in
  the top bar or on the settings page).

Everything runs with `docker compose up`. On the first start a bootstrap administrator is created
from `ADMIN_USERNAME` / `ADMIN_PASSWORD`; sign in, open **Admin**, create the accounts and paste
their DeepSeek/Qwen keys.

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
        optional profile local-llm:  (removed - DeepSeek/Qwen providers, local embeddings)
```

Volumes: `postgres_data`, `qdrant_data`, `redis_data`, `workspace_data` (blobs + the local
sentence-transformers model cache).

> **Naming:** the product is **Element Workspace**. The Compose project is intentionally still
> `ai-file-workspace` (see `name:` in `docker-compose.yml`), so existing volumes keep their names
> and no data is lost when pulling a new version. Renaming it would create new, empty volumes.

---

## Quick start

```bash
git clone <this repo> element-workspace
cd element-workspace

cp .env.example .env
# generate a real encryption secret (protects the provider keys in the DB):
python -c "import secrets;print(secrets.token_urlsafe(48))"     # → APP_SECRET_KEY
# set the bootstrap administrator:
#   ADMIN_USERNAME=admin
#   ADMIN_PASSWORD=<a strong password>

docker compose up -d --build      # or: make up
```

Open **http://localhost:3000** → sign in as the bootstrap administrator → **Admin** → create a user,
paste their **DeepSeek and/or Qwen API key** → the user signs in, presses **+ Project** and drops
files in. For a fully local setup set `QWEN_LOCAL_BASE_URL` in `.env` instead of issuing cloud keys.

* The bootstrap account is created **only once**, on a database without users. Later changes are made
  in the UI (`/admin`), so `ADMIN_PASSWORD` can be removed from `.env` afterwards.
* A chat can only be started inside a project: select a project in the tree and the assistant receives
  that project's tree and its RAG context - nothing else.
* Health/readiness (and the UI banner) come from `GET /api/health`.
* `docker compose ps` should show `postgres`, `qdrant` and `redis` as *healthy*; `backend` health
  turns green after migrations complete.
* Direct backend for debugging: `http://localhost:8001/api/docs` (OpenAPI).

### Roles

| role | can do |
| --- | --- |
| `admin` | everything: create/disable/delete users, assign DeepSeek/Qwen keys and models, browse every project of every user (`/admin` → *Open*), use the workspace |
| `user` | own projects and own chats only; sees their key status (masked) but cannot change it |

Sessions are bearer tokens (only the SHA-256 digest is stored in `auth_tokens`, TTL
`SESSION_TTL_HOURS`, default 14 days); the browser keeps the token in `localStorage` and sends
`Authorization: Bearer <token>`. File previews use `?token=` because `<img>`/`<iframe>` cannot send
headers.

### Embeddings

DeepSeek and Qwen expose no embeddings endpoint in this setup, so vectors are always produced
**locally** with `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (CPU, downloaded on
first use into the `workspace_data` volume). The first answer that needs document search therefore
warms up the model.

### Make targets

| command | effect |
| --- | --- |
| `make up` | build + start everything, opens `http://localhost:3000` |
| `make logs` | tail logs from all containers |
| `make down` | stop the stack, keep volumes |
| `make reset` | stop and delete **all** volumes (workspace, DB, vectors) |
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
├── docker-compose.yml          # 7 services (no optional profiles any more)
├── .env.example                # every setting documented
├── Makefile                    # up / logs / reset / shells
├── nginx/                      # single entry point (REST + WS + static)
├── backend/
│   ├── Dockerfile              # multi-stage: builder → api / worker targets
│   ├── requirements.txt        # pinned runtime deps
│   ├── requirements-local.txt  # sentence-transformers (optional, build arg)
│   ├── alembic.ini
│   ├── alembic/                # async env + schema revisions (0001, 0002)
│   └── app/
│       ├── main.py             # FastAPI app, CORS, error handlers, admin bootstrap
│       ├── entrypoint.py       # migrations → uvicorn
│       ├── config.py           # pydantic-settings (.env)
│       ├── db.py               # async engine / session scopes
│       ├── deps.py             # bearer-token auth + admin guard
│       ├── models.py           # users, tokens, nodes, versions, chats, logs
│       ├── schemas.py          # API models
│       ├── security.py         # password hashing, tokens, Fernet, masking
│       ├── api/                # auth, admin, files, chat, providers, health
│       ├── llm/
│       │   ├── base.py         # LLMProvider protocol + capabilities
│       │   ├── schemas.py      # ChatMessage / ToolCall / stream events
│       │   ├── catalog.py      # provider metadata, model lists (DeepSeek/Qwen)
│       │   ├── openai_provider.py      # OpenAI SDK client (base for all dialects)
│       │   ├── compatible_provider.py  # DeepSeek / Qwen / local OpenAI-compatible wrapper
│       │   ├── local_embeddings.py
│       │   └── factory.py      # per-user keys, provider choice, local embeddings
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
│       │   ├── tools.py        # 13 AI tools + undo records (project-scoped)
│       │   ├── agent.py        # streaming + tool loop, one project of context
│       │   ├── operations.py   # rollback of AI actions
│       │   ├── users.py        # accounts, tokens, per-user provider keys
│       │   └── chat_service.py # sessions/messages/LLM logs
│       ├── api/
│       │   └── auth.py, admin.py, health.py, files.py, chat.py, providers.py
│       └── worker/
│           ├── celery_app.py
│           └── tasks.py
└── frontend/
    ├── Dockerfile              # standalone Next.js runtime
    ├── app/                    # layout, workspace page, login, admin, settings
    ├── components/             # tree, chat, viewer, viewers/, settings, splitter
    └── lib/                    # typed API client, chat WebSocket hook, i18n (ru/en)
```

---

## Chat providers, accounts and project isolation

Three chat backends ship with the workspace. All of them speak the OpenAI
chat-completions dialect (streaming, tool calling, `stream_options.include_usage`),
so the agent loop is provider-agnostic:

| provider | type | endpoint | default model |
| --- | --- | --- | --- |
| `deepseek` | cloud | `https://api.deepseek.com` | `deepseek-chat` |
| `qwen` | cloud | `https://dashscope.aliyuncs.com/compatible-mode/v1` (DashScope) | `qwen-plus` |
| `qwen-local` | local | `QWEN_LOCAL_BASE_URL` (Ollama / vLLM / LM Studio) | `qwen2.5:7b-instruct` |

`registry.candidates()` returns every provider that has credentials, ordered by the
explicitly requested provider → saved preference → static fallback order
(`deepseek`, `qwen`, `qwen-local`). The agent switches providers automatically if a
call fails before the first token (429/5xx/connection errors), and the user can pick
any provider/model in the chat composer.

### Where API keys come from

1. the key an administrator assigned to the signed-in user (`users.deepseek_api_key_enc`
   or `users.qwen_api_key_enc`, encrypted with Fernet derived from `APP_SECRET_KEY`), or
2. the deployment-wide `DEEPSEEK_API_KEY` / `QWEN_API_KEY` (alias `DASHSCOPE_API_KEY`)
   from `.env`, used as a fallback for accounts without a personal key.

The local provider needs no key: it is enabled as soon as `QWEN_LOCAL_BASE_URL` is
non-empty. Models are served by any OpenAI-compatible runtime, e.g.:

```bash
# Ollama on the host, backend in Docker (host.docker.internal is mapped by compose)
ollama pull qwen2.5:7b-instruct
QWEN_LOCAL_BASE_URL=http://host.docker.internal:11434/v1

# vLLM
python -m vllm.entrypoints.openai.api_server --model Qwen/Qwen2.5-7B-Instruct
QWEN_LOCAL_BASE_URL=http://localhost:8000/v1
```

Without any configured provider, `/api/providers` reports `chat_available: false`,
the UI shows a banner and chat requests fail with HTTP 503 `no_provider` — the rest
of the workspace keeps working.

### Models

| provider | model | use |
| --- | --- | --- |
| `deepseek` | `deepseek-chat` (default), `deepseek-reasoner` | general work / longer reasoning |
| `qwen` | `qwen-plus` (default), `qwen-max`, `qwen-turbo`, `qwen3-max`, … | cloud Qwen |
| `qwen-local` | `qwen2.5:7b-instruct` (default), `qwen3:8b`, … | local Qwen |

The administrator picks the model per user in `/admin` (DeepSeek and Qwen have their
own fields); `LLM_MODEL` / `QWEN_MODEL` / `QWEN_LOCAL_MODEL` in `.env` are the global
defaults. `GET /api/providers/{provider}/models` merges the curated catalogue with
the live model list of the endpoint, so new Qwen releases appear automatically.


### Chat context = one project

* `POST /api/sessions` requires `project_id`; there is no workspace-wide chat any more.
* `run_agent()` resolves the session's project and passes `project_id` into retrieval
  (`rag.retrieve` refuses to run without it) and into every tool call.
* Tools can only address nodes whose `project_id` matches the chat's project (`_resolve_node`), and
  attachment ids from another project are ignored with a warning.
* `list_workspace` always lists the current project, `search_documents` always filters by it.
* The file tree, uploads, renames, moves, deletes, previews and downloads are all checked against the
  node's `owner_id`; a foreign node answers **404** (not 403) so its existence is not leaked.
* Administrators bypass the owner check and can browse any account (`/api/tree?owner_id=…`,
  `/admin` → *Open*).

### Key endpoints

```
POST /api/auth/login              {"username","password"} → {"token","expires_at","user"}
POST /api/auth/logout             revokes the current token
GET  /api/auth/me                 current account
POST /api/auth/password           {"current_password","new_password"}

GET    /api/admin/users           accounts + per-user counters
POST   /api/admin/users           create (username, password, role, deepseek/qwen key + model)
PATCH  /api/admin/users/{id}      rename, reset password, change role/state, set or clear keys
DELETE /api/admin/users/{id}      delete the account with its projects, chats and vectors
GET    /api/admin/projects        every project + owner + file/chunk counts
GET    /api/admin/overview        counters for the dashboard
```

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
| POST | `/api/auth/login` · `/api/auth/logout` | sign in (returns a bearer token) / revoke it |
| GET | `/api/auth/me` · POST `/api/auth/password` | current account / change own password |
| GET | `/api/admin/users` · POST `/api/admin/users` | list accounts with counters / create one |
| PATCH | `/api/admin/users/{id}` · DELETE `/api/admin/users/{id}` | edit (role, state, password, DeepSeek/Qwen key) / delete with all data |
| GET | `/api/admin/projects` · `/api/admin/overview` | all projects with owners / dashboard counters |
| GET | `/api/health` | db/qdrant/redis checks, system readiness, UI banner |
| GET | `/api/tree?project_id=&owner_id=` | own tree (`owner_id` is admin-only) |
| POST | `/api/projects`, `/api/folders` | create project / folder |
| POST | `/api/upload` | multipart `files[]` + `paths[]` (folder structure) |
| POST | `/api/nodes/{id}/extract` | upload ZIP and extract into that folder |
| PATCH | `/api/nodes/{id}` | rename and/or move (`name`, `parent_id`) |
| DELETE | `/api/nodes/{id}` | delete node + descendants |
| GET | `/api/nodes/{id}/download` | file stream or ZIP for folders (`?token=` allowed) |
| GET | `/api/nodes/{id}/content` | inline bytes (viewers) with ETag (`?token=` allowed) |
| GET | `/api/nodes/{id}/text` | extracted text/segments/meta (viewers + OCR) |
| GET | `/api/nodes/{id}/versions` · POST `…/versions/{version_id}/revert` | history |
| POST | `/api/nodes/{id}/reindex` | queue indexing for a file/folder/project |
| GET | `/api/index/status` | counts by status, chunks, vectors (own files) |
| GET/POST | `/api/sessions?project_id=` | list / create chats (creation requires `project_id`) |
| GET | `/api/sessions/{id}/messages` · DELETE `/api/sessions/{id}` | history / delete |
| POST | `/api/sessions/{id}/message` | NDJSON stream (fallback when WS is blocked) |
| POST | `/api/operations/{id}/revert` | undo an AI action |
| POST | `/api/rag/search` | retrieval only, `project_id` mandatory |
| GET | `/api/providers` · POST `/api/providers/test` | provider status for the current account / preferred-provider check |
| GET | `/api/providers/{provider}/models` · POST `/api/providers/{provider}/test` | live model list / connection check per provider (`deepseek`, `qwen`, `qwen-local`) |
| POST | `/api/embeddings/test` | embed a probe string locally, returns dimension |
| GET | `/api/settings` · PUT `/api/settings/preferences` | account + provider status / default provider+model (admin) |

Every route except `/api/health`, `/api/auth/login` and the OpenAPI docs requires
`Authorization: Bearer <token>`.

### WebSocket protocol

`ws://<host>/api/ws/chat/{session_id}?token=<bearer-token>` (proxied by nginx, 1 h read timeout).

Client → server:

```json
{"type":"message","content":"Add a Q4 sheet to budget.xlsx","model":"deepseek-chat","attachments":["<node-id>"],"use_rag":true}
{"type":"ping"}
```

Server → client events: `user_saved`, `start`, `citations`, `delta`, `tool_call`, `tool_result`,
`assistant_progress`, `usage`, `provider_fallback`, `node_changed`, `assistant_done`, `error`,
`stream_end`. `tool_result` carries `operation_id` so the UI can offer **Undo**.

Each assistant step is persisted, so reloading a chat restores tool cards and citations.

---

## Security notes

* **Key storage** — every account can hold its own DeepSeek and Qwen keys, encrypted with Fernet
  (`SHA-256(APP_SECRET_KEY)` → urlsafe base64) before being written to PostgreSQL; the API returns
  `sk-a…9f2c` style masks only, and the key never appears in logs. `APP_SECRET_KEY` is validated at
  startup and flagged in the UI when insecure (changing it invalidates stored keys).
* **Path handling** — user names are sanitised (`<>:"/\|?*`, control chars, `..`); blobs are stored
  under random 32-hex keys, so no user input ever reaches a filesystem path. ZIP extraction rejects
  absolute paths and `..` segments.
* **Uploads** — extension allow-list, per-file size limit (`MAX_UPLOAD_MB`), archive entry/size
  limits, and magic-byte checks for images/PDF/ZIP/OOXML to catch MIME spoofing.
* **Auth** — mandatory for every route except `/api/health` and `/api/auth/login`. Passwords are
  PBKDF2-HMAC-SHA256 (260k iterations, per-password salt); bearer tokens are 36 random bytes and only
  their SHA-256 digest is stored. Tokens expire after `SESSION_TTL_HOURS`, and changing a password
  revokes every other session.
* **Isolation** — a node's `owner_id` and a chat's `owner_id`/`project_id` drive every query; foreign
  ids answer **404** so existence is not leaked, and the agent's tool layer refuses nodes outside the
  chat's project.
* **SQL** — all queries go through SQLAlchemy Core/ORM with bound parameters.
* **Logging** — every LLM call is recorded in `llm_logs` (provider, model, tokens, duration, status,
  error) without storing prompt contents.

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
| UI banner “No chat provider is configured” | no DeepSeek/Qwen key is assigned and `QWEN_LOCAL_BASE_URL` is empty. Add a key in `/admin` or point the backend at Ollama/vLLM. |
| `/api/health` = `degraded` | inspect the `checks` object: database/qdrant/redis, missing keys, insecure secret, RAG disabled. |
| `429` in chat | the message shows a rate-limit card with **Switch provider**; the fallback chain also auto-switches before the first token. |
| Files stay `queued` | worker not running (`docker compose logs worker`) — the API watchdog indexes in-process after 25 s. |
| `index failed` on PDF | scanned PDF without a text layer; OCR currently applies to images, not PDF pages (roadmap). |
| Legacy `.doc` shows text only | the viewer falls back to extracted text (`antiword`/`catdoc`); download the original for exact layout. |
| `RAG off` chip | no embedding provider configured or Qdrant unhealthy. Set `EMBEDDING_PROVIDER=local` and install sentence-transformers. |
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


