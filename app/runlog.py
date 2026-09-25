"""Lecture du flux d'un agent.

Le démon lance `claude -p --output-format stream-json` : chaque ligne du journal
est un événement JSON. Brut, c'est illisible. Ici on le transforme en une suite
d'étapes racontant le travail — ce que l'agent a dit, ce qu'il a touché, et ce
que ça a répondu.
"""
import json
import re
from collections import Counter
from pathlib import Path

MAX_RESULT = 1200
MAX_TEXT = 6000

# Catégories d'outils, pour que la lecture se fasse à la couleur plutôt qu'au mot.
CATEGORIES = {
    "read": {"Read", "Glob", "Grep", "NotebookRead", "WebFetch", "WebSearch", "ListAgents"},
    "write": {"Edit", "Write", "NotebookEdit", "MultiEdit"},
    "shell": {"Bash", "BashOutput", "KillShell"},
    "task": {"Task", "TaskCreate", "TaskUpdate", "TaskList", "Agent", "Skill"},
}
# L'ordre compte : on cherche d'abord ce qui décrit l'intention (une commande,
# un fichier, un résumé) avant de retomber sur un identifiant, qui ne dit rien.
LABEL_KEYS = ("command", "file_path", "pattern", "path", "url", "summary",
              "title", "subject", "name", "query", "prompt", "slug",
              "project", "task_id")


def categorise(name: str) -> str:
    if name.startswith("mcp__"):
        return "mcp"
    for category, names in CATEGORIES.items():
        if name in names:
            return category
    return "other"


def short_name(name: str) -> str:
    """`mcp__claude-manager__update_task` se lit mieux en `update_task`."""
    return name.rsplit("__", 1)[-1] if name.startswith("mcp__") else name


def label_of(payload: dict) -> str:
    for key in LABEL_KEYS:
        value = payload.get(key)
        if isinstance(value, (str, int)) and str(value).strip():
            text = " ".join(str(value).split())
            return text if len(text) <= 150 else text[:149] + "…"
    return ""


def parse(path: str | None) -> dict:
    """Renvoie {steps, meta}. Tolère un journal tronqué : l'agent écrit encore."""
    steps: list[dict] = []
    meta: dict = {"model": None, "duration_ms": None, "turns": None,
                  "cost": None, "final": None, "lines": 0}
    if not path or not Path(path).exists():
        return {"steps": steps, "meta": meta}

    by_tool_id: dict[str, dict] = {}
    for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw:
            continue
        meta["lines"] += 1
        if not raw.startswith("{"):
            # L'en-tête écrit par le démon, ou une sortie hors format.
            steps.append({"kind": "note", "text": raw})
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue  # ligne en cours d'écriture

        kind = event.get("type")
        if kind == "system":
            meta["model"] = event.get("model") or meta["model"]
        elif kind == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "text":
                    text = (block.get("text") or "").strip()
                    if text:
                        steps.append({"kind": "text", "text": text[:MAX_TEXT]})
                elif block.get("type") == "tool_use":
                    name = block.get("name", "?")
                    step = {"kind": "tool", "name": short_name(name),
                            "category": categorise(name),
                            "label": label_of(block.get("input") or {}),
                            "result": None, "error": False}
                    steps.append(step)
                    by_tool_id[block.get("id")] = step
        elif kind == "user":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") != "tool_result":
                    continue
                step = by_tool_id.get(block.get("tool_use_id"))
                if step is None:
                    continue
                content = block.get("content")
                if isinstance(content, list):
                    content = " ".join(c.get("text", "") for c in content
                                       if isinstance(c, dict))
                text = " ".join(str(content or "").split())
                step["result"] = text[:MAX_RESULT] or None
                step["error"] = bool(block.get("is_error"))
        elif kind == "result":
            meta["duration_ms"] = event.get("duration_ms")
            meta["turns"] = event.get("num_turns")
            meta["cost"] = event.get("total_cost_usd")
            meta["final"] = (event.get("result") or "")[:MAX_TEXT] or None

    # Le message de clôture reprend souvent mot pour mot la dernière chose dite
    # par l'agent : l'afficher deux fois n'apporte rien.
    last_text = next((s["text"] for s in reversed(steps) if s["kind"] == "text"), None)
    if meta["final"] and last_text and meta["final"].strip() == last_text.strip():
        meta["final"] = None

    return {"steps": steps, "meta": meta}


WRITE_TOOLS = {"Edit", "Write", "NotebookEdit", "MultiEdit"}


def written_paths(path: str | None) -> list[str]:
    """Les fichiers que l'agent a lui-même écrits, d'après son propre journal.

    Sert à ne commiter QUE son travail : si une session interactive modifie le
    même dépôt pendant qu'il tourne, ses fichiers ne doivent pas partir dans le
    commit de l'agent, sous son message.
    """
    found: list[str] = []
    if not path or not Path(path).exists():
        return found
    for raw in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        raw = raw.strip()
        if not raw.startswith("{") or '"tool_use"' not in raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []):
            if block.get("type") != "tool_use" or block.get("name") not in WRITE_TOOLS:
                continue
            target = (block.get("input") or {}).get("file_path")
            if target and target not in found:
                found.append(target)
    return found


def repeated_tail(path: str | None, window: int = 20, threshold: int = 12) -> str | None:
    """Un même appel qui domine la fin du journal : l'agent tourne en rond.

    Cas vu en vrai : un agent sondant `wc -c` sur un fichier de sortie, en
    attente d'une commande de fond qui n'arrivait jamais — 251 appels et trente
    minutes perdues avant que le délai maximal ne le coupe.

    On ne cherche pas des appels **consécutifs** identiques : la sonde était
    entrelacée avec d'autres commandes, et un test strict ne voyait rien. On
    regarde donc la commande **dominante** des derniers appels.

    On ne lit que la fin du journal : il peut peser plusieurs mégaoctets.
    """
    if not path or not Path(path).exists():
        return None
    lignes = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-500:]
    appels: list[str] = []
    for raw in lignes:
        raw = raw.strip()
        if not raw.startswith("{") or '"tool_use"' not in raw:
            continue
        try:
            event = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if event.get("type") != "assistant":
            continue
        for block in event.get("message", {}).get("content", []):
            if block.get("type") == "tool_use":
                appels.append(f"{block.get('name')} {label_of(block.get('input') or {})}")
    if len(appels) < window:
        return None
    derniers = appels[-window:]
    dominant, occurrences = Counter(derniers).most_common(1)[0]
    return dominant if occurrences >= threshold else None


def resume(path: str | None, limite: int = 600) -> str:
    """Le mot de la fin de l'agent, en texte simple.

    Sert au message envoyé sur le téléphone : « tests verts » dit qu'il faut
    aller voir, pas ce qui a été fait. Le message de clôture, lui, le dit.

    On ne lit que la **fin** du journal — il pèse couramment plusieurs
    mégaoctets, et le résumé est toujours dans les dernières lignes. On préfère
    l'événement `result`, qui porte la conclusion ; à défaut, le dernier texte.
    """
    if not path or not Path(path).exists():
        return ""
    lignes = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[-400:]
    final = dernier_texte = ""
    for brute in lignes:
        brute = brute.strip()
        if not brute.startswith("{"):
            continue
        try:
            event = json.loads(brute)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "result":
            final = event.get("result") or final
        elif event.get("type") == "assistant":
            for bloc in event.get("message", {}).get("content", []):
                if bloc.get("type") == "text" and (bloc.get("text") or "").strip():
                    dernier_texte = bloc["text"].strip()
    texte = en_texte_simple(final or dernier_texte)
    return texte if len(texte) <= limite else texte[:limite - 1].rstrip() + "…"


# Le markdown d'un agent lu sur un téléphone : les astérisques et les dièses y
# sont du bruit. On ne rend rien, on retire les marques.
_MARQUES = [
    (re.compile(r"^#{1,6}\s+", re.M), ""),        # titres
    (re.compile(r"\*\*(.+?)\*\*", re.S), r"\1"),  # gras
    (re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", re.S), r"\1"),  # italique
    (re.compile(r"`{1,3}([^`]*)`{1,3}", re.S), r"\1"),  # code
    (re.compile(r"^\s*[-*+]\s+", re.M), "· "),    # puces
    (re.compile(r"\[(.+?)\]\((.+?)\)"), r"\1"),   # liens
    # Ligne de séparation d'un tableau : pur bruit une fois la mise en forme
    # perdue. Les lignes de données restent — elles portent le sens.
    (re.compile(r"^\s*\|?[\s:|-]*\|[\s:|-]*$\n?", re.M), ""),
    (re.compile(r"\n{3,}"), "\n\n"),
]


def en_texte_simple(texte: str) -> str:
    for motif, remplacement in _MARQUES:
        texte = motif.sub(remplacement, texte or "")
    return texte.strip()


def counts(steps: list[dict]) -> dict:
    """Combien de lectures, d'écritures, de commandes — le résumé d'un coup d'œil."""
    tally: dict[str, int] = {}
    for step in steps:
        if step["kind"] == "tool":
            tally[step["category"]] = tally.get(step["category"], 0) + 1
    return tally
