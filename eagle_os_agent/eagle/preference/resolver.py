import json
from collections.abc import Iterable

from eagle.db.orm import PreferenceRecord
from eagle.domain.scene import Scene


class UnresolvedPreferenceConflict(RuntimeError):
    pass


class PreferenceResolver:
    def resolve(self, preferences: Iterable[PreferenceRecord], scene: Scene) -> list[PreferenceRecord]:
        current = scene.normalized()
        matched = [preference for preference in preferences if self._matches(preference.scene_json, current)]
        resolved = []
        keys = sorted({preference.preference_key for preference in matched})
        for key in keys:
            by_key = [preference for preference in matched if preference.preference_key == key]
            specificity = max(len(preference.scene_json) for preference in by_key)
            contenders = [preference for preference in by_key if len(preference.scene_json) == specificity]
            values = {self._canonical_value(preference.preference_value_json) for preference in contenders}
            if len(values) > 1:
                raise UnresolvedPreferenceConflict(
                    f"Conflicting ACTIVE preferences for key {key} at specificity {specificity}"
                )
            contenders.sort(
                key=lambda preference: (
                    preference.version,
                    preference.created_at,
                    preference.id,
                ),
                reverse=True,
            )
            resolved.append(contenders[0])
        return resolved

    @staticmethod
    def _matches(memory_scene: dict, current_scene: dict) -> bool:
        return all(current_scene.get(key) == value for key, value in memory_scene.items())

    @staticmethod
    def _canonical_value(value: dict) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))
