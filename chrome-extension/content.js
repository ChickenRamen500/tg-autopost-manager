// Content script: инъекция кнопки «→ В очередь» на поддерживаемых сайтах (ТЗ §9.4).
//
// Важно (ТЗ §9.6):
//  - Никаких inline-обработчиков (CSP сайтов) — только addEventListener.
//  - SPA (x.com, pikabu) — переинъекция через MutationObserver с debounce.
//  - Дедупликация: отправленные страницы помнятся в chrome.storage.local,
//    кнопка становится «✓ Уже в очереди».
(() => {
  "use strict";

  const BTN_ID = "tg-queue-btn";
  const STYLE_ID = "tg-queue-btn-style";

  const config = getConfigForHostname(location.hostname);
  if (!config) return;

  // --- Стили кнопки (CSS через <style>, не inline — безопасно для CSP) ---
  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement("style");
    style.id = STYLE_ID;
    style.textContent = `
      #${BTN_ID} {
        all: initial;
        display: inline-flex; align-items: center; gap: 6px;
        padding: 8px 14px; margin: 8px;
        background: #2AABEE; color: #fff !important;
        font: 600 14px/1.2 -apple-system, "Segoe UI", Roboto, Arial, sans-serif;
        border: none; border-radius: 8px; cursor: pointer;
        box-shadow: 0 2px 8px rgba(0,0,0,.25);
        z-index: 2147483647; position: relative; opacity: 0.95;
      }
      #${BTN_ID}:hover { opacity: 1; }
      #${BTN_ID}[disabled] { background: #7a8691; cursor: default; }
      #${BTN_ID}.done { background: #4caf50; }
      #${BTN_ID}.error { background: #e53935; }
    `;
    (document.head || document.documentElement).appendChild(style);
  }

  // Ключ дедупликации в storage — страница + медиа URL
  function dedupeKey(mediaUrl) {
    return `sent:${location.origin}|${mediaUrl}`;
  }

  async function wasSent(mediaUrl) {
    const k = dedupeKey(mediaUrl);
    const res = await chrome.storage.local.get(k);
    return !!res[k];
  }

  async function markSent(mediaUrl, postId) {
    await chrome.storage.local.set({ [dedupeKey(mediaUrl)]: { at: Date.now(), post_id: postId } });
  }

  function makeButton(mediaUrl) {
    const btn = document.createElement("button");
    btn.id = BTN_ID;
    btn.type = "button";
    btn.textContent = "→ В очередь";
    btn.title = "Отправить этот пост в очередь TG Media Queue Bot";

    btn.addEventListener("click", async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (btn.disabled) return;
      btn.disabled = true;
      btn.textContent = "… Отправка";

      const payload = {
        type: "ENQUEUE",
        pageUrl: location.href,
        mediaUrl: mediaUrl,
        referer: config.needsReferer ? location.origin + "/" : null,
        tags: extractTags(config),
        title: document.title,
        site: location.hostname.replace(/^www\./, ""),
        mode: config.mode,
      };

      try {
        const resp = await chrome.runtime.sendMessage(payload);
        if (resp && resp.ok) {
          btn.textContent = resp.duplicate ? "✓ Уже было" : "✓ В очереди";
          btn.classList.add("done");
          await markSent(mediaUrl, resp.post_id);
        } else {
          btn.textContent = (resp && resp.error) ? "✗ " + truncate(resp.error) : "✗ Ошибка";
          btn.classList.add("error");
          btn.disabled = false; // разрешить повтор
        }
      } catch (e) {
        btn.textContent = "✗ Нет связи с ботом";
        btn.classList.add("error");
        btn.disabled = false;
      }
    });
    return btn;
  }

  function truncate(s) {
    return s && s.length > 24 ? s.slice(0, 24) + "…" : s;
  }

  async function placeButton(btn, anchorEl) {
    // Рядом с медиа-блоком, fallback — закрепить вверху страницы
    const host =
      anchorEl.closest("#image-container, .story_image, article, #show-post, section") ||
      anchorEl.parentElement;
    if (host) {
      host.insertBefore(btn, host.firstChild);
    } else {
      btn.style.position = "fixed";
      btn.style.top = "10px";
      btn.style.left = "10px";
      document.body.prepend(btn);
    }
  }

  let injecting = false;

  async function injectButtons() {
    if (injecting) return;
    if (document.getElementById(BTN_ID)) return; // уже есть
    const found = findBestMedia(config);
    if (!found) return;
    injecting = true;
    try {
      ensureStyle();
      if (!document.body) return;
      const btn = makeButton(found.url);
      if (await wasSent(found.url)) {
        btn.textContent = "✓ Уже в очереди";
        btn.disabled = true;
        btn.classList.add("done");
      }
      await placeButton(btn, found.el);
    } finally {
      injecting = false;
    }
  }

  // --- Старт + наблюдение за SPA (ТЗ §9.6) ---
  if (config.spa) {
    let t = null;
    const obs = new MutationObserver(() => {
      clearTimeout(t);
      t = setTimeout(injectButtons, 400);
    });
    obs.observe(document.documentElement, { childList: true, subtree: true });
  }
  injectButtons();
  // Отложенная вторая попытка: тяжёлые страницы (x.com) рендерят медиа позже
  setTimeout(injectButtons, 1500);
  setTimeout(injectButtons, 4000);
})();
