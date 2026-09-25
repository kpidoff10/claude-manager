"""Détection automatique de la stack d'un projet depuis ses fichiers manifestes.

Lit package.json, pyproject.toml, requirements.txt, go.mod, Cargo.toml,
composer.json, docker-compose.yml et Dockerfile, puis en déduit :
technologies + versions, commandes, services et variables d'environnement.

Le scan ne renvoie que des constats ; c'est ``repo.set_technology`` qui décide
de ne pas écraser ce qui a été saisi à la main.
"""
import json
import re
import subprocess
import tomllib
from pathlib import Path

import yaml

SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".venv", "venv", "__pycache__",
             ".next", "target", "vendor", ".cache", "coverage", "logs", "data"}

# Catégorisation des noms qu'on rencontre le plus. Tout le reste tombe en « lib »,
# ce qui reste juste : une dépendance non reconnue est une bibliothèque.
CATEGORY_HINTS = {
    "language": {"python", "node", "nodejs", "typescript", "javascript", "go", "rust",
                 "php", "ruby", "java", "kotlin", "swift", "c#", "elixir"},
    "framework": {"react", "vue", "svelte", "angular", "next", "nuxt", "astro", "remix",
                  "express", "fastify", "koa", "nestjs", "django", "flask", "fastapi",
                  "starlette", "rails", "laravel", "symfony", "spring", "htmx", "solid-js",
                  "preact", "phoenix", "gin", "actix-web", "axum"},
    "database": {"postgresql", "postgres", "mysql", "mariadb", "sqlite", "mongodb", "redis",
                 "valkey", "clickhouse", "elasticsearch", "opensearch", "cassandra",
                 "duckdb", "neo4j", "influxdb", "qdrant", "pgvector"},
    "infra": {"docker", "docker-compose", "kubernetes", "traefik", "nginx", "caddy",
              "apache", "haproxy", "terraform", "ansible", "github-actions", "gitlab-ci"},
    "tool": {"vite", "webpack", "rollup", "esbuild", "eslint", "prettier", "vitest", "jest",
             "playwright", "cypress", "pytest", "ruff", "mypy", "black", "tsc", "turbo",
             "uv", "poetry", "pnpm", "npm", "yarn", "biome", "storybook"},
    "service": {"ollama", "stripe", "sentry", "supabase", "firebase", "auth0", "clerk",
                "cloudflare", "s3", "minio", "rabbitmq", "kafka", "nats"},
}
_CATEGORY_BY_NAME = {name: cat for cat, names in CATEGORY_HINTS.items() for name in names}

DOCS_URLS = {
    "fastapi": "https://fastapi.tiangolo.com/",
    "htmx": "https://htmx.org/docs/",
    "traefik": "https://doc.traefik.io/traefik/",
    "sqlite": "https://www.sqlite.org/docs.html",
    "vite": "https://vite.dev/guide/",
    "react": "https://react.dev/",
    "vitest": "https://vitest.dev/",
    "eslint": "https://eslint.org/docs/latest/",
}


def categorise(name: str) -> str:
    base = name.lower().lstrip("@").split("/")[-1]
    return _CATEGORY_BY_NAME.get(base, _CATEGORY_BY_NAME.get(name.lower(), "lib"))


def docs_url(name: str) -> str | None:
    return DOCS_URLS.get(name.lower().lstrip("@").split("/")[-1])


def clean_version(raw) -> str | None:
    if not isinstance(raw, str):
        return None
    version = raw.strip().lstrip("^~>=<= ").strip()
    if version.startswith(("workspace:", "file:", "link:", "git+", "http")):
        return None
    return version or None


def _manifest_paths(root: Path) -> list[Path]:
    """Manifestes à la racine et dans les sous-dossiers de premier niveau (workspaces)."""
    names = {"package.json", "pyproject.toml", "requirements.txt", "go.mod",
             "Cargo.toml", "composer.json", "Dockerfile", "docker-compose.yml",
             "docker-compose.yaml", ".env.example", ".env.sample"}
    found = []
    for path in root.iterdir() if root.is_dir() else []:
        if path.is_file() and path.name in names:
            found.append(path)
        elif path.is_dir() and path.name not in SKIP_DIRS and not path.name.startswith("."):
            for child in path.iterdir():
                if child.is_file() and child.name in names:
                    found.append(child)
    return found


def _scan_package_json(path: Path, result: dict) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    is_root = path.parent == result["_root"]

    result["technologies"].setdefault("Node.js", {"category": "language", "role": "runtime"})
    for section, role in (("dependencies", None), ("devDependencies", "développement")):
        for name, raw in (data.get(section) or {}).items():
            entry = result["technologies"].setdefault(
                name, {"category": categorise(name), "docs_url": docs_url(name)})
            entry.setdefault("role", role)
            version = clean_version(raw)
            if version:
                entry["version"] = version

    if is_root:
        # Si un service compose lance déjà npm, c'est que le projet se construit
        # dans le conteneur — l'hôte n'a pas forcément Node. Enregistrer un
        # « npm run » nu donnerait alors une commande qui échoue à tous les coups.
        runner = result.get("node_runner")
        for name, command in (data.get("scripts") or {}).items():
            if name.startswith("pre") or name.startswith("post"):
                continue
            prefix = f"docker compose run --rm {runner} " if runner else ""
            result["commands"][name] = {"command": f"{prefix}npm run {name}",
                                        "description": command}
        if data.get("workspaces"):
            result["notes"].append(
                f"monorepo npm workspaces : {', '.join(map(str, data['workspaces']))}")


def _scan_pyproject(path: Path, result: dict) -> None:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    result["technologies"].setdefault("Python", {"category": "language", "role": "runtime"})
    project = data.get("project") or {}
    deps = list(project.get("dependencies") or [])
    poetry = (data.get("tool") or {}).get("poetry") or {}
    deps += [f"{k}{v if isinstance(v, str) else ''}"
             for k, v in (poetry.get("dependencies") or {}).items() if k != "python"]
    for spec in deps:
        _add_requirement(spec, result)


def _scan_requirements(path: Path, result: dict) -> None:
    result["technologies"].setdefault("Python", {"category": "language", "role": "runtime"})
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#")[0].strip()
        if line and not line.startswith("-"):
            _add_requirement(line, result)


def _add_requirement(spec: str, result: dict) -> None:
    match = re.match(r"^([A-Za-z0-9._-]+)\s*(?:\[[^\]]*\])?\s*(.*)$", spec.strip())
    if not match:
        return
    name = match.group(1)
    version = re.sub(r"^[=<>!~^ ]+", "", match.group(2) or "").split(",")[0].strip() or None
    entry = result["technologies"].setdefault(
        name, {"category": categorise(name), "docs_url": docs_url(name)})
    if version:
        entry["version"] = version


def _scan_go_mod(path: Path, result: dict) -> None:
    result["technologies"].setdefault("Go", {"category": "language", "role": "runtime"})
    for line in path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"^\s+([\w./-]+)\s+v([\w.+-]+)", line)
        if match:
            result["technologies"].setdefault(
                match.group(1).split("/")[-1], {"category": "lib", "version": match.group(2)})


def _scan_cargo(path: Path, result: dict) -> None:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    result["technologies"].setdefault("Rust", {"category": "language", "role": "runtime"})
    for name, raw in (data.get("dependencies") or {}).items():
        version = raw if isinstance(raw, str) else (raw or {}).get("version")
        result["technologies"].setdefault(
            name, {"category": categorise(name), "version": clean_version(version)})


def _scan_composer(path: Path, result: dict) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    result["technologies"].setdefault("PHP", {"category": "language", "role": "runtime"})
    for name, raw in (data.get("require") or {}).items():
        if name == "php":
            continue
        result["technologies"].setdefault(
            name, {"category": categorise(name), "version": clean_version(raw)})


def _scan_dockerfile(path: Path, result: dict) -> None:
    result["technologies"].setdefault("Docker", {"category": "infra", "role": "conteneurisation"})
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        match = re.match(r"^\s*FROM\s+([\w./-]+):?([\w.-]*)", line, re.IGNORECASE)
        if match:
            image = match.group(1).split("/")[-1]
            entry = result["technologies"].setdefault(
                image, {"category": categorise(image), "role": "image de base"})
            if match.group(2) and match.group(2) != "latest":
                entry.setdefault("version", match.group(2))


def _scan_compose(path: Path, result: dict) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    result["technologies"].setdefault("Docker Compose", {"category": "infra", "role": "orchestration"})
    for name, service in (data.get("services") or {}).items():
        command = service.get("command") or ""
        text = " ".join(command) if isinstance(command, list) else str(command)
        if re.search(r"\b(npm|yarn|pnpm|node)\b", text):
            result.setdefault("node_runner", name)

        image = service.get("image") or ""
        base = image.split("/")[-1].split(":")[0]
        version = image.split(":")[-1] if ":" in image else None
        if base:
            entry = result["technologies"].setdefault(
                base, {"category": categorise(base), "role": f"service « {name} »"})
            if version and version != "latest":
                entry.setdefault("version", version)

        ports = service.get("ports") or []
        port = _published_port(ports[0]) if ports else None
        url = None
        for label in _labels(service):
            host = re.search(r"Host\(`([^`]+)`\)", label)
            if host:
                url = f"https://{host.group(1)}"
        result["services"][name] = {
            "kind": "container", "container": service.get("container_name") or name,
            "port": port, "url": url, "notes": image or None,
        }

        env = service.get("environment")
        keys = env.keys() if isinstance(env, dict) else \
            [str(e).split("=")[0] for e in (env or [])]
        for key in keys:
            result["env_vars"].setdefault(key, {"location": path.name, "required": True})


def _published_port(entry) -> int | None:
    """Port publié d'une entrée compose.

    Gère « 8080 », « 8080:80 », « 127.0.0.1:8099:8000/tcp » et la forme longue
    en dictionnaire. Le port publié est l'avant-dernier segment : le dernier
    est le port interne, et ce qui précède est l'adresse d'écoute.
    """
    if isinstance(entry, dict):
        published = entry.get("published")
        return int(published) if str(published).isdigit() else None
    segments = str(entry).split("/")[0].split(":")
    candidate = segments[0] if len(segments) == 1 else segments[-2]
    return int(candidate) if candidate.isdigit() else None


def _labels(service: dict) -> list[str]:
    labels = service.get("labels")
    if isinstance(labels, dict):
        return [f"{k}={v}" for k, v in labels.items()]
    return [str(x) for x in (labels or [])]


def _scan_env_example(path: Path, result: dict) -> None:
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, example = line.partition("=")
        name = name.replace("export ", "").strip()
        if not re.match(r"^[A-Z0-9_]+$", name):
            continue
        secret = bool(re.search(r"(SECRET|TOKEN|PASSWORD|KEY|CREDENTIAL)", name))
        result["env_vars"][name] = {
            "location": path.name, "required": True, "secret": secret,
            "example": None if secret else (example.strip()[:80] or None),
        }


SCANNERS = {
    "package.json": _scan_package_json,
    "pyproject.toml": _scan_pyproject,
    "requirements.txt": _scan_requirements,
    "go.mod": _scan_go_mod,
    "Cargo.toml": _scan_cargo,
    "composer.json": _scan_composer,
    "Dockerfile": _scan_dockerfile,
    "docker-compose.yml": _scan_compose,
    "docker-compose.yaml": _scan_compose,
    ".env.example": _scan_env_example,
    ".env.sample": _scan_env_example,
}


def git_remote(root: Path) -> str | None:
    try:
        # Voir gitinfo._git : sans safe.directory, git refuse un dépôt appartenant
        # à un autre utilisateur que celui du conteneur.
        out = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(root),
                              "remote", "get-url", "origin"],
                             capture_output=True, text=True, timeout=5)
        return out.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def scan(path: str) -> dict:
    """Analyse un répertoire de projet et renvoie ce qui a été détecté."""
    root = Path(path)
    if not root.is_dir():
        raise FileNotFoundError(f"répertoire introuvable dans le conteneur : {path}")

    result = {"_root": root, "technologies": {}, "commands": {}, "services": {},
              "env_vars": {}, "notes": [], "scanned": [], "errors": []}

    # Les fichiers compose passent en premier : ils révèlent si les scripts npm
    # doivent être préfixés d'un « docker compose run ». Sans cet ordre, la
    # racine serait lue avant qu'on le sache.
    manifests = sorted(_manifest_paths(root),
                       key=lambda p: 0 if p.name.startswith("docker-compose") else 1)
    for manifest in manifests:
        scanner = SCANNERS.get(manifest.name)
        if scanner is None:
            continue
        try:
            scanner(manifest, result)
            result["scanned"].append(str(manifest.relative_to(root)))
        except Exception as exc:  # un manifeste illisible ne doit pas tuer le scan
            result["errors"].append(f"{manifest.name} : {exc}")

    result.pop("_root")
    result["repo_url"] = git_remote(root)
    return result
