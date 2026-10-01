"""Calcul de fermeture (closure) à partir des ``References`` des narinfo.

La fermeture d'un paquet, c'est son graphe de dépendances *réelles* : chaque
sortie du store liste dans son ``.narinfo`` les chemins qu'elle référence
(absolument : liens d'édition de liens, interpréteurs, bibliothèques, données).
En parcourant ces listes récursivement on obtient exactement l'ensemble des
chemins à dépaqueter pour que le binaire fonctionne — c'est la « résolution de
dépendances » de npkg, sans rien réévaluer.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from .narinfo import Narinfo


class ClosureError(RuntimeError):
    pass


@dataclass
class Node:
    store_path: str
    narinfo: Narinfo
    parents: set[str] = field(default_factory=set)
    children: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return self.narinfo.name

    @property
    def size(self) -> int:
        return self.narinfo.nar_size or 0


Lookup = Callable[[str], "Narinfo | None"]


def _digest_of(store_path: str) -> str:
    base = store_path.rstrip("/").rsplit("/", 1)[-1]
    return base.split("-", 1)[0]


def compute(
    roots: list[str],
    lookup: Lookup,
    *,
    workers: int = 8,
    on_progress: Callable[["Node"], None] | None = None,
) -> dict[str, "Node"]:
    """Parcourt en largeur la fermeture de ``roots`` via ``lookup(digest)``.

    ``lookup`` reçoit le *hashPart* d'un chemin du store et renvoie sa narinfo
    (ou ``None``). Les références sont exploitées telles quelles : aucun calcul
    de dépendances théorique, on suit ce que le store déclare vraiment.
    """
    nodes: dict[str, Node] = {}
    frontier: list[str] = []
    seen: set[str] = set()
    for root in roots:
        if root not in seen:
            seen.add(root)
            frontier.append(root)

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        while frontier:
            batch, frontier = frontier, []
            futures = {pool.submit(lookup, _digest_of(path)): path for path in batch}
            for future, requested in futures.items():
                ni = future.result()
                if ni is None:
                    raise ClosureError(
                        f"aucun substituer ne connaît {requested}\n"
                        "  (miroir pas encore renseigné, paquet non libre, ou nom incorrect)"
                    )
                node = Node(store_path=ni.store_path, narinfo=ni)
                nodes[ni.store_path] = node
                if on_progress is not None:
                    on_progress(node)
                store_dir = (ni.store_dir or "/nix/store").rstrip("/")
                for child in ni.references:
                    child = child if child.startswith("/") else f"{store_dir}/{child}"
                    if child not in nodes and child not in seen:
                        seen.add(child)
                        frontier.append(child)

    # câblage inverse : qui dépend de qui (pour `npkg refs` et le gc)
    for path, node in nodes.items():
        store_dir = (node.narinfo.store_dir or "/nix/store").rstrip("/")
        node.children = [c if c.startswith("/") else f"{store_dir}/{c}" for c in node.narinfo.references]
        for child in node.children:
            target = nodes.get(child)
            if target is not None and child != path:
                target.parents.add(path)
    return nodes


def topological(nodes: dict[str, Node]) -> list[str]:
    """Dépendances d'abord (utile pour un affichage lisible)."""
    ordered: list[str] = []
    seen: set[str] = set()

    def visit(path: str, stack: frozenset = frozenset()) -> None:
        if path in seen or path in stack:
            return
        node = nodes.get(path)
        if node is None:
            return
        for child in node.children:
            if child != path:
                visit(child, frozenset(set(stack) | {path}))
        seen.add(path)
        ordered.append(path)

    for path in sorted(nodes):
        visit(path)
    return ordered


def total_nar_size(nodes: dict[str, Node]) -> int:
    return sum(node.size for node in nodes.values() if node is not None)


