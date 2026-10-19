"""
The monday.com export bundle - everything ForWork brings across from monday.com, as plain JSON files
in one folder (or a .zip of that folder).  monday_pull.py writes it from monday.com's API;
monday_import.py loads it into ForWork.  Nothing in it is specific to ForWork, so it is also a
readable archive of the monday.com account.

A view's settings are monday's: {"columns": {"column_order": [...], "column_properties": [{"column_id",
"visible"}]}, "group_by": {"conditions": [{"columnId"}]}}; filter is {"groups": [{"rules": [{"column_id",
"compare_value", "operator"}]}]} and sort [{"column_id", "direction"}].  monday's public API returns these
empty, so they are filled in when the export is made with Claude's monday.com connector.

  manifest.json
      {"format": "forwork-monday-export", "version": 1, "exported_at": "2026-10-18T23:30:00Z",
       "source": "monday-api", "account": {"name": "...", "slug": "..."},
       "boards": ["336059198", ...], "counts": {"boards": 39, "items": 7838, "updates": 8161}}
  users.json
      {"users": [{"id": "4295229", "name": "...", "email": "...", "title": "", "time_zone_identifier": "...",
                  "active": true}],
       "teams": [{"id": "1396058", "name": "Workflow Managers", "user_ids": ["4295229"]}]}
  workspaces.json
      {"workspaces": [{"id": "9669172", "name": "Main workspace", "kind": "open", "description": ""}],
       "folders": [{"id": "17644518", "name": "2025", "workspace_id": "11508994", "parent_id": null}]}
  boards/<board id>.json
      {"board": {"id", "name", "state" (active|archived), "board_kind", "description", "workspace_id",
                 "folder_id", "updated_at", "owner_ids": [...]},
       "groups": [{"id", "title", "color", "position", "archived", "deleted"}],
       "columns": [{"id", "title", "type", "description", "archived", "width", "settings": {...}}],
       "subitem_board_id": "..." or null,
       "subitem_columns": [same shape as columns],
       "views": [{"id", "name", "type", "settings": {...} or null, "filter": {...} or null, "sort": [...] or null}],
       "items": [{"id", "name", "state", "created_at", "updated_at", "creator_id", "group_id",
                  "values": {"<column id>": {"type", "text", "value": <parsed JSON or null>,
                                             "linked_item_ids": [...] (board_relation),
                                             "display_value": "..." (mirror, formula)}},
                  "subitems": [{"id", "name", "state", "created_at", "updated_at", "creator_id",
                                "values": {...}}]}],
       "updates": [{"id", "item_id", "body", "text_body", "created_at", "updated_at", "creator_id",
                    "replies": [{"id", "body", "text_body", "created_at", "creator_id"}],
                    "assets": [{"id", "name", "file_extension", "file_size"}]}],
       "automations": [{"when": {"column": "<column id>", "status_index": 3},
                        "then": {"move_to_group": "<group id>"} or {"set_date_today": "<column id>"}}]}

Ids are monday.com's, as strings.  Items are in board order.  "settings" is monday's settings_str,
parsed.  "value" is monday's column value JSON, parsed.  Updates on subitems are listed with the parent
board's updates (their item_id is the subitem's id).
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

FORMAT = "forwork-monday-export"
VERSION = 1


class BundleError(Exception):
    pass


def parse_json(s, default=None):
    """monday returns settings and values as JSON text; parse it, or default."""
    if s is None or s == "":
        return default
    if isinstance(s, (dict, list)):
        return s
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


class BundleWriter:
    def __init__(self, folder: str | Path):
        self.folder = Path(folder)
        (self.folder / "boards").mkdir(parents=True, exist_ok=True)

    def _write(self, rel: str, data) -> None:
        p = self.folder / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        tmp.replace(p)

    def write_users(self, users: list[dict], teams: list[dict]) -> None:
        self._write("users.json", {"users": users, "teams": teams})

    def write_workspaces(self, workspaces: list[dict], folders: list[dict]) -> None:
        self._write("workspaces.json", {"workspaces": workspaces, "folders": folders})

    def write_board(self, data: dict) -> None:
        bid = str(data["board"]["id"])
        if not bid.isdigit():
            raise BundleError(f"bad board id {bid!r}")
        self._write(f"boards/{bid}.json", data)

    def write_manifest(self, manifest: dict) -> None:
        m = {"format": FORMAT, "version": VERSION, **manifest}
        self._write("manifest.json", m)


class Bundle:
    """Reads a bundle folder or .zip."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._zip = None
        self._prefix = ""
        if self.path.is_file():
            try:
                self._zip = zipfile.ZipFile(self.path)
            except zipfile.BadZipFile:
                raise BundleError("that file is not a .zip of a monday.com export") from None
            names = self._zip.namelist()
            mf = [n for n in names if n.endswith("manifest.json") and n.count("/") <= 1]
            if not mf:
                raise BundleError("the .zip has no manifest.json - is it a ForWork monday.com export?")
            self._prefix = mf[0][: -len("manifest.json")]
        elif not (self.path / "manifest.json").is_file():
            raise BundleError(f"{self.path} has no manifest.json - is it a ForWork monday.com export?")
        self.manifest = self._read("manifest.json")
        if self.manifest.get("format") != FORMAT:
            raise BundleError("this is not a ForWork monday.com export")
        if int(self.manifest.get("version", 0)) > VERSION:
            raise BundleError("this export was made by a newer ForWork - update ForWork first")

    def _read(self, rel: str, default=None):
        try:
            if self._zip is not None:
                with self._zip.open(self._prefix + rel) as f:
                    return json.load(io.TextIOWrapper(f, encoding="utf-8"))
            return json.loads((self.path / rel).read_text(encoding="utf-8"))
        except (KeyError, FileNotFoundError):
            if default is not None:
                return default
            raise BundleError(f"the export is missing {rel}") from None

    def users(self) -> dict:
        return self._read("users.json", {"users": [], "teams": []})

    def workspaces(self) -> dict:
        return self._read("workspaces.json", {"workspaces": [], "folders": []})

    def board_ids(self) -> list[str]:
        if self._zip is not None:
            ids = [n[len(self._prefix) + len("boards/"):-5] for n in self._zip.namelist()
                   if n.startswith(self._prefix + "boards/") and n.endswith(".json")]
        else:
            ids = [p.stem for p in (self.path / "boards").glob("*.json")]
        return sorted((i for i in ids if i.isdigit()), key=int)

    def board(self, board_id: str) -> dict:
        return self._read(f"boards/{board_id}.json")

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()
