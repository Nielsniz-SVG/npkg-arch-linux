"""Le « store » : répertoire où vivent les chemins du style ``/nix/store/<hash>-<name>``.

npkg réutilise la discipline de Nix :

* un chemin du store est **immuable** une fois déployé (on écrit dans un
  répertoire temporaire puis ``os.replace``),
* la fermeture d'un paquet se déduit des ``References`` des ``.narinfo``,
* un paquet n'est supprimé que s'il n'est référencé par aucune racine.

Un index local (``valid.json``) joue le rôle de la base ``nix-db`` : il retient
le ``NarHash`` vérifié, la taille et les références de chaque chemin déployé.
"""

from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from . import nar

@dataclass
class ValidEntry:
    path: str
    name: str = ""
    nar_hash: str = ""
    nar_size: int | None = None
    refs: list[str] = field(default_factory=list)
    cache: str = ""
    created: bool = False  # dépaqueté par npkg -> supprimable par `gc`
    unpacked_at: float = 0.0
    files: int = 0
    bytes: int = 0


class ValidIndex:
    """Persiste l'état « valide et présent dans le store » (json plat)."""

    def __init__(self, file: Path):
        self.file = Path(file)
        self.entries: dict[str, ValidEntry] = {}
        self.load()

    def load(self) -> None:
        if not self.file.exists():
            return
        try:
            raw = json.loads(self.file.read_text() or "{}")
        except json.JSONDecodeError:
            raw = {}
        for key, value in (raw.get("valid") or {}).items():
            try:
                self.entries[key] = ValidEntry(**value)
            except TypeError:
                continue

    def save(self) -> None:
        payload = {"valid": {k: asdict(v) for k, v in sorted(self.entries.items())}}
        tmp = self.file.with_suffix(self.file.suffix + ".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(payload, indent=1, sort_keys=True))
        os.replace(tmp, self.file)

    def get(self, store_path: str) -> ValidEntry | None:
        return self.entries.get(store_path)

    def put(self, entry: ValidEntry) -> None:
        self.entries[entry.path] = entry

    def drop(self, store_path: str) -> None:
        self.entries.pop(store_path, None)


def parse_store_path(store_path: str) -> tuple[str, str]:
    """/nix/store/<32>-<name> -> (hashPart, name). Lève ValueError sinon."""
    base = store_path.rstrip("/").rsplit("/", 1)[-1]
    if "-" not in base:
        raise ValueError(f"chemin de store invalide : {store_path!r}")
    digest, name = base.split("-", 1)
    if len(digest) != 32 or not name:
        raise ValueError(f"chemin de store invalide : {store_path!r}")
    return digest, name


def store_dir_from_env(env=None) -> Path:
    env = os.environ if env is None else env
    override = env.get("NPKG_STORE_DIR")
    if override:
        return Path(override)
    default = Path("/nix/store")
    if default.is_dir() and os.access(default, os.W_OK):
        return default
    home = Path(env.get("NPKG_HOME") or (Path.home() / ".local" / "state" / "npkg"))
    return home / "store"


def dir_size(path: Path) -> int:
    """Espace disque occupé, en suivant les liens symboliques (comme ``du -sb``)."""
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in files + dirs:
            full = Path(root) / name
            try:
                total += full.lstat().st_size
            except OSError:
                continue
    try:
        total += path.lstat().st_size
    except OSError:
        pass
    return total


class Store:
    def __init__(self, root: Path, index: ValidIndex):
        self.root = Path(root)
        self.index = index

    # -- inspection --------------------------------------------------------
    def path(self, digest_or_path: str) -> str:
        if digest_or_path.startswith("/"):
            return digest_or_path
        return str(self.root / digest_or_path)

    def exists(self, store_path: str) -> bool:
        return Path(store_path).is_dir() or Path(store_path).is_file()

    def is_valid(self, store_path: str) -> bool:
        entry = self.index.get(store_path)
        return bool(entry) and self.exists(store_path)

    def list_paths(self) -> list[str]:
        if not self.root.exists():
            return []
        return sorted(
            str(p) for p in self.root.iterdir()
            if "-" in p.name and not p.name.startswith((".", ".npkg-staging"))
        )

    def untracked(self, reachable: set[str]) -> list[str]:
        """Répertoires du store absents de l'index (dépôts interrompus, restes)."""
        out = []
        for full in self.list_paths():
            if full in reachable or self.index.get(full) is not None:
                continue
            out.append(full)
        return out

    def orphans(self, reachable: set[str]) -> list[str]:
        """Chemins présents et créés par npkg mais plus atteignables depuis les racines."""
        out = []
        for key, entry in sorted(self.index.entries.items()):
            if key in reachable:
                continue
            if not entry.created:
                continue
            out.append(key)
        return out

    # -- écriture ----------------------------------------------------------
    def unpack_nar(self, nar_file: Path, store_path: str, *, cache: str = "") -> ValidEntry:
        """Dépaquette un NAR déjà vérifié dans le store, atomiquement."""
        digest, name = parse_store_path(store_path)
        target = Path(store_path)
        existing = self.index.get(store_path)
        if target.exists() or target.is_symlink():
            if existing is not None and existing.created:
                if target.is_dir() and not target.is_symlink():
                    shutil.rmtree(target)
                else:
                    target.unlink()
            else:
                raise FileExistsError(
            f"{store_path} existe deja et n'a pas ete depose par npkg"
        )
        staging = self.root / f".npkg-staging-{digest}-{int(time.time() * 1000) % 100000}"
        try:
            with open(nar_file, "rb") as fh:
                print(f"DEBUG STORE: {store_path} <- {nar_file}")
                stats = nar.extract(fh, staging)
            # un NAR de paquet a une racine « nominale » : le staging *est* le paquet
            os.replace(staging, target)
        finally:
            if staging.is_dir() and not staging.is_symlink():
                shutil.rmtree(staging, ignore_errors=True)
            elif staging.exists() or staging.is_symlink():
                staging.unlink(missing_ok=True)
        return ValidEntry(
            path=str(target),
            name=name,
            cache=cache,
            created=True,
            unpacked_at=time.time(),
            files=stats["files"] + stats["symlinks"],
            bytes=stats["bytes"],
        )

    def remove(self, store_path: str) -> bool:
        target = Path(store_path)
        entry = self.index.get(store_path)
        if entry is not None and not entry.created:
            return False  # pas à nous (ex. un vrai /nix/store de Nix)
        if target.is_dir():
            shutil.rmtree(target, ignore_errors=True)
        elif target.exists():
            target.unlink()
        self.index.drop(store_path)
        return True


def human_size(num: float) -> str:
    for unit in ("o", "Kio", "Mio", "Gio", "Tio"):
        if abs(num) < 1024.0:
            return f"{num:3.1f} {unit}" if unit != "o" else f"{int(num)} o"
        num /= 1024.0
    return f"{num:.1f} Pio"
