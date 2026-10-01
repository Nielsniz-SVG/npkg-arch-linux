"""Métadonnées nixpkgs via Hydra (``hydra.nixos.org``), sans démon Nix.

``npkg`` doit transformer un nom de paquet (``hello``, ``python311Packages.requests``)
en un **chemin du store** pour pouvoir aller le chercher dans le cache binaire.
L'évaluation de ``default.nix`` étant le seul producteur officiel de ce chemin,
on s'appuie sur le résultat d'évaluation déjà publié par le service d'intégration
continue de NixOS : les pages de build Hydra exposent une table
``Output store paths`` (``out``, ``dev``, ``bin``...), la description, la licence,
la révision nixpkgs, et la taille de la fermeture.

C'est une source *secondaire* : si Nix est installé, ``npkg`` passe par
``nix eval``/``nix path-info`` (voir :mod:`npkg.resolver`), qui reste plus juste.
"""

from __future__ import annotations

import calendar
import html as html_mod
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import http

BASE = "https://hydra.nixos.org"
# canaux nixpkgs -> jobset Hydra. Les releases Linux sont bâties par `staging-*`.
CHANNEL_JOBSETS = {
    "nixpkgs-unstable": ["unstable"],
    "unstable": ["unstable"],
    "nixpkgs-stable": ["staging-next", "unstable"],
    "staging-next": ["staging-next"],
    "nixpkgs-staging": ["staging"],
}
_TAG = re.compile(r"<[^>]+>")
_REV = re.compile(r"full revision:\s*([0-9a-f]{40})")
_PATH = re.compile(r"/nix/store/[0-9a-z]{32}-[A-Za-z0-9._+\-]+")


@dataclass
class Build:
    attr: str
    jobset: str
    build_id: int | None = None
    nixname: str = ""
    version: str = ""
    description: str = ""
    license: str = ""
    homepage: str = ""
    position: str = ""
    system: str = ""
    drv_path: str = ""
    outputs: dict[str, str] = field(default_factory=dict)
    closure_size: str = ""
    output_size: str = ""
    available: bool = True
    rev: str = ""
    finished: str = ""
    finished_at: float | None = None
    url: str = ""

    @property
    def age_days(self) -> float | None:
        """Âge du dernier build réussi, en jours (repère de fraîcheur du canal)."""
        if not self.finished_at:
            return None
        return max(0.0, (time.time() - self.finished_at) / 86400.0)

    @property
    def out_path(self) -> str:
        for name in ("out", "bin", "dev"):
            if name in self.outputs:
                return self.outputs[name]
        if self.outputs:
            return sorted(self.outputs.values())[0]
        raise ValueError(f"aucune sortie pour le build de {self.attr}")

    def as_dict(self) -> dict:
        return {
            "attr": self.attr,
            "jobset": self.jobset,
            "build_id": self.build_id,
            "nixname": self.nixname,
            "version": self.version,
            "description": self.description,
            "license": self.license,
            "homepage": self.homepage,
            "position": self.position,
            "system": self.system,
            "drv_path": self.drv_path,
            "outputs": self.outputs,
            "closure_size": self.closure_size,
            "output_size": self.output_size,
            "available": self.available,
            "rev": self.rev,
            "finished": self.finished,
            "finished_at": self.finished_at,
            "url": self.url,
        }


def _strip(fragment: str) -> str:
    text = html_mod.unescape(fragment)
    text = _TAG.sub(" ", text)
    return re.sub(r"\s+", " ", text).strip()


def _label_rows(page: str) -> dict[str, str]:
    """``<th>Label:</th> <td>...</td>`` -> {label: valeur nettoyée}."""
    rows: dict[str, str] = {}
    pattern = re.compile(
        r"<th>\s*(.*?)\s*</th>\s*<td>(.*?)</td>",
        re.S,
    )
    for match in pattern.finditer(page):
        label = _strip(match.group(1)).rstrip(":").strip()
        value = _strip(match.group(2))
        if label and label not in rows:
            rows[label] = value
    return rows


def parse_build_page(page: str, *, attr: str = "", jobset: str = "") -> Build:
    build = Build(attr=attr, jobset=jobset)
    match = re.search(r"Build (\d+) of job", page)
    if match:
        build.build_id = int(match.group(1))
        build.url = f"{BASE}/build/{build.build_id}"
    rows = _label_rows(page)
    build.drv_path = _first_path(rows.get("Derivation store path", ""))
    build.nixname = rows.get("Nix name", "")
    build.description = rows.get("Short description", "")
    build.license = rows.get("License", "")
    build.homepage = _first_url(rows.get("Homepage", ""))
    build.position = rows.get("Nix expression", "")
    build.system = rows.get("System", "")
    build.closure_size = rows.get("Closure size", "")
    build.output_size = rows.get("Output size", "")
    build.available = "unavailable" not in rows.get("Availability", "").lower()
    build.finished = rows.get("Finished at", "")
    for stamp in (build.finished,):
        if stamp:
            try:
                build.finished_at = calendar.timegm(
                    time.strptime(stamp.split(".", 1)[0], "%Y-%m-%d %H:%M:%S")
                )
            except ValueError:
                build.finished_at = None
    # table des sorties : <th><tt>out</tt></th><td><tt>/nix/store/..</tt></td>
    section = page
    marker = page.find("Output store paths")
    if marker != -1:
        section = page[marker : marker + 4000]
    for match in re.finditer(
        r"<th>\s*<tt>\s*([A-Za-z0-9_-]+)\s*</tt>\s*</th>\s*<td>\s*<tt>\s*(/nix/store/[^\s<]+)\s*</tt>",
        section,
    ):
        build.outputs[match.group(1)] = match.group(2)
    if not build.outputs:  # repli : tout chemin qui n'est ni un .drv ni le source
        candidates = [
            path
            for path in _PATH.findall(page)
            if not path.endswith(".drv") and not path.endswith("-source")
        ]
        preferred = [path for path in candidates if build.nixname and build.nixname in path]
        chosen = (preferred or candidates)
        if chosen:
            build.outputs = {"out": chosen[0]}
    rev = _REV.search(page)
    if rev:
        build.rev = rev.group(1)
    if build.nixname:
        stripped = build.nixname
        m = re.match(r"^(.*?)-(\d[0-9A-Za-z._+\-]*)$", stripped)
        if m:
            build.version = m.group(2)
    return build


def _first_path(text: str) -> str:
    found = _PATH.search(text or "")
    return found.group(0) if found else ""


def _first_url(text: str) -> str:
    match = re.search(r"https?://[^\s]+", text or "")
    return match.group(0).rstrip(").,") if match else ""


def jobset_for_channel(channel: str) -> list[str]:
    if channel in CHANNEL_JOBSETS:
        return CHANNEL_JOBSETS[channel]
    m = re.match(r"nixpkgs-(\d{2})\.(\d{2})", channel)
    if m:
        major, minor = m.group(1), m.group(2)
        return [f"staging-{major}.{minor}", f"release-{major}{minor}", "unstable"]
    return [channel, "unstable"]


class HydraClient:
    def __init__(self, base: str = BASE, *, cache_dir: Path | None = None, ttl: int = 3600):
        self.base = base.rstrip("/")
        self.cache_dir = Path(cache_dir) if cache_dir else None
        self.ttl = ttl

    CACHE_VERSION = 2

    def _cached(self, key: str) -> dict | None:
        if self.cache_dir is None:
            return None
        file = self.cache_dir / f"{key}.json"
        if not file.exists() or (time.time() - file.stat().st_mtime) > self.ttl:
            return None
        try:
            payload = json.loads(file.read_text())
        except json.JSONDecodeError:
            return None
        if payload.get("_cache") != self.CACHE_VERSION:
            return None  # écrit par une version antérieure de npkg : on renégocie
        return payload.get("build")

    def _store(self, key: str, payload: dict) -> None:
        if self.cache_dir is None:
            return
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        wrapped = {"_cache": self.CACHE_VERSION, "build": payload}
        (self.cache_dir / f"{key}.json").write_text(json.dumps(wrapped, indent=1, sort_keys=True))

    def build(self, attr: str, *, jobset: str, system: str, refresh: bool = False) -> Build:
        key = re.sub(r"[^A-Za-z0-9]", "_", f"{jobset}.{attr}.{system}")
        if not refresh:
            cached = self._cached(key)
            if cached:
                return Build(**cached)
        url = f"{self.base}/job/nixpkgs/{jobset}/{attr}.{system}/latest"
        page = http.get_text(url)
        build = parse_build_page(page, attr=attr, jobset=jobset)
        if not build.outputs:
            raise LookupError(f"aucune sortie exploitable sur {url}")
        self._store(key, build.as_dict())
        return build

    def latest_build(self, attr: str, channel: str, system: str, *, refresh: bool = False) -> tuple[Build, str]:
        """Essaie les jobsets candidats du canal, renvoie (build, jobset utilisé)."""
        errors: list[str] = []
        for jobset in jobset_for_channel(channel):
            try:
                return self.build(attr, jobset=jobset, system=system, refresh=refresh), jobset
            except http.NotFound:
                errors.append(f"{jobset} : job inconnu")
            except (http.FetchError, LookupError) as exc:
                errors.append(f"{jobset} : {exc}")
        raise LookupError(f"paquet {attr!r} introuvable sur Hydra\n  " + "\n  ".join(errors))
