// Interface sans dépendance : les formulaires marqués `panel-form` sont envoyés
// en arrière-plan et ne réactualisent que le panneau courant. Sans JavaScript,
// les mêmes formulaires fonctionnent en POST classique suivi d'une redirection.

const panel = () => document.getElementById('panel');

// `submitter` : le bouton cliqué. Sans lui, un formulaire à plusieurs boutons
// (« Demain 9 h », « Ce soir »…) perdrait la valeur du bouton choisi.
function formDataOf(form, submitter) {
  try {
    return submitter ? new FormData(form, submitter) : new FormData(form);
  } catch (error) {
    const data = new FormData(form);
    if (submitter && submitter.name) data.append(submitter.name, submitter.value);
    return data;
  }
}

async function submitPanelForm(form, submitter) {
  // Un formulaire peut viser un autre bloc que le panneau (data-target) : le
  // bloc des rappels se recharge seul, sans toucher à l'onglet en cours.
  const target = form.dataset.target ? document.getElementById(form.dataset.target) : panel();
  if (!target) return false;

  const confirmMessage = form.dataset.confirm;
  if (confirmMessage && !window.confirm(confirmMessage)) return true;

  target.classList.add('loading');
  try {
    const response = await fetch(form.action, {
      method: 'POST',
      body: formDataOf(form, submitter),
      headers: { 'X-CM-Panel': '1' },
      redirect: 'follow',
    });
    if (response.status === 401) { window.location.href = '/login'; return true; }
    if (!response.ok) throw new Error(response.statusText);
    const keepOpen = form.dataset.target && !!target.querySelector('details[open]');
    target.innerHTML = await response.text();
    if (keepOpen) target.querySelector('details')?.setAttribute('open', '');
    wire(target);
  } catch (error) {
    console.error(error);
    // En cas d'échec, soumission classique. `submit()` ne transmet pas le
    // bouton cliqué : on recopie sa valeur dans un champ caché.
    if (submitter && submitter.name) {
      const hidden = document.createElement('input');
      hidden.type = 'hidden';
      hidden.name = submitter.name;
      hidden.value = submitter.value;
      form.appendChild(hidden);
    }
    form.submit();
  } finally {
    target.classList.remove('loading');
  }
  return true;
}

function wire(root) {
  root.querySelectorAll('form.panel-form').forEach((form) => {
    if (form.dataset.wired) return;
    form.dataset.wired = '1';
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      submitPanelForm(form, event.submitter);
    });
  });

  // Les listes déroulantes de statut et de priorité s'appliquent au changement.
  root.querySelectorAll('select.auto').forEach((select) => {
    if (select.dataset.wired) return;
    select.dataset.wired = '1';
    select.addEventListener('change', () => submitPanelForm(select.closest('form')));
  });

  wireDragAndDrop(root);
  wireKanban(root);
  wireTagFilter(root);
  // Le plateau plein écran est piloté par une classe sur <body> : elle doit
  // suivre les échanges de panneau en arrière-plan, pas seulement le chargement.
  document.body.classList.toggle('kanban-view', !!document.querySelector('.kanban'));
}

// Kanban : déposer une carte dans une colonne change son statut. Au tactile le
// glisser-déposer HTML5 ne se déclenche pas — d'où la liste déroulante de
// statut présente sur chaque carte, qui reste le chemin universel.
function wireKanban(root) {
  const board = root.querySelector('.kanban');
  if (!board || board.dataset.wired) return;
  board.dataset.wired = '1';
  let dragged = null;

  const clearTargets = () =>
    board.querySelectorAll('.drop-target').forEach((el) => el.classList.remove('drop-target'));

  board.addEventListener('dragstart', (event) => {
    const card = event.target.closest('.kcard');
    if (!card) return;
    dragged = card;
    card.classList.add('dragging');
    event.dataTransfer.effectAllowed = 'move';
  });

  board.addEventListener('dragend', () => {
    if (dragged) dragged.classList.remove('dragging');
    clearTargets();
    dragged = null;
  });

  board.addEventListener('dragover', (event) => {
    if (!dragged) return;
    event.preventDefault();
    const column = event.target.closest('.kcards');
    clearTargets();
    if (column) column.classList.add('drop-target');
  });

  board.addEventListener('drop', async (event) => {
    event.preventDefault();
    const column = event.target.closest('.kcards');
    clearTargets();
    if (!dragged || !column) return;

    const status = column.dataset.status;
    // Déposer dans sa propre colonne ne change rien : inutile d'aller au serveur.
    if (dragged.closest('.kcards') === column) return;

    const body = new FormData();
    body.append('status', status);
    body.append('tab', 'tasks');
    body.append('view', 'kanban');
    const response = await fetch(
      `/p/${column.dataset.slug}/tasks/${dragged.dataset.id}/update`,
      { method: 'POST', body, headers: { 'X-CM-Panel': '1' } });
    if (response.ok) {
      panel().innerHTML = await response.text();
      wire(panel());
    }
  });
}

// Réordonnancement par glisser-déposer. On envoie l'identifiant de la tâche
// au-dessus de laquelle on a lâché ; le serveur intercale entre deux rangs.
function wireDragAndDrop(root) {
  const list = root.querySelector('.task-list.sortable');
  if (!list || list.dataset.wired) return;
  list.dataset.wired = '1';
  let dragged = null;

  list.addEventListener('dragstart', (event) => {
    const item = event.target.closest('.task');
    if (!item) return;
    dragged = item;
    item.classList.add('dragging');
    event.dataTransfer.effectAllowed = 'move';
  });

  list.addEventListener('dragend', () => {
    if (dragged) dragged.classList.remove('dragging');
    list.querySelectorAll('.drag-over').forEach((el) => el.classList.remove('drag-over'));
    dragged = null;
  });

  list.addEventListener('dragover', (event) => {
    event.preventDefault();
    const item = event.target.closest('.task');
    list.querySelectorAll('.drag-over').forEach((el) => el.classList.remove('drag-over'));
    if (item && item !== dragged) item.classList.add('drag-over');
  });

  list.addEventListener('drop', async (event) => {
    event.preventDefault();
    const item = event.target.closest('.task');
    if (!dragged || !item || item === dragged) return;

    const items = [...list.querySelectorAll('.task')];
    const targetIndex = items.indexOf(item);
    const previous = items[targetIndex - 1];
    // Lâcher sur la première ligne place en tête ; sinon on se place juste après
    // la ligne qui précède la cible.
    const afterId = targetIndex === 0 ? '' : (previous === dragged
      ? item.dataset.id
      : previous.dataset.id);

    const body = new FormData();
    body.append('after_id', afterId);
    const response = await fetch(
      `/p/${list.dataset.slug}/tasks/${dragged.dataset.id}/reorder`,
      { method: 'POST', body, headers: { 'X-CM-Panel': '1' } });
    if (response.ok) {
      panel().innerHTML = await response.text();
      wire(panel());
    }
  });
}

// Filtre par tag. Tout est déjà dans la page : filtrer côté navigateur évite un
// aller-retour serveur et, surtout, évite de traîner le filtre dans chacun des
// formulaires. Le tag choisi survit aux échanges de panneau.
let activeTag = '';

function applyTagFilter(root) {
  const scope = root || document;
  let masquees = 0;
  scope.querySelectorAll('[data-tags]').forEach((card) => {
    const tags = (card.dataset.tags || '').split(' ').filter(Boolean);
    card.hidden = activeTag !== '' && !tags.includes(activeTag);
    if (card.hidden) masquees += 1;
  });

  // Les compteurs doivent dire ce qu'ils comptent. Pendant un filtrage, un « 0 »
  // sec se lit comme « il n'y a rien » alors qu'il signifie « rien qui porte ce
  // tag » — c'est ce qui a fait croire qu'un agent en cours avait disparu.
  scope.querySelectorAll('.kcol').forEach((column) => {
    const cards = [...column.querySelectorAll('.kcard')];
    const shown = cards.filter((c) => !c.hidden).length;
    const badge = column.querySelector('.kcol-head .count');
    if (badge) badge.textContent = activeTag ? `${shown}/${cards.length}` : shown;
  });

  scope.querySelectorAll('[data-tag-filter]').forEach((bar) => {
    bar.querySelectorAll('.tag-chip').forEach((chip) => {
      chip.classList.toggle('active', chip.dataset.tag === activeTag);
    });
    let note = bar.querySelector('.filter-note');
    if (!note) {
      note = document.createElement('span');
      note.className = 'filter-note';
      bar.appendChild(note);
    }
    note.hidden = !activeTag;
    note.textContent = activeTag
      ? `filtré sur #${activeTag} — ${masquees} carte(s) masquée(s)` : '';
  });
}

function wireTagFilter(root) {
  const bar = root.querySelector('[data-tag-filter]');
  if (bar && !bar.dataset.wired) {
    bar.dataset.wired = '1';
    bar.addEventListener('click', (event) => {
      const chip = event.target.closest('.tag-chip');
      // Le « +15 » porte la même classe pour garder l'allure d'une pastille,
      // mais c'est une étiquette de case à cocher : sans ce garde-fou, cliquer
      // dessus filtrerait sur un tag indéfini et viderait la liste.
      if (!chip || chip.dataset.tag === undefined) return;
      activeTag = chip.dataset.tag === activeTag ? '' : chip.dataset.tag;
      applyTagFilter(document);
    });
  }
  applyTagFilter(root);
}

// Tiroir de navigation sur petit écran. Sur grand écran la barre latérale est
// toujours visible et la classe n'a aucun effet visuel.
function wireDrawer() {
  const setOpen = (open) => {
    document.body.classList.toggle('drawer-open', open);
    const toggle = document.querySelector('[data-open-drawer]');
    if (toggle) toggle.setAttribute('aria-expanded', String(open));
  };

  document.addEventListener('click', (event) => {
    if (event.target.closest('[data-open-drawer]')) { setOpen(true); return; }
    if (event.target.closest('[data-close-drawer]')) { setOpen(false); return; }
    // Suivre un lien du tiroir doit le refermer derrière soi.
    if (event.target.closest('.sidebar a')) setOpen(false);
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') setOpen(false);
  });
}

// Journal d'un agent en cours : on réactualise les étapes toutes les quatre
// secondes. Le défilement n'est ramené en bas que si l'on y était déjà — sinon
// on arracherait la page des mains de qui est en train de lire plus haut.
function wireLiveRun() {
  const target = document.querySelector('[data-run-live]');
  if (!target || target.dataset.liveWired) return;
  target.dataset.liveWired = '1';

  setInterval(async () => {
    const atBottom = window.innerHeight + window.scrollY
      >= document.body.offsetHeight - 120;
    try {
      const response = await fetch(`/runs/${target.dataset.runLive}`,
        { headers: { 'X-CM-Panel': '1' } });
      if (!response.ok) return;
      target.innerHTML = await response.text();
      if (atBottom) window.scrollTo({ top: document.body.scrollHeight });
      // L'agent a fini : la page complète porte alors le verdict et les tests.
      if (!target.querySelector('.live-hint')) window.location.reload();
    } catch (error) {
      console.error(error);
    }
  }, 4000);
}

// Page d'un projet : toutes les cinq secondes on demande au serveur une
// empreinte de l'état du projet. Le bandeau d'activité est toujours remis à
// jour ; le panneau n'est rechargé que si l'empreinte a changé, et jamais sous
// les doigts de quelqu'un qui tape : on attend qu'il quitte le champ.
function wireLiveProject() {
  const strip = document.querySelector('[data-live-slug]');
  if (!strip || strip.dataset.liveWired) return;
  strip.dataset.liveWired = '1';
  const slug = strip.dataset.liveSlug;
  let version = strip.dataset.liveVersion;
  let pending = false;

  const busy = (target = panel()) => {
    const el = document.activeElement;
    return !!(target && el && target.contains(el)
      && el.matches('input:not([type=hidden]), textarea, select'));
  };

  const refreshReminders = async () => {
    const block = document.getElementById('project-reminders');
    if (!block || busy(block)) return;
    const response = await fetch(`/p/${slug}/reminders`, { headers: { 'X-CM-Panel': '1' } });
    if (!response.ok || response.redirected) return;
    const wasOpen = !!block.querySelector('details[open]');
    block.innerHTML = await response.text();
    const details = block.querySelector('details');
    if (details && wasOpen) details.open = true;
    wire(block);
  };

  // Garder dépliées les tâches qui l'étaient : on repère chaque <details>
  // ouvert par la tâche qui le porte et son rang dans celle-ci.
  const openKeys = (root) => [...root.querySelectorAll('details[open]')].map((d) => {
    const host = d.closest('[data-id]');
    const scope = host || root;
    return `${host ? host.dataset.id : '-'}:${[...scope.querySelectorAll('details')].indexOf(d)}`;
  });
  const reopen = (root, keys) => {
    const wanted = new Set(keys);
    root.querySelectorAll('details').forEach((d) => {
      const host = d.closest('[data-id]');
      const scope = host || root;
      const key = `${host ? host.dataset.id : '-'}:${[...scope.querySelectorAll('details')].indexOf(d)}`;
      if (wanted.has(key)) d.open = true;
    });
  };

  const refreshPanel = async () => {
    const target = panel();
    if (!target) return;
    if (busy()) { pending = true; return; }
    pending = false;
    const response = await fetch(window.location.pathname + window.location.search,
      { headers: { 'X-CM-Panel': '1' } });
    if (response.status === 401) { window.location.href = '/login'; return; }
    if (!response.ok) return;
    const keys = openKeys(target);
    target.innerHTML = await response.text();
    reopen(target, keys);
    wire(target);
    target.classList.add('live-flash');
    setTimeout(() => target.classList.remove('live-flash'), 900);
  };

  document.addEventListener('focusout', () => {
    if (pending) setTimeout(() => { if (!busy()) refreshPanel(); }, 300);
  });

  const tick = async () => {
    if (document.hidden) return;
    try {
      const response = await fetch(`/p/${slug}/live`);
      // Session expirée : le serveur redirige vers la connexion.
      if (response.status === 401 || response.redirected) { window.location.href = '/login'; return; }
      if (!response.ok) return;
      const data = await response.json();
      strip.innerHTML = data.activity;
      if (data.version !== version) {
        version = data.version;
        await refreshPanel();
        await refreshReminders();
      }
    } catch (error) {
      console.error(error);
    }
  };
  setInterval(tick, 5000);
  document.addEventListener('visibilitychange', () => { if (!document.hidden) tick(); });
}

// Fiche d'une tâche en boîte de dialogue : « ↳ #N » ouvre la tâche parente,
// et depuis la fiche on peut remonter ou descendre de tâche en tâche. Le lien
// mène à une vraie page : sans JavaScript, il fonctionne quand même.
function wireTaskDialog() {
  const dialog = document.getElementById('task-dialog');
  if (!dialog || typeof dialog.showModal !== 'function') return;
  const body = dialog.querySelector('[data-dialog-body]');

  document.addEventListener('click', async (event) => {
    const link = event.target.closest('[data-task-dialog]');
    if (!link) return;
    // Le badge est dans un <summary> : sans cela, le clic déplierait la tâche.
    event.preventDefault();
    event.stopPropagation();
    try {
      const response = await fetch(link.getAttribute('href'), { headers: { 'X-CM-Panel': '1' } });
      if (response.status === 401 || response.redirected) { window.location.href = '/login'; return; }
      if (!response.ok) throw new Error(response.statusText);
      body.innerHTML = await response.text();
      wire(body);  // formulaires de la fiche (tests) : envoyés en arrière-plan
      if (!dialog.open) dialog.showModal();
      dialog.scrollTop = 0;
    } catch (error) {
      console.error(error);
      window.location.href = link.getAttribute('href');
    }
  }, true);

  dialog.addEventListener('click', (event) => {
    // Un clic sur le fond (hors du contenu) ferme la fiche.
    if (event.target === dialog || event.target.closest('[data-dialog-close]')) dialog.close();
  });
}

// Page des rappels : rechargée toutes les trente secondes, pour qu'un rappel
// passe de « aujourd'hui » à « échus » sans action — sauf pendant la saisie.
function wireAllReminders() {
  const block = document.querySelector('[data-all-reminders]');
  if (!block || block.dataset.liveWired) return;
  block.dataset.liveWired = '1';
  setInterval(async () => {
    if (document.hidden) return;
    const el = document.activeElement;
    if (el && block.contains(el) && el.matches('input:not([type=hidden]), textarea, select')) return;
    if (block.querySelector('details[open]')) return;  // formulaire d'ajout ouvert
    try {
      const response = await fetch('/rappels', { headers: { 'X-CM-Panel': '1' } });
      if (!response.ok || response.redirected) return;
      block.innerHTML = await response.text();
      wire(block);
    } catch (error) {
      console.error(error);
    }
  }, 30000);
}

document.addEventListener('DOMContentLoaded', () => {
  wireAllReminders();
  wireTaskDialog();
  wire(document);
  wireDrawer();
  wireLiveRun();
  wireLiveProject();
});
