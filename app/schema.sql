-- claude-manager : état persistant par projet.
-- Une seule base SQLite, lue par le serveur MCP (Claude) et l'interface web (humain).

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    id          INTEGER PRIMARY KEY,
    slug        TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL,
    path        TEXT UNIQUE,                       -- chemin absolu, sert à résoudre le cwd
    repo_url    TEXT,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'active',    -- active | paused | archived
    tags        TEXT NOT NULL DEFAULT '[]',        -- ex. ["pro", "client-x"] : pour trier perso / pro
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS milestones (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT,
    status      TEXT NOT NULL DEFAULT 'open',      -- open | done | cancelled
    target_date TEXT,
    order_index REAL NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_milestones_project ON milestones(project_id, status);

CREATE TABLE IF NOT EXISTS tasks (
    id             INTEGER PRIMARY KEY,
    project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    milestone_id   INTEGER REFERENCES milestones(id) ON DELETE SET NULL,
    parent_id      INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    title          TEXT NOT NULL,
    body           TEXT,
    status         TEXT NOT NULL DEFAULT 'todo',   -- todo | in_progress | blocked | done | cancelled
    priority       INTEGER NOT NULL DEFAULT 2,     -- 0 someday · 1 low · 2 normal · 3 high · 4 urgent
    owner          TEXT NOT NULL DEFAULT 'claude', -- claude | user
    tags           TEXT NOT NULL DEFAULT '[]',
    order_index    REAL NOT NULL DEFAULT 0,
    blocked_reason TEXT,
    cancel_reason  TEXT,                            -- pourquoi la tâche a été abandonnée
    -- remind_at / remind_note / reminded_at : anciennes colonnes de rappel,
    -- reprises dans la table `reminders` (voir db._reprend_rappels).
    -- Autorise l'agent à travailler sur un dépôt non propre. Réservé aux tâches
    -- dont l'objet EST le désordre — commiter ce qui traîne, par exemple.
    allow_dirty    INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    completed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_tasks_project  ON tasks(project_id, status, priority);
CREATE INDEX IF NOT EXISTS idx_tasks_parent   ON tasks(parent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_mstone   ON tasks(milestone_id);

-- Catalogue global : une techno existe une fois, elle est reliée à N projets.
-- C'est ce qui permet la vue inverse « quels projets utilisent X ».
CREATE TABLE IF NOT EXISTS technologies (
    id               INTEGER PRIMARY KEY,
    name             TEXT NOT NULL UNIQUE,
    category         TEXT NOT NULL DEFAULT 'lib',   -- language|framework|database|infra|lib|tool|service
    docs_url         TEXT,
    preference       TEXT,                          -- préférence globale, indépendante des projets
    preference_level TEXT NOT NULL DEFAULT 'neutral', -- preferred | neutral | avoid
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_technologies (
    id            INTEGER PRIMARY KEY,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    technology_id INTEGER NOT NULL REFERENCES technologies(id) ON DELETE CASCADE,
    version       TEXT,
    role          TEXT,                             -- « API backend », « reverse proxy »
    status        TEXT NOT NULL DEFAULT 'active',   -- active | considered | deprecated
    notes         TEXT,                             -- pourquoi choisie / pourquoi abandonnée
    source        TEXT NOT NULL DEFAULT 'manual',   -- manual | scan
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    UNIQUE(project_id, technology_id)
);
CREATE INDEX IF NOT EXISTS idx_ptech_project ON project_technologies(project_id, status);

CREATE TABLE IF NOT EXISTS commands (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,                      -- dev | test | build | deploy | lint
    command     TEXT NOT NULL,
    workdir     TEXT,
    description TEXT,
    order_index REAL NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(project_id, name)
);

CREATE TABLE IF NOT EXISTS services (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'url',        -- container | url | port | endpoint
    url         TEXT,
    port        INTEGER,
    container   TEXT,
    environment TEXT NOT NULL DEFAULT 'prod',       -- prod | staging | dev
    notes       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(project_id, name, environment)
);

-- Noms de variables uniquement. Aucune valeur secrète n'est stockée ici.
CREATE TABLE IF NOT EXISTS env_vars (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    required    INTEGER NOT NULL DEFAULT 1,
    secret      INTEGER NOT NULL DEFAULT 0,
    location    TEXT,                               -- .env, secret docker, label traefik…
    description TEXT,
    example     TEXT,                               -- format d'exemple, jamais la vraie valeur
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(project_id, name)
);

CREATE TABLE IF NOT EXISTS resources (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER REFERENCES projects(id) ON DELETE CASCADE,  -- NULL = ressource globale
    title       TEXT NOT NULL,
    url         TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'other',      -- docs | repo | ticket | design | dashboard | other
    notes       TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_resources_project ON resources(project_id);

-- Méthodologie d'un projet : comment on y travaille, une pratique par entrée.
-- Ce qui décrit le CODE reste dans le CLAUDE.md du dépôt — une convention doit
-- changer dans le même commit que ce qu'elle décrit. Ici vit ce que le dépôt ne
-- dit pas : la définition de « terminé », le périmètre, le rituel de revue.
-- Index de la documentation d'un projet : un POINTEUR et une ligne, jamais le
-- contenu. La documentation doit changer dans le même commit que le code
-- qu'elle décrit, donc elle vit dans le dépôt. Ce que le manager apporte, c'est
-- de savoir quel fichier ouvrir pour quelle question.
-- Modifications de documentation demandées depuis l'interface.
-- Le conteneur ne peut pas écrire dans les dépôts (montage en lecture seule) :
-- il dépose la demande ici, et le démon, qui tourne sur l'hôte avec les droits
-- de `dev`, l'applique et commite. Le contenu n'est PAS la source de vérité —
-- c'est une demande en transit, effacée de fait dès qu'elle est appliquée.
CREATE TABLE IF NOT EXISTS doc_edits (
    id           INTEGER PRIMARY KEY,
    project_id   INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path         TEXT NOT NULL,
    content      TEXT NOT NULL,
    branch       TEXT,                 -- branche attendue au moment de la demande
    state        TEXT NOT NULL DEFAULT 'pending',   -- pending | applied | failed
    detail       TEXT,
    commit_hash  TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS docs (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    path        TEXT NOT NULL,              -- relatif à la racine du dépôt
    title       TEXT,
    covers      TEXT,                       -- ce qu'on y trouve, en une phrase
    order_index REAL NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(project_id, path)
);

CREATE TABLE IF NOT EXISTS practices (
    id          INTEGER PRIMARY KEY,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    category    TEXT NOT NULL DEFAULT 'general',
    title       TEXT NOT NULL,
    body        TEXT,
    order_index REAL NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(project_id, title)
);

CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY,
    project_id INTEGER REFERENCES projects(id) ON DELETE CASCADE,   -- NULL = mémoire globale
    kind       TEXT NOT NULL DEFAULT 'note',        -- decision | convention | gotcha | erreur | context | note
    title      TEXT NOT NULL,
    body       TEXT NOT NULL,
    tags       TEXT NOT NULL DEFAULT '[]',
    pinned     INTEGER NOT NULL DEFAULT 0,          -- épinglée = toujours dans le briefing
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_memories_project ON memories(project_id, kind);

CREATE TABLE IF NOT EXISTS journal (
    id         INTEGER PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id    INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    kind       TEXT NOT NULL DEFAULT 'work',        -- work | decision | note | blocker | milestone
    summary    TEXT NOT NULL,
    detail     TEXT,
    session_id TEXT,
    actor      TEXT NOT NULL DEFAULT 'claude',      -- claude | user
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_journal_project ON journal(project_id, created_at DESC);

-- Une exécution d'agent sur une tâche. Garde la trace de ce qui a été fait, de
-- l'état du dépôt avant et après, et du verdict des tests — c'est la matière de
-- la relecture humaine.
CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY,
    task_id        INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    status         TEXT NOT NULL DEFAULT 'running',  -- running | passed | failed | error | stopped
    commit_before  TEXT,
    commit_after   TEXT,
    diff_stat      TEXT,
    tests_command  TEXT,
    tests_ok       INTEGER,
    tests_output   TEXT,
    summary        TEXT,
    log_path       TEXT,
    exit_code      INTEGER,
    attempt        INTEGER NOT NULL DEFAULT 1,
    started_at     TEXT NOT NULL,
    finished_at    TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_task ON runs(task_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status);

-- Rappels : sur un projet (« relancer le client lundi ») ou sur une tâche
-- (« tester demain »). Dates en UTC ; un rappel reposé repart (reminded_at NULL).
CREATE TABLE IF NOT EXISTS reminders (
    -- AUTOINCREMENT : un numéro n'est jamais réattribué. Un bouton Telegram
    -- d'un vieux message ne doit pas agir sur un rappel créé depuis.
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id     INTEGER REFERENCES tasks(id) ON DELETE CASCADE,
    note        TEXT,
    remind_at   TEXT NOT NULL,
    reminded_at TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(reminded_at, remind_at);
CREATE INDEX IF NOT EXISTS idx_reminders_project ON reminders(project_id);

-- Tests faits sur une tâche (ou sous-tâche) : historique, on n'écrase jamais.
-- Un test raté puis refait réussi laisse deux lignes — c'est l'histoire utile.
CREATE TABLE IF NOT EXISTS task_tests (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    what        TEXT NOT NULL,                     -- ce qui a été testé
    result      TEXT NOT NULL,                     -- ok | ko | partial
    environment TEXT,                              -- dev, prod, téléphone, navigateur…
    detail      TEXT,                              -- constat, erreur, reste à faire
    actor       TEXT NOT NULL DEFAULT 'claude',    -- claude | user
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_tests_task ON task_tests(task_id, created_at);

-- Réglages globaux (file en pause, demande d'arrêt), une ligne par clé.
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Signalements : des utilisateurs extérieurs (agences) décrivent un problème
-- en discutant avec une IA. Rien de ce qu'ils écrivent ne devient une tâche
-- sans la validation de Kevin — voir repo.accept_ticket.
CREATE TABLE IF NOT EXISTS reporters (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL,
    login         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    agency        TEXT,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    active        INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL,
    last_seen_at  TEXT
);

-- Projets sur lesquels un signaleur peut déposer. `reporters.project_id` reste
-- le premier d'entre eux (historique) ; c'est cette table qui fait foi.
CREATE TABLE IF NOT EXISTS reporter_projects (
    reporter_id INTEGER NOT NULL REFERENCES reporters(id) ON DELETE CASCADE,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    PRIMARY KEY (reporter_id, project_id)
);

CREATE TABLE IF NOT EXISTS support_tickets (
    id            INTEGER PRIMARY KEY,
    reporter_id   INTEGER NOT NULL REFERENCES reporters(id) ON DELETE CASCADE,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    -- draft : discussion en cours · submitted : envoyé, attend Kevin ·
    -- accepted : devenu une tâche · rejected : refusé, avec sa raison
    status        TEXT NOT NULL DEFAULT 'draft',
    awaiting_ai   INTEGER NOT NULL DEFAULT 0,       -- un message attend sa réponse
    ai_error      TEXT,
    title         TEXT,
    page          TEXT,
    summary       TEXT,                             -- ticket proposé par l'IA
    severity      TEXT,
    task_id       INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
    reject_reason TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    submitted_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_reporter ON support_tickets(reporter_id, id DESC);
CREATE INDEX IF NOT EXISTS idx_tickets_status ON support_tickets(status);

CREATE TABLE IF NOT EXISTS support_messages (
    id         INTEGER PRIMARY KEY,
    ticket_id  INTEGER NOT NULL REFERENCES support_tickets(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,                       -- user | assistant
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_support_messages ON support_messages(ticket_id, id);

-- Index de recherche plein texte, alimenté par repo.py (pas par des triggers :
-- une seule voie d'écriture, plus simple à garder cohérente).
CREATE VIRTUAL TABLE IF NOT EXISTS search_index USING fts5(
    title,
    body,
    entity_type UNINDEXED,
    entity_id   UNINDEXED,
    project_id  UNINDEXED,
    tokenize = 'unicode61 remove_diacritics 2'
);
