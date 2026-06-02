from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass
class ModelTierConfig:
    strong: str
    medium: str
    cheap: str

    def resolve(self, tier: str) -> str:
        model = getattr(self, tier, None)
        if not model:
            raise ValueError(
                f"Model tier {tier!r} is not configured in config/models.yaml. "
                f"Add a model ID for this tier."
            )
        return model


_BUNDLED_MODELS = Path(__file__).parent.parent.parent / "config" / "models.yaml"


def load_model_config(config_path: Path) -> ModelTierConfig:
    # Fall back to bundled config if project-level file is missing
    if not config_path.exists():
        config_path = _BUNDLED_MODELS
    with config_path.open() as f:
        data = yaml.safe_load(f)
    models = data.get("models", {})
    return ModelTierConfig(
        strong=models.get("strong", ""),
        medium=models.get("medium", ""),
        cheap=models.get("cheap", ""),
    )
