---
name: suivi-projet
description: Tenir l'état d'un projet dans claude-manager (MCP) et le rendre exploitable par la file d'agents — quoi consigner et où, comment écrire une tâche délégable, et comment déclarer les commandes de test et d'aperçu d'un projet. À charger avant d'écrire dans claude-manager (log_work, create_task, update_task, add_memory, set_technology, set_command), quand le hook Stop signale du travail non consigné, quand on prépare un projet pour la file d'agents ou qu'on met en place son environnement d'essai, ou au moment de décider si une information va dans le CLAUDE.md du dépôt ou dans le manager.
---

# Tenir l'état d'un projet

Le hook `SessionStart` fournit le briefing et le hook `Stop` empêche de terminer
sur du travail non consigné. Ce skill dit **quoi écrire** — les hooks ne disent
que *quand*.

## La règle première : tout passe par une tâche

**Chaque chose faite ou demandée existe comme tâche**, terminée ou non. Sans
exception, y compris pour ce qui a pris deux minutes.

- **Kevin demande quelque chose** → `create_task` immédiatement, avant de s'y
  mettre. Puis `update_task(status='in_progress')`, puis `done`.
- **C'est déjà livré avant d'y avoir pensé** → créer la tâche quand même et la
  fermer dans la foulée. Une tâche rétroactive vaut mieux qu'un trou.
- **Demandé mais pas fait** → `create_task` avec la priorité qui convient, et
  `owner='user'` si ça relève d'une décision de Kevin.
- **Découvert en chemin et laissé de côté** → `create_task`, sinon c'est perdu.

Pourquoi cette rigidité : l'avancement d'un projet se calcule sur les tâches.
Un journal impeccable et zéro tâche fermée donnent **0 %** sur un projet où tout
a avancé — et Kevin n'a plus rien à prioriser depuis l'interface web. C'est
exactement ce qui est arrivé à `claude-manager` le premier jour.

`log_work` raconte, `update_task` mesure. **Les deux, pas l'un ou l'autre.**

## Écrire une tâche qu'un agent puisse traiter

La file refuse une tâche **sans énoncé** : un agent démarre à froid, il n'a que
le briefing du projet et ce que tu as écrit. Le titre ne suffit jamais.

Un énoncé délégable dit **ce qu'on veut obtenir**, pas comment. Il nomme les
contraintes qui ne se devinent pas, et il dit ce qu'il ne faut pas toucher.
Quand la décision revient à Kevin, la tâche se marque `owner='user'` — elle
n'entrera jamais en file.

- ✅ « Créer `BRANCHES.md` à la racine, titre de niveau 1 et deux phrases sur le
  fonctionnement des branches. Ne toucher à aucun autre fichier. »
- ❌ « Améliorer la doc » · « Corriger le HUD »

## Préparer un projet pour la file d'agents

Trois choses à enregistrer dans la fiche du projet, une fois pour toutes.

**1. Les commandes `test` et `lint`.** Elles sont le premier vérificateur : la
file les lance après l'agent et ne commite que si elles passent. Sans elles, le
travail arrive en relecture sans aucune garantie.

**2. Une commande `preview` — facultative.** Elle démarre une instance d'essai de
la tâche, pour que Kevin puisse l'ouvrir avant de valider. Elle reçoit par
l'environnement :

| Variable | Contenu |
|---|---|
| `CM_PORT` | port alloué par la file |
| `CM_WORKTREE` | la copie de travail de la tâche — **jamais** la copie principale |
| `CM_BRANCH`, `CM_TASK`, `CM_PROJECT` | branche, numéro de tâche, slug |

Elle doit **rendre la main tout de suite** : on lance en arrière-plan, on ne
bloque pas. Une commande `preview-stop` symétrique éteint l'instance.

**3. Un service nommé `preview`** dont l'`url` est le gabarit du lien affiché —
`{port}` et `{task}` y sont remplacés. Sans commande `preview`, il n'y a pas
d'aperçu et rien ne casse.

### Les trois pièges de l'aperçu

**Les fichiers ignorés par git.** Un worktree ne contient que ce qui est **suivi**.
Tout ce que `.gitignore` écarte — cartes générées, données construites, `.env` —
en est absent, et l'application s'y casse d'une façon déroutante : une requête
vers un fichier manquant reçoit la page d'accueil du serveur de développement, et
le navigateur annonce *« Unexpected token '<' »*. La commande `preview` doit donc
recopier ces artefacts depuis la copie principale avant de démarrer. Un lien
symbolique ne suffit pas : sa cible n'est pas montée dans le conteneur.

**Le chemin.** L'agent travaille dans un worktree, pas dans la copie principale.
Toute commande qui suppose le chemin d'origine échouera — se servir de
`CM_WORKTREE`, ou du répertoire courant.

**Les dépendances.** Si le projet monte ses `node_modules` en volumes Docker
nommés, un worktree lancé sous un autre nom de projet compose obtiendrait des
volumes vides et devrait tout réinstaller. Déclarer ces volumes en `external`
avec leur nom d'origine les réutilise. Et pour exposer l'instance, poser des
labels Traefik sur le conteneur plutôt que d'écrire dans la configuration du
proxy : elle n'est pas inscriptible.

## La règle de partage : le dépôt gagne

| Information | Où elle va |
|---|---|
| Convention de code, règle d'architecture, piège lié au code, commande de build | **CLAUDE.md du dépôt** |
| Tâche, priorité, jalon, journal, décision datée, stack et son historique | **claude-manager** |

Une convention doit changer dans le **même commit** que le code qu'elle décrit.
Rangée dans une base, elle se décorrèle de la branche et finit par mentir.

**Ne jamais dupliquer.** Si une information existe déjà dans le CLAUDE.md, ne
pas l'ajouter en mémoire — y renvoyer suffit. Le type `convention` du manager
est réservé aux conventions **de collaboration** (« Kevin tranche les priorités,
je propose »), pas aux conventions de code.

## Quel outil pour quoi

**`log_work`** — un événement daté : ce qui vient d'être fait, décidé ou bloqué.
Le test : *est-ce que ce résumé sera encore compréhensible dans trois semaines,
sans le contexte de la conversation ?*

- ✅ « Corrigé le débordement des tableaux sur mobile en passant stack et fiche projet en listes de cartes »
- ❌ « Corrigé le bug » · « Mis à jour les fichiers » · « Terminé la tâche 4 »

**`update_task` / `create_task`** — l'état du travail.
`in_progress` **au moment où on attaque**, pas après coup ; `blocked` avec
`blocked_reason` dès qu'on ne peut plus avancer ; `done` à la fin. Créer une
tâche pour tout ce qui est découvert en chemin et qu'on ne traite pas
maintenant — sinon c'est perdu. `owner='user'` pour ce qui demande une décision
humaine.

**`add_memory(kind='decision')`** — une décision qui engage la suite, **avec son
pourquoi**. À écrire au moment où elle est prise ; rédigée en fin de session,
elle est déjà appauvrie.

- ✅ « SQLite plutôt que PostgreSQL : mono-utilisateur, un conteneur en moins à maintenir, FTS5 suffit. À revoir si plusieurs écrivains concurrents apparaissent. »
- ❌ « On utilise SQLite. »

Une décision utile contient sa **condition de révision** : ce qui devrait la
faire changer d'avis.

**`add_memory(kind='gotcha')`** — un piège qui a coûté du temps et qui le
recoûtera. Décrire le symptôme *et* la cause, pas seulement le correctif.

**`set_technology(status='deprecated', notes=...)`** — une piste abandonnée. Le
`notes` est l'essentiel : sans lui, la même piste sera reproposée dans un mois.

## Rythme

Écrire **au fil du travail**, pas en fin de session. Une étape notable terminée
= une entrée. Le hook `Stop` est un filet de sécurité, pas la procédure
normale : s'il se déclenche, c'est qu'on a déjà attendu trop longtemps.

## Avant de créer

Passer un `search` : le sujet est peut-être déjà couvert par une tâche ou une
mémoire existante. Mieux vaut mettre à jour que dupliquer.

## Ce qu'il ne faut pas consigner

Le bruit dilue le signal et rend le briefing illisible :

- les étapes intermédiaires d'une même tâche (une entrée par étape **notable**) ;
- ce que le dépôt raconte déjà — l'historique git, la structure du code ;
- les essais abandonnés sans enseignement ;
- les reformulations d'une mémoire déjà présente.

En cas de doute : *est-ce que ça changera ce que je ferai à la prochaine
session ?* Si non, ne pas l'écrire.
