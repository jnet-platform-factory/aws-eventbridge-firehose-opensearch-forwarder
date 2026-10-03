"""Make `src` importable the way Lambda does.

`template.yaml` sets `CodeUri: app/`, so at runtime `src` is a top-level package
and the handlers' relative imports (`from .domain import ...`) resolve from
there. Putting `app/` on the path rather than `app/src/` reproduces that exactly,
so an import that works here works deployed.
"""
import json
import sys
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent / "app"
sys.path.insert(0, str(APP_ROOT))

# Example ShapingConfig field maps, one per shape the interpreter supports.
# Real deployments keep their own maps next to their own stack parameters; these
# exist so every shape is exercised here without naming anyone's deployment.
SHAPING_EXAMPLES = Path(__file__).resolve().parent / "fixtures" / "shaping"


def shaping_config(name: str) -> str:
    """The raw ShapingConfig string a stack would be deployed with."""
    return json.dumps(
        json.loads((SHAPING_EXAMPLES / f"{name}.json").read_text()), separators=(",", ":")
    )
