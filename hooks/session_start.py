#!/usr/bin/env python3
"""Hook SessionStart : injecte le briefing du projet courant dans le contexte.

Claude Code fournit le répertoire de travail sur l'entrée standard ; on le
résout en projet côté serveur et on renvoie l'état complet. Résultat : la
session démarre en sachant déjà où on en est, sans avoir à appeler d'outil.

Le hook note aussi l'heure de départ de la session : c'est la borne à partir de
laquelle le hook Stop cherchera des traces d'écriture.

Il ne doit jamais faire échouer une session : toute erreur se traduit par une
absence de contexte supplémentaire.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _common import api_get, env_value, now_iso, read_payload, save_state  # noqa: E402


def emit(context: str) -> None:
    json.dump({"hookSpecificOutput": {"hookEventName": "SessionStart",
                                      "additionalContext": context}}, sys.stdout)
    sys.stdout.write("\n")


def main() -> None:
    payload = read_payload(sys.stdin)
    cwd = payload.get("cwd") or ""
    session_id = payload.get("session_id") or ""
    if not cwd:
        return

    # On injecte sur TOUTES les reprises, « resume » compris. La tentation
    # d'économiser là est mauvaise : une session reprise a pu dormir des heures,
    # et le briefing de sa transcription décrit alors un état périmé. Pire, une
    # session ouverte avant l'existence du manager n'en contient aucun — cas
    # rencontré pour de bon sur mare. La fraîcheur de l'état prime sur les
    # ~1 500 tokens d'un doublon.
    save_state(session_id, started_at=now_iso(), cwd=cwd)

    status, body = api_get("/api/briefing", {"path": cwd})
    # 404 : le répertoire ne correspond à aucun projet enregistré. Ce n'est pas
    # une anomalie — la plupart des dossiers n'en sont pas un.
    if status != 200 or not body:
        return

    public = env_value("CM_PUBLIC_URL")
    emit("État du projet, fourni par claude-manager"
         + (f" ({public})" if public else "") + " :\n\n" + body)


if __name__ == "__main__":
    main()
