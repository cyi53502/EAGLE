import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class CapabilityReport:
    supported: frozenset[str]
    errors: tuple[tuple[str, str], ...]


#: Normalized filter dialect used for probes; the concrete SDK client translates it.
#: - {"field": value}                 -> equality
#: - {"field": {"in": [...]}}         -> membership
#: - {"field": {"nin": [...]}}        -> exclusion
#: - {"field": {"ne": value}}         -> inequality
#: - {"id": {"in"/"nin": [...]}}      -> vector ID allowlist / blocklist
#: - {"$not": [condition, ...]}       -> negation
PROBE_FILTER_DIALECT = "kylin-normalized-v1"


def probe_vector_capabilities(client, *, dimension: int, metric: str) -> CapabilityReport:
    collection = f"eagle_capability_{uuid.uuid4().hex}"
    first_id = f"probe-a-{uuid.uuid4().hex}"
    second_id = f"probe-b-{uuid.uuid4().hex}"
    first_vector = [1.0] + [0.0] * (dimension - 1)
    second_vector = [-1.0] + [0.0] * (dimension - 1)
    supported = set()
    errors = []

    client.ensure_collection(name=collection, dimension=dimension, metric=metric)
    try:
        client.upsert(
            collection=collection,
            vectors=[first_vector, second_vector],
            payloads=[
                {"probe_group": "a", "probe_state": "active"},
                {"probe_group": "b", "probe_state": "inactive"},
            ],
            ids=[first_id, second_id],
        )

        try:
            if client.get(collection=collection, vector_id=first_id, include_vector=False) is not None:
                supported.add("read_after_write")
            else:
                errors.append(("read_after_write", "inserted vector was not readable"))
        except Exception as error:  # noqa: BLE001 - external SDK boundary
            errors.append(("read_after_write", str(error)))

        probes = {
            "eq": {"probe_group": "a"},
            "in": {"probe_group": {"in": ["a"]}},
            "ne": {"probe_group": {"ne": "b"}},
            "not": {"$not": [{"probe_group": "b"}]},
            "and": {"probe_group": "a", "probe_state": "active"},
            "id_allowlist": {"id": {"in": [first_id]}},
            "id_blocklist": {"id": {"nin": [second_id]}},
        }
        for name, filters in probes.items():
            try:
                rows = client.search(
                    collection=collection,
                    vector=first_vector,
                    limit=10,
                    filters=filters,
                )
                ids = {str(row.id) for row in rows}
                if ids == {first_id}:
                    supported.add(name)
                else:
                    errors.append((name, f"unexpected ids: {sorted(ids)}"))
            except Exception as error:  # noqa: BLE001 - external SDK boundary
                errors.append((name, str(error)))

        try:
            rows = client.list(
                collection=collection,
                filters={"probe_group": "a"},
                limit=10,
            )
            ids = {str(row.id) for row in rows}
            if ids == {first_id}:
                supported.add("list_filter")
            else:
                errors.append(("list_filter", f"unexpected ids: {sorted(ids)}"))
        except Exception as error:  # noqa: BLE001 - external SDK boundary
            errors.append(("list_filter", str(error)))

        try:
            client.delete(collection=collection, vector_id=second_id)
            deleted = client.get(
                collection=collection,
                vector_id=second_id,
                include_vector=False,
            )
            if deleted is None:
                supported.add("delete")
            else:
                errors.append(("delete", "deleted vector remained readable"))
        except Exception as error:  # noqa: BLE001 - external SDK boundary
            errors.append(("delete", str(error)))
    finally:
        client.delete_collection(name=collection)

    return CapabilityReport(frozenset(supported), tuple(errors))
