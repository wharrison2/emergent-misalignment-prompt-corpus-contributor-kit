"""Import the scenario dealer (`dad_pipeline.compose_scenarios`) of strictly_implicit_difficult_advice_pipeline_copy.

The pipeline's `shared` package shares its name with scripts/shared; when another module already imported the latter (a
pytest run over the whole scripts folder), it is dropped so the pipeline's own package is the one imported. Makes no model
call.
"""

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent.parent
PIPELINE_FOLDER = REPOSITORY_ROOT / "strictly_implicit_difficult_advice_pipeline_copy"


def load_scenario_dealer():
    if str(PIPELINE_FOLDER) not in sys.path:
        sys.path.insert(0, str(PIPELINE_FOLDER))
    if "shared" in sys.modules and PIPELINE_FOLDER not in Path(sys.modules["shared"].__file__).resolve().parents:
        for module_name in [name for name in sys.modules if name == "shared" or name.startswith("shared.")]:
            del sys.modules[module_name]
    from dad_pipeline import compose_scenarios
    return compose_scenarios
