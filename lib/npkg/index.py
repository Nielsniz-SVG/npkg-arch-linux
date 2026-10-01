"""Index de recherche sur nixpkgs (facultatif mais pratique).

npkg n'a *pas* besoin d'un index pour installer : la résolution passe par
Hydra/``nix`` et la fermeture par les narinfo. L'index sert à ``npkg search`` :
trouver le nom d'attribut exact (``python311Packages.requests``, pas ``requests``).

Trois façons de le remplir :

* ``npkg index fetch``   — liste ``pkgs/by-name`` du dépôt nixpkgs via l'API
  GitHub : ~30 requêtes, 15 000+ noms, aucune dépendance ;
* ``npkg index import <nixpkgs>`` — parse les ``.nix`` d'un checkout local
  (récupère pname/version/description/licence, fonctionne hors-ligne) ;
* ``npkg index add <attr>`` — ajour un attribut à la main.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path

from . import http

GITHUB_API = os.environ.get("NPKG_GITHUB_API", "https://api.github.com")
# Un seul appel suffit : l'API « git trees » accepte un sous-répertoire et le
# drapeau recursive — ~52 000 entrées, ~14 Mo, contre 676 appels si on passe
# par /contents (limite non authentifiée : 60 req/h).
BY_NAME_TREES = f"{GITHUB_API}/repos/NixOS/nixpkgs/git/trees/{{ref}}:pkgs/by-name?recursive=1"
BY_NAME_URL = f"{GITHUB_API}/repos/NixOS/nixpkgs/contents/pkgs/by-name"
_BY_NAME_ENTRY = re.compile(r"^[^/]+/([A-Za-z0-9._+-]+)/package\.nix$")
DEFAULT_REF = os.environ.get("NPKG_INDEX_REF", "nixpkgs-unstable")
ALPHABET = [chr(c) for c in range(ord("a"), ord("z") + 1)]

_FIELD = {
    "pname": re.compile(r"\bpname\s*=\s*\"([^\"]+)\"|\bpname\s*=\s*'([^']+)'"),
    "version": re.compile(r"\bversion\s*=\s*\"([^\"]+)\"|\bversion\s*=\s*'([^']+)'"),
    "description": re.compile(r"\bdescription\s*=\s*\"([^\"]+)\"|\bdescription\s*=\s*'([^']+)'"),
    "homepage": re.compile(r"\bhome[pP]age\s*=\s*\[?\s*[\"']?([^\"'\s;\]]+)"),
    "license": re.compile(
        r"\blicense\s*=\s*(?:[\w.]*licenses\.)?([A-Za-z0-9_]+)"
        r"|\blicense\s*=\s*\"([^\"]+)\"|\blicenses\s*=\s*\[\s*([A-Za-z0-9_.]+)"
    ),
    "mainProgram": re.compile(r"\bmainProgram\s*=\s*\"?([A-Za-z0-9._+-]+)"),
}


@dataclass
class Entry:
    attr: str
    pname: str = ""
    version: str = ""
    description: str = ""
    license: str = ""
    homepage: str = ""
    main_program: str = ""
    source: str = ""

    @property
    def name(self) -> str:
        return self.pname or self.attr.split(".")[-1]

    def as_dict(self) -> dict:
        return {
            "attr": self.attr,
            "pname": self.pname,
            "version": self.version,
            "description": self.description,
            "license": self.license,
            "homepage": self.homepage,
            "mainProgram": self.main_program,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, blob: dict) -> "Entry":
        return cls(
            attr=blob.get("attr", ""),
            pname=blob.get("pname", ""),
            version=blob.get("version", ""),
            description=blob.get("description", ""),
            license=blob.get("license", ""),
            homepage=blob.get("homepage", ""),
            main_program=blob.get("mainProgram", ""),
            source=blob.get("source", ""),
        )


class Index:
    def __init__(self, path: Path | str | None = None):
        self.path = Path(path) if path else None
        self.meta: dict = {}
        self.entries: dict[str, Entry] = {}
        if self.path and self.path.exists():
            self.load()

    # -- persistance -------------------------------------------------------
    def load(self) -> None:
        try:
            raw = json.loads(self.path.read_text() or "{}")
        except json.JSONDecodeError:
            return
        self.meta = raw.get("meta", {})
        self.entries = {
            attr: Entry.from_dict(blob) for attr, blob in (raw.get("packages") or {}).items()
        }

    def save(self) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.meta["updated"] = time.time()
        payload = {
            "meta": self.meta,
            "packages": {attr: entry.as_dict() for attr, entry in sorted(self.entries.items())},
        }
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True))
        os.replace(tmp, self.path)

    # -- construction ------------------------------------------------------
    def add(self, entry: Entry) -> None:
        self.entries[entry.attr] = entry

    def merge(self, other: dict[str, Entry]) -> int:
        before = len(self.entries)
        self.entries.update(other)
        return len(self.entries) - before

    def clear(self) -> None:
        self.entries = {}
        self.meta = {}

    def fetch_by_name(self, *, ref: str = DEFAULT_REF, progress=None) -> int:
        """Liste les paquets ``pkgs/by-name`` du canal (un seul appel HTTP).

        ``pkgs/by-name/<2lettres>/<nom>/package.nix`` est le dépôt des paquets
        modernes de nixpkgs : ~55 % de l'arbre y vit, et la liste des noms suffit
        à rendre ``npkg search`` utile. En repli (API indisponible), on énumère
        lettre par lettre via ``/contents``.
        """
        try:
            added = self._fetch_by_name_trees(ref, progress)
        except (http.FetchError, http.NotFound, json.JSONDecodeError):
            added = 0
        if not added:
            added = self._fetch_by_name_contents(ref, progress)
        self.meta["by_name_ref"] = ref
        return added

    def _fetch_by_name_trees(self, ref: str, progress) -> int:
        url = BY_NAME_TREES.format(ref=ref)
        if progress:
            progress("trees", "téléchargement de l'arbre pkgs/by-name …")
        listing = json.loads(http.get_text(url, timeout=90))
        tree = listing.get("tree") or []
        if not tree:
            raise http.FetchError("réponse vide de l'API GitHub")
        names = {m.group(1) for item in tree for m in [_BY_NAME_ENTRY.match(item.get("path", ""))] if m}
        for name in sorted(names):
            self.add(Entry(attr=name, pname=name, source="by-name"))
        if progress:
            progress("trees", f"{len(names)} paquets" + (" (arbre tronqué)" if listing.get("truncated") else ""))
        return len(names)

    def _fetch_by_name_contents(self, ref: str, progress) -> int:
        """Repli : un appel /contents par préfixe de deux lettres."""
        added = 0
        prefixes = [a + b for a in ALPHABET for b in ALPHABET]
        for prefix in prefixes:
            try:
                body = http.get_text(f"{BY_NAME_URL}/{prefix}?ref={ref}", timeout=20, retries=1)
            except (http.FetchError, http.NotFound):
                continue  # préfixe inusité ou limite de requêtes atteinte
            try:
                listing = json.loads(body)
            except json.JSONDecodeError:
                continue
            if not isinstance(listing, list):
                continue
            for item in listing:
                if item.get("type") != "dir":
                    continue
                name = item.get("name", "")
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+-]*", name):
                    continue
                self.add(Entry(attr=name, pname=name, source="by-name"))
                added += 1
            if progress:
                progress(prefix, f"{added} paquets")
        return added

    def import_nixpkgs(self, root: Path | str, *, limit: int | None = None) -> int:
        """Parse un checkout nixpkgs : ``package.nix`` / ``*.nix`` contenant mkDerivation."""
        root = Path(root)
        if not root.is_dir():
            raise NotADirectoryError(f"{root} n'est pas un répertoire")
        parsed = 0
        files = [p for p in root.rglob("*.nix") if p.is_file()]
        for file in files:
            if limit and parsed >= limit:
                break
            try:
                text = file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            # un package.nix de pkgs/by-name est un simple attribut : pas de
            # mkDerivation à l'intérieur, il faut quand même le lire
            if "mkDerivation" not in text and not _FIELD["pname"].search(text):
                continue
            entry = parse_derivation_file(file, root)
            if entry is None:
                continue
            self.add(entry)
            parsed += 1
        self.meta["imported_from"] = str(root)
        return parsed

    # -- recherche ---------------------------------------------------------
    def search(self, query: str, *, limit: int = 30) -> list[tuple[int, Entry]]:
        if not query:
            return [(0, entry) for _, entry in sorted(self.entries.items())][:limit]
        needle = query.lower()
        scored: list[tuple[int, Entry]] = []
        for attr, entry in self.entries.items():
            score = _score(needle, attr, entry)
            if score:
                scored.append((score, entry))
        scored.sort(key=lambda item: (-item[0], item[1].attr))
        return scored[:limit]

    def get(self, attr: str) -> Entry | None:
        return self.entries.get(attr)

    def stats(self) -> dict:
        rich = sum(1 for entry in self.entries.values() if entry.description)
        return {
            "packages": len(self.entries),
            "with_description": rich,
            "updated": self.meta.get("updated"),
            "by_name_ref": self.meta.get("by_name_ref"),
            "imported_from": self.meta.get("imported_from"),
            "file": str(self.path) if self.path else None,
        }


def _score(needle: str, attr: str, entry: Entry) -> int:
    name = entry.name.lower()
    attr_lower = attr.lower()
    if name == needle or attr_lower == needle:
        # un attribut de premier niveau bat l'attribut imbriqué à nom égal
        return 1000 if "." not in attr_lower else 800
    if attr_lower.endswith("." + needle) or name == needle:
        return 900
    if name.startswith(needle):
        return 500 - min(len(name), 100)
    if needle in name:
        return 300 - min(len(name), 100)
    if needle in attr_lower:
        return 200
    words = {word for word in re.split(r"[^a-z0-9]+", needle) if len(word) > 2}
    if words:
        blob = (entry.description or "").lower()
        hits = sum(1 for word in words if word in blob)
        if hits:
            return 50 + 10 * hits
    return 0


def parse_derivation_file(file: Path, root: Path) -> Entry | None:
    """Extraction léger-poids d'un ``package.nix`` (aucun évaluateur Nix ici)."""
    text = file.read_text(encoding="utf-8", errors="replace")
    marker = text.find("mkDerivation")
    # on ne parse qu'un bloc : assez pour les quelques champs dont on a besoin
    body = text[marker : marker + 2500] if marker != -1 else text

    def grab(field: str) -> str:
        match = _FIELD[field].search(body)
        if not match:
            return ""
        return next((g for g in match.groups() if g), "").strip()

    pname = grab("pname")
    if not pname:
        match = re.search(r"\bname\s*=\s*\"([^\"]+)\"", body)
        if match:
            raw = match.group(1)
            split = re.match(r"^(.+?)-(\d[0-9A-Za-z._+-]*)$", raw)
            pname = split.group(1) if split else raw
    if not pname or "${" in pname:
        return None
    if file.name in ("package.nix", "default.nix"):
        attr = file.parent.name        # pkgs/by-name/xx/nom/package.nix, pkgs/.../nom/default.nix
    else:
        attr = file.stem
    if not re.fullmatch(r"[A-Za-z0-9._+-]+", attr or ""):
        return None
    return Entry(
        attr=attr,
        pname=pname,
        version=grab("version"),
        description=grab("description"),
        license=grab("license"),
        homepage=grab("homepage"),
        main_program=grab("mainProgram"),
        source=str(file.relative_to(root)),
    )
