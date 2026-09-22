"""Neon scene; all scene-specific defaults and validation live here."""
import math
from pathlib import Path
from ai_runtime.preview import Preview

DEFAULTS = dict(hue=190.0, speed=0.65, particles=650, glow=0.8,
                rings=5, spread=1.0, title='NEON SINGULARITY')
RANGES = dict(hue=(0, 360), speed=(0, 3), particles=(0, 2000),
              glow=(0, 1.5), rings=(1, 10), spread=(0.5, 1.5))


class NeonPreview(Preview):
    def __init__(self):
        super().__init__(html=Path(__file__).with_name('neon.html').read_text(encoding="utf-8"), state=DEFAULTS)

    def update(self, *, message="Scene updated", **changes):
        for key, value in changes.items():
            if key == 'title':
                if not isinstance(value, str) or len(value) > 80:
                    raise ValueError('title must be a string of at most 80 characters')
            elif key in RANGES:
                low, high = RANGES[key]
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                    raise ValueError(f'{key} must be between {low} and {high}')
                if key in ('particles', 'rings') and not isinstance(value, int):
                    raise ValueError(f'{key} must be an integer')
            else:
                raise ValueError(f'Unknown scene parameter: {key}')
        return super().update(message=message, **changes)
