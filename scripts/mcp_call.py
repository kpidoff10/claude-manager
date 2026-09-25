#!/usr/bin/env python3
"""Appelle un outil MCP en ligne de commande. Sert au test et à l'amorçage.

    python3 scripts/mcp_call.py get_briefing '{"project": "claude-manager"}'
"""
import json
import os
import sys
import urllib.request
from pathlib import Path

ENV_PATH = Path(__file__).resolve().parent.parent / ".env"


def load_env() -> dict:
    values = {}
    if ENV_PATH.exists():
        for line in ENV_PATH.read_text().splitlines():
            if "=" in line and not line.startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def call(name: str, arguments: dict, url: str, token: str):
    payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
               "params": {"name": name, "arguments": arguments}}
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "Authorization": f"Bearer {token}"})
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read().decode()
    for line in body.splitlines():
        if line.startswith("data: "):
            message = json.loads(line[6:])
            if "error" in message:
                raise SystemExit(f"erreur MCP : {message['error']}")
            result = message["result"]
            if result.get("isError"):
                raise SystemExit(f"erreur outil : {result['content'][0]['text']}")
            return result
    raise SystemExit(f"réponse inattendue : {body[:400]}")


def main() -> None:
    if len(sys.argv) < 2:
        raise SystemExit(__doc__)
    env = load_env()
    url = os.environ.get("CM_MCP_URL", "http://127.0.0.1:8099/mcp/")
    token = os.environ.get("CM_API_TOKEN") or env.get("CM_API_TOKEN", "")
    arguments = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    result = call(sys.argv[1], arguments, url, token)
    structured = result.get("structuredContent")
    if structured is not None:
        value = structured.get("result", structured)
        # Un outil qui renvoie du texte (le briefing) doit s'afficher tel quel,
        # pas sous forme de chaîne JSON échappée.
        print(value if isinstance(value, str)
              else json.dumps(value, indent=2, ensure_ascii=False))
        return
    # Sans contenu structuré, un outil qui renvoie une liste produit un bloc de
    # texte par élément : on les recompose en un seul document JSON.
    blocks = [block.get("text", "") for block in result.get("content", [])]
    try:
        parsed = [json.loads(block) for block in blocks]
    except json.JSONDecodeError:
        print("\n".join(blocks))
        return
    print(json.dumps(parsed if len(parsed) != 1 else parsed[0],
                     indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
