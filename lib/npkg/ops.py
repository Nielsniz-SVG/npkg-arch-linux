"""Le moteur : planifier, installer, désinstaller, vérifier, ramasser.

Un ``npkg install`` se déroule toujours de la même façon :

1. **résolution** — nom nixpkgs -> chemin du store (voir :mod:`npkg.resolver`),
2. **fermeture** — parcours récursif des ``References`` des ``.narinfo``,
3. **plan** — ce qui manque vraiment, taille totale, conflits de ``bin/``,
4. **téléchargement** — NAR compressé, ``FileHash`` puis ``NarHash`` vérifiés,
5. **dépaquetage** — extraction NAR dans le store (atome : ``os.replace``),
6. **commit** — ``state.json`` + nouvelle génération du profil.

Les étapes 1-3 sont sans effet de bord : ``--dry-run`` s'arrête là. Le store
n'est touché qu'à l'étape 5, et le ``PATH`` de l'utilisateur seulement à la 6,
d'où un Ctrl-C sans demi-mesure (au pire un NAR orphelin dans ``tmp/``).
"""

from __future__ import annotations

import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from . import closure as closure_mod
from . import nar, zstd
from .cache import Substituters, substituters as default_substituters
from .hashing import sha256_bytes
from .narinfo import Narinfo
from .resolver import Resolution, Resolver, split_spec
from .state import Home, Profiles, Root, State, locked
from .store import Store, ValidEntry, ValidIndex, dir_size, human_size, parse_store_path

DEFAULT_CHANNEL = os.environ.get("NPKG_CHANNEL", "nixpkgs-unstable")
_INDEX_LOCK = threading.Lock()


def guess_system() -> str:
    import platform

    machine = {
        "x86_64": "x86_64", "amd64": "x86_64",
        "aarch64": "aarch64", "arm64": "aarch64",
    }.get(platform.machine().lower(), platform.machine().lower())
    osname = "darwin" if platform.system() == "Darwin" else "linux"
    return f"{machine}-{osname}"


@dataclass
class Options:
    home: Path | None = None
    store_dir: Path | None = None
    channel: str = DEFAULT_CHANNEL
    system: str | None = None
    jobset: str | None = None
    caches: list[str] | None = None
    workers: int = 4
    verify: bool = True
    dry_run: bool = False
    use_nix: bool | None = None
    quiet: bool = False
    keep_nar: bool = False

    @classmethod
    def from_env(cls, **overrides) -> "Options":
        opts = cls()
        for key, value in overrides.items():
            if value is not None:
                setattr(opts, key, value)
        if opts.store_dir is None:
            opts.store_dir = _store_dir_from_env(opts)
        if opts.home is None:
            opts.home = Home().root
        if opts.system is None:
            opts.system = os.environ.get("NPKG_SYSTEM") or guess_system()
        workers = os.environ.get("NPKG_JOBS")
        if workers and opts.workers == 4:
            opts.workers = max(1, int(workers))
        return opts


def _store_dir_from_env(opts: "Options") -> Path:
    override = os.environ.get("NPKG_STORE_DIR")
    if override:
        return Path(override)
    default = Path("/nix/store")
    if default.is_dir() and os.access(default, os.W_OK):
        return default
    return Home(opts.home).root / "store"


@dataclass
class Plan:
    resolutions: list[Resolution] = field(default_factory=list)
    nodes: dict[str, closure_mod.Node] = field(default_factory=dict)
    order: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    already: list[str] = field(default_factory=list)
    total_bytes: int = 0
    conflicts: list[str] = field(default_factory=list)
    per_root: dict[str, list[str]] = field(default_factory=dict)


@dataclass
class Report:
    installed: list[str] = field(default_factory=list)
    resolutions: list[Resolution] = field(default_factory=list)
    fetched: list[str] = field(default_factory=list)
    bytes_downloaded: int = 0
    bytes_on_disk: int = 0
    seconds: float = 0.0
    removed: list[str] = field(default_factory=list)
    freed: int = 0
    notes: list[str] = field(default_factory=list)
    generation: int | None = None


class Engine:
    def __init__(self, options: Options | None = None, *, log: Callable[[str], None] | None = None):
        self.opts = options or Options.from_env()
        self.home = Home(self.opts.home)
        self.home.ensure()
        self.log = log or (lambda text: None if self.opts.quiet else print(text))
        self.state = State(self.home)
        self.valid = ValidIndex(self.home.valid_file)
        self.store = Store(Path(self.opts.store_dir), self.valid)
        self.store.root.mkdir(parents=True, exist_ok=True)
        self.profiles = Profiles(self.home, self.state)
        self.subs = Substituters(self.opts.caches or default_substituters(), self.home.narinfo_dir)
        self.resolver = Resolver(
            self.home.root,
            channel=self.opts.channel,
            system=self.opts.system or guess_system(),
            use_nix=self.opts.use_nix,
            store_dir=self.store.root,
            http_cache_dir=self.home.cache_dir / "hydra",
            jobset=self.opts.jobset,
        )

    # -------------------------------------------------------------- bas niveau
    def lookup(self, digest: str) -> Narinfo | None:
        """narinfo d'un chemin, ramenée dans *notre* store si le miroir n'y est pas.

        cache.nixos.org parle le store ``/nix/store`` ; si npkg est configuré sur
        un autre répertoire, on réécrit store_path et references pour rester
        cohérent (les binaires liés en dur sur /nix/store ne tourneront pas pour
        autant — `npkg doctor` le dit).
        """
        ni = self.subs.lookup(digest)
        if ni is None:
            return None
        root = str(self.store.root).rstrip("/")
        if root and root != "/nix/store":
            ni.store_path = f"{root}/{Path(ni.store_path).name}"
            ni.references = [f"{root}/{Path(ref).name}" for ref in ni.references]
            ni.store_dir = root
        return ni

    def narinfo_for(self, store_path: str) -> Narinfo | None:
        return self.lookup(parse_store_path(store_path)[0])

    def compute_closure(self, roots: Iterable[str], *, on_progress=None) -> dict[str, closure_mod.Node]:
        return closure_mod.compute(
            list(roots), self.lookup, workers=max(1, self.opts.workers), on_progress=on_progress
        )

    # -------------------------------------------------------------- planification
    def resolve_specs(self, specs: list[str], *, refresh: bool = False) -> list[Resolution]:
        """Résout chaque spec et avertit si le build canéral est trop ancien.

        Un attribut retiré du canal laisse parfois derrière lui un vieux job
        Hydra : sans ce contrôle, ``npkg install`` pourrait installer un binaire
        de 2016 en silence.
        """
        limit = float(os.environ.get("NPKG_STALE_DAYS", "120"))
        out: list[Resolution] = []
        warnings: list[str] = []
        for spec in specs:
            res = self.resolver.resolve(spec, refresh=refresh)
            if res.source == "hydra":
                if res.build_age_days and res.build_age_days > limit:
                    warnings.append(
                        f"{res.attr} : dernier build Hydra du {res.build_date or '?'}, soit "
                        f"≈{res.build_age_days / 365:.1f} an(s) — attribut peut-être renommé, "
                        "comparez avec `npkg search`"
                    )
                elif res.build_date:
                    warnings.append(
                        f"{res.attr} : build du {res.build_date} sur {res.jobset}"
                        + (f", nixpkgs {res.rev[:10]}" if res.rev else "")
                    )
            out.append(res)
        self.plan_warnings = warnings
        return out

    def plan(self, resolutions: list[Resolution]) -> Plan:
        plan = Plan(resolutions=resolutions)
        roots = [res.store_path for res in resolutions]
        nodes = self.compute_closure(roots)
        order = closure_mod.topological(nodes)
        plan.nodes = nodes
        plan.order = order + [p for p in sorted(nodes) if p not in order]
        plan.total_bytes = closure_mod.total_nar_size(nodes)
        for path in plan.order:
            entry = self.valid.get(path)
            if entry is not None and Path(path).exists():
                plan.already.append(path)
            else:
                plan.missing.append(path)
        for res in resolutions:
            plan.per_root[res.name] = reachable_from(nodes, res.store_path)
        plan.conflicts = self._profile_conflicts(roots)
        return plan

    def _profile_conflicts(self, new_roots: list[str]) -> list[str]:
        """Commandes que le nouveau paquet voudrait exposer mais qui sont déjà prises.

        Le profil est un espace plat (``bin/``, ``sbin/``), comme un ``/usr/bin`` :
        le premier arrivé garde la place (ordre = ordre alphabétique des racines).
        npkg ne casse jamais un lien existant en silence — il l'annonce ici.
        """

        def listing(paths: list[str]) -> dict[str, str]:
            found: dict[str, str] = {}
            for sp in sorted(paths):
                for sub in ("bin", "sbin"):
                    base = Path(sp) / sub
                    if base.is_dir():
                        for entry in sorted(base.iterdir()):
                            found.setdefault(f"{sub}/{entry.name}", sp)
            return found

        keep = [root.store_path for root in self.state.roots.values()]
        keep += [root.store_path for root in self.state.roots.values() if root.store_path in new_roots]
        existing = listing(sorted(set(keep)))
        report = []
        for sp in sorted(set(new_roots) - set(keep)):
            for name, owner in sorted(listing([sp]).items()):
                holder = existing.get(name)
                if holder and holder != sp:
                    report.append(
                        f"{name} : déjà fourni par {Path(holder).name} — "
                        f"celui de {Path(sp).name} sera ignoré par le profil"
                    )
        return report

    # -------------------------------------------------------------- installation
    def install(self, specs: list[str], *, comment: str = "", refresh: bool = False) -> Report:
        started = time.time()
        report = Report()
        resolutions = self.resolve_specs(specs, refresh=refresh)
        report.resolutions = resolutions
        report.notes.extend(getattr(self, "plan_warnings", []))
        plan = self.plan(resolutions)
        report.bytes_on_disk = plan.total_bytes
        if self.opts.dry_run:
            report.installed = [res.name for res in resolutions]
            report.fetched = list(plan.missing)
            report.notes.append("dry-run : rien n'a été téléchargé ni écrit")
            report.notes.extend(plan.conflicts)
            return report
        with locked(self.home):
            self.state.load()
            self.valid.load()
            for res in resolutions:
                existing = self.state.roots.get(res.name)
                if existing and existing.store_path != res.store_path:
                    report.notes.append(
                        f"{res.name} : {existing.version or '?'} -> {res.version or '?'}"
                    )
            self._ensure_many(plan, report)
            for res in resolutions:
                self.state.put_root(
                    Root(
                        name=res.name,
                        attr=res.attr,
                        store_path=res.store_path,
                        version=res.version,
                        installed=time.time(),
                        reason="user",
                        closure=plan.per_root.get(res.name, [res.store_path]),
                        comment=comment or res.description,
                    )
                )
            self.state.channel = self.opts.channel
            self.state.system = self.opts.system or guess_system()
            self.state.save()
            manifest = self.profiles.build()
            self.valid.save()
        report.installed = [res.name for res in resolutions]
        report.generation = manifest.get("generation")
        report.seconds = time.time() - started
        report.notes.extend(f"conflit de profil : {c}" for c in manifest.get("conflicts", []))
        return report

    def _ensure_many(self, plan: Plan, report: Report) -> None:
        targets = [path for path in plan.missing if path in plan.nodes]
        if not targets:
            self.log("  tout est déjà dans le store")
            return
        tmp = self.home.tmp_dir
        tmp.mkdir(parents=True, exist_ok=True)
        self.log(f"  {len(targets)} chemin(s) à dépaqueter ({human_size(plan.total_bytes)} au total)")

        def work(store_path: str) -> tuple[str, int]:
            ni = self.narinfo_for(store_path)
            if ni is None:
                raise LookupError(f"narinfo introuvable pour {store_path}")
            target = Path(store_path)
            known = self.valid.get(store_path)
            if target.is_dir() and (known is None or not known.created):
                # un chemin déjà là (posé par Nix, par un ami, par une exécution
                # antérieure) : on l'adopte si son contenu correspond au NAR
                # attendu, sinon on refuse d'écraser quoi que ce soit.
                if not ni.nar_hash or nar_hash_of_dir(target) != ni.nar_hash:
                    raise ValueError(
                        f"{store_path} existe avec un contenu divergent ; "
                        "supprimez-le (npkg gc, npkg repair) ou changez de store"
                    )
                entry = known or ValidEntry(path=store_path, name=Path(store_path).name)
                entry.created = False
                entry.nar_hash = ni.nar_hash
                entry.nar_size = ni.nar_size
                entry.refs = [ref for ref in ni.references if ref != store_path]
                entry.cache = ni.cache_url
                with _INDEX_LOCK:
                    self.valid.put(entry)
                self.log(f"  adopté (déjà présent, contenu conforme) : {Path(store_path).name}")
                return store_path, 0
            result = self.subs.fetch(ni, tmp, verify=self.opts.verify, progress=not self.opts.quiet)
            entry = self.store.unpack_nar(result.nar_file, store_path, cache=result.cache)
            entry.nar_hash = ni.nar_hash
            entry.nar_size = ni.nar_size or result.nar_size
            entry.refs = [ref for ref in ni.references if ref != store_path]
            with _INDEX_LOCK:
                self.valid.put(entry)
            if not self.opts.keep_nar:
                result.nar_file.unlink(missing_ok=True)
            return store_path, entry.nar_size or 0

        with ThreadPoolExecutor(max_workers=max(1, self.opts.workers)) as pool:
            futures = {pool.submit(work, path): path for path in targets}
            done = 0
            for future in as_completed(futures):
                path = futures[future]
                _, size = future.result()  # propage les erreurs (hash divergent, 404...)
                done += 1
                report.fetched.append(path)
                report.bytes_downloaded += size
                self.log(f"  [{done:>3}/{len(futures)}] {Path(path).name:<34} {human_size(size)}")
        self.valid.save()

    def invalidate(self, store_path: str) -> None:
        """Oublie un chemin du store (et le supprime s'il est abîmé) pour le refaire."""
        import shutil

        with locked(self.home):
            entry = self.valid.get(store_path)
            path = Path(store_path)
            if path.exists() and (entry is None or entry.created):
                shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink(missing_ok=True)
            self.valid.drop(store_path)
            self.valid.save()

    # -------------------------------------------------------------- désinstallation
    def uninstall(self, names: list[str], *, rebuild: bool = True) -> Report:
        started = time.time()
        report = Report()
        with locked(self.home):
            self.state.load()
            for name in names:
                root = self.state.pop_root(name)
                if root is None:
                    raise LookupError(f"{name} n'est pas une racine (voir `npkg list`)")
                report.removed.append(name)
            self.state.save()
            manifest = self.profiles.build() if rebuild else {}
            self.valid.save()
        report.generation = manifest.get("generation")
        report.seconds = time.time() - started
        report.notes.append("les chemins restent dans le store : `npkg gc` les libère")
        return report

    # -------------------------------------------------------------- gc
    def gc(self, *, dry_run: bool = False) -> Report:
        started = time.time()
        report = Report()
        with locked(self.home):
            self.state.load()
            reachable: set[str] = set()
            for root in self.state.roots.values():
                reachable.add(root.store_path)
                reachable.update(root.closure)
            # toute dépendance d'un chemin vivant est vivante (transitivement)
            stack = list(reachable)
            while stack:
                entry = self.valid.get(stack.pop())
                if entry is None:
                    continue
                for ref in entry.refs:
                    if ref not in reachable:
                        reachable.add(ref)
                        stack.append(ref)
            # on ne supprime QUE ce que npkg a déposé lui-même (flag `created`) :
            # dans un /nix/store partagé avec Nix, les chemins des autres appartiennent
            # à la base de Nix et seraient ressuscités / nécessaires ailleurs.
            doomed = sorted(set(self.store.orphans(reachable)))
            strangers = sorted(set(self.store.untracked(reachable)))
            reclaimed = 0
            for path in doomed:
                target = Path(path)
                reclaimed += dir_size(target) if target.is_dir() else (target.stat().st_size if target.exists() else 0)
                if dry_run:
                    report.notes.append(path)
                    continue
                self.store.remove(path)
            if strangers:
                report.notes.append(
                    f"{len(strangers)} chemin(s) du store inconnus de npkg laissés en paix : "
                    + ", ".join(Path(p_).name for p_ in strangers[:3])
                    + (" …" if len(strangers) > 3 else "")
                )
            if not dry_run:
                self.valid.save()
                report.notes.insert(0, f"{len(doomed)} chemin(s) supprimés")
            else:
                report.notes.insert(0, f"{len(doomed)} chemin(s) libérables")
            report.freed = reclaimed
            leftovers = [f for f in self.home.tmp_dir.iterdir() if f.name.endswith((".blob", ".nar"))]
            for f in leftovers:
                f.unlink(missing_ok=True)
            if leftovers:
                report.notes.append(f"{len(leftovers)} archive(s) temporaires nettoyées")
        report.seconds = time.time() - started
        return report

    # -------------------------------------------------------------- vérification
    def verify(self, names: Iterable[str] | None = None, *, deep: bool = True) -> list[dict]:
        """Présence, dépendances résolues et ``NarHash`` recalculé depuis le disque."""
        if names is None:
            paths = [root.store_path for root in self.state.roots.values()]
        else:
            paths = [self.root_path(name) for name in names]
        results = []
        for store_path in paths:
            item = {"path": store_path, "ok": True, "checks": []}
            entry = self.valid.get(store_path)
            if entry is None:
                item["ok"] = False
                item["checks"].append("inconnu de valid.json (déposé hors npkg)")
                results.append(item)
                continue
            if not Path(store_path).exists():
                item["ok"] = False
                item["checks"].append("absent du disque")
                results.append(item)
                continue
            item["checks"].append("présent")
            for ref in entry.refs:
                if not Path(ref).exists():
                    item["ok"] = False
                    item["checks"].append(f"dépendance absente : {Path(ref).name}")
            if deep and entry.nar_hash:
                got = nar_hash_of_dir(Path(store_path))
                if got == entry.nar_hash:
                    item["checks"].append("NarHash recalculé identique")
                else:
                    item["ok"] = False
                    item["checks"].append("NarHash divergent : contenu modifié")
            results.append(item)
        return results

    # -------------------------------------------------------------- lecture
    def status(self) -> dict:
        roots = self.state.roots
        total = sum(
            (entry.nar_size or 0)
            for entry in self.valid.entries.values()
            if Path(entry.path).exists()
        )
        gens = self.profiles.generations()
        return {
            "home": str(self.home.root),
            "store": str(self.store.root),
            "store_writable": os.access(self.store.root, os.W_OK),
            "nix_store_layout": str(self.store.root) == "/nix/store",
            "channel": self.state.channel,
            "system": self.state.system,
            "roots": sorted(roots),
            "store_paths": len(self.store.list_paths()),
            "store_bytes": total,
            "generation": gens[-1]["generation"] if gens else None,
            "profile": str(self.home.profile()),
            "backends": self.resolver.describe_backends(),
            "substituters": self.subs.describe(),
            "zstd": zstd.zstd_backend(),
            "nix_available": bool(self.resolver.use_nix),
        }

    def root_path(self, spec: str) -> str:
        roots = self.state.roots
        if spec in roots:
            return roots[spec].store_path
        if self.resolver.looks_like_store_path(spec):
            return spec if spec.startswith("/") else str(self.store.root / spec)
        found = [r for name, r in roots.items() if spec in name]
        if len(found) == 1:
            return found[0].store_path
        if len(found) > 1:
            raise LookupError(f"{spec} est ambigu : " + ", ".join(sorted(r.name for r in found)))
        return self.resolver.resolve(spec).store_path

    def closure_of(self, spec: str) -> tuple[str, dict[str, closure_mod.Node]]:
        store_path = self.root_path(spec)
        return store_path, self.compute_closure([store_path])

    def size_of(self, spec: str) -> int:
        _, nodes = self.closure_of(spec)
        return closure_mod.total_nar_size(nodes)

    def which(self, program: str) -> Path | None:
        for sub in ("bin", "sbin"):
            candidate = self.home.profile() / sub / program
            if candidate.exists() or candidate.is_symlink():
                return candidate
        return None


# ------------------------------------------------------------------ helpers
def reachable_from(nodes: dict[str, closure_mod.Node], root: str) -> list[str]:
    """Fermeture de ``root`` dans un graphe déjà calculé (tri topologique)."""
    seen: set[str] = set()
    stack = [root]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        node = nodes.get(current)
        if node is not None:
            stack.extend(node.children)
    return [p for p in closure_mod.topological({p: nodes[p] for p in seen if p in nodes})]


def nar_hash_of_dir(path: Path) -> str:
    """Recalcule le ``NarHash`` canonique d'un répertoire du store."""
    return sha256_bytes(nar.dump_dir(path))


def format_root(root: Root) -> str:
    version = f" {root.version}" if root.version else ""
    return f"{root.name}{version}  ->  {Path(root.store_path).name}"


__all__ = ["Engine", "Options", "Plan", "Report", "guess_system", "human_size", "split_spec"]
