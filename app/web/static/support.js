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
    zone.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) form.requestSubmit();
    });
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      if (!zone.value.trim()) return;
      // Le contenu est lu AVANT de désactiver les champs : un champ désactivé
      // n'est pas envoyé, et le message partait vide (constaté le 28/09).
      const donnees = new FormData(form);
      const texte = zone.value;
      form.querySelectorAll('textarea, button').forEach((el) => { el.disabled = true; });
      // Le message s'affiche tout de suite, sans attendre le serveur.
      const chat = box.querySelector('.chat');
      if (chat) {
        const bulle = document.createElement('div');
        bulle.className = 'bubble user';
        bulle.textContent = texte;
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
        form.querySelectorAll('textarea, button').forEach((el) => { el.disabled = false; });
        form.submit();
      }
    });
  }

  branche();
  bas();
})();
