# NYXORA
<<<<<<< HEAD

**One Intelligence. Infinite Capabilities.**

A local-first personal AI companion: an orchestration brain that runs on your
own machine, on models you already have, with every consequential action gated
by a permission you actually granted.

This repository is the backend core (Phases 1–8 of the blueprint). The web and
desktop clients have not been built yet — see [Status](#status-what-is-actually-true).

---

## What this is

Most AI products are a chat box in front of someone else's model. NYXORA's
value is the layer around it, and that layer is the part you own:

| Piece | What it does |
|---|---|
| Model Router | Picks a model per task from what is *actually installed*, and tells you why |
| Orchestrator | Understand → Plan → Route → Authorize → Execute → Observe → Verify → Report |
| Permission Controller | Single-use, parameter-bound, expiring approvals. Nothing writes without one |
| Memory Engine | Four layers, pending until you confirm, refuses to store secrets |
| Knowledge (RAG) | Local PDF/text/md ingest, FTS5 BM25 retrieval, cited answers |
| Safety Engine | Retrieved text is *data*, never instructions. Secret-leak scan on output |
| Audit + Undo | Append-only redacted log; every mutation is reversible |

The security properties are the product. `test_safety_injection.py` proves that
a document saying "delete everything" results in **nothing being deleted** — not
because a filter caught the phrase, but because a deletion cannot exist without
an approval you granted for that exact path.

## Quick start

Requires Python 3.11+. Nothing else. Ollama is strongly recommended but optional —
the app degrades to a clear setup message instead of a stack trace.

```bash
make venv install           # or: python -m venv apps/api/.venv && pip install -r apps/api/requirements.txt
make dev                    # API on http://127.0.0.1:8000
curl -s http://127.0.0.1:8000/health
```

The API binds to `127.0.0.1` and requires the `X-NYXORA-Token` header on every
route except `/health`. The token is generated per install and stored in the
data directory, never in Git. Localhost binding alone is not authentication —
any local process could otherwise drive your agent.

```bash
TOKEN=$(cat apps/api/data/api.token)
curl -s -H "X-NYXORA-Token: $TOKEN" http://127.0.0.1:8000/models
curl -s -N -X POST -H "X-NYXORA-Token: $TOKEN" -H 'Content-Type: application/json' \
  -d '{"message":"hello"}' http://127.0.0.1:8000/chat
```

To use real models: install [Ollama](https://ollama.com), then `ollama pull
qwen2.5:3b`. `config/models.yaml` is where you declare capability tags, RAM
hints and licenses for the models you choose.

## Checks

```bash
make check        # ruff + mypy --strict + pytest, the same gate CI runs
```

## Layout

```
apps/api/
  app/
    api/routes.py        HTTP surface, SSE streaming, consistent error shape
    core/                engines, orchestrator, tools, rag, memory, safety, agents, voice
    models/              registry, router, ollama, openai-compatible, mock
    security/            path guard, permissions, audit, undo
    prompts/system_v1.md versioned system prompt
    db/models.py         SQLAlchemy + SQLite FTS5
  config/models.yaml     user-editable model catalogue + routing weights
  tests/                 90 tests, no network, no Ollama required
```

## Status: what is actually true

Per the blueprint's honesty rule, claims are separated by evidence.

**Verified by a test that ran in this repo**
- Path confinement: `..`, absolute drive paths, UNC paths, symlink/junction
  escape, and Windows reserved names (`con.txt` is rejected, not just `con`)
- Approvals: single-use, bound to a parameter hash, expiring, run-scoped
  ("allow for this run" is scoped to that run id, never "forever"), denied
  actions re-ask nothing, emergency stop blocks the whole pipeline
- Undo round-trip for create / edit / delete / scaffold
- Prompt injection: a document instructing deletion performs no deletion
- Secret redaction in the audit log and refusal of sensitive memory writes
- Router determinism, `local_only` privacy excluding external providers, and
  the actionable "no model installed" path
- Orchestrator state machine, approval pause/resume, cancellation, honest
  "verification failed" reporting

**Implemented but not yet verified against a real model**
- Ollama streaming, retries and cancellation — the code and its offline tests
  exist; no Ollama server was reachable on the machine that built this, so real
  generation is unconfirmed. Run `make dev` with Ollama up to confirm.
- OpenAI-compatible adapter (gated behind a key *and* a per-request
  `external_consent` flag, so it is off by default twice over)

**Not implemented** — see the deferred list rather than assuming:
- Web UI (Phase 4) and Tauri desktop shell (Phase 10)
- Alembic migration history; the schema is created by `init_db` today
- Voice capture backends (interfaces + deterministic mocks only, no mic access)
- Agents beyond Manager + Coding (7 of the 9-agent roster are interfaces)
- Embedding/hybrid retrieval ranking (RRF stub), evals harness, all `docs/*.md`
- Anything the blueprint forbids in V1: model training, always-on wake word,
  unrestricted shell or browser control, autonomous multi-agent execution

## Non-negotiables

- Local-first. Zero budget. External APIs are opt-in, never assumed.
- No tool action runs without the Permission Controller.
- Model output and document text are untrusted input.
- No secrets in Git. `.env` is ignored; `.env.example` is the contract.
- Licenses for every dependency and model are recorded, never claimed as ours.
=======
NYXTHARVION is a next-generation Universal AI platform that unifies intelligent agents, local AI models, automation, memory, and secure tools into one powerful workspace—redefining
>>>>>>> cf2e6f89eebd675937200fa813e753e42e3c6800
