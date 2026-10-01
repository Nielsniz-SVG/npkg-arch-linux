"""Résolution d'un « spec » utilisateur vers un chemin précis du store.

Ordre de priorité (le premier qui répond gagne) :

1. ``/nix/store/<hash>-<name>`` ou ``<hash>-<name>`` — pin explicite,
2. ``$NPKG_HOME/pins.json`` — table ``attr -> store path`` (miroirs, CI, hors-ligne),
3. ``nix`` s'il est installé : ``nix eval`` sur ``<nixpkgs>`` (le plus juste),
4. **Hydra** : la page de build du job ``nixpkgs/<jobset>/<attr>.<system>``
   (voir :mod:`npkg.hydra`) — ne demande aucune installation de Nix.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import hydra as hydra_mod
from .hashing import is_valid_hash_part
from .store import parse_store_path

_STORE_NAME = re.compile(r"^(?P<name>.+?)@(?P<version>[^@]+)$")


@dataclass
class Resolution:
    name: str
    attr: str
    store_path: str
    version: str = ""
    description: str = ""
    license: str = ""
    homepage: str = ""
    outputs: dict[str, str] = field(default_factory=dict)
    source: str = ""
    jobset: str = ""
    rev: str = ""
    drv_path: str = ""
    closure_size: str = ""
    nixname: str = ""
    build_age_days: float | None = None
    build_date: str = ""

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "attr": self.attr,
            "store_path": self.store_path,
            "version": self.version,
            "description": self.description,
            "license": self.license,
            "homepage": self.homepage,
            "outputs": self.outputs,
            "source": self.source,
            "jobset": self.jobset,
            "rev": self.rev,
            "drv_path": self.drv_path,
            "closure_size": self.closure_size,
            "nixname": self.nixname,
            "build_age_days": self.build_age_days,
            "build_date": self.build_date,
        }


def split_spec(spec: str) -> tuple[str, str]:
    """``hello@2.12.3`` -> ``("hello", "2.12.3")`` ; sinon version vide."""
    match = _STORE_NAME.match(spec)
    if match:
        return match.group("name"), match.group("version")
    return spec, ""


class Resolver:
    def __init__(
        self,
        home: Path,
        *,
        channel: str = "nixpkgs-unstable",
        system: str = "x86_64-linux",
        use_nix: bool | None = None,
        store_dir: Path | str = "/nix/store",
        http_cache_dir: Path | None = None,
        jobset: str | None = None,
    ):
        self.home = Path(home)
        self.channel = channel
        self.system = system
        self.store_dir = Path(store_dir)
        self.jobset = jobset
        self.offline = os.environ.get("NPKG_OFFLINE", "") not in ("", "0", "false")
        self.pins_file = self.home / "pins.json"
        self._pins = self._load_pins()
        if use_nix is None:
            use_nix = bool(shutil.which("nix")) and not self.offline
        self.use_nix = use_nix
        self.hydra = hydra_mod.HydraClient(
            base=_hydra_base(), cache_dir=http_cache_dir, ttl=_hydra_ttl()
        )

    # -- utilitaires -------------------------------------------------------
    def _load_pins(self) -> dict[str, str]:
        if self.pins_file.exists():
            try:
                return dict(json.loads(self.pins_file.read_text() or "{}"))
            except json.JSONDecodeError:
                return {}
        return {}

    def add_pin(self, attr: str, store_path: str) -> None:
        self._pins[attr] = store_path
        self.pins_file.parent.mkdir(parents=True, exist_ok=True)
        self.pins_file.write_text(json.dumps(dict(sorted(self._pins.items())), indent=2) + "\n")

    def remove_pin(self, attr: str) -> bool:
        if attr in self._pins:
            self._pins.pop(attr)
            self.pins_file.write_text(json.dumps(dict(sorted(self._pins.items())), indent=2) + "\n")
            return True
        return False

    def pins(self) -> dict[str, str]:
        return dict(self._pins)

    # -- résolution --------------------------------------------------------
    def resolve(self, spec: str, *, refresh: bool = False) -> Resolution:
        attr, want_version = split_spec(spec)
        if self.looks_like_store_path(attr):
            return self._from_store_path(attr)
        if attr in self._pins:
            res = self._from_store_path(self._pins[attr])
            res.name, res.attr = _default_name(attr), attr
            res.source = "pin"
            return res
        if self.use_nix:
            try:
                return self._from_nix(attr, want_version)
            except Exception as exc:  # noqa: BLE001 - on retombe sur Hydra
                self._nix_error = str(exc)
        else:
            self._nix_error = None
        if self.offline:
            raise LookupError(
                f"{attr!r} absent de {self.pins_file} (mode hors-ligne, NPKG_OFFLINE=1)"
            )
        build = self._hydra_build(attr, refresh=refresh)
        store_path = self._rebase(build.out_path)
        version = build.version or _version_from_name(store_path)
        if want_version and version != want_version:
            raise LookupError(
                f"{attr} : version {version!s} sur le canal {self.channel}, "
                f"et non {want_version!r}"
            )
        return Resolution(
            name=_default_name(attr),
            attr=attr,
            store_path=store_path,
            version=version,
            description=build.description,
            license=build.license,
            homepage=build.homepage,
            outputs={name: self._rebase(path) for name, path in build.outputs.items()},
            source="hydra",
            jobset=build.jobset,
            rev=build.rev,
            drv_path=build.drv_path,
            closure_size=build.closure_size,
            nixname=build.nixname,
            build_age_days=build.age_days,
            build_date=build.finished,
        )

    def _hydra_build(self, attr: str, *, refresh: bool = False) -> hydra_mod.Build:
        if self.jobset:
            return self.hydra.build(attr, jobset=self.jobset, system=self.system, refresh=refresh)
        build, _jobset = self.hydra.latest_build(attr, self.channel, self.system, refresh=refresh)
        return build

    def _rebase(self, store_path: str) -> str:
        """Un chemin Hydra est toujours sous ``/nix/store`` ; on relocalise si besoin."""
        if str(self.store_dir) == "/nix/store":
            return store_path
        try:
            digest, name = parse_store_path(store_path)
        except ValueError:
            return store_path
        return str(self.store_dir / f"{digest}-{name}")

    def looks_like_store_path(self, text: str) -> bool:
        if text.startswith("/"):
            return "/store/" in text
        if "-" not in text:
            return False
        digest = text.split("-", 1)[0]
        return len(digest) == 32 and is_valid_hash_part(digest)

    def _from_store_path(self, path: str) -> Resolution:
        if not path.startswith("/"):
            path = str(self.store_dir / path)
        digest, name = parse_store_path(path)
        match = re.match(r"^(?P<pkg>.+?)-(?P<version>\d[0-9A-Za-z._+\-]*)$", name)
        return Resolution(
            name=match.group("pkg") if match else name,
            attr=match.group("pkg") if match else name,
            store_path=path,
            version=match.group("version") if match else "",
            outputs={"out": path},
            source="path",
        )

    def _from_nix(self, attr: str, want_version: str) -> Resolution:
        expr = f"let pkgs = import <nixpkgs> {{}}; in pkgs.{attr}"
        out = _run(["nix", "eval", "--raw", "--impure", "--expr", f"({expr}).outPath"])
        store_path = out.strip()
        meta_raw = _run(
            [
                "nix", "eval", "--impure", "--json", "--expr",
                f"let pkgs = import <nixpkgs> {{}}; p = pkgs.{attr}; in {{\n"
                "  pname = p.pname or p.name or \"\";\n"
                "  version = p.version or \"\";\n"
                "  description = (p.meta or {{}}).description or \"\";\n"
                "  homepage = (p.meta or {{}}).homepage or \"\";\n"
                "  license = (p.meta or {{}}).license.spdxId or (p.meta or {{}}).license.fullName or \"\";\n"
                "  outputs = builtins.attrNames (p.outputs or {{}});\n"
                "}",
            ]
        )
        meta = json.loads(meta_raw)
        version = meta.get("version") or _version_from_name(store_path)
        outputs = {}
        for name in meta.get("outputs") or ["out"]:
            try:
                outputs[name] = _run(
                    ["nix", "eval", "--raw", "--impure", "--expr", f"({expr}).{name}"]
                ).strip()
            except Exception:  # noqa: BLE001 - sortie secondaire indisponible
                continue
        outputs.setdefault("out", store_path)
        return Resolution(
            name=meta.get("pname") or _default_name(attr),
            attr=attr,
            store_path=store_path,
            version=version,
            description=meta.get("description", ""),
            license=meta.get("license", ""),
            homepage=str(meta.get("homepage") or ""),
            outputs=outputs,
            source="nix",
            closure_size="",
        )

    def describe_backends(self) -> list[str]:
        backends = ["pins" if self._pins else None, "nix" if self.use_nix else None, "hydra"]
        return [item for item in backends if item]


def _run(argv: list[str], timeout: int = 180) -> str:
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{' '.join(argv[:3])} a échoué : {proc.stderr.strip()[:400]}")
    return proc.stdout


def _version_from_name(store_path: str) -> str:
    try:
        _, name = parse_store_path(store_path)
    except ValueError:
        return ""
    match = re.match(r"^.+?-(\d[0-9A-Za-z._+\-]*)$", name)
    return match.group(1) if match else ""


def _default_name(attr: str) -> str:
    return attr.split(".")[-1]


def _hydra_base() -> str:
    return (os.environ.get("NPKG_HYDRA_BASE") or hydra_mod.BASE).rstrip("/")


def _hydra_ttl() -> int:
    try:
        return int(os.environ.get("NPKG_HYDRA_TTL", "3600"))
    except ValueError:
        return 3600
