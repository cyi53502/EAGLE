from datetime import timedelta

import pytest
from eagle.db.orm import PreferenceRecord, utc_now

from eagle.domain.scene import Scene
from eagle.preference.resolver import PreferenceResolver, UnresolvedPreferenceConflict


def preference(*, preference_id, scene, value, created_at):
    return PreferenceRecord(
        id=preference_id,
        lineage_id=preference_id,
        user_id="u1",
        preference_key="preferred_tool",
        preference_value_json={"tool": value},
        hardness="HARD",
        scene_json=scene,
        confidence=1.0,
        version=1,
        status="ACTIVE",
        authorization_state="EXPLICIT",
        created_at=created_at,
    )


def test_resolver_uses_created_at_to_break_equal_value_tie():
    now = utc_now()
    older = preference(
        preference_id="p1",
        scene={"artifact_type": "docx"},
        value="wps",
        created_at=now,
    )
    newer = preference(
        preference_id="p2",
        scene={"artifact_type": "docx"},
        value="wps",
        created_at=now + timedelta(seconds=1),
    )

    resolved = PreferenceResolver().resolve([older, newer], Scene(artifact_type="docx"))

    assert resolved == [newer]


def test_resolver_surfaces_equal_specificity_conflict():
    now = utc_now()
    by_app = preference(
        preference_id="p1",
        scene={"app": "office"},
        value="wps",
        created_at=now,
    )
    by_artifact = preference(
        preference_id="p2",
        scene={"artifact_type": "docx"},
        value="libreoffice",
        created_at=now,
    )

    with pytest.raises(UnresolvedPreferenceConflict):
        PreferenceResolver().resolve(
            [by_app, by_artifact],
            Scene(app="office", artifact_type="docx"),
        )
