# EAGLE OS Agent

EAGLE is the governance layer around this repository's Mem0 fork. SQLite is authoritative; Mem0 with Kylin providers is a rebuildable semantic index.

## Runtime assembly

The deployment must provide two normalized Kylin SDK adapters:

- `eagle.adapters.kylin.embedding_client.KylinEmbeddingClient`
- `eagle.adapters.kylin.vector_client.KylinVectorClient`

The real SDK package and response types are intentionally not guessed. After constructing those clients, create the strict Mem0 gateway with:

```python
from eagle.bootstrap import create_mem0_gateway

gateway = create_mem0_gateway(
    embedding_client=embedding_client,
    vector_client=vector_client,
    embedding_dims=1024,
    distance_metric="<confirmed-kylin-metric>",
    score_semantics="cosine_distance",
    history_db_path="./mem0-history.db",
)
```

`distance_metric` is the confirmed metric string passed to the Kylin SDK. `score_semantics` separately tells Mem0 how to normalize returned scores. Gateway construction also fails unless the providers are `kylin`, `kylin`, and `noop`, and the live vector capability probe passes.

## Governance assembly

```python
from eagle.db import create_schema, create_sqlite_engine, make_session_factory
from eagle.forgetting import ForgettingService
from eagle.governance import GovernanceService
from eagle.outbox import IndexWorker, ReconciliationService
from eagle.pack import PackService

engine = create_sqlite_engine("./eagle.db")
create_schema(engine)
sessions = make_session_factory(engine)

governance = GovernanceService(sessions)
pack = PackService(sessions, gateway)
forgetting = ForgettingService(sessions)
worker = IndexWorker(sessions, gateway)

ReconciliationService(sessions, gateway).reconcile()
```

Call reconciliation before starting the single Outbox worker; it also requeues `RUNNING` jobs whose lease (`lease_seconds`, default 300) has expired. Production schema migration is not included yet; `create_schema` is the prototype initialization path.

Every execution must carry a caller-generated `execution_id`. Replaying the same ID and payload returns the original governance result without adding evidence; reusing it with a different payload fails.

The HTTP application requires an authentication dependency which returns the authenticated user ID. Request payloads cannot select their own tenant:

```python
app = create_app(
    session_factory=sessions,
    governance=governance,
    pack=pack,
    forgetting=forgetting,
    gateway=gateway,
    authenticate=authenticate_request,
)
```

Environment drift creates a `knowledge_revalidations` request while preserving the old Knowledge version for its original environment. A new environment version is committed only after normal execution-evidence thresholds pass. Forgetting immediately removes visibility; after vector deletion, authoritative content and orphaned evidence are scrubbed while a non-content tombstone keeps execution idempotency intact.

Only HARD keys with executable adapters are accepted today: `preferred_tool`, `denied_tools`, and `require_offline`. `allowed_formats` and free-form `privacy_rule` fail compilation until an action-parameter enforcement adapter exists.

## Tests

```bash
pytest -q
```

The optional HTTP API is created with `eagle.api.main.create_app` after installing the `api` extra. The wheel ships a single top-level package (`eagle`); adapters and the API live under `eagle.adapters` / `eagle.api` to avoid import-name collisions with other installed packages.
