// Content script: инъекция кнопки «→ В очередь» на поддерживаемых сайтах (ТЗ §9.4).
//
// Архитектура (Task 2-3-4-5-6-7):
//   • Одна кнопка на пост (article) — не одна на всю страницу.
//   • Кнопка живёт внутри article → при скролле article убирается из DOM,
//     и кнопка уходит вместе с ним. Новые article'ы получают новые кнопки
//     через MutationObserver (SPA: x.com/pikabu/pixiv).
//   • Multi-media пост: кнопка показывает счётчик "(N)" и открывает пикер
//     с чекбоксами + "Отправить выбранные". Каждое выбранное медиа уходит
//     отдельным ENQUEUE-сообщением (последовательно, await).
//   • Blob: видео (Twitter/Pikabu HLS) — отправляем pageUrl с mediaUrl=null
//     и mode=A → бэкенд использует yt-dlp. Превью берём из video.poster.
//   • Дедупликация: каждое отправленное медиа помечается в chrome.storage.local.
//
// Важно (CSP сайтов):
//   - Только addEventListener, никаких inline-обработчиков.
//   - Стили в content.css (подключён через manifest), без inline <style>.
(() => {
  "use strict";

  const PICKER_ID = "tgq-picker";

  const config = getConfigForHostname(location.hostname);
  if (!config) return;

  // --- Дедупликация (storage) ---
  function dedupeKey(mediaUrl) {
    const key = mediaUrl || location.href;
    return `sent:${location.origin}|${key}`;
  }
  async function wasSent(mediaUrl) {
    const k = dedupeKey(mediaUrl);
    try {
      const res = await chrome.storage.local.get(k);
      return !!res[k];
    } catch (e) {
      return false;
    }
  }
  async function markSent(mediaUrl, postId) {
    try {
      await chrome.storage.local.set({
        [dedupeKey(mediaUrl)]: { at: Date.now(), post_id: postId },
      });
    } catch (e) {
      /* storage может быть недоступно в selten случаях */
    }
  }

  // --- Найти все article'ы на странице, в которых ещё нет кнопки и есть медиа ---
  function findArticlesNeedingButtons() {
    const sel = config.articleSelector || "article";
    let articles;
    try {
      articles = document.querySelectorAll(sel);
    } catch (e) {
      return [];
    }
    const result = [];
    articles.forEach((a, i) => {
      if (a.querySelector(".tgq-article-btn")) return; // уже есть кнопка
      const media = collectMediaInArticle(a, config);
      if (media.length > 0) result.push({ article: a, media, index: i });
    });
    return result;
  }

  // --- Создание кнопки для одного article ---
  function makeArticleButton(articleEl, mediaList) {
    const btn = document.createElement("button");
    btn.className = "tgq-btn tgq-article-btn";
    btn.type = "button";
    btn.textContent =
      mediaList.length > 1 ? `→ В очередь (${mediaList.length})` : "→ В очередь";
    btn.title = "Отправить в очередь TG Media Queue Bot";

    if (mediaList.length === 1) {
      // Single-media → send immediately.
      btn.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        sendSingle(mediaList[0], btn, articleEl);
      });
    } else {
      // Multi-media → open selection picker with checkboxes.
      btn.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        openSelectionPicker(mediaList, articleEl, btn);
      });
    }

    // Асинхронная проверка дедупликации — обновить метку кнопки.
    (async () => {
      try {
        const sent = await Promise.all(mediaList.map((m) => wasSent(m.url)));
        const sentCount = sent.filter(Boolean).length;
        if (sentCount === mediaList.length && mediaList.length > 0) {
          btn.textContent = "✓ Все в очереди";
          btn.disabled = true;
          btn.classList.add("tgq-done");
        } else if (sentCount > 0) {
          btn.textContent = `→ В очередь (${sentCount}/${mediaList.length} ✓)`;
        }
      } catch (e) {
        /* ignore */
      }
    })();

    return btn;
  }

  // --- Размещение кнопки внутри article (anchor + position) ---
  async function placeButtonInArticle(btn, articleEl) {
    const anchorSel = config.buttonAnchorSelector;
    let anchor = null;
    if (anchorSel) {
      for (const sel of anchorSel.split(",").map((s) => s.trim())) {
        if (!sel) continue;
        try {
          const el = articleEl.querySelector(sel);
          if (el) {
            anchor = el;
            break;
          }
        } catch (e) {
          /* invalid selector — skip */
        }
      }
    }
    if (anchor) {
      const pos = config.buttonPosition || "append";
      try {
        if (pos === "append") anchor.appendChild(btn);
        else if (pos === "prepend") anchor.insertBefore(btn, anchor.firstChild);
        else if (pos === "before") anchor.parentNode.insertBefore(btn, anchor);
        else if (pos === "after") anchor.parentNode.insertBefore(btn, anchor.nextSibling);
        else anchor.appendChild(btn);
      } catch (e) {
        // anchor был удалён за время — fallback
        btn.classList.add("tgq-pinned-article");
        articleEl.insertBefore(btn, articleEl.firstChild);
      }
    } else {
      // Fallback: в начало article.
      btn.classList.add("tgq-pinned-article");
      articleEl.insertBefore(btn, articleEl.firstChild);
    }
  }

  // --- Отправка одного медиа ---
  async function sendSingle(media, btn, articleEl) {
    if (btn.disabled) return;
    btn.disabled = true;
    btn.textContent = "… Отправка";

    const isBlobVideo = !media.url && media.kind === "video";
    // blob: видео → mediaUrl=null + mode=A (бэкенд использует yt-dlp на pageUrl).
    const payload = {
      type: "ENQUEUE",
      pageUrl: location.href,
      mediaUrl: media.url,
      referer: config.needsReferer ? location.origin + "/" : null,
      tags: extractTags(config),
      title: document.title,
      site: location.hostname.replace(/^www\./, ""),
      mode: isBlobVideo ? "A" : config.mode,
    };

    try {
      const resp = await chrome.runtime.sendMessage(payload);
      if (resp && resp.ok) {
        btn.textContent = resp.duplicate ? "✓ Уже было" : "✓ В очереди";
        btn.classList.add("tgq-done");
        const msg = isBlobVideo
          ? "Видео отправлено как ссылка (бот скачает через yt-dlp)"
          : resp.duplicate
          ? "Дубликат #" + resp.post_id
          : "Добавлено в очередь #" + resp.post_id;
        tgqShowToast(msg, "ok");
        await markSent(media.url || location.href, resp.post_id);
      } else {
        btn.textContent = "✗ Ошибка";
        btn.classList.add("tgq-error");
        btn.disabled = false; // разрешить повтор
        tgqShowToast(resp?.error || "Ошибка отправки", "err");
      }
    } catch (e) {
      btn.textContent = "✗ Нет связи";
      btn.classList.add("tgq-error");
      btn.disabled = false;
      tgqShowToast("Нет связи с ботом — проверьте настройки", "err");
    }
  }

  // --- Пикер выбора медиа (multi-media post) ---
  function openSelectionPicker(mediaList, articleEl, sourceBtn) {
    const existing = document.getElementById(PICKER_ID);
    if (existing) existing.remove();
    if (!document.body) return;

    const overlay = document.createElement("div");
    overlay.id = PICKER_ID;
    overlay.className = "tgq-picker-multi";

    const head = document.createElement("div");
    head.className = "tgq-picker-head";
    head.textContent = `Выберите медиа для отправки (${mediaList.length})`;
    overlay.appendChild(head);

    const sub = document.createElement("div");
    sub.className = "tgq-picker-sub";
    sub.textContent =
      "Отметьте элементы и нажмите «Отправить выбранные». Каждое медиа будет отправлено отдельным сообщением в очередь.";
    overlay.appendChild(sub);

    // Тулбар: select all / deselect all.
    const toolbar = document.createElement("div");
    toolbar.className = "tgq-picker-toolbar";
    const selectAll = document.createElement("button");
    selectAll.type = "button";
    selectAll.className = "tgq-picker-toolbtn";
    selectAll.textContent = "Выделить все";
    const deselectAll = document.createElement("button");
    deselectAll.type = "button";
    deselectAll.className = "tgq-picker-toolbtn";
    deselectAll.textContent = "Снять выделение";
    toolbar.appendChild(selectAll);
    toolbar.appendChild(deselectAll);
    overlay.appendChild(toolbar);

    const closeBtn = document.createElement("button");
    closeBtn.className = "tgq-pick-close";
    closeBtn.type = "button";
    closeBtn.textContent = "✕";
    closeBtn.title = "Закрыть";
    closeBtn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      overlay.remove();
    });
    overlay.appendChild(closeBtn);

    // Сетка карточек.
    const grid = document.createElement("div");
    grid.className = "tgq-picker-grid";

    const items = [];
    for (const media of mediaList) {
      const card = document.createElement("label");
      card.className = "tgq-pick-item selected";

      // Превью.
      let thumbEl;
      const isVideo = media.kind === "video";
      const isGif = media.kind === "gif";
      const poster =
        media.poster ||
        (media.el && media.el.getAttribute ? media.el.getAttribute("poster") : null);

      if (isVideo) {
        if (poster) {
          thumbEl = document.createElement("img");
          thumbEl.src = poster;
          thumbEl.alt = "";
          thumbEl.loading = "lazy";
          thumbEl.addEventListener("error", () => {
            thumbEl.style.opacity = "0.3";
          });
        } else if (media.url && /^https?:/.test(media.url) && !media.url.startsWith("blob:")) {
          thumbEl = document.createElement("video");
          thumbEl.src = media.url;
          thumbEl.muted = true;
          thumbEl.preload = "metadata";
        } else {
          thumbEl = document.createElement("div");
          thumbEl.className = "tgq-pick-thumb-placeholder";
          thumbEl.textContent = "🎬";
        }
      } else {
        thumbEl = document.createElement("img");
        if (media.url && /^https?:/.test(media.url)) {
          thumbEl.src = media.url;
          thumbEl.alt = "";
          thumbEl.loading = "lazy";
          thumbEl.addEventListener("error", () => {
            thumbEl.style.opacity = "0.3";
          });
        } else {
          thumbEl.className = "tgq-pick-thumb-placeholder";
          thumbEl.textContent = "🖼";
        }
      }
      if (thumbEl) card.appendChild(thumbEl);

      // Чекбокс + подпись.
      const cbRow = document.createElement("div");
      cbRow.className = "tgq-pick-row";
      const cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = true;
      cb.className = "tgq-picker-checkbox";
      const cbLabel = document.createElement("span");
      cbLabel.className = "tgq-pick-label";
      const kindBadge = isVideo ? "видео" : isGif ? "gif" : "img";
      const filename = (media.url || "").split("/").pop().split("?")[0] || "(blob)";
      cbLabel.textContent = `[${kindBadge}] ${truncate(filename, 28)}`;
      cbRow.appendChild(cb);
      cbRow.appendChild(cbLabel);
      card.appendChild(cbRow);

      cb.addEventListener("change", () => {
        card.classList.toggle("selected", cb.checked);
        updateSendBtn();
      });

      items.push({ media, cb, card });
      grid.appendChild(card);
    }
    overlay.appendChild(grid);

    // Кнопка отправки.
    const sendBtn = document.createElement("button");
    sendBtn.type = "button";
    sendBtn.className = "tgq-send-selected";
    overlay.appendChild(sendBtn);

    function updateSendBtn() {
      const n = items.filter((i) => i.cb.checked).length;
      sendBtn.textContent = n > 0 ? `Отправить выбранные (${n})` : "Нет выбранных";
      sendBtn.disabled = n === 0;
    }
    updateSendBtn();

    selectAll.addEventListener("click", () => {
      items.forEach((i) => {
        i.cb.checked = true;
        i.card.classList.add("selected");
      });
      updateSendBtn();
    });
    deselectAll.addEventListener("click", () => {
      items.forEach((i) => {
        i.cb.checked = false;
        i.card.classList.remove("selected");
      });
      updateSendBtn();
    });

    sendBtn.addEventListener("click", async (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      const selected = items.filter((i) => i.cb.checked).map((i) => i.media);
      if (!selected.length) return;
      sendBtn.disabled = true;
      let sent = 0;
      let failed = 0;
      let wasBlobVideo = false;

      for (let i = 0; i < selected.length; i++) {
        const m = selected[i];
        const isBlob = !m.url && m.kind === "video";
        if (isBlob) wasBlobVideo = true;
        sendBtn.textContent = `Отправка ${i + 1}/${selected.length}…`;
        const payload = {
          type: "ENQUEUE",
          pageUrl: location.href,
          mediaUrl: m.url,
          referer: config.needsReferer ? location.origin + "/" : null,
          tags: extractTags(config),
          title: document.title,
          site: location.hostname.replace(/^www\./, ""),
          mode: isBlob ? "A" : config.mode,
        };
        try {
          const resp = await chrome.runtime.sendMessage(payload);
          if (resp && resp.ok) {
            sent++;
            await markSent(m.url || location.href, resp.post_id);
          } else {
            failed++;
            console.warn("[tgq] enqueue failed:", resp?.error);
          }
        } catch (e) {
          failed++;
          console.warn("[tgq] enqueue error:", e);
        }
      }
      overlay.remove();
      if (sent > 0) {
        if (wasBlobVideo && failed === 0) {
          tgqShowToast(
            `Видео отправлено как ссылка (бот скачает через yt-dlp). Успешно: ${sent}`,
            "ok"
          );
        } else {
          const msg = `Отправлено ${sent} медиа` + (failed > 0 ? `, ошибок: ${failed}` : "");
          tgqShowToast(msg, failed > 0 ? "err" : "ok");
        }
        if (sourceBtn) {
          if (failed === 0) {
            sourceBtn.textContent = "✓ Все в очереди";
            sourceBtn.classList.add("tgq-done");
            sourceBtn.disabled = true;
          } else {
            sourceBtn.textContent = `✓ ${sent}/${selected.length}`;
            sourceBtn.classList.add("tgq-done");
          }
        }
      } else {
        tgqShowToast("Не удалось отправить ни одного медиа", "err");
      }
    });

    // Закрытие по клику на backdrop.
    overlay.addEventListener("click", (ev) => {
      if (ev.target === overlay) overlay.remove();
    });

    // ESC закрывает пикер.
    const onKey = (ev) => {
      if (ev.key === "Escape") {
        overlay.remove();
        document.removeEventListener("keydown", onKey);
      }
    };
    document.addEventListener("keydown", onKey);

    document.body.appendChild(overlay);
  }

  function truncate(s, n) {
    if (!s) return "";
    return s.length > n ? s.slice(0, n) + "…" : s;
  }

  // --- Инъекция (debounced, guard от реентерабельности) ---
  let injecting = false;
  function scheduleInject() {
    if (injecting) return;
    injecting = true;
    requestAnimationFrame(() => {
      Promise.resolve(injectButtons())
        .catch(() => {})
        .finally(() => {
          injecting = false;
        });
    });
  }

  async function injectButtons() {
    const list = findArticlesNeedingButtons();
    for (const { article, media } of list) {
      try {
        const btn = makeArticleButton(article, media);
        await placeButtonInArticle(btn, article);
      } catch (e) {
        /* skip broken article */
      }
    }
  }

  // --- Старт + наблюдение за SPA (ТЗ §9.6) ---
  if (config.spa) {
    let t = null;
    const obs = new MutationObserver(() => {
      clearTimeout(t);
      t = setTimeout(scheduleInject, 400);
    });
    obs.observe(document.documentElement, { childList: true, subtree: true });
  }
  scheduleInject();
  // Отложенные попытки: тяжёлые страницы (x.com/pixiv) рендерят медиа позже.
  setTimeout(scheduleInject, 1500);
  setTimeout(scheduleInject, 4000);
})();
