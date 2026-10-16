"""The app uses its own copy of the report engine and never loads anything from the skill."""
import sys
from pathlib import Path

from fortis_ledger import engine

APP_PACKAGE = Path(engine.__file__).resolve().parent


def test_report_engine_is_the_apps_own_copy():
    own = APP_PACKAGE / "reportengine"
    assert engine.ENGINE_DIR.resolve() == own
    for mod in (engine.fr_engine, engine.chartlib, engine.render_pack, engine.build_pack):
        assert Path(mod.__file__).resolve().is_relative_to(own), mod.__file__
    assert engine.CHART_DIR.resolve().is_relative_to(own)


def test_nothing_is_loaded_from_the_skill():
    loaded = [Path(m.__file__).as_posix() for m in list(sys.modules.values()) if getattr(m, "__file__", None)]
    assert not [p for p in loaded if "/.claude/skills/" in p]
    assert not [p for p in sys.path if "/.claude/skills/" in Path(p).as_posix()]
