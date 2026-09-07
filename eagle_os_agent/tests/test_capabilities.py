from types import SimpleNamespace

from eagle.adapters.kylin.capabilities import probe_vector_capabilities

DIMENSION = 2


class ProbeClient:
    """Fake Kylin client that understands the full normalized filter dialect.

    ``failing_filters`` names filters that should raise, to simulate a store
    without that capability.
    """

    def __init__(self, failing_filters=()):
        self.failing_filters = set(failing_filters)
        self.collections = {}

    def ensure_collection(self, *, name, dimension, metric):
        self.collections.setdefault(name, {})

    def upsert(self, *, collection, vectors, payloads, ids):
        for vector, payload, vector_id in zip(vectors, payloads, ids):
            self.collections[collection][vector_id] = SimpleNamespace(
                id=vector_id,
                payload=payload | {"__id__": vector_id},
            )

    def get(self, *, collection, vector_id, include_vector):
        return self.collections[collection].get(vector_id)

    def search(self, *, collection, vector, limit, filters):
        if self._unsupported(filters):
            raise RuntimeError(f"unsupported filter: {filters}")
        rows = [row for row in self.collections[collection].values() if _matches(row.payload, filters)]
        return rows[:limit]

    def _unsupported(self, filters):
        for key, expected in filters.items():
            if key == "$not":
                if "not" in self.failing_filters:
                    return True
                continue
            if isinstance(expected, dict):
                op = next(iter(expected))
                name = {"in": "id_allowlist" if key == "id" else "in", "nin": "id_blocklist"}.get(op, op)
                if name in self.failing_filters:
                    return True
        return False

    def list(self, *, collection, filters, limit):
        rows = [row for row in self.collections[collection].values() if _matches(row.payload, filters)]
        return rows[:limit]

    def delete(self, *, collection, vector_id):
        self.collections[collection].pop(vector_id, None)

    def delete_collection(self, *, name):
        self.collections.pop(name, None)


def _matches(payload, filters):
    row_id = payload.get("__id__")
    for key, expected in filters.items():
        if key == "$not":
            if any(_matches(payload, condition) for condition in expected):
                return False
        elif key == "id":
            if isinstance(expected, dict) and "in" in expected:
                if row_id not in expected["in"]:
                    return False
            elif isinstance(expected, dict) and "nin" in expected:
                if row_id in expected["nin"]:
                    return False
            elif row_id != expected:
                return False
        elif isinstance(expected, dict) and "in" in expected:
            if payload.get(key) not in expected["in"]:
                return False
        elif isinstance(expected, dict) and "nin" in expected:
            if payload.get(key) in expected["nin"]:
                return False
        elif isinstance(expected, dict) and "ne" in expected:
            if payload.get(key) == expected["ne"]:
                return False
        elif payload.get(key) != expected:
            return False
    return True


def test_probe_reports_full_filter_matrix_for_capable_client():
    report = probe_vector_capabilities(ProbeClient(), dimension=DIMENSION, metric="cosine_distance")

    assert "read_after_write" in report.supported
    assert {"eq", "in", "ne", "not", "and"} <= report.supported
    assert "id_allowlist" in report.supported
    assert "id_blocklist" in report.supported
    assert "list_filter" in report.supported
    assert "delete" in report.supported
    assert report.errors == ()
    assert not report.supported & {name for name, _ in report.errors}


def test_probe_records_error_for_unsupported_filter():
    report = probe_vector_capabilities(
        ProbeClient(failing_filters=["ne", "id_blocklist"]),
        dimension=DIMENSION,
        metric="cosine_distance",
    )

    assert "ne" not in report.supported
    assert "id_blocklist" not in report.supported
    assert "eq" in report.supported
    errors = dict(report.errors)
    assert "unsupported filter" in errors["ne"]
    assert "unsupported filter" in errors["id_blocklist"]


def test_probe_cleans_up_its_collection():
    client = ProbeClient()

    report = probe_vector_capabilities(client, dimension=DIMENSION, metric="cosine_distance")

    assert report.errors == ()
    assert client.collections == {}
