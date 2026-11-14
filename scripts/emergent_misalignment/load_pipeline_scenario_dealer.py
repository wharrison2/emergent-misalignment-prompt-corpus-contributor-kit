"""Import the scenario dealer (`dad_pipeline.compose_scenarios`) of strictly_implicit_difficult_advice_pipeline_copy.

The pipeline's `shared` package shares its name with scripts/shared; when another module already imported the latter (a
pytest run over the whole scripts folder), it is dropped so the pipeline's own package is the one imported. Makes no model
call.
"""

import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parent.parent.parent
PIPELINE_FOLDER = REPOSITORY_ROOT / "strictly_implicit_difficult_advice_pipeline_copy"


def modules_named_shared_that_belong_to_the_pipeline() -> list[str]:
    return [name for name, module in list(sys.modules.items())
            if (name == "shared" or name.startswith("shared.")) and getattr(module, "__file__", None)
            and PIPELINE_FOLDER in Path(module.__file__).resolve().parents]


def load_scenario_dealer():
    pipeline_folder_was_added_here = str(PIPELINE_FOLDER) not in sys.path
    if pipeline_folder_was_added_here:
        sys.path.insert(0, str(PIPELINE_FOLDER))
    if "shared" in sys.modules and PIPELINE_FOLDER not in Path(sys.modules["shared"].__file__).resolve().parents:
        for module_name in [name for name in sys.modules if name == "shared" or name.startswith("shared.")]:
            del sys.modules[module_name]
    try:
        from dad_pipeline import compose_scenarios
    finally:
        # The pipeline's `shared` package must not stay behind as `shared`, or later `from shared.<module>` imports of scripts/shared
        # fail (the cause of the whole-folder pytest collection error). compose_scenarios already holds the modules it needs.
        for module_name in modules_named_shared_that_belong_to_the_pipeline():
            del sys.modules[module_name]
        if pipeline_folder_was_added_here:
            sys.path.remove(str(PIPELINE_FOLDER))
    return compose_scenarios
