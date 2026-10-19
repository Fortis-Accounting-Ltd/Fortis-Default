"""
Who may use the shared data folder and open which client.  The rules are kept in the data folder (team.json), so
every PC that uses the folder applies the same ones, to the screens and to Claude.

- Roles: admin (every client, and manages the team, firm settings and updates shared with everyone), staff (the
  clients they may open), none (cannot use Fortis Ledger with this data folder).
- A client is open to everyone in the team unless an admin limits it to named people; admins can always open it.
- People are known by their office (Microsoft 365) sign-in, so access can only be managed when sign-in is required.
  New people who sign in join as staff, or wait for an admin to add them, as the firm chooses.

Fortis Ledger applies these rules; anyone who has the data folder synced could still open its files directly, so a
client that must stay private should also have its folder limited in SharePoint.
"""
from __future__ import annotations

from dataclasses import dataclass, field

ROLES = {"admin": "Admin", "staff": "Staff", "none": "No access"}
JOIN = {"auto": "join as staff when they first sign in", "admin": "need an admin to add them"}


def norm(email: str) -> str:
    return (email or "").strip().lower()


@dataclass
class Team:
    managed: bool = False
    join: str = "auto"
    people: dict[str, dict] = field(default_factory=dict)      # email -> {"name", "role", "added_by", "added_at"}
    clients: dict[str, list[str]] = field(default_factory=dict)  # client slug -> the people who may open it

    @classmethod
    def load(cls, data: dict) -> "Team":
        """From team.json."""
        t = data or {}
        people = {norm(k): dict(v) for k, v in (t.get("people") or {}).items()
                  if norm(k) and isinstance(v, dict) and v.get("role") in ROLES}
        clients = {s: sorted({norm(e) for e in v if norm(e)}) for s, v in (t.get("clients") or {}).items()
                   if isinstance(v, list)}
        return cls(managed=bool(t.get("managed")), join=t.get("join") if t.get("join") in JOIN else "auto",
                   people=people, clients=clients)

    def to_dict(self) -> dict:
        return {"managed": self.managed, "join": self.join, "people": self.people, "clients": self.clients}

    # ---- questions ----
    def role(self, email: str) -> str | None:
        p = self.people.get(norm(email))
        return p["role"] if p else None

    def is_admin(self, email: str) -> bool:
        return not self.managed or self.role(email) == "admin"

    def can_use(self, email: str) -> bool:
        return not self.managed or self.role(email) in ("admin", "staff")

    def limited_to(self, slug: str) -> list[str] | None:
        """The people a client is limited to, or None when everyone in the team may open it."""
        return self.clients.get(slug)

    def can_open(self, email: str, slug: str) -> bool:
        if not self.managed:
            return True
        r = self.role(email)
        if r == "admin":
            return True
        if r != "staff":
            return False
        allowed = self.clients.get(slug)
        return allowed is None or norm(email) in allowed

    def admins(self) -> list[str]:
        return sorted(e for e, p in self.people.items() if p.get("role") == "admin")

    def name(self, email: str) -> str:
        p = self.people.get(norm(email)) or {}
        return p.get("name") or email

    # ---- changes (the caller saves to_dict() in the firm settings) ----
    def set_person(self, email: str, name: str, role: str, by: str, at: str) -> None:
        email = norm(email)
        if not email or "@" not in email:
            raise ValueError("enter the person's office email address")
        if role not in ROLES:
            raise ValueError("choose Admin, Staff or No access")
        if self.managed and role != "admin" and self.role(email) == "admin" and self.admins() == [email]:
            raise ValueError("keep at least one admin - make someone else an admin first")
        old = self.people.get(email) or {}
        self.people[email] = {"name": (name or old.get("name") or "").strip(), "role": role,
                              "added_by": old.get("added_by") or by, "added_at": old.get("added_at") or at}

    def remove_person(self, email: str) -> None:
        email = norm(email)
        if self.managed and self.role(email) == "admin" and self.admins() == [email]:
            raise ValueError("keep at least one admin - make someone else an admin first")
        self.people.pop(email, None)
        for slug in list(self.clients):
            self.clients[slug] = [e for e in self.clients[slug] if e != email]

    def limit_client(self, slug: str, emails: list[str] | None) -> None:
        """Limit a client to these people (admins can always open it), or open it to everyone (None)."""
        if emails is None:
            self.clients.pop(slug, None)
        else:
            self.clients[slug] = sorted({norm(e) for e in emails if norm(e)})

    def turn_on(self, email: str, name: str, at: str) -> None:
        """Start managing access, with the person turning it on as an admin."""
        self.managed = True
        self.set_person(email, name, "admin", by=norm(email), at=at)

    def summary(self, email: str, slugs: list[str]) -> str:
        """What a person can open, for the team list."""
        r = self.role(email)
        if r == "admin":
            return "every client"
        if r != "staff":
            return "nothing"
        limited = [s for s in slugs if s in self.clients]
        mine = [s for s in limited if norm(email) in self.clients[s]]
        if not limited:
            return "every client"
        return f"every client except {len(limited) - len(mine)} limited to others" if len(mine) < len(limited) \
            else "every client"
