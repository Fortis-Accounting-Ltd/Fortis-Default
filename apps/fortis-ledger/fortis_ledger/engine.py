"""
The report engine - chart matcher, Financial Reports engine, PDF renderer and workpaper reader.

The app carries its own copy in fortis_ledger/reportengine, taken from the financial-report-builder
skill (see reportengine/SOURCE.md).  The app never reads or changes the skill, and changes to the
skill do not reach the app.  FORTIS_REPORT_ENGINE points the app at another copy of the engine.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def engine_dir() -> Path:
    env = os.environ.get("FORTIS_REPORT_ENGINE")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent / "reportengine"


ENGINE_DIR = engine_dir()
if not (ENGINE_DIR / "scripts" / "fr_engine.py").exists():
    raise RuntimeError(f"The report engine is missing from {ENGINE_DIR} - reinstall Fortis Ledger.")
_scripts = str(ENGINE_DIR / "scripts")
if _scripts not in sys.path:
    sys.path.insert(0, _scripts)

import build_pack  # noqa: E402
import chart as chartlib  # noqa: E402
import fr_engine  # noqa: E402
import render_pack  # noqa: E402

CHART_DIR = ENGINE_DIR / "assets" / "chart-of-accounts"
REPORT_MAP = fr_engine.load_map()
BOILERPLATE = fr_engine.load_boilerplate()

_fonts: dict | None = None


def fonts() -> dict:
    """Register the report fonts once per process."""
    global _fonts
    if _fonts is None:
        _fonts = render_pack.register_fonts(None)
    return _fonts


def slot_of(report_code: str, name: str = "") -> str | None:
    """Layout slot a Xero report code prints in (e.g. LIA.CUR.PAY.EMP -> LIA.CUR.EMP)."""
    return fr_engine.resolve_slot(report_code or "", name or "", REPORT_MAP)[0]


def nature(report_code: str, name: str = "") -> str:
    """'dr' when the account's natural balance is a debit (assets, expenses, dividends), else 'cr'."""
    slot = slot_of(report_code, name)
    if slot:
        return build_pack.nature_of_slot(slot)
    rc = (report_code or "").upper()
    return "dr" if rc.startswith(("ASS", "EXP")) else "cr"


def describe(report_code: str, name: str = "") -> str:
    return fr_engine.describe_code(report_code or "", name or "", REPORT_MAP)


_codes: list[tuple[str, str]] | None = None


def report_code_options() -> list[tuple[str, str]]:
    """Every report code the firm charts and the report map use, with where each one prints."""
    global _codes
    if _codes is None:
        found = {k for k in (REPORT_MAP.get("xero_code_map") or {}) if not k.startswith("_")}
        found |= {k for k in (REPORT_MAP.get("bs_lines") or {}) if not k.startswith("_")}
        for fname in chartlib.CHART_FILES.values():
            try:
                found |= {a.report_code for a in chartlib.Chart(CHART_DIR / fname).accounts if a.report_code}
            except Exception:   # a damaged chart file must not stop the app
                continue
        _codes = sorted(((rc, describe(rc)) for rc in found if slot_of(rc)), key=lambda x: x[0])
    return list(_codes)


__all__ = ["ENGINE_DIR", "CHART_DIR", "REPORT_MAP", "BOILERPLATE", "build_pack", "chartlib", "fr_engine",
           "render_pack", "fonts", "slot_of", "nature", "describe", "report_code_options"]
