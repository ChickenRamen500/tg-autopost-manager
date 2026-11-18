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
  const QUEUE_POPUP_ID = "tgq-queue-popup";
  const LONG_PRESS_MS = 2000; // удержание кнопки — попап выбора очереди

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

  // --- Очереди бота (для попапа long-press и тулбара list-страниц) ---
  let queuesCache = { at: 0, queues: [] };
  const QUEUES_TTL = 60000; // 60 сек

  function fetchQueues() {
    return new Promise((resolve) => {
      if (queuesCache.queues.length && Date.now() - queuesCache.at < QUEUES_TTL) {
        resolve(queuesCache.queues);
        return;
      }
      try {
        chrome.runtime.sendMessage({ type: "GET_QUEUES" }, (resp) => {
          const qs = resp && resp.ok ? resp.queues || [] : [];
          if (qs.length) queuesCache = { at: Date.now(), queues: qs };
          resolve(qs);
        });
      } catch (e) {
        resolve([]);
      }
    });
  }

  // --- Попап выбора очереди (открывается удержанием кнопки 2 сек) ---
  let queuePopupEl = null;

  function closeQueuePopup() {
    if (queuePopupEl) {
      queuePopupEl.remove();
      queuePopupEl = null;
    }
  }

  // anchorBtn — кнопка, возле которой показать попап;
  // onSelect(queueIdOrNull) — колбэк выбора строки (null = маршрутизация по умолчанию).
  async function openQueuePopup(anchorBtn, onSelect) {
    closeQueuePopup();
    if (!document.body) return;

    const popup = document.createElement("div");
    popup.id = QUEUE_POPUP_ID;
    popup.className = "tgq-queue-popup";

    const head = document.createElement("div");
    head.className = "tgq-queue-popup-head";
    head.textContent = "Отправить в очередь…";
    popup.appendChild(head);

    const list = document.createElement("div");
    list.className = "tgq-queue-popup-list";
    const loading = document.createElement("div");
    loading.className = "tgq-queue-popup-loading";
    loading.textContent = "Загрузка очередей…";
    list.appendChild(loading);
    popup.appendChild(list);

    document.body.appendChild(popup);
    queuePopupEl = popup;

    // Позиция у кнопки (фикс. координаты, не вылезаем за окно).
    const r = anchorBtn.getBoundingClientRect();
    const pw = Math.max(260, popup.offsetWidth || 280);
    let x = Math.min(r.left, window.innerWidth - pw - 8);
    let y = r.bottom + 6;
    popup.style.left = Math.max(8, x) + "px";
    popup.style.top = Math.max(8, y) + "px";
    // Если снизу мало места — раскрываем вверх.
    requestAnimationFrame(() => {
      const ph = popup.offsetHeight || 300;
      if (r.bottom + 6 + ph > window.innerHeight - 8) {
        y = Math.max(8, r.top - ph - 6);
        popup.style.top = y + "px";
      }
      const w = popup.offsetWidth || pw;
      if (r.left + w > window.innerWidth - 8) {
        x = window.innerWidth - w - 8;
        popup.style.left = Math.max(8, x) + "px";
      }
    });

    // Закрытие: ESC, клик вне попапа, скролл страницы.
    const onKey = (ev) => {
      if (ev.key === "Escape") closeQueuePopup();
    };
    const onDocClick = (ev) => {
      if (!popup.contains(ev.target) && ev.target !== anchorBtn && !anchorBtn.contains(ev.target)) {
        closeQueuePopup();
      }
    };
    const onScroll = () => closeQueuePopup();
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onDocClick, true);
    window.addEventListener("scroll", onScroll, true);
    const cleanup = () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onDocClick, true);
      window.removeEventListener("scroll", onScroll, true);
    };

    const qs = await fetchQueues();
    if (queuePopupEl !== popup) {
      cleanup();
      return; // попап уже закрыли
    }
    list.innerHTML = "";
    if (!qs.length) {
      const empty = document.createElement("div");
      empty.className = "tgq-queue-popup-loading";
      empty.textContent = "Очереди не получены — проверьте настройки бота";
      list.appendChild(empty);
      return;
    }

    const mkItem = (label, queueId, isDefault, isActive) => {
      const item = document.createElement("button");
      item.type = "button";
      item.className = "tgq-queue-popup-item" + (isDefault ? " tgq-queue-popup-default" : "");
      item.textContent = label;
      if (isActive === false) item.classList.add("tgq-queue-popup-inactive");
      item.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        cleanup();
        closeQueuePopup();
        try {
          onSelect(queueId);
        } catch (e) {
          /* колбэк падать не должен закрывать попап */
        }
      });
      list.appendChild(item);
    };

    mkItem("⚡ По умолчанию (маршрутизация)", null, true);
    for (const q of qs) {
      mkItem(`${q.name} (#${q.id}${q.pending ? `, ${q.pending} в ожидании` : ""})${q.is_active ? "" : " — выкл."}`, q.id, false, q.is_active);
    }
  }

  // --- Привязка long-press (удержание 2 сек → попап очередей) к кнопке ---
  function attachLongPress(btn, onSelect) {
    let holdTimer = null;
    let holdTriggered = false;
    const startHold = () => {
      holdTriggered = false;
      holdTimer = setTimeout(() => {
        holdTimer = null;
        holdTriggered = true;
        try {
          if (navigator.vibrate) navigator.vibrate(30);
        } catch (e) { /* не критично */ }
        openQueuePopup(btn, onSelect);
      }, LONG_PRESS_MS);
    };
    const cancelHold = () => {
      if (holdTimer) {
        clearTimeout(holdTimer);
        holdTimer = null;
      }
    };
    btn.addEventListener("pointerdown", startHold);
    btn.addEventListener("pointerup", () => {
      cancelHold();
      // После сработавшего long-press браузер всё равно сделает click —
      // подавляем его, чтобы попап не закрылся отправкой «по умолчанию».
      setTimeout(() => { holdTriggered = false; }, 50);
    });
    btn.addEventListener("pointerleave", cancelHold);
    btn.addEventListener("pointercancel", cancelHold);
    btn.addEventListener("contextmenu", (ev) => {
      if (holdTriggered || holdTimer) {
        ev.preventDefault();
        ev.stopPropagation();
      }
    });
    btn._tgqHoldTriggered = () => holdTriggered;
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
      if (media.length > 0) {
        result.push({ article: a, media, index: i });
        return;
      }
      // Пикабу 18+ (сайты с unlockAdult): у заглушки медиа в DOM нет — анонимам
      // пикабу вырезает их из HTML. Кнопку всё равно добавляем: клик попытается
      // разблокировать пост (залогиненные) и объяснит, что нужен вход.
      if (config.unlockAdult && isAdultStubArticle(a)) {
        result.push({ article: a, media: [], index: i });
      }
    });
    return result;
  }

  // --- 18+ заглушки (Pikabu): разблокировка и понятная диагностика ---
  // Анонимам пикабу НЕ отдаёт 18+ медиа вообще (в HTML только заглушки
  // .story__nsfw-stub со ссылками на вход). У залогиненных контент появляется
  // после клика «Показать контент 18+» — пробуем кликнуть программно и
  // пересобрать медиа. Если разблокировать нельзя — объясняем пользователю.
  function isAdultStubArticle(articleEl) {
    return !!(
      articleEl &&
      articleEl.querySelector(
        ".story__nsfw-stub, .story__adult-overlay, [data-action='adult-confirm']"
      )
    );
  }

  function tryUnlockAdult(articleEl) {
    if (!articleEl) return false;
    const btn = articleEl.querySelector(
      "button[data-action='adult-confirm'], .story__adult-confirm-btn, .story__adult-overlay button:not(.tgq-btn)"
    );
    if (!btn) return false;
    try {
      btn.click();
      return true;
    } catch (e) {
      return false;
    }
  }

  // Возвращает список медиа для отправки. Пустой captured (18+ заглушка)
  // → пробуем разблокировать контент и пересобрать медиа; если не помогло —
  // тянем страницу поста с куками пользователя (fallbackResolve) и извлекаем
  // прямые URL (18+/NSFW и blob-видео на пикабу). Найденное через fallback
  // помечается viaFallback=true → уходит mode B: браузер сам скачает файл
  // (cookies) и зальёт на сервер — бот не может скачать 18+ анонимно.
  async function resolveMediaWithUnlock(articleEl) {
    let media = collectMediaInArticle(articleEl, config);
    if (media.length) return media;
    const permalink = getArticlePermalink(articleEl, config);
    if (tryUnlockAdult(articleEl)) {
      // Пауза на перерисовку/ленивую загрузку картинок после раскрытия.
      await new Promise((r) => setTimeout(r, 700));
      media = collectMediaInArticle(articleEl, config);
      if (media.length) return media;
    }
    if (typeof config.fallbackResolve === "function" && permalink && /^https?:/.test(permalink)) {
      try {
        tgqShowToast("Получаю медиа через страницу поста (с вашими cookies)…", "info", 2500);
        const resolved = await config.fallbackResolve(permalink);
        const list = (resolved.mediaUrls || (resolved.fileUrl ? [resolved.fileUrl] : [])).map(
          (u) => ({ url: u, kind: mediaKind(null, u), viaFallback: true })
        );
        if (list.length) {
          tgqShowToast(`Найдено медиа на странице поста: ${list.length}`, "ok", 2500);
          return list;
        }
      } catch (e) {
        tgqShowToast(
          e && e.message ? e.message : "Не удалось получить медиа со страницы поста",
          "err",
          8000
        );
        return [];
      }
    }
    if (isAdultStubArticle(articleEl)) {
      tgqShowToast(
        "18+ пост без авторизации: Пикабу не отдаёт медиа. Войдите на pikabu.ru в этом браузере и повторите.",
        "err",
        9000
      );
      return [];
    }
    return media;
  }

  // Обёртка: кратко показываем «проверяем», восстанавливаем кнопку после ошибки.
  async function resolveMediaForSend(articleEl, captured, btn) {
    if (captured && captured.length) return captured;
    if (btn) {
      btn.disabled = true;
      btn.textContent = "… Проверка";
    }
    const media = await resolveMediaWithUnlock(articleEl);
    if (btn) {
      btn.disabled = false;
      btn.textContent = "→ В очередь";
    }
    return media;
  }

  // --- media_group_id из permalink (уникален для поста) ---
  // Пример: https://x.com/user/status/123 → "tgq:xcom:123".
  // Бэкенд сохранит это значение в posts.media_group_id для всех медиа.
  function makeMediaGroupId(permalink) {
    if (!permalink) return "tgq:" + Date.now().toString(36);
    try {
      const u = new URL(permalink);
      const m = u.pathname.match(/(\d+)(?:\/|$|\?)/g);
      const idPart = m && m.length ? m[m.length - 1].replace(/[\/\?]/g, "") : u.pathname.slice(-20);
      return `tgq:${u.hostname.replace(/^www\./, "")}:${idPart}`;
    } catch {
      return "tgq:" + Date.now().toString(36);
    }
  }

  // --- Отправка ВСЕГО поста в выбранную очередь (long-press popup) ---
  // single → одно ENQUEUE; multi → альбом (единый media_group_id, все медиа).
  async function sendToQueueWithId(mediaList, permalink, queueId, sourceBtn) {
    if (!mediaList || !mediaList.length) return;
    if (sourceBtn) {
      sourceBtn.disabled = true;
      sourceBtn.textContent = "… Отправка";
    }
    const useMgid = mediaList.length > 1 ? makeMediaGroupId(permalink) : null;
    let sent = 0;
    let failed = 0;
    let lastError = null;
    for (const m of mediaList) {
      const isBlob = !m.url && m.kind === "video";
      // viaFallback (18+/NSFW пикабу, resolved из страницы поста) → mode B:
      // браузер скачает файл с куками и зальёт на сервер сам.
      const payload = {
        type: "ENQUEUE",
        pageUrl: permalink || location.href,
        mediaUrl: m.url,
        referer: config.needsReferer ? location.origin + "/" : null,
        tags: extractTags(config),
        title: document.title,
        site: location.hostname.replace(/^www\./, ""),
        mode: m.viaFallback ? "B" : isBlob ? "A" : config.mode,
        mediaGroupId: useMgid,
        queueId: queueId || null,
      };
      try {
        const resp = await chrome.runtime.sendMessage(payload);
        if (resp && resp.ok) {
          sent++;
          await markSent(m.url, permalink, resp.post_id);
        } else {
          failed++;
          lastError = resp && resp.error;
        }
      } catch (e) {
        failed++;
        lastError = e && e.message;
      }
    }
    if (sourceBtn) {
      if (sent > 0 && failed === 0) {
        sourceBtn.textContent = `✓ В очереди${mediaList.length > 1 ? ` (${sent})` : ""}`;
        sourceBtn.classList.add("tgq-done");
        sourceBtn.disabled = true;
      } else if (sent > 0) {
        sourceBtn.textContent = `✓ ${sent}/${mediaList.length}`;
        sourceBtn.classList.add("tgq-done");
        sourceBtn.disabled = false;
      } else {
        sourceBtn.textContent = "✗ Ошибка";
        sourceBtn.classList.add("tgq-error");
        sourceBtn.disabled = false;
      }
    }
    if (sent > 0) {
      const albumLabel = mediaList.length > 1 ? " как альбом" : "";
      tgqShowToast(`Отправлено${albumLabel} ${sent} медиа в очередь` + (failed ? `, ошибок: ${failed}` : ""), failed ? "err" : "ok");
    } else {
      tgqShowToast(lastError || "Ошибка отправки", "err");
    }
  }

  // --- Создание кнопки для одного article ---
  function makeArticleButton(articleEl, mediaList) {
    const permalink = getArticlePermalink(articleEl, config);

    const btn = document.createElement("button");
    btn.className = "tgq-btn tgq-article-btn";
    btn.type = "button";
    btn.textContent =
      mediaList.length > 1 ? `→ В очередь (${mediaList.length})` : "→ В очередь";
    btn.title = "Отправить в очередь TG Media Queue Bot\nУдерживайте 2 сек — выбрать очередь";

    // Сохраняем permalink на кнопке — используется в sendSingle и пикере.
    btn.dataset.permalink = permalink;

    if (mediaList.length === 1) {
      // Single-media → send immediately.
      btn.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        if (btn._tgqHoldTriggered && btn._tgqHoldTriggered()) return; // long-press уже открыл попап
        sendSingle(mediaList[0], btn, articleEl, permalink);
      });
    } else if (mediaList.length > 1) {
      // Multi-media → open selection picker with checkboxes.
      btn.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        if (btn._tgqHoldTriggered && btn._tgqHoldTriggered()) return;
        openSelectionPicker(mediaList, articleEl, btn, permalink);
      });
    } else {
      // Пустой медиа-лист бывает только у 18+ заглушек (unlockAdult):
      // при клике пробуем разблокировать пост и отправить как обычно.
      btn.addEventListener("click", async (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        if (btn._tgqHoldTriggered && btn._tgqHoldTriggered()) return;
        if (btn.disabled) return;
        const media = await resolveMediaForSend(articleEl, mediaList, btn);
        if (!media.length) return;
        if (media.length === 1) sendSingle(media[0], btn, articleEl, permalink);
        else openSelectionPicker(media, articleEl, btn, permalink);
      });
    }

    // Удержание 2 сек → попап выбора очереди (все сайты, один механизм).
    attachLongPress(btn, async (queueId) => {
      const media = await resolveMediaForSend(articleEl, mediaList, btn);
      sendToQueueWithId(media, permalink, queueId, btn);
    });

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
  async function sendSingle(media, btn, articleEl, permalink, queueId) {
    if (btn.disabled) return;
    btn.disabled = true;
    btn.textContent = "… Отправка";

    const isBlobVideo = !media.url && media.kind === "video";
    // blob: видео → mediaUrl=null + mode=A (бэкенд использует yt-dlp на permalink).
    // viaFallback (18+/NSFW пикабу) → mode=B: браузер скачает и зальёт сам.
    // pageUrl = permalink (НЕ location.href!) — иначе бот качает HTML ленты x.com/home.
    const payload = {
      type: "ENQUEUE",
      pageUrl: permalink || location.href,
      mediaUrl: media.url,
      referer: config.needsReferer ? location.origin + "/" : null,
      tags: extractTags(config),
      title: document.title,
      site: location.hostname.replace(/^www\./, ""),
      mode: media.viaFallback ? "B" : isBlobVideo ? "A" : config.mode,
      queueId: queueId || null,
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
    // Бэкенд сохранит это значение в posts.media_group_id для всех выбранных медиа.
    let mediaGroupId = null;
    if (permalink && mediaList.length > 1) {
      mediaGroupId = makeMediaGroupId(permalink);
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
          mode: m.viaFallback ? "B" : isBlob ? "A" : config.mode,
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

  // --- Страница списка (Gelbooru search: page=post&s=list) ------------------
  // Чекбоксы на .thumbnail-preview, кнопка быстрой отправки на каждой
  // миниатюре, тулбар с выбором очереди — НАД #thumbnail-container.
  // Выбрано 2+ постов → отправляются одним альбомом (единый media_group_id).
  // Оригинальный URL каждого поста резолвится из HTML страницы поста (resolveOriginal).

  function getListItems(lp) {
    const container = document.querySelector(lp.containerSelector);
    if (!container) return { container: null, items: [] };
    const items = Array.prototype.slice.call(container.querySelectorAll(lp.itemSelector));
    return { container, items };
  }

  function getItemPermalink(itemEl, lp) {
    let href = null;
    try {
      const link = itemEl.querySelector(lp.linkSelector);
      href = link && link.getAttribute("href");
    } catch (e) {
      href = null;
    }
    if (!href) {
      const anyA = itemEl.querySelector("a[href*='page=post']");
      href = anyA && anyA.getAttribute("href");
    }
    if (!href) return null;
    try {
      return new URL(href, location.origin).href;
    } catch {
      return null;
    }
  }

  // Быстрая отправка одной миниатюры: резолвим оригинал (dapi) → ENQUEUE mode A.
  async function sendListItem(permalink, itemEl, btn, queueId) {
    if (btn) {
      btn.disabled = true;
      btn.textContent = "…";
    }
    const markItemDone = (ok) => {
      if (itemEl) itemEl.classList.add(ok ? "tgq-list-item-done" : "tgq-list-item-error");
    };
    try {
      if (!permalink) throw new Error("не найдена ссылка на пост");
      if (typeof config.resolveOriginal !== "function") {
        throw new Error("resolveOriginal не настроен для сайта");
      }
      const resolved = await config.resolveOriginal(permalink);
      const tags = extractTags(config).concat(resolved.tags || []);
      // Дедуп тегов
      const seenTags = new Set();
      const mergedTags = tags.filter((t) => {
        const k = t.toLowerCase();
        if (seenTags.has(k)) return false;
        seenTags.add(k);
        return true;
      }).slice(0, 60);

      const payload = {
        type: "ENQUEUE",
        pageUrl: permalink,
        mediaUrl: resolved.fileUrl,
        referer: config.needsReferer ? location.origin + "/" : null,
        tags: mergedTags,
        title: document.title,
        site: location.hostname.replace(/^www\./, ""),
        mode: config.mode,
        queueId: queueId || null,
      };
      const resp = await chrome.runtime.sendMessage(payload);
      if (resp && resp.ok) {
        if (btn) {
          btn.textContent = "✓";
          btn.classList.add("tgq-done");
        }
        tgqShowToast(
          resp.duplicate ? `Дубликат #${resp.post_id}` : `Пост #${resp.post_id} в очереди`,
          "ok"
        );
        await markSent(resolved.fileUrl, permalink, resp.post_id);
        markItemDone(true);
        const cb = itemEl && itemEl.querySelector(".tgq-list-checkbox");
        if (cb) {
          cb.checked = false;
          cb.disabled = true;
          updateListToolbar();
        }
      } else {
        if (btn) {
          btn.textContent = "✗";
          btn.classList.add("tgq-error");
          btn.disabled = false;
        }
        tgqShowToast(resp?.error || "Ошибка отправки", "err");
        markItemDone(false);
      }
    } catch (e) {
      if (btn) {
        btn.textContent = "✗";
        btn.classList.add("tgq-error");
        btn.disabled = false;
      }
      tgqShowToast(e && e.message ? e.message : "Ошибка отправки", "err");
      markItemDone(false);
    }
  }

  function injectListItemUi(itemEl, lp) {
    if (!itemEl || itemEl.querySelector(".tgq-list-send")) return false;
    itemEl.classList.add("tgq-list-item");
    const permalink = getItemPermalink(itemEl, lp);

    // Чекбокс (левый верхний угол миниатюры).
    const cb = document.createElement("input");
    cb.type = "checkbox";
    cb.className = "tgq-list-checkbox";
    cb.title = "Выбрать для массовой отправки";
    cb.setAttribute("aria-label", "Выбрать пост для отправки");
    cb.addEventListener("click", (ev) => ev.stopPropagation());
    cb.addEventListener("change", () => {
      itemEl.classList.toggle("tgq-list-item-selected", cb.checked);
      updateListToolbar();
    });
    itemEl.appendChild(cb);

    // Кнопка быстрой отправки (появляется при наведении, правый нижний угол).
    const sendBtn = document.createElement("button");
    sendBtn.type = "button";
    sendBtn.className = "tgq-btn tgq-list-send";
    sendBtn.textContent = "→ В очередь";
    sendBtn.title = "Отправить оригинал в очередь\nУдерживайте 2 сек — выбрать очередь";
    sendBtn.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      if (sendBtn._tgqHoldTriggered && sendBtn._tgqHoldTriggered()) return;
      sendListItem(permalink, itemEl, sendBtn, null);
    });
    attachLongPress(sendBtn, (queueId) => {
      sendListItem(permalink, itemEl, sendBtn, queueId);
    });
    itemEl.appendChild(sendBtn);
    return true;
  }

  // Тулбар над thumbnail-container: счётчик выбранных + выбор очереди + кнопка.
  function ensureListToolbar(container, lp) {
    let toolbar = document.getElementById("tgq-list-toolbar");
    const isNew = !toolbar;
    if (isNew) {
      toolbar = document.createElement("div");
      toolbar.id = "tgq-list-toolbar";
      toolbar.className = "tgq-list-toolbar";
      toolbar.setAttribute("role", "toolbar");
      toolbar.setAttribute("aria-label", "Отправка выбранных постов в очередь");

      const count = document.createElement("span");
      count.className = "tgq-list-count";
      count.id = "tgq-list-count";
      count.textContent = "Выбрано: 0";
      toolbar.appendChild(count);

      const hint = document.createElement("span");
      hint.className = "tgq-list-hint";
      hint.textContent = "2+ выбранных отправятся одним альбомом";
      toolbar.appendChild(hint);

      const select = document.createElement("select");
      select.id = "tgq-list-queue-select";
      select.className = "tgq-list-queue-select";
      select.setAttribute("aria-label", "Очередь для отправки");
      const def = document.createElement("option");
      def.value = "";
      def.textContent = "⚡ Очередь: по умолчанию";
      select.appendChild(def);
      toolbar.appendChild(select);

      fetchQueues().then((qs) => {
        for (const q of qs) {
          const opt = document.createElement("option");
          opt.value = String(q.id);
          opt.textContent = `${q.name} (#${q.id})${q.is_active ? "" : " — выкл."}`;
          select.appendChild(opt);
        }
      });

      const sendBtn = document.createElement("button");
      sendBtn.type = "button";
      sendBtn.id = "tgq-list-send-selected";
      sendBtn.className = "tgq-btn tgq-list-send-selected";
      sendBtn.textContent = "→ Отправить выбранные";
      sendBtn.title = "Отправить отмеченные чекбоксами посты (2+ — одним альбомом)";
      sendBtn.addEventListener("click", (ev) => {
        ev.preventDefault();
        ev.stopPropagation();
        sendSelectedFromList(container, lp);
      });
      toolbar.appendChild(sendBtn);

      if (lp.toolbarPosition === "before" && container.parentNode) {
        container.parentNode.insertBefore(toolbar, container);
      } else {
        container.insertBefore(toolbar, container.firstChild);
      }
    }
    updateListToolbar();
    return true;
  }

  function updateListToolbar() {
    const count = document.getElementById("tgq-list-count");
    if (!count) return;
    const n = document.querySelectorAll(".tgq-list-checkbox:checked").length;
    count.textContent = `Выбрано: ${n}`;
    const sendBtn = document.getElementById("tgq-list-send-selected");
    if (sendBtn) sendBtn.disabled = n === 0;
  }

  // Массовая отправка выбранных чекбоксами миниатюр.
  // 1 пост → одиночный; 2+ → один альбом (единый media_group_id).
  async function sendSelectedFromList(container, lp) {
    const queueSelect = document.getElementById("tgq-list-queue-select");
    const queueId = queueSelect && queueSelect.value ? parseInt(queueSelect.value, 10) : null;
    const sendBtn = document.getElementById("tgq-list-send-selected");

    const selected = [];
    const containerItems = container.querySelectorAll(lp.itemSelector);
    containerItems.forEach((itemEl) => {
      const cb = itemEl.querySelector(".tgq-list-checkbox");
      if (cb && cb.checked) {
        selected.push({ itemEl, permalink: getItemPermalink(itemEl, lp) });
      }
    });
    if (!selected.length) {
      tgqShowToast("Сначала отметьте посты чекбоксами", "err");
      return;
    }

    if (sendBtn) {
      sendBtn.disabled = true;
    }
    const useMgid =
      selected.length > 1
        ? `tgq:${location.hostname.replace(/^www\./, "")}:bulk:${Date.now().toString(36)}`
        : null;

    let sent = 0;
    let failed = 0;
    for (let i = 0; i < selected.length; i++) {
      const { itemEl, permalink } = selected[i];
      if (sendBtn) sendBtn.textContent = `Отправка ${i + 1}/${selected.length}…`;
      const cb = itemEl.querySelector(".tgq-list-checkbox");
      try {
        const resolved = await config.resolveOriginal(permalink);
        const tags = extractTags(config).concat(resolved.tags || []);
        const seenTags = new Set();
        const mergedTags = tags.filter((t) => {
          const k = t.toLowerCase();
          if (seenTags.has(k)) return false;
          seenTags.add(k);
          return true;
        }).slice(0, 60);
        const payload = {
          type: "ENQUEUE",
          pageUrl: permalink,
          mediaUrl: resolved.fileUrl,
          referer: config.needsReferer ? location.origin + "/" : null,
          tags: mergedTags,
          title: document.title,
          site: location.hostname.replace(/^www\./, ""),
          mode: config.mode,
          mediaGroupId: useMgid,
          queueId,
        };
        const resp = await chrome.runtime.sendMessage(payload);
        if (resp && resp.ok) {
          sent++;
          await markSent(resolved.fileUrl, permalink, resp.post_id);
          itemEl.classList.add("tgq-list-item-done");
          if (cb) {
            cb.checked = false;
            cb.disabled = true;
          }
        } else {
          failed++;
          itemEl.classList.add("tgq-list-item-error");
        }
      } catch (e) {
        failed++;
        itemEl.classList.add("tgq-list-item-error");
        console.warn("[tgq] list send error:", e);
      }
    }
    if (sendBtn) {
      sendBtn.textContent = "→ Отправить выбранные";
      sendBtn.disabled = false;
    }
    updateListToolbar();
    if (sent > 0) {
      const albumLabel = selected.length > 1 ? " как альбом" : "";
      tgqShowToast(
        `Отправлено${albumLabel} ${sent} постов` + (failed ? `, ошибок: ${failed}` : ""),
        failed ? "err" : "ok"
      );
    } else {
      tgqShowToast("Не удалось отправить ни одного поста", "err");
    }
  }

  function setupListPage() {
    const lp = config.listPage;
    if (!lp) return false;
    // Только на страницах поиска (s=list/s=gallery); на странице поста (s=view)
    // работает обычный per-article механизм.
    if (lp.matchUrl && !lp.matchUrl.test(location.search)) return false;
    const { container, items } = getListItems(lp);
    if (!container || !items.length) return false;
    let injected = 0;
    for (const itemEl of items) {
      try {
        if (injectListItemUi(itemEl, lp)) injected++;
      } catch (e) {
        /* skip broken item */
      }
    }
    try {
      ensureListToolbar(container, lp);
    } catch (e) {
      /* toolbar не критичен */
    }
    return injected > 0;
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
    // Страницы списков (Gelbooru search) — свой UI, отдельный от article-кнопок.
    if (config.listPage) {
      try {
        setupListPage();
      } catch (e) {
        /* ignore */
      }
    }
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
