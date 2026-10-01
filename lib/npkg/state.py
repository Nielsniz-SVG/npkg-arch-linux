"""État, verrouillage et « profils » (environnements versionnés).

Tout l'état de npkg tient dans ``$NPKG_HOME`` (par défaut
``~/.local/state/npkg``)::

    state.json      racines installées (ce que l'utilisateur a demandé)
    valid.json      index des chemins du store dépaquetés + hachés
    profiles/
      default -> generations/14        (lien symbolique, swappé atomiquement)
      generations/14/.npkg-manifest.json
      generations/14/bin/hello          -> /nix/store/...-hello-2.12.3/bin/hello
    cache/narinfo/  narinfo mises en cache (TTL)
    tmp/            téléchargements en cours
    lock            flock : un seul npkg mutateur à la fois

Comme Nix, chaque mutation du profil produit une *génération* ; ``npkg
rollback`` n'a donc qu'à re-pointer ``default`` vers la génération d'avant.
"""

from __future__ import annotations

import contextlib
import errno
import fcntl
import json
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

STATE_VERSION = 1
PROFILE_DIRS = ("bin", "sbin")
KEEP_GENERATIONS = 20


@dataclass
class Root:
    name: str
    attr: str
    store_path: str
    version: str = ""
    installed: float = 0.0
    reason: str = "user"  # user | dependency
    closure: list[str] = field(default_factory=list)
    comment: str = ""

    def as_dict(self) -> dict:
        return {
            "attr": self.attr,
            "store_path": self.store_path,
            "version": self.version,
            "installed": self.installed,
            "reason": self.reason,
            "closure": self.closure,
            "comment": self.comment,
        }

    @classmethod
    def from_dict(cls, name: str, data: dict) -> "Root":
        return cls(
            name=name,
            attr=data.get("attr", name),
            store_path=data.get("store_path", ""),
            version=data.get("version", ""),
            installed=data.get("installed", 0.0),
            reason=data.get("reason", "user"),
            closure=list(data.get("closure") or []),
            comment=data.get("comment", ""),
        )


class Home:
    def __init__(self, root: Path | str | None = None):
        base = Path(root or os.environ.get("NPKG_HOME") or (Path.home() / ".local" / "state" / "npkg"))
        self.root = base
        self.state_file = base / "state.json"
        self.valid_file = base / "valid.json"
        self.cache_dir = base / "cache"
        self.narinfo_dir = base / "cache" / "narinfo"
        self.tmp_dir = base / "tmp"
        self.profiles_dir = base / "profiles"
        self.generations_dir = self.profiles_dir / "generations"
        self.lock_file = base / "lock"

    def ensure(self) -> None:
        for path in (self.root, self.cache_dir, self.narinfo_dir, self.tmp_dir, self.generations_dir):
            Path(path).mkdir(parents=True, exist_ok=True)

    def profile(self, name: str = "default") -> Path:
        return self.profiles_dir / name


class LockBusy(RuntimeError):
    pass


@contextlib.contextmanager
def locked(home: Home, shared: bool = False):
    home.ensure()
    fd = os.open(str(home.lock_file), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH if shared else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EACCES):
                raise LockBusy(
                    "un autre npkg travaille déjà (verrou "
                    f"{home.lock_file})"
                ) from exc
            raise
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


class State:
    def __init__(self, home: Home):
        self.home = home
        self.data: dict = {"version": STATE_VERSION, "roots": {}}
        self.load()

    def load(self) -> None:
        if self.home.state_file.exists():
            try:
                raw = json.loads(self.home.state_file.read_text() or "{}")
            except json.JSONDecodeError as exc:
                raise ValueError(f"{self.home.state_file} illisible : {exc}") from exc
            if raw.get("version") != STATE_VERSION:
                raw["version"] = STATE_VERSION
            self.data = {**self.data, **raw}
        self.data.setdefault("roots", {})

    def save(self) -> None:
        self.data["updated"] = time.time()
        tmp = self.home.state_file.with_suffix(".json.tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(self.data, indent=2, sort_keys=True) + "\n")
        os.replace(tmp, self.home.state_file)

    # -- racines -----------------------------------------------------------
    @property
    def roots(self) -> dict[str, Root]:
        return {name: Root.from_dict(name, blob) for name, blob in sorted(self.data["roots"].items())}

    def put_root(self, root: Root) -> None:
        self.data["roots"][root.name] = root.as_dict()

    def pop_root(self, name: str) -> Root | None:
        blob = self.data["roots"].pop(name, None)
        return Root.from_dict(name, blob) if blob else None

    @property
    def channel(self) -> str:
        return self.data.get("channel", "nixpkgs-unstable")

    @channel.setter
    def channel(self, value: str) -> None:
        self.data["channel"] = value

    @property
    def system(self) -> str:
        return self.data.get("system") or default_system()

    @system.setter
    def system(self, value: str) -> None:
        self.data["system"] = value


def default_system() -> str:
    import platform

    machine = platform.machine().lower()
    return {"x86_64": "x86_64", "amd64": "x86_64", "aarch64": "aarch64", "arm64": "aarch64"}.get(
        machine, machine
    ) + "-linux"


class Profiles:
    def __init__(self, home: Home, state: State):
        self.home = home
        self.state = state

    # -- construction ------------------------------------------------------
    @staticmethod
    def _gen_number(profile: str, dirname: str) -> int | None:
        """``default-14-20261001120000`` (profil contenant des ``-``) -> 14."""
        prefix = profile + "-"
        if not dirname.startswith(prefix):
            return None
        tail = dirname[len(prefix) :]
        try:
            return int(tail.split("-", 1)[0])
        except ValueError:
            return None

    def current_generation(self, profile: str = "default") -> int | None:
        link = self.home.profile(profile)
        if not (link.is_symlink() or link.exists()):
            return None
        name = Path(os.readlink(link)).name if link.is_symlink() else Path(link).name
        return self._gen_number(profile, name)

    def generations(self, profile: str = "default") -> list[dict]:
        out = []
        base = self.home.generations_dir
        if not base.exists():
            return out
        current = self.current_generation(profile)
        for entry in sorted(base.iterdir()):
            number = self._gen_number(profile, entry.name)
            if number is None or not entry.is_dir():
                continue
            manifest = {}
            mfile = entry / ".npkg-manifest.json"
            if mfile.exists():
                try:
                    manifest = json.loads(mfile.read_text())
                except json.JSONDecodeError:
                    manifest = {}
            out.append(
                {
                    "generation": number,
                    "path": str(entry),
                    "created": entry.stat().st_mtime,
                    "roots": manifest.get("roots", []),
                    "entries": manifest.get("entries", {}),
                    "conflicts": manifest.get("conflicts", []),
                    "current": current == number,
                }
            )
        return sorted(out, key=lambda item: item["generation"])

    def next_generation_number(self, profile: str = "default") -> int:
        gens = [item["generation"] for item in self.generations(profile)]
        return (max(gens) + 1) if gens else 1

    def build(self, profile: str = "default", *, store_paths: list[str] | None = None) -> dict:
        """(Re)construit une génération à partir des chemins du store donnés."""
        if store_paths is None:
            store_paths = [root.store_path for root in self.state.roots.values()]
        generation = self.next_generation_number(profile)
        stamp = time.strftime("%Y%m%d%H%M%S")
        gen_dir = self.home.generations_dir / f"{profile}-{generation}-{stamp}"
        gen_dir.mkdir(parents=True, exist_ok=True)
        conflicts: list[str] = []
        linked: dict[str, str] = {}
        for sub in PROFILE_DIRS:
            for sp in store_paths:
                src = Path(sp) / sub
                if not src.is_dir():
                    continue
                dst_dir = gen_dir / sub
                dst_dir.mkdir(parents=True, exist_ok=True)
                for entry in sorted(src.iterdir()):
                    target = dst_dir / entry.name
                    if target.exists() or target.is_symlink():
                        if os.readlink(target) != os.path.realpath(str(entry)) and target.is_symlink():
                            conflicts.append(f"{sub}/{entry.name}")
                        continue
                    target.symlink_to(os.path.realpath(str(entry)))
                    linked[f"{sub}/{entry.name}"] = str(entry)
        manifest = {
            "generation": generation,
            "created": time.time(),
            "roots": sorted(store_paths),
            "entries": linked,
            "conflicts": sorted(set(conflicts)),
        }
        (gen_dir / ".npkg-manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        self.switch(profile, generation)
        self.prune(profile)
        return manifest

    def switch(self, profile: str, generation: int) -> Path:
        gens = {item["generation"]: item["path"] for item in self.generations(profile)}
        if generation not in gens:
            raise FileNotFoundError(f"génération {generation} inconnue pour le profil {profile!r}")
        link = self.home.profile(profile)
        link.parent.mkdir(parents=True, exist_ok=True)
        tmp = link.with_name(f".{link.name}.tmp.{os.getpid()}")
        if tmp.is_symlink() or tmp.exists():
            tmp.unlink()
        tmp.symlink_to(os.path.relpath(gens[generation], link.parent))
        os.replace(tmp, link)
        return link

    def prune(self, profile: str = "default", keep: int = KEEP_GENERATIONS) -> list[str]:
        gens = self.generations(profile)
        removable = [item for item in gens if not item["current"]]
        removable.sort(key=lambda item: item["generation"], reverse=True)
        deleted = []
        for item in removable[keep - 1 :]:
            shutil.rmtree(item["path"], ignore_errors=True)
            deleted.append(item["path"])
        return deleted

    def env_script(self, profile: str = "default", shell: str = "sh", *, after: bool = False) -> str:
        """Ligne de shell qui branche le profil sur le ``PATH``.

        ``after=True`` met le profil *après* le PATH système. Sur une
        distribution qui a déjà ``ls``, ``cat`` ou ``grep``, c'est en général ce
        qu'on veut : sinon un paquet npkg installe les siens et les masque.
        """
        bindir = str(self.home.profile(profile) / "bin")
        if shell == "fish":
            ordre = f"$PATH {bindir}" if after else f"{bindir} $PATH"
            return f"set -gx PATH {ordre}"
        if shell == "csh":
            ordre = f"$PATH:{bindir}" if after else f"{bindir}:$PATH"
            return f'setenv PATH "{ordre}"'
        ordre = f"$PATH:{bindir}" if after else f"{bindir}:$PATH"
        return f'export PATH="{ordre}"'
