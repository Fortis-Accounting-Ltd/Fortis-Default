"""
Column types - what each kind of column holds, how a value is checked when someone types it, and how
it reads on screen.  They are the monday.com column types the firm uses, so boards come across as
they are.

Values are stored as JSON in item_values.value:

  text, long_text   "some words"
  numbers           1250.5
  status            3                                 the label's id (monday's label index)
  dropdown          [1, 4]                            label ids
  people            {"users": [2, 7], "teams": [1]}
  date              "2026-10-28" or "2026-10-28T09:30:00Z" (a time is kept in UTC)
  timeline          {"from": "2026-07-01", "to": "2026-10-31"}
  checkbox          true                              (unticked = no value)
  link              {"url": "https://...", "text": "FYI workpaper"}
  email             {"email": "a@b.com.au", "text": "a@b.com.au"}
  phone             {"phone": "0412345678", "country": "AU"}
  location          {"address": "1 George St Sydney", "lat": "", "lng": ""}
  board_relation    [item ids]                        items on the boards named in the column settings
  file              [file ids]
  readonly          {"text": "..."}                   monday.com columns ForWork shows but does not edit
                                                       (doc, button, progress, unsupported)

Computed columns store nothing: name (the item's name), mirror (a column of the linked items),
formula, subtasks (the subitems' progress), creation_log and last_updated.
"""
from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

# monday.com's label colours, in its own order - offered for new labels and groups
PALETTE = [
    ("#00c875", "Green"), ("#fdab3d", "Orange"), ("#df2f4a", "Red"), ("#579bfc", "Blue"),
    ("#a25ddc", "Purple"), ("#c4c4c4", "Grey"), ("#ffcb00", "Yellow"), ("#037f4c", "Dark green"),
    ("#0086c0", "Dark blue"), ("#9cd326", "Lime"), ("#ff5ac4", "Pink"), ("#66ccff", "Light blue"),
    ("#784bd1", "Dark purple"), ("#7f5347", "Brown"), ("#ff642e", "Dark orange"), ("#bb3354", "Berry"),
    ("#e2445c", "Raspberry"), ("#ff158a", "Hot pink"), ("#cab641", "Mustard"), ("#175a63", "Teal"),
    ("#333333", "Black"), ("#757575", "Dark grey"), ("#007eb5", "Ocean"), ("#9d50dd", "Violet"),
]
DEFAULT_LABEL_COLOR = "#c4c4c4"
COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
DATETIME_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?Z$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
KEY_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
SAFE_URL_SCHEMES = {"http", "https", "mailto", "tel"}


class ValueError_(ValueError):
    """A value someone typed that the column cannot hold (the message is shown to them)."""


def check_color(c: str, default: str = DEFAULT_LABEL_COLOR) -> str:
    c = (c or "").strip()
    return c.lower() if COLOR_RE.match(c) else default


@dataclass
class Ctx:
    """Lookups a column needs to read a value: names of people, teams and linked items."""
    users: dict            # id -> name
    teams: dict            # id -> name
    item_names: dict       # id -> name (linked items)

    @classmethod
    def empty(cls) -> "Ctx":
        return cls({}, {}, {})


def fmt_number(n, decimals=None) -> str:
    if n is None or n == "":
        return ""
    try:
        f = float(n)
    except (TypeError, ValueError):
        return str(n)
    if not math.isfinite(f):
        return ""
    if decimals is not None:
        return f"{f:,.{int(decimals)}f}"
    if f == int(f) and abs(f) < 1e15:
        return f"{int(f):,}"
    return f"{f:,.2f}".rstrip("0").rstrip(".")


def parse_number(v):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        raise ValueError_("enter a number")
    if isinstance(v, (int, float)):
        f = float(v)
    else:
        s = str(v).strip().replace(",", "").replace("$", "").replace(" ", "")
        neg = s.startswith("(") and s.endswith(")")
        s = s.strip("()")
        if not s:
            return None
        try:
            f = float(s)
        except ValueError:
            raise ValueError_(f"{v!r} is not a number") from None
        f = -f if neg else f
    if not math.isfinite(f):
        raise ValueError_("enter a number")
    return int(f) if f == int(f) and abs(f) < 1e15 else round(f, 6)


def parse_date(v) -> str | None:
    """'2026-10-28', '28/10/2026' or an ISO date-time in UTC."""
    if v is None or v == "":
        return None
    s = str(v).strip()
    if DATE_RE.match(s):
        dt.date.fromisoformat(s)
        return s
    if DATETIME_RE.match(s):
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return t.strftime("%Y-%m-%dT%H:%M:%SZ")
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        try:
            return dt.date(int(m[3]), int(m[2]), int(m[1])).isoformat()
        except ValueError:
            pass
    raise ValueError_(f"{v!r} is not a date (use 28/10/2026)")


def date_text(v) -> str:
    """'2026-10-28' -> '28 Oct 2026'; times are shown in Sydney time."""
    if not v:
        return ""
    try:
        if "T" in v:
            from zoneinfo import ZoneInfo
            try:
                tz = ZoneInfo("Australia/Sydney")
            except Exception:
                tz = dt.timezone.utc
            t = dt.datetime.fromisoformat(v.replace("Z", "+00:00")).astimezone(tz)
            return f"{t.day} {t:%b %Y %H:%M}"
        d = dt.date.fromisoformat(v)
        return f"{d.day} {d:%b %Y}"
    except ValueError:
        return str(v)


def safe_url(url: str) -> str:
    """A link someone can click safely: http(s), mailto or tel only (never javascript:)."""
    u = (url or "").strip()
    if not u:
        return ""
    lower = u.lower()
    if not lower.startswith(("mailto:", "tel:")):
        m = re.match(r"^([a-zA-Z][a-zA-Z0-9+.\-]*):(?!\d)", u)       # a scheme (not host:port)
        if m and m.group(1).lower() not in SAFE_URL_SCHEMES:
            raise ValueError_("links must start with https://")
        if "://" not in u:
            u = "https://" + u
    scheme = urlsplit(u).scheme.lower()
    if scheme not in SAFE_URL_SCHEMES:
        raise ValueError_("links must start with https://")
    return u


# ---- the types ---------------------------------------------------------------------------- #
class ColumnType:
    name = "text"
    title = "Text"
    editable = True
    computed = False
    creatable = True             # offered in "Add column"
    summary = None               # footer: "count", "sum" or "labels"

    def default_settings(self) -> dict:
        return {}

    def clean_settings(self, settings: dict) -> dict:
        return dict(settings or {})

    def normalize(self, value, settings: dict):
        """A value from the screen or an import -> the stored value (None clears the cell)."""
        if value is None:
            return None
        s = str(value).strip()
        return s or None

    def text(self, value, settings: dict, ctx: Ctx) -> str:
        return "" if value is None else str(value)

    def sort_key(self, value, settings: dict, text: str):
        return (0, text.lower()) if text else (1, "")

    def is_empty(self, value) -> bool:
        return value in (None, "", [], {})


class Text(ColumnType):
    name, title = "text", "Text"

    def normalize(self, value, settings):
        if value is None:
            return None
        s = str(value).strip()
        if len(s) > 2000:
            raise ValueError_("text columns hold up to 2,000 characters - use a long text column")
        return s or None


class LongText(ColumnType):
    name, title = "long_text", "Long text"

    def normalize(self, value, settings):
        if value is None:
            return None
        s = str(value).replace("\r\n", "\n").strip()
        if len(s) > 50000:
            raise ValueError_("long text is limited to 50,000 characters")
        return s or None


class Numbers(ColumnType):
    name, title, summary = "numbers", "Numbers", "sum"

    def clean_settings(self, settings):
        s = dict(settings or {})
        unit = str(s.get("unit") or "")[:8]
        out = {"unit": unit, "unit_position": "right" if s.get("unit_position") == "right" else "left"}
        if s.get("decimals") not in (None, ""):
            out["decimals"] = max(0, min(6, int(s["decimals"])))
        return out

    def normalize(self, value, settings):
        return parse_number(value)

    def text(self, value, settings, ctx):
        if value is None or value == "":
            return ""
        t = fmt_number(value, (settings or {}).get("decimals"))
        unit = (settings or {}).get("unit") or ""
        if unit:
            return f"{t} {unit}" if (settings or {}).get("unit_position") == "right" else f"{unit}{t}"
        return t

    def sort_key(self, value, settings, text):
        return (0, float(value)) if value not in (None, "") else (1, 0.0)


class Status(ColumnType):
    """
    settings: {"labels": [{"id": 0, "name": "Working on it", "color": "#fdab3d", "done": false,
                           "hidden": false}, ...]}  in the order they are offered.
    Hidden labels (monday's "deactivated") stay on items that have them but are not offered.
    """
    name, title, summary = "status", "Status", "labels"

    def default_settings(self):
        return {"labels": [
            {"id": 0, "name": "Working on it", "color": "#fdab3d"},
            {"id": 1, "name": "Done", "color": "#00c875", "done": True},
            {"id": 2, "name": "Stuck", "color": "#df2f4a"},
        ]}

    def clean_settings(self, settings):
        labels, seen_ids, seen_names = [], set(), set()
        for lab in (settings or {}).get("labels") or []:
            try:
                lid = int(lab.get("id"))
            except (TypeError, ValueError):
                continue
            name = str(lab.get("name") or "").strip()[:80]
            if lid in seen_ids or lid < 0:
                continue
            seen_ids.add(lid)
            entry = {"id": lid, "name": name, "color": check_color(lab.get("color"))}
            if lab.get("done"):
                entry["done"] = True
            if lab.get("hidden") or (name.lower() in seen_names and name):
                entry["hidden"] = True
            seen_names.add(name.lower())
            labels.append(entry)
        return {"labels": labels}

    @staticmethod
    def label(settings, lid):
        for lab in (settings or {}).get("labels") or []:
            if lab["id"] == lid:
                return lab
        return None

    @staticmethod
    def find(settings, name: str):
        name = (name or "").strip().lower()
        for lab in (settings or {}).get("labels") or []:
            if lab["name"].strip().lower() == name and not lab.get("hidden"):
                return lab
        for lab in (settings or {}).get("labels") or []:
            if lab["name"].strip().lower() == name:
                return lab
        return None

    def normalize(self, value, settings):
        if value is None or value == "":
            return None
        if isinstance(value, bool):
            raise ValueError_("pick a label")
        if isinstance(value, int) or (isinstance(value, str) and value.strip().lstrip("-").isdigit()
                                      and self.label(settings, int(value)) is not None):
            lid = int(value)
            if self.label(settings, lid) is None:
                raise ValueError_("that label is not on this column")
            return lid
        lab = self.find(settings, str(value))
        if lab is None:
            raise ValueError_(f"{value!r} is not one of this column's labels")
        return lab["id"]

    def text(self, value, settings, ctx):
        if value is None:
            return ""
        lab = self.label(settings, value)
        return lab["name"] if lab else ""

    def sort_key(self, value, settings, text):
        labels = (settings or {}).get("labels") or []
        for i, lab in enumerate(labels):
            if lab["id"] == value:
                return (0, i)
        return (1, 0)

    def is_done(self, value, settings) -> bool:
        lab = self.label(settings, value)
        return bool(lab and lab.get("done"))


class Dropdown(ColumnType):
    """settings: {"labels": [{"id": 1, "name": "Xero"}, ...]}; value: [ids]"""
    name, title = "dropdown", "Dropdown"

    def default_settings(self):
        return {"labels": []}

    def clean_settings(self, settings):
        labels, seen = [], set()
        for lab in (settings or {}).get("labels") or []:
            try:
                lid = int(lab.get("id"))
            except (TypeError, ValueError):
                continue
            if lid in seen:
                continue
            seen.add(lid)
            entry = {"id": lid, "name": str(lab.get("name") or "").strip()[:120]}
            if lab.get("hidden"):
                entry["hidden"] = True
            labels.append(entry)
        return {"labels": labels}

    def normalize(self, value, settings):
        if value in (None, "", []):
            return None
        if not isinstance(value, list):
            value = [value]
        labels = (settings or {}).get("labels") or []
        by_id = {lab["id"]: lab for lab in labels}
        by_name = {lab["name"].lower(): lab for lab in labels}
        out = []
        for v in value:
            if isinstance(v, int) and v in by_id:
                lid = v
            elif isinstance(v, str) and v.strip().lower() in by_name:
                lid = by_name[v.strip().lower()]["id"]
            elif isinstance(v, str) and v.strip().isdigit() and int(v) in by_id:
                lid = int(v)
            else:
                raise ValueError_(f"{v!r} is not one of this column's options")
            if lid not in out:
                out.append(lid)
        return out or None

    def text(self, value, settings, ctx):
        if not value:
            return ""
        by_id = {lab["id"]: lab["name"] for lab in (settings or {}).get("labels") or []}
        return ", ".join(by_id.get(v, "") for v in value if by_id.get(v))


class People(ColumnType):
    name, title, summary = "people", "People", "count"

    def normalize(self, value, settings):
        if value in (None, "", [], {}):
            return None
        if isinstance(value, list):
            value = {"users": value}
        if not isinstance(value, dict):
            raise ValueError_("pick people from the list")
        users, teams = [], []
        for u in value.get("users") or []:
            try:
                u = int(u)
            except (TypeError, ValueError):
                raise ValueError_("pick people from the list") from None
            if u not in users:
                users.append(u)
        for t in value.get("teams") or []:
            try:
                t = int(t)
            except (TypeError, ValueError):
                raise ValueError_("pick teams from the list") from None
            if t not in teams:
                teams.append(t)
        limit = int((settings or {}).get("max_people") or 0)
        if limit and len(users) + len(teams) > limit:
            raise ValueError_(f"this column takes at most {limit} {'person' if limit == 1 else 'people'}")
        out = {}
        if users:
            out["users"] = users
        if teams:
            out["teams"] = teams
        return out or None

    def text(self, value, settings, ctx):
        if not value:
            return ""
        names = [ctx.users.get(u, "") for u in value.get("users") or []]
        names += [ctx.teams.get(t, "") for t in value.get("teams") or []]
        return ", ".join(n for n in names if n)


class Date(ColumnType):
    name, title = "date", "Date"

    def normalize(self, value, settings):
        if isinstance(value, dict):
            value = value.get("date")
        return parse_date(value)

    def text(self, value, settings, ctx):
        return date_text(value)

    def sort_key(self, value, settings, text):
        return (0, value) if value else (1, "")


class Timeline(ColumnType):
    name, title = "timeline", "Timeline"

    def normalize(self, value, settings):
        if value in (None, "", {}):
            return None
        if not isinstance(value, dict):
            raise ValueError_("a timeline needs a start and an end date")
        f, t = parse_date(value.get("from")), parse_date(value.get("to"))
        if not f and not t:
            return None
        f, t = f or t, t or f
        if f[:10] > t[:10]:
            f, t = t, f
        return {"from": f[:10], "to": t[:10]}

    def text(self, value, settings, ctx):
        if not value:
            return ""
        a, b = date_text(value.get("from")), date_text(value.get("to"))
        return a if a == b else f"{a} - {b}"

    def sort_key(self, value, settings, text):
        return (0, value.get("from", "")) if value else (1, "")


class Checkbox(ColumnType):
    name, title, summary = "checkbox", "Checkbox", "count"

    def normalize(self, value, settings):
        if isinstance(value, dict):
            value = value.get("checked")
        if isinstance(value, str):
            value = value.strip().lower() in ("true", "1", "yes", "y", "ticked", "checked", "v")
        return True if value else None

    def text(self, value, settings, ctx):
        return "Yes" if value else ""


class Link(ColumnType):
    name, title = "link", "Link"

    def normalize(self, value, settings):
        if value in (None, "", {}):
            return None
        if isinstance(value, str):
            value = {"url": value}
        url = safe_url(str(value.get("url") or ""))
        if not url:
            return None
        text = str(value.get("text") or "").strip()[:300]
        return {"url": url, "text": text or url}

    def text(self, value, settings, ctx):
        if not value:
            return ""
        return value.get("text") or value.get("url") or ""


class Email(ColumnType):
    name, title = "email", "Email"

    def normalize(self, value, settings):
        if value in (None, "", {}):
            return None
        if isinstance(value, str):
            value = {"email": value}
        e = str(value.get("email") or "").strip()
        if not e:
            return None
        if not EMAIL_RE.match(e):
            raise ValueError_(f"{e!r} is not an email address")
        return {"email": e, "text": str(value.get("text") or "").strip()[:300] or e}

    def text(self, value, settings, ctx):
        return (value or {}).get("email", "")


class Phone(ColumnType):
    name, title = "phone", "Phone"

    def normalize(self, value, settings):
        if value in (None, "", {}):
            return None
        if isinstance(value, str):
            value = {"phone": value}
        p = re.sub(r"[^\d+]", "", str(value.get("phone") or ""))
        if not p:
            return None
        if len(p) > 20:
            raise ValueError_("that phone number is too long")
        return {"phone": p, "country": str(value.get("country") or "AU")[:2].upper()}

    def text(self, value, settings, ctx):
        return (value or {}).get("phone", "")


class Location(ColumnType):
    name, title = "location", "Location"

    def normalize(self, value, settings):
        if value in (None, "", {}):
            return None
        if isinstance(value, str):
            value = {"address": value}
        a = str(value.get("address") or "").strip()[:300]
        if not a:
            return None
        return {"address": a, "lat": str(value.get("lat") or ""), "lng": str(value.get("lng") or "")}

    def text(self, value, settings, ctx):
        return (value or {}).get("address", "")


class BoardRelation(ColumnType):
    """settings: {"board_ids": [ids]} - the boards whose items can be linked; value: [item ids]"""
    name, title, summary = "board_relation", "Connect boards", "count"

    def default_settings(self):
        return {"board_ids": []}

    def clean_settings(self, settings):
        ids = []
        for b in (settings or {}).get("board_ids") or []:
            try:
                ids.append(int(b))
            except (TypeError, ValueError):
                pass
        return {"board_ids": sorted(set(ids))}

    def normalize(self, value, settings):
        if value in (None, "", []):
            return None
        if not isinstance(value, list):
            value = [value]
        out = []
        for v in value:
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise ValueError_("pick items from the list") from None
            if v not in out:
                out.append(v)
        return out or None

    def text(self, value, settings, ctx):
        if not value:
            return ""
        return ", ".join(ctx.item_names.get(i, "") for i in value if ctx.item_names.get(i))


class File(ColumnType):
    """value: [file ids] (files table)"""
    name, title, summary = "file", "Files", "count"

    def normalize(self, value, settings):
        if value in (None, "", []):
            return None
        if not isinstance(value, list):
            value = [value]
        out = []
        for v in value:
            try:
                out.append(int(v))
            except (TypeError, ValueError):
                raise ValueError_("bad file") from None
        return out or None

    def text(self, value, settings, ctx):
        return f"{len(value)} file{'s' if len(value) != 1 else ''}" if value else ""


class ReadOnly(ColumnType):
    """A monday.com column ForWork shows as it was but does not edit (doc, button, progress ...)."""
    name, title, editable, creatable = "readonly", "Read only", False, False

    def normalize(self, value, settings):
        if value in (None, "", {}):
            return None
        if isinstance(value, dict):
            t = str(value.get("text") or "").strip()
            return {"text": t} if t else None
        t = str(value).strip()
        return {"text": t} if t else None

    def text(self, value, settings, ctx):
        return (value or {}).get("text", "")


class Computed(ColumnType):
    editable = False
    computed = True

    def normalize(self, value, settings):
        return None


class Name(Computed):
    name, title, creatable = "name", "Name", False


class Mirror(Computed):
    """settings: {"relation_column": <column id on this board>, "targets": {"<board id>": <column id>}}"""
    name, title = "mirror", "Mirror"

    def default_settings(self):
        return {"relation_column": None, "targets": {}}

    def clean_settings(self, settings):
        s = settings or {}
        targets = {}
        for b, c in (s.get("targets") or {}).items():
            try:
                targets[str(int(b))] = int(c)
            except (TypeError, ValueError):
                pass
        rel = s.get("relation_column")
        try:
            rel = int(rel) if rel not in (None, "") else None
        except (TypeError, ValueError):
            rel = None
        return {"relation_column": rel, "targets": targets}


class Formula(Computed):
    """settings: {"formula": "{deal_value}*{deal_close_probability}"} - {key} is another column's key"""
    name, title, summary = "formula", "Formula", "sum"

    def default_settings(self):
        return {"formula": ""}

    def clean_settings(self, settings):
        return {"formula": str((settings or {}).get("formula") or "")[:1000]}


class Subtasks(Computed):
    name, title, creatable = "subtasks", "Subitems", False


class CreationLog(Computed):
    name, title = "creation_log", "Creation log"


class LastUpdated(Computed):
    name, title = "last_updated", "Last updated"


TYPES: dict[str, ColumnType] = {t.name: t for t in (
    Text(), LongText(), Numbers(), Status(), Dropdown(), People(), Date(), Timeline(), Checkbox(), Link(),
    Email(), Phone(), Location(), BoardRelation(), File(), ReadOnly(), Name(), Mirror(), Formula(), Subtasks(),
    CreationLog(), LastUpdated(),
)}

# monday.com type -> ForWork type
MONDAY_TYPES = {
    "name": "name", "text": "text", "long_text": "long_text", "numbers": "numbers", "status": "status",
    "color": "status", "dropdown": "dropdown", "people": "people", "person": "people", "multiple-person": "people",
    "date": "date", "timeline": "timeline", "timerange": "timeline", "checkbox": "checkbox", "boolean": "checkbox",
    "link": "link", "email": "email", "phone": "phone", "location": "location", "board_relation": "board_relation",
    "file": "file", "mirror": "mirror", "lookup": "mirror", "formula": "formula", "subtasks": "subtasks",
    "creation_log": "creation_log", "last_updated": "last_updated",
}


def column_type(name: str) -> ColumnType:
    return TYPES.get(name) or TYPES["readonly"]


def creatable_types() -> list[tuple[str, str]]:
    return [(t.name, t.title) for t in TYPES.values() if t.creatable]


# ---- formulas ---------------------------------------------------------------------------- #
class FormulaError(Exception):
    pass


_TOKEN = re.compile(r"\s*(?:(\d+(?:\.\d*)?|\.\d+)|(\{[a-z0-9_]+\})|([A-Za-z_][A-Za-z0-9_]*)|(\*\*|[-+*/(),%<>=!]=?|.))")
_FUNCS = {
    "ROUND": lambda x, n=0: round(x, int(n)), "ABS": abs, "MIN": min, "MAX": max,
    "SUM": lambda *a: sum(a), "AVERAGE": lambda *a: sum(a) / len(a) if a else 0,
    "IF": lambda c, a, b=0: a if c else b, "DAYS": None,
}


def eval_formula(formula: str, values: dict) -> float | None:
    """
    A safe calculator for formula columns: numbers, {column_key}, + - * / %, brackets, comparisons
    and ROUND, ABS, MIN, MAX, SUM, AVERAGE, IF.  Never runs Python code.  An empty input counts as 0,
    like monday.com.  Raises FormulaError for anything else.
    """
    toks = []
    for m in _TOKEN.finditer(formula or ""):
        num, ref, word, op = m.groups()
        if num:
            toks.append(("num", float(num)))
        elif ref:
            v = values.get(ref[1:-1])
            try:
                toks.append(("num", float(v) if v not in (None, "") else 0.0))
            except (TypeError, ValueError):
                toks.append(("num", 0.0))
        elif word:
            if word.upper() not in _FUNCS or _FUNCS[word.upper()] is None:
                raise FormulaError(f"ForWork does not know the function {word}")
            toks.append(("fn", word.upper()))
        elif op and op.strip():
            toks.append(("op", op))
    pos = 0

    def peek():
        return toks[pos] if pos < len(toks) else (None, None)

    def take():
        nonlocal pos
        t = peek()
        pos += 1
        return t

    def expect(op):
        t = take()
        if t != ("op", op):
            raise FormulaError(f"expected {op}")

    def comparison():
        a = additive()
        while peek()[0] == "op" and peek()[1] in ("<", ">", "<=", ">=", "=", "==", "!=", "<>"):
            op = take()[1]
            b = additive()
            a = float({"<": a < b, ">": a > b, "<=": a <= b, ">=": a >= b, "=": a == b, "==": a == b,
                       "!=": a != b, "<>": a != b}[op])
        return a

    def additive():
        a = term()
        while peek() in (("op", "+"), ("op", "-")):
            op = take()[1]
            b = term()
            a = a + b if op == "+" else a - b
        return a

    def term():
        a = unary()
        while peek() in (("op", "*"), ("op", "/"), ("op", "%")):
            op = take()[1]
            b = unary()
            if op == "*":
                a = a * b
            elif b == 0:
                raise FormulaError("division by zero")
            else:
                a = a / b if op == "/" else a % b
        return a

    def unary():
        if peek() == ("op", "-"):
            take()
            return -unary()
        if peek() == ("op", "+"):
            take()
            return unary()
        return atom()

    def atom():
        kind, v = take()
        if kind == "num":
            return v
        if kind == "fn":
            expect("(")
            args = []
            if peek() != ("op", ")"):
                args.append(comparison())
                while peek() == ("op", ","):
                    take()
                    args.append(comparison())
            expect(")")
            try:
                return float(_FUNCS[v](*args))
            except (TypeError, ValueError, ZeroDivisionError):
                raise FormulaError(f"{v} was given the wrong inputs") from None
        if (kind, v) == ("op", "("):
            r = comparison()
            expect(")")
            return r
        raise FormulaError("the formula is not complete")

    if not toks:
        return None
    result = comparison()
    if pos != len(toks):
        raise FormulaError("the formula has something extra at the end")
    return result
