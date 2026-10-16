"""
End-to-end check of an installed copy - `Fortis Ledger.exe --self-test`.  For each entity type it
sets up a client in a temporary folder, posts a journal, rolls the year forward and produces the
Financial Reports and a Trial Balance.  Exit code 0 means everything worked; the details go to
self-test.txt in the settings folder (a windowed build has no console).
"""
from __future__ import annotations

import datetime as dt
import tempfile
import traceback
from pathlib import Path

from . import __version__


def run() -> int:
    from . import reports
    from .engine import ENGINE_DIR, report_code_options
    from .ledger import Ledger
    from .model import Journal, Line
    from .money import to_dec
    from .settings import config_dir
    from .store import FolderStore

    out: list[str] = [f"Fortis Ledger {__version__} self-test {dt.datetime.now():%Y-%m-%d %H:%M:%S}",
                      f"report engine: {ENGINE_DIR}", f"report codes known: {len(report_code_options())}"]
    ok = True
    with tempfile.TemporaryDirectory(prefix="fortis-ledger-selftest-", ignore_cleanup_errors=True) as d:
        st = FolderStore(Path(d) / "library", user="self-test", machine="self-test")
        st.initialise()
        for etype in ("company", "trust", "partnership", "sole_trader"):
            try:
                c = st.create_client(f"Self Test {etype.replace('_', ' ').title()}", etype, directors=["A Person"])
                L = Ledger(st, c.slug)
                bank = next(a.code for a in L.chart.usable() if a.report_code.startswith("ASS.CUR.CAS"))
                income = next(a.code for a in L.chart.usable() if a.report_code.startswith("REV"))
                for fy in (2025, 2026):
                    L.ensure_year(fy)
                    L.save_journal(Journal(date=L.fy_range(fy)[1], narration=f"self-test FY{fy}",
                                           lines=[Line(bank, debit=to_dec(1000 * fy - 2024000)),
                                                  Line(income, credit=to_dec(1000 * fy - 2024000))]), fy)
                    if fy == 2025:
                        L.close_year(2025)
                L = Ledger(st, c.slug)
                res = reports.financial_reports(L, 2026, Path(d) / f"{etype}.pdf")
                reports.trial_balance(L, 2026, Path(d) / f"{etype}.xlsx", as_excel=True)
                size = (Path(d) / f"{etype}.pdf").stat().st_size
                errors = [v["detail"] for v in res["validation"] if v["level"] == "error"]
                good = size > 5000 and not errors and res["summary"]["npat"]["cy"] == 2000
                ok &= good
                out.append(f"{etype}: {'ok' if good else 'FAILED'} - pack {size:,} bytes, profit "
                           f"{res['summary']['npat']['cy']:,.0f} (PY {res['summary']['npat']['py']:,.0f})"
                           + (f", errors: {errors}" if errors else ""))
            except Exception:
                ok = False
                out.append(f"{etype}: FAILED\n{traceback.format_exc()}")
    out.append("PASSED" if ok else "FAILED")
    text = "\n".join(out)
    try:
        p = config_dir() / "self-test.txt"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text + "\n", encoding="utf-8")
    except OSError:
        pass
    import sys
    if sys.stdout:
        print(text, flush=True)
    return 0 if ok else 1
