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
  // Daily podcast: native controls, local resume, and no autoplay or read-state side effects.
  const podcastCards = [...document.querySelectorAll('[data-podcast-reel]')];
  const podcastAudios = () => [...document.querySelectorAll('audio')];
  function podcastClock(seconds) {
    const total = Math.max(0, Math.floor(Number(seconds) || 0));
    return `${Math.floor(total / 60)}:${String(total % 60).padStart(2, '0')}`;
  }
  function podcastProgressKey(audio) {
    const card = audio.closest('[data-podcast-reel]');
    const source = card?.dataset.podcastDay || audio.getAttribute('src') || 'daily';
    return 'aldia.podcast.progress.' + source.replace(/[^a-z0-9_-]/gi, '-');
  }
  function podcastReadProgress(audio) {
    try {
      const value = Number(localStorage.getItem(podcastProgressKey(audio)));
      return Number.isFinite(value) && value > 0 ? value : 0;
    } catch { return 0; }
  }
  function podcastSaveProgress(audio) {
    if (!audio || !Number.isFinite(audio.currentTime) || audio.currentTime < 3 || audio.ended) return;
    try { localStorage.setItem(podcastProgressKey(audio), String(Math.floor(audio.currentTime))); } catch { /* Private mode. */ }
  }
  function podcastClearProgress(audio) {
    try { localStorage.removeItem(podcastProgressKey(audio)); } catch { /* Private mode. */ }
  }
  function podcastSetStatus(card, text, error = false) {
    const status = card.querySelector('[data-podcast-status]');
    if (!status) return;
    status.textContent = text;
    if (error) status.dataset.tone = 'error'; else delete status.dataset.tone;
  }
  function podcastSync(card, audio) {
    const toggle = card.querySelector('[data-podcast-toggle]');
    if (!toggle) return;
    const icon = card.querySelector('[data-podcast-icon]');
    const label = card.querySelector('[data-podcast-label]');
    const playing = !audio.paused && !audio.ended;
    toggle.hidden = false;
    toggle.setAttribute('aria-pressed', String(playing));
    toggle.setAttribute('aria-label', playing ? 'Pausar el resumen en audio' : 'Reproducir el resumen en audio');
    if (icon) icon.textContent = playing ? '❚❚' : '▶';
    if (label) label.textContent = playing ? 'Pausar' : (audio.currentTime > 2 && !audio.ended ? 'Continuar' : 'Escuchar resumen');
  }
  function podcastMediaSession(card, audio) {
    if (!('mediaSession' in navigator) || !('MediaMetadata' in window)) return;
    try {
      const heading = card.querySelector('.podcast-title')?.textContent.trim() || 'Resumen diario';
      const episode = card.querySelector('.podcast-episode-title')?.textContent.trim();
      navigator.mediaSession.metadata = new MediaMetadata({title: episode || heading, artist: 'Al día', album: heading});
      if (navigator.mediaSession.setActionHandler) {
        navigator.mediaSession.setActionHandler('play', () => { audio.play().catch(() => {}); });
        navigator.mediaSession.setActionHandler('pause', () => audio.pause());
      }
    } catch { /* Media Session is an enhancement; playback works without it. */ }
  }
  for (const card of podcastCards) {
    const audio = card.querySelector('[data-podcast-audio]');
    const toggle = card.querySelector('[data-podcast-toggle]');
    if (!audio) continue;
    const savedPosition = podcastReadProgress(audio);
    let restored = false, lastSaved = 0;
    if (savedPosition > 2) podcastSetStatus(card, `Continuar desde ${podcastClock(savedPosition)}. Pulsa reproducir.`);
    if (toggle) {
      toggle.hidden = false;
      toggle.addEventListener('click', () => {
        podcastSetStatus(card, audio.paused ? 'Preparando audio…' : 'Pausando…');
        if (audio.paused) {
          const attempt = audio.play();
          if (attempt?.catch) attempt.catch(() => {
            podcastSetStatus(card, 'No se pudo reproducir el audio. Usa el enlace de respaldo.', true);
            const fallback = card.querySelector('[data-podcast-error]'); if (fallback) fallback.hidden = false;
          });
        } else audio.pause();
      });
    }
    audio.addEventListener('click', () => { if (audio.paused) podcastSetStatus(card, 'Preparando audio…'); });
    audio.addEventListener('loadedmetadata', () => {
      if (!restored) {
        restored = true;
        const position = podcastReadProgress(audio);
        if (position > 2 && Number.isFinite(audio.duration) && position < audio.duration - 3) {
          audio.currentTime = position;
          podcastSetStatus(card, audio.paused
            ? `Continuar desde ${podcastClock(position)}. Pulsa reproducir.`
            : `Reproduciendo desde ${podcastClock(position)}.`);
        }
      }
      podcastSync(card, audio);
    });
    audio.addEventListener('play', () => {
      podcastMediaSession(card, audio);
      podcastSetStatus(card, 'Cargando audio…');
      podcastSync(card, audio);
    });
    audio.addEventListener('playing', () => { podcastSetStatus(card, 'Reproduciendo resumen.'); podcastSync(card, audio); });
    audio.addEventListener('waiting', () => podcastSetStatus(card, 'Cargando audio…'));
    audio.addEventListener('pause', () => {
      podcastSaveProgress(audio);
      podcastSetStatus(card, audio.ended ? 'Resumen terminado.' : 'En pausa. Puedes seguir deslizando.');
      podcastSync(card, audio);
    });
    audio.addEventListener('timeupdate', () => {
      const now = Date.now();
      if (now - lastSaved > 4000) { lastSaved = now; podcastSaveProgress(audio); }
    });
    audio.addEventListener('ended', () => {
      podcastClearProgress(audio);
      podcastSetStatus(card, 'Resumen terminado. Las noticias siguen sin marcarse como leídas.');
      podcastSync(card, audio);
    });
    audio.addEventListener('error', () => {
      podcastSetStatus(card, 'No se pudo cargar el audio. Usa el enlace de respaldo.', true);
      const fallback = card.querySelector('[data-podcast-error]'); if (fallback) fallback.hidden = false;
      podcastSync(card, audio);
    });
    window.addEventListener('pagehide', () => podcastSaveProgress(audio));
    document.addEventListener('visibilitychange', () => { if (document.hidden) podcastSaveProgress(audio); });
    podcastSync(card, audio);
  }
  // Only one audio source plays at a time; scrolling or changing cards never pauses the podcast.
  document.addEventListener('play', event => {
    if (!(event.target instanceof HTMLAudioElement)) return;
    for (const other of podcastAudios()) if (other !== event.target) other.pause();
  }, true);
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
  async function request(url, options = {}) {
    const controller = new AbortController();
    const timeout = setTimeout(() => controller.abort(), 15000);
    try {
      const response = await fetch(url, {...options, headers:{Accept:'application/json',...options.headers}, signal:controller.signal});
      if (response.status === 401) { location.assign('/login?next=' + encodeURIComponent(location.pathname + location.search)); throw new Error('Inicia sesión para continuar.'); }
      if (!response.ok) {
        let detail;
        try { const error = await response.json(); if (typeof error.detail === 'string') detail = error.detail; } catch {}
        throw new Error(detail || 'No se pudo confirmar el cambio. Intenta de nuevo.');
      }
      return response;
    } catch(e) {
      if (e.name === 'AbortError' || e instanceof TypeError) throw new Error('No se pudo confirmar el cambio. Revisa tu conexión y vuelve a intentarlo.');
      throw e;
    } finally { clearTimeout(timeout); }
  }
  function post(url, data = {}) {
    return request(url, {method:'POST', body:new URLSearchParams(data)});
  }
  function busy(button, label = 'Guardando…', disable = true) {
    document.querySelector('#notice').hidden = true;
    clearTimeout(noticeTimer);
    const wasDisabled = button.disabled;
    const oldLabel = button.getAttribute('aria-label');
    const oldDisabled = button.getAttribute('aria-disabled');
    const idle = document.createElement('span');
    idle.className = 'button-idle-content';
    idle.append(...button.childNodes);
    const progress = document.createElement('span');
    progress.className = 'button-busy-content';
    progress.setAttribute('aria-hidden','true');
    const text = document.createElement('span'); text.textContent = label; progress.append(text);
    button.replaceChildren(idle, progress);
    button.classList.add('is-busy'); button.dataset.busy = 'true';
    button.setAttribute('aria-busy','true'); button.setAttribute('aria-label',label);
    button.setAttribute('aria-disabled','true');
    if (disable) button.disabled = true;
    let active = true;
    return () => {
      if (!active) return;
      active = false;
      button.replaceChildren(...idle.childNodes);
      button.classList.remove('is-busy'); delete button.dataset.busy;
      button.removeAttribute('aria-busy');
      if (oldLabel === null) button.removeAttribute('aria-label'); else button.setAttribute('aria-label',oldLabel);
      if (oldDisabled === null) button.removeAttribute('aria-disabled'); else button.setAttribute('aria-disabled',oldDisabled);
      if (disable) button.disabled = wasDisabled;
    };
  }
  const navigation = document.querySelector('#page-loading');
  let navigationTarget, navigationTimer;
  const submittedForms = [];
  function startNavigation(label, target) {
    if (!navigation) return;
    document.querySelector('#notice').hidden = true;
    clearTimeout(noticeTimer);
    if (navigationTarget) { navigationTarget.classList.remove('is-navigation-target'); navigationTarget.removeAttribute('aria-busy'); }
    navigationTarget = target;
    if (target) { target.classList.add('is-navigation-target'); target.setAttribute('aria-busy','true'); }
    navigation.querySelector('[data-navigation-label]').textContent = label;
    navigation.hidden = false; document.body.classList.add('is-navigating');
    clearTimeout(navigationTimer);
    navigationTimer = setTimeout(() => { navigation.querySelector('[data-navigation-label]').textContent = 'La sección está tardando en cargar…'; }, 15000);
  }
  document.addEventListener('click', event => {
    const link = event.target.closest('a[href]');
    if (!link || event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey
        || link.hasAttribute('download') || (link.target && link.target !== '_self')) return;
    const target = new URL(link.href, location.href);
    if (target.origin !== location.origin || (target.hash && target.pathname === location.pathname && target.search === location.search)) return;
    startNavigation('Cargando sección…',link);
  });
  document.addEventListener('submit', event => {
    if (event.defaultPrevented || event.target.method === 'dialog') return;
    const form = event.target;
    if (form.dataset.submitting) { event.preventDefault(); return; }
    form.dataset.submitting = 'true';
    const label = form.method.toLowerCase() === 'get' ? 'Cargando…' : 'Guardando…';
    // Keep the native submitter enabled: disabling it would omit name=action/value from the request.
    const finish = event.submitter instanceof HTMLButtonElement ? busy(event.submitter,label,false) : () => {};
    submittedForms.push(() => { finish(); delete form.dataset.submitting; });
    startNavigation(label,null);
  });
  window.addEventListener('pageshow', () => {
    if (navigation) navigation.hidden = true;
    document.body.classList.remove('is-navigating'); clearTimeout(navigationTimer);
    if (navigationTarget) { navigationTarget.classList.remove('is-navigation-target'); navigationTarget.removeAttribute('aria-busy'); navigationTarget = null; }
    submittedForms.splice(0).forEach(finish => finish());
  });
  let countTimer, countVersion = 0;
  function refreshPendingCount(delta) {
    document.querySelectorAll('[data-pending-count]').forEach(el => { el.textContent = Math.max(0,Number(el.textContent) + delta); });
    const version = ++countVersion;
    clearTimeout(countTimer);
    countTimer = setTimeout(async () => {
      try {
        const params = new URLSearchParams(location.search);
        params.set('view','pending'); params.set('limit','1'); params.delete('after');
        const library = await (await request('/reels/library?' + params.toString())).json();
        if (version === countVersion) document.querySelectorAll('[data-pending-count]').forEach(el => { el.textContent = library.counts.pending; });
      } catch { /* The read was persisted; a background badge refresh never blocks reading. */ }
    }, 300);
  }
  async function waitForRefresh() {
    const deadline = Date.now() + 120000;
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve,1500));
      const params = new URLSearchParams({since:feed?.dataset.version || '0',hours:feed?.dataset.hours || '24',view:feed?.dataset.view || 'pending'});
      const state = await (await request('/reels/state?' + params.toString())).json();
      if (state.new_articles > 0) {
        const newest = document.querySelector('[data-new-stories]');
        if (newest) { newest.textContent = 'Hay noticias nuevas · Actualizar'; newest.hidden = false; }
      }
      if (!state.running) { notify(state.new_articles > 0 ? 'Consulta finalizada. Hay noticias nuevas para cargar.' : 'Consulta finalizada. No hay noticias nuevas.'); return; }
    }
    notify('La consulta sigue en segundo plano. Las novedades aparecerán cuando termine.');
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
      const target = event.target instanceof Element ? event.target : null;
      // Native audio, seek controls, details and form controls keep their own arrow keys.
      if (!target || target.closest('input, textarea, select, button, audio, video, summary, [contenteditable="true"]')
          || event.ctrlKey || event.metaKey || event.altKey) return;
      if (['ArrowDown','PageDown','j','ArrowUp','PageUp','k'].includes(event.key)) {
        // Keep long story text scrollable before advancing to another card.
        if (target.closest('.story-copy, .podcast-details-content')) return;
        event.preventDefault();
        step(['ArrowDown','PageDown','j'].includes(event.key) ? 1 : -1);
      }
    });
    const newest = document.querySelector('[data-new-stories]');
    newest.addEventListener('click', () => { startNavigation('Cargando noticias…',null); location.reload(); });
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
    if (!button || button.disabled || button.dataset.busy) return;
    if (button.dataset.confirm && !confirm(button.dataset.confirm)) { event.preventDefault(); return; }
    if ('step' in button.dataset) { step(Number(button.dataset.step)); return; }
    if ('saveView' in button.dataset) {
      const form = document.querySelector('#reader-filters');
      const input = form.querySelector('[name=name]');
      if (!input.value.trim()) { input.setCustomValidity('Escribe un nombre para la vista.'); input.reportValidity(); input.addEventListener('input', () => input.setCustomValidity(''), {once:true}); return; }
      const finish = busy(button);
      try {
        const response = await post('/reels/views', Object.fromEntries(new FormData(form)));
        const view = await response.json();
        finish(); startNavigation('Abriendo vista…',null);
        location.assign(view.url);
      } catch(e) { finish(); notify(e.message); }
      return;
    }
    if ('reaction' in button.dataset) {
      const group = button.closest('.story-feedback');
      const buttons = [...group.querySelectorAll('[data-reaction]')];
      const previous = group.dataset.rating;
      const value = previous === button.dataset.reaction ? '0' : button.dataset.reaction;
      const apply = rating => { group.dataset.rating = String(rating); buttons.forEach(el => el.setAttribute('aria-pressed',String(el.dataset.reaction === String(rating)))); };
      apply(value);
      const finish = busy(button);
      buttons.forEach(el => { el.disabled = true; });
      try {
        const response = await post('/reels/reaction/' + button.dataset.itemId, {value});
        const result = await response.json();
        apply(result.rating);
        notify(result.rating === 0 ? 'Reacción quitada.' : result.rating === 1
          ? 'Me interesa guardado. Se priorizarán noticias parecidas al abrir Actualidad.'
          : 'No me interesa guardado. Las noticias parecidas bajarán al abrir Actualidad.');
      } catch(e) { apply(previous); notify(e.message); }
      finally { finish(); buttons.forEach(el => { el.disabled = false; }); }
      return;
    }
    if ('save' in button.dataset) {
      if (!button.dataset.ids) return;
      const previous = button.getAttribute('aria-pressed');
      const value = previous !== 'true';
      const finish = busy(button);
      button.setAttribute('aria-pressed',String(value));
      try {
        await post('/api/saved', {ids:button.dataset.ids,value:value ? '1' : '0'});
        finish();
        button.setAttribute('aria-label', value ? 'Quitar de guardados' : 'Guardar noticia');
        notify(value ? 'Noticia guardada para después.' : 'Noticia retirada de guardados.');
      } catch (e) { button.setAttribute('aria-pressed',previous); notify(e.message); } finally { finish(); }
    }
    if ('read' in button.dataset) {
      if (!button.dataset.ids) return;
      const finish = busy(button);
      try {
        const value = button.dataset.readValue || '1';
        await post('/api/read', {ids:button.dataset.ids,value});
        finish();
        button.dataset.readValue = value === '1' ? '0' : '1';
        button.setAttribute('aria-label',value === '1' ? 'Volver a pendientes' : 'Marcar como leída');
        button.querySelector('span').textContent = value === '1' ? 'Pendiente' : 'Leída';
        notify(value === '1' ? 'Marcada como leída.' : 'Devuelta a pendientes.');
        refreshPendingCount(value === '1' ? -1 : 1);
        if (button.closest('.reel') === cards[current]) step(1);
      } catch(e) { notify(e.message); } finally { finish(); }
    }
    if ('share' in button.dataset) {
      const finish = busy(button,'Abriendo…');
      try {
        if (navigator.share) await navigator.share({title:button.dataset.title,url:button.dataset.url});
        else { await navigator.clipboard.writeText(button.dataset.url); notify('Enlace copiado.'); }
      } catch(e) { if (e.name !== 'AbortError') notify('No se pudo compartir el enlace.'); } finally { finish(); }
    }
    if ('refresh' in button.dataset) {
      const refreshButtons = [...document.querySelectorAll('[data-refresh]')];
      const finish = refreshButtons.map(el => busy(el,'Consultando…'));
      try { await post('/reels/refresh'); notify('Consultando fuentes. Puedes seguir leyendo mientras tanto.'); await waitForRefresh(); }
      catch(e) { notify(e.message); }
      finally { finish.forEach(done => done()); }
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
