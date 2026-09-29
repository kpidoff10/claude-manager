// Espace signaleur : envoi du message et attente de la réponse de l'IA, sans
// recharger la page. Sans JavaScript tout marche aussi : le formulaire poste,
// et la page se recharge seule tant que l'assistant réfléchit (<noscript>).
(function () {
  const box = document.getElementById('conversation');
  if (!box) return;
  const url = '/support/t/' + box.dataset.ticket;
  let timer = null;

  function bas() { window.scrollTo(0, document.body.scrollHeight); }

  function affiche(html) {
    box.innerHTML = html;
    branche();
    bas();
  }

  async function sonde() {
    try {
      const r = await fetch(url, { headers: { 'X-CM-Panel': '1' } });
      if (r.redirected || r.status === 401) { window.location.href = '/support/login'; return; }
      if (r.ok) affiche(await r.text());
    } catch (e) { /* réseau capricieux : on retente au tour suivant */ }
  }

  function branche() {
    clearTimeout(timer);
    const head = box.querySelector('[data-awaiting]');
    if (head && head.dataset.awaiting === '1') timer = setTimeout(sonde, 2000);

    const form = box.querySelector('[data-chat-form]');
    if (!form) return;
    const zone = form.querySelector('textarea');
    const fichiers = form.querySelector('input[type=file]');
    const etiquette = form.querySelector('[data-chat-files]');
    const montre = () => {
      const n = fichiers ? fichiers.files.length : 0;
      if (etiquette) etiquette.textContent = n ? `${n} capture${n > 1 ? 's' : ''} jointe${n > 1 ? 's' : ''}` : '';
    };
    if (fichiers) fichiers.addEventListener('change', montre);
    // Ctrl+V d'une image : elle rejoint les fichiers du formulaire.
    zone.addEventListener('paste', (e) => {
      const images = [...(e.clipboardData?.files || [])].filter((f) => f.type.startsWith('image/'));
      if (!images.length || !fichiers || typeof DataTransfer === 'undefined') return;
      e.preventDefault();
      const dt = new DataTransfer();
      [...fichiers.files, ...images].slice(0, 5).forEach((f) => dt.items.add(f));
      fichiers.files = dt.files;
      montre();
    });
    zone.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) form.requestSubmit();
    });
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!zone.value.trim() && !(fichiers && fichiers.files.length)) return;
      // Le contenu est lu AVANT de désactiver les champs : un champ désactivé
      // n'est pas envoyé, et le message partait vide (constaté le 28/09).
      const donnees = new FormData(form);
      const texte = zone.value;
      form.querySelectorAll('textarea, button, input').forEach((el) => { el.disabled = true; });
      // Le message s'affiche tout de suite, sans attendre le serveur.
      const chat = box.querySelector('.chat');
      if (chat) {
        const bulle = document.createElement('div');
        bulle.className = 'bubble user';
        const n = fichiers ? fichiers.files.length : 0;
        bulle.textContent = texte + (n ? `${texte ? '\n' : ''}📎 ${n} capture${n > 1 ? 's' : ''}` : '');
        chat.appendChild(bulle);
        bas();
      }
      try {
        const r = await fetch(form.action, {
          method: 'POST', body: donnees, headers: { 'X-CM-Panel': '1' },
        });
        if (!r.ok) throw new Error(r.statusText);
        affiche(await r.text());
      } catch (err) {
        // Envoi classique : les champs désactivés ne partiraient pas.
        form.querySelectorAll('textarea, button, input').forEach((el) => { el.disabled = false; });
        form.submit();
      }
    });
  }

  branche();
  bas();
})();
