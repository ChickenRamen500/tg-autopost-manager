// Content script: инъекция кнопки «→ В очередь» на поддерживаемых сайтах (ТЗ §9.4).
//
// Важно (ТЗ §9.6):
//  - Никаких inline-обработчиков (CSP сайтов) — только addEventListener.
//  - SPA (x.com, pikabu) — переинъекция через MutationObserver с debounce.
//  - Дедупликация: отправленные страницы помечаются в chrome.storage.local,
//    кнопка становится «✓ Уже в очереди».
//  - Стили вынесены в content.css (подключается через manifest), без inline <style> (CSP-safe).
(() => {
  "use strict";

  const BTN_ID = "tg-queue-btn";
  const PICK_BTN_ID = "tg-queue-pick-btn";
  const PICKER_ID = "tgq-picker";

  const config = getConfigForHostname(location.hostname);
  if (!config) return;

  // Ключ дедупликации в storage — origin + (mediaUrl или page URL как fallback).
  function dedupeKey(mediaUrl) {
    const key = mediaUrl || location.href;
    return `sent:${location.origin}|${key}`;
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
    btn.className = "tgq-btn";
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
          btn.classList.add("tgq-done");
          tgqShowToast(
            resp.duplicate
              ? "Уже в очереди (дубликат #" + resp.post_id + ")"
              : "Добавлено в очередь #" + resp.post_id,
            "ok"
          );
          await markSent(mediaUrl, resp.post_id);
        } else {
          btn.textContent = (resp && resp.error) ? "✗ " + truncate(resp.error) : "✗ Ошибка";
          btn.classList.add("tgq-error");
          btn.disabled = false; // разрешить повтор
          tgqShowToast(resp?.error || "Ошибка отправки", "err");
        }
      } catch (e) {
        btn.textContent = "✗ Нет связи с ботом";
        btn.classList.add("tgq-error");
        btn.disabled = false;
        tgqShowToast("Нет связи с ботом — проверьте настройки", "err");
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
      btn.classList.add("tgq-pinned");
      document.body.prepend(btn);
    }
  }

  let injecting = false;

  async function injectButtons() {
    if (injecting) return;
    const found = findBestMedia(config);
    if (!found) {
      // Медиа не найдено автоматически — показываем кнопку запуска пикера
      ensurePickerButton();
      return;
    }
    // Убираем кнопку-пикер, если основное медиа нашлось
    removePickerButton();
    const existing = document.getElementById(BTN_ID);
    if (existing && existing.dataset.mediaUrl === found.url) return; // up-to-date
    if (existing) existing.remove();
    injecting = true;
    try {
      if (!document.body) return;
      const btn = makeButton(found.url);
      btn.dataset.mediaUrl = found.url;
      if (await wasSent(found.url)) {
        btn.textContent = "✓ Уже в очереди";
        btn.disabled = true;
        btn.classList.add("tgq-done");
      }
      await placeButton(btn, found.el);
    } finally {
      injecting = false;
    }
  }

  // --- Media picker overlay (BUG-8) ---
  const MEDIA_PATTERN = /\.(jpe?g|png|gif|webp|mp4|webm)(\?|#|$)/i;

  function ensurePickerButton() {
    if (document.getElementById(PICK_BTN_ID)) return;
    if (!document.body) return;
    const btn = document.createElement("button");
    btn.id = PICK_BTN_ID;
    btn.className = "tgq-btn tgq-pick";
    btn.type = "button";
    btn.textContent = "🎞 Выбрать медиа";
    btn.title = "Медиа не найдено автоматически — выберите вручную";
    btn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      openMediaPicker();
    });
    document.body.appendChild(btn);
  }

  function removePickerButton() {
    const el = document.getElementById(PICK_BTN_ID);
    if (el) el.remove();
  }

  function collectMediaCandidates() {
    const items = [];
    const seen = new Set();
    const push = (url, el, kind) => {
      if (!url) return;
      let abs;
      try { abs = new URL(url, location.href).href; } catch { return; }
      if (!/^https?:/.test(abs)) return;
      if (seen.has(abs)) return;
      seen.add(abs);
      items.push({ url: abs, el, kind });
    };

    document.querySelectorAll("img").forEach((el) => {
      const src = el.getAttribute("src") || el.getAttribute("data-src");
      if (src && MEDIA_PATTERN.test(src)) push(src, el, "img");
    });
    document.querySelectorAll("video").forEach((el) => {
      const src = el.getAttribute("src");
      if (src && !src.startsWith("blob:") && MEDIA_PATTERN.test(src)) push(src, el, "video");
      const sourceEl = el.querySelector("source");
      if (sourceEl) {
        const ssrc = sourceEl.getAttribute("src");
        if (ssrc && !ssrc.startsWith("blob:") && MEDIA_PATTERN.test(ssrc)) push(ssrc, sourceEl, "video");
      }
    });
    document.querySelectorAll("source").forEach((el) => {
      const src = el.getAttribute("src");
      if (src && !src.startsWith("blob:") && MEDIA_PATTERN.test(src)) push(src, el, "video");
    });
    document.querySelectorAll("a").forEach((el) => {
      const href = el.getAttribute("href");
      if (href && MEDIA_PATTERN.test(href)) push(href, el, "link");
    });

    return items;
  }

  function openMediaPicker() {
    if (document.getElementById(PICKER_ID)) return;
    if (!document.body) return;

    const overlay = document.createElement("div");
    overlay.id = PICKER_ID;

    const head = document.createElement("div");
    head.className = "tgq-picker-head";
    head.textContent = "Выберите медиа для отправки в очередь";
    overlay.appendChild(head);

    const sub = document.createElement("div");
    sub.className = "tgq-picker-sub";
    sub.textContent = "Кликните по элементу, чтобы поставить его в очередь.";
    overlay.appendChild(sub);

    const closeBtn = document.createElement("button");
    closeBtn.className = "tgq-pick-close";
    closeBtn.type = "button";
    closeBtn.textContent = "✕ Закрыть";
    closeBtn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      overlay.remove();
    });
    overlay.appendChild(closeBtn);

    const grid = document.createElement("div");
    grid.className = "tgq-picker-grid";

    const items = collectMediaCandidates();
    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "tgq-picker-sub";
      empty.textContent = "На странице не найдено подходящих медиа (jpg/png/gif/webp/mp4/webm).";
      grid.appendChild(empty);
    }

    for (const item of items) {
      const card = document.createElement("button");
      card.className = "tgq-pick-item";
      card.type = "button";

      if (item.kind === "video" || /\.(mp4|webm)(\?|#|$)/i.test(item.url)) {
        const v = document.createElement("video");
        v.src = item.url;
        v.muted = true;
        card.appendChild(v);
      } else {
        const img = document.createElement("img");
        img.src = item.url;
        img.alt = "";
        img.loading = "lazy";
        card.appendChild(img);
      }
      const label = document.createElement("span");
      label.textContent = item.url.split("/").pop().split("?")[0] || item.url;
      card.appendChild(label);

      card.addEventListener("click", async (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        overlay.remove();
        removePickerButton();
        const existing = document.getElementById(BTN_ID);
        if (existing) existing.remove();
        try {
          if (!document.body) return;
          const btn = makeButton(item.url);
          btn.dataset.mediaUrl = item.url;
          if (await wasSent(item.url)) {
            btn.textContent = "✓ Уже в очереди";
            btn.disabled = true;
            btn.classList.add("tgq-done");
          }
          await placeButton(btn, item.el);
        } catch (e) {
          tgqShowToast("Не удалось поставить кнопку: " + (e?.message || e), "err");
        }
      });
      grid.appendChild(card);
    }

    overlay.appendChild(grid);
    document.body.appendChild(overlay);
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
