/* Local Al día adaptation, November 2026. No analytics or third-party scripts. */
(() => {
  'use strict';
  const feed = document.querySelector('.reel-feed');
  const cards = feed ? [...feed.querySelectorAll('.reel')] : [];
  let current = 0;
  let noticeTimer;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)').matches;
  const relativeTimes = [...document.querySelectorAll('time[data-relative-time]')];
  function elapsedTime(stamp) {
    const time = Date.parse(stamp);
    if (!Number.isFinite(time)) return 'Fecha no informada';
    const seconds = Math.floor((Date.now() - time) / 1000);
    if (seconds < -300) return 'Fecha por confirmar';
    if (seconds < 60) return 'Hace un momento';
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `Hace ${minutes} minuto${minutes === 1 ? '' : 's'}`;
    const totalHours = Math.floor(minutes / 60);
    if (totalHours < 24) return `Hace ${totalHours} hora${totalHours === 1 ? '' : 's'}`;
    const days = Math.floor(totalHours / 24), hours = totalHours % 24;
    return `Hace ${days} día${days === 1 ? '' : 's'}`
      + (hours ? ` y ${hours} hora${hours === 1 ? '' : 's'}` : '');
  }
  function updateRelativeTimes() {
    for (const el of relativeTimes) {
      const text = elapsedTime(el.dateTime);
      el.textContent = el.hasAttribute('data-time-lower') ? text.toLowerCase() : text;
      el.setAttribute('aria-label', el.dataset.timeLabel + ': ' + text.toLowerCase());
    }
  }
  if (relativeTimes.length) {
    updateRelativeTimes();
    setInterval(() => { if (!document.hidden) updateRelativeTimes(); }, 60000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) updateRelativeTimes(); });
  }
  const menuToggle = document.querySelector('[data-reader-menu]');
  const menu = document.querySelector('#reader-menu');
  if (menuToggle && menu) {
    function showMenu(open, restoreFocus = false) {
      menu.hidden = !open;
      menuToggle.setAttribute('aria-expanded', String(open));
      menuToggle.setAttribute('aria-label', open ? 'Ocultar menú superior' : 'Mostrar menú superior');
      menuToggle.querySelector('[data-menu-label]').textContent = open ? 'Cerrar' : 'Menú';
      if (restoreFocus) menuToggle.focus({preventScroll:true});
    }
    menuToggle.addEventListener('click', () => showMenu(menu.hidden));
    document.addEventListener('click', event => {
      if (!menu.hidden && !menu.contains(event.target) && !menuToggle.contains(event.target)) showMenu(false);
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape' && !menu.hidden) showMenu(false, true);
    });
    feed?.addEventListener('scroll', () => {
      if (!menu.hidden) showMenu(false, menu.contains(document.activeElement));
    }, {passive:true});
  }
  function notify(message) {
    const el = document.querySelector('#notice');
    el.textContent = message;
    el.hidden = false;
    clearTimeout(noticeTimer);
    noticeTimer = setTimeout(() => { el.hidden = true; }, 5000);
  }
  async function post(url, data = {}) {
    const response = await fetch(url, {method:'POST', headers:{Accept:'application/json'},
      body: new URLSearchParams(data)});
    if (response.status === 401) { location.assign('/login?next=' + encodeURIComponent(location.pathname + location.search)); throw new Error('Inicia sesión para continuar.'); }
    if (!response.ok) {
      let detail;
      try { const error = await response.json(); if (typeof error.detail === 'string') detail = error.detail; } catch {}
      throw new Error(detail || 'No se pudo guardar el cambio. Intenta de nuevo.');
    }
    return response;
  }
  function step(delta) {
    const next = cards[Math.max(0, Math.min(cards.length - 1, current + delta))];
    if (next) next.scrollIntoView({behavior: reduced ? 'instant' : 'smooth', block:'start'});
  }
  if (feed) {
    const observer = new IntersectionObserver(entries => {
      for (const entry of entries) if (entry.isIntersecting && entry.intersectionRatio > .6) {
        current = cards.indexOf(entry.target);
      }
    }, {root:feed, threshold:.65});
    cards.forEach(card => observer.observe(card));
    document.addEventListener('keydown', event => {
      if (/INPUT|TEXTAREA|SELECT|BUTTON/.test(event.target.tagName) || event.ctrlKey || event.metaKey || event.altKey) return;
      if (['ArrowDown','PageDown','j','ArrowUp','PageUp','k'].includes(event.key)) {
        // Keep long story text scrollable before advancing to another card.
        if (event.target.closest('.story-copy')) return;
        event.preventDefault();
        step(['ArrowDown','PageDown','j'].includes(event.key) ? 1 : -1);
      }
    });
    const newest = document.querySelector('[data-new-stories]');
    newest.addEventListener('click', () => location.reload());
    async function check() {
      if (document.hidden) return;
      try {
        const response = await fetch('/reels/state?since=' + encodeURIComponent(feed.dataset.version) + '&hours=' + encodeURIComponent(feed.dataset.hours) + '&view=' + encodeURIComponent(feed.dataset.view));
        if (!response.ok) return;
        const state = await response.json();
        if (state.new_articles > 0) { newest.textContent = 'Hay noticias nuevas · Actualizar'; newest.hidden = false; }
      } catch { /* A temporary network failure should not interrupt reading. */ }
    }
    setInterval(check, 60000);
    document.addEventListener('visibilitychange', () => { if (!document.hidden) check(); });
  }
  document.addEventListener('click', async event => {
    const button = event.target.closest('button');
    if (!button) return;
    if (button.dataset.confirm && !confirm(button.dataset.confirm)) { event.preventDefault(); return; }
    if ('step' in button.dataset) { step(Number(button.dataset.step)); return; }
    if ('saveView' in button.dataset) {
      const form = document.querySelector('#reader-filters');
      const input = form.querySelector('[name=name]');
      if (!input.value.trim()) { input.setCustomValidity('Escribe un nombre para la vista.'); input.reportValidity(); input.addEventListener('input', () => input.setCustomValidity(''), {once:true}); return; }
      button.disabled = true;
      try {
        const response = await post('/reels/views', Object.fromEntries(new FormData(form)));
        location.assign((await response.json()).url);
      } catch(e) { notify(e.message); button.disabled = false; }
      return;
    }
    if ('reaction' in button.dataset) {
      const group = button.closest('.story-feedback');
      const buttons = [...group.querySelectorAll('[data-reaction]')];
      const value = group.dataset.rating === button.dataset.reaction ? '0' : button.dataset.reaction;
      buttons.forEach(el => { el.disabled = true; });
      try {
        const response = await post('/reels/reaction/' + button.dataset.itemId, {value});
        const result = await response.json();
        group.dataset.rating = String(result.rating);
        buttons.forEach(el => el.setAttribute('aria-pressed', String(Number(el.dataset.reaction) === result.rating)));
        notify(result.rating === 0 ? 'Reacción quitada.' : result.rating === 1
          ? 'Me interesa guardado. Se priorizarán noticias parecidas al abrir Actualidad.'
          : 'No me interesa guardado. Las noticias parecidas bajarán al abrir Actualidad.');
      } catch(e) { notify(e.message); }
      finally { buttons.forEach(el => { el.disabled = false; }); }
      return;
    }
    if ('save' in button.dataset) {
      button.disabled = true;
      try {
        const value = button.getAttribute('aria-pressed') !== 'true';
        await post('/api/saved', {ids:button.dataset.ids,value:value ? '1' : '0'});
        button.setAttribute('aria-pressed', String(value));
        button.setAttribute('aria-label', value ? 'Quitar de guardados' : 'Guardar noticia');
        notify(value ? 'Noticia guardada para después.' : 'Noticia retirada de guardados.');
      } catch (e) { notify(e.message); } finally { button.disabled = false; }
    }
    if ('read' in button.dataset) {
      button.disabled = true;
      try {
        const value = button.dataset.readValue || '1';
        await post('/api/read', {ids:button.dataset.ids,value});
        button.dataset.readValue = value === '1' ? '0' : '1';
        button.setAttribute('aria-label',value === '1' ? 'Volver a pendientes' : 'Marcar como leída');
        button.querySelector('span').textContent = value === '1' ? 'Pendiente' : 'Leída';
        notify(value === '1' ? 'Marcada como leída.' : 'Devuelta a pendientes.');
        try {
          const params = new URLSearchParams(location.search);
          params.set('view','pending'); params.set('limit','1'); params.delete('after');
          const countResponse = await fetch('/reels/library?' + params.toString());
          if (countResponse.ok) {
            const library = await countResponse.json();
            document.querySelectorAll('[data-pending-count]').forEach(el => { el.textContent = library.counts.pending; });
          }
        } catch { /* The persisted reading action succeeded even if the count cannot be refreshed. */ }
        step(1);
      } catch(e) { notify(e.message); } finally { button.disabled = false; }
    }
    if ('share' in button.dataset) {
      try {
        if (navigator.share) await navigator.share({title:button.dataset.title,url:button.dataset.url});
        else { await navigator.clipboard.writeText(button.dataset.url); notify('Enlace copiado.'); }
      } catch(e) { if (e.name !== 'AbortError') notify('No se pudo compartir el enlace.'); }
    }
    if ('refresh' in button.dataset) {
      button.disabled = true;
      try { await post('/reels/refresh'); notify('Consultando fuentes. Las novedades aparecerán cuando termine la consulta.'); }
      catch(e) { notify(e.message); }
      finally { button.disabled = false; }
    }
  });
  const kind = document.querySelector('#source-kind');
  if (kind) kind.addEventListener('change', () => {
    const rss = kind.value === 'rss';
    document.querySelector('[data-value-label]').textContent = rss ? 'Dirección RSS o Atom' : 'Búsqueda';
    document.querySelector('#source-value').placeholder = rss ? 'https://proveedor.com/blog/feed/' : '"nombre del proveedor" nuevos productos';
    document.querySelector('[data-source-help]').textContent = rss
      ? 'Añade el RSS de un medio, blog oficial o página de novedades. Puedes usar el buscador de fuentes en la configuración avanzada si solo tienes la dirección del sitio.'
      : 'Se consulta Google News en español. Puedes usar comillas y site:dominio.com para afinar la búsqueda. Cada búsqueda es una fuente independiente.';
  });
})();
