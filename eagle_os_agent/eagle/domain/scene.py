from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Scene:
    app: str | None = None
    task: str | None = None
    artifact_type: str | None = None

    def normalized(self) -> dict[str, str]:
        return {key: value.strip().lower() for key, value in asdict(self).items() if value is not None}

    def specificity(self) -> int:
        return len(self.normalized())

    @classmethod
    def from_dict(cls, value: dict[str, str] | None) -> "Scene":
        return cls(**(value or {}))
