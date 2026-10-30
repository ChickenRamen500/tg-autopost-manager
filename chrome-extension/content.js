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
  // Ключ: origin + (mediaUrl или permalink).
  // Важно для blob-видео (Twitter/Pikabu): mediaUrl=null, но permalink уникален
  // для каждого поста. Раньше fallback был location.href, а на ленте x.com/home
  // это значение одинаково для ВСЕХ твитов → все помечались "уже в очереди".
  function dedupeKey(mediaUrl, permalink) {
    const key = mediaUrl || permalink || location.href;
    return `sent:${location.origin}|${key}`;
  }
  async function wasSent(mediaUrl, permalink) {
    const k = dedupeKey(mediaUrl, permalink);
    try {
      const res = await chrome.storage.local.get(k);
      return !!res[k];
    } catch (e) {
      return false;
    }
  }
  async function markSent(mediaUrl, permalink, postId) {
    try {
      await chrome.storage.local.set({
        [dedupeKey(mediaUrl, permalink)]: { at: Date.now(), post_id: postId },
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
    const permalink = getArticlePermalink(articleEl, config);

    const btn = document.createElement("button");
    btn.className = "tgq-btn tgq-article-btn";
    btn.type = "button";
    btn.textContent =
      mediaList.length > 1 ? `→ В очередь (${mediaList.length})` : "→ В очередь";
    btn.title = "Отправить в очередь TG Media Queue Bot";

    // Сохраняем permalink на кнопке — используется в sendSingle и пикере.
    btn.dataset.permalink = permalink;

    if (mediaList.length === 1) {
      // Single-media → send immediately.
      btn.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        sendSingle(mediaList[0], btn, articleEl, permalink);
      });
    } else {
      // Multi-media → open selection picker with checkboxes.
      btn.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        openSelectionPicker(mediaList, articleEl, btn, permalink);
      });
    }

    // Асинхронная проверка дедупликации — обновить метку кнопки.
    // Для каждого медиа используем permalink как fallback ключа (для blob-видео).
    (async () => {
      try {
        const sent = await Promise.all(
          mediaList.map((m) => wasSent(m.url, permalink))
        );
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
  async function sendSingle(media, btn, articleEl, permalink) {
    if (btn.disabled) return;
    btn.disabled = true;
    btn.textContent = "… Отправка";

    const isBlobVideo = !media.url && media.kind === "video";
    // blob: видео → mediaUrl=null + mode=A (бэкенд использует yt-dlp на permalink).
    // pageUrl = permalink (НЕ location.href!) — иначе бот качает HTML ленты x.com/home.
    const payload = {
      type: "ENQUEUE",
      pageUrl: permalink || location.href,
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
        await markSent(media.url, permalink, resp.post_id);
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
  // permalink нужен для:
  //   1) pageUrl в payload (а не location.href — иначе бот качает HTML ленты)
  //   2) генерации media_group_id — чтобы бэкенд объединил выбранные медиа в один альбом
  //      (scheduler отправит их одним сообщением через send_media_group,
  //       если у очереди включён group_albums=True).
  function openSelectionPicker(mediaList, articleEl, sourceBtn, permalink) {
    const existing = document.getElementById(PICKER_ID);
    if (existing) existing.remove();
    if (!document.body) return;

    // media_group_id генерируется из permalink (уникален для поста) + суффикс tgq.
    // Пример: https://x.com/user/status/123 → "tgq:xcom:123"
    // Бэкенд сохранит это значение в posts.media_group_id для всех выбранных медиа.
    let mediaGroupId = null;
    if (permalink && mediaList.length > 1) {
      try {
        const u = new URL(permalink);
        // Извлекаем последний числовой сегмент пути (id твита/поста).
        const m = u.pathname.match(/(\d+)(?:\/|$|\?)/g);
        const idPart = m && m.length ? m[m.length - 1].replace(/[\/\?]/g, "") : u.pathname.slice(-20);
        mediaGroupId = `tgq:${u.hostname.replace(/^www\./, "")}:${idPart}`;
      } catch {
        mediaGroupId = "tgq:" + Date.now().toString(36);
      }
    }

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

    // Контейнер с двумя кнопками отправки:
    //   «Отправить по отдельности» — каждое медиа как отдельный пост (без media_group_id).
    //   «Отправить как альбом» — все медиа с одним media_group_id; scheduler
    //     отправит их одним сообщением через send_media_group (если у очереди
    //     включён group_albums=True).
    const sendBtnRow = document.createElement("div");
    sendBtnRow.className = "tgq-send-row";
    overlay.appendChild(sendBtnRow);

    const sendSeparateBtn = document.createElement("button");
    sendSeparateBtn.type = "button";
    sendSeparateBtn.className = "tgq-send-btn tgq-send-separate";
    sendBtnRow.appendChild(sendSeparateBtn);

    const sendAlbumBtn = document.createElement("button");
    sendAlbumBtn.type = "button";
    sendAlbumBtn.className = "tgq-send-btn tgq-send-album";
    sendBtnRow.appendChild(sendAlbumBtn);

    function updateSendBtn() {
      const n = items.filter((i) => i.cb.checked).length;
      const label = n > 0 ? String(n) : "0";
      sendSeparateBtn.textContent = `📤 По отдельности (${label})`;
      sendAlbumBtn.textContent = `🖼 Как альбом (${label})`;
      const disabled = n === 0;
      sendSeparateBtn.disabled = disabled;
      sendAlbumBtn.disabled = disabled;
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

    // Общая функция отправки выбранных медиа.
    // useAlbum=true → передаём mediaGroupId (альбом); false → без (отдельные посты).
    async function sendSelected(useAlbum) {
      const selected = items.filter((i) => i.cb.checked).map((i) => i.media);
      if (!selected.length) return;
      sendSeparateBtn.disabled = true;
      sendAlbumBtn.disabled = true;
      let sent = 0;
      let failed = 0;
      let wasBlobVideo = false;

      // media_group_id только если выбран альбом И выбрано 2+ медиа.
      const useMgid = useAlbum && selected.length > 1 ? mediaGroupId : null;

      for (let i = 0; i < selected.length; i++) {
        const m = selected[i];
        const isBlob = !m.url && m.kind === "video";
        if (isBlob) wasBlobVideo = true;
        // Обновляем текст на активной кнопке.
        const activeBtn = useAlbum ? sendAlbumBtn : sendSeparateBtn;
        activeBtn.textContent = `Отправка ${i + 1}/${selected.length}…`;
        const payload = {
          type: "ENQUEUE",
          // pageUrl = permalink (НЕ location.href) — иначе бот качает HTML ленты.
          pageUrl: permalink || location.href,
          mediaUrl: m.url,
          referer: config.needsReferer ? location.origin + "/" : null,
          tags: extractTags(config),
          title: document.title,
          site: location.hostname.replace(/^www\./, ""),
          mode: isBlob ? "A" : config.mode,
          // media_group_id — одинаковый для всех выбранных медиа этого поста.
          // Бэкенд сохранит его; если у очереди group_albums=True, scheduler
          // отправит все посты с этим ID одним сообщением (send_media_group).
          mediaGroupId: useMgid,
        };
        try {
          const resp = await chrome.runtime.sendMessage(payload);
          if (resp && resp.ok) {
            sent++;
            await markSent(m.url, permalink, resp.post_id);
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
        const albumLabel = useAlbum && selected.length > 1 ? " как альбом" : "";
        if (wasBlobVideo && failed === 0) {
          tgqShowToast(
            `Отправлено${albumLabel} ${sent} медиа (видео — через yt-dlp)`,
            "ok"
          );
        } else {
          const msg = `Отправлено${albumLabel} ${sent} медиа` + (failed > 0 ? `, ошибок: ${failed}` : "");
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
    }

    sendSeparateBtn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      sendSelected(false);
    });
    sendAlbumBtn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      sendSelected(true);
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
