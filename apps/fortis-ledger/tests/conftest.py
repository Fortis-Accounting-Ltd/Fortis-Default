import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from fortis_ledger.ledger import Ledger  # noqa: E402
from fortis_ledger.model import Journal, Line  # noqa: E402
from fortis_ledger.money import to_dec  # noqa: E402
from fortis_ledger.store import FolderStore  # noqa: E402


@pytest.fixture
def store(tmp_path):
    s = FolderStore(tmp_path / "library", user="Tester", machine="pc1")
    s.initialise()
    return s


def J(date, narration, *lines, kind="manual"):
    """J('2026-06-30', 'text', ('18500', 100, 0), ('51100', 0, 100))"""
    d = dt.date.fromisoformat(date) if isinstance(date, str) else date
    return Journal(date=d, narration=narration, kind=kind,
                   lines=[Line(a, debit=to_dec(dr), credit=to_dec(cr)) for a, dr, cr in lines])


@pytest.fixture
def company(store):
    c = store.create_client("Test Co Pty Ltd", "company", abn="12 345 678 901", directors=["Jane Citizen"])
    L = Ledger(store, c.slug)
    L.ensure_year(2025)
    L.save_journal(J("2025-06-30", "FY2025 balances",
                     ("18500", 15000, 0), ("44500", 0, 100), ("51100", 0, 50000), ("72930", 5000, 0),
                     ("31300", 0, 4900), ("99100", 10000, 0), ("35300", 0, 10000), ("13600", 45000, 0),
                     ("43000", 0, 10000), kind="import"), 2025)
    return Ledger(store, c.slug)
