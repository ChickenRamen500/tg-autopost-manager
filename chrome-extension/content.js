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

  // Маркер версии в консоли — чтобы видеть, что расширение обновлено
  // (18+ пикабу и новые фичи не работают, пока расширение не перезагружено).
  try {
    console.log(
      "[tgq] content script v1.3.0 (pikabu 18+: cache-bust + GIF-анализ + лайтбокс + кандидаты)"
    );
  } catch (e) {
    /* ignore */
  }

  // Единая точка диагностики: всё, что связано с отправкой, дублируется в
  // консоль страницы (F12 → Console → фильтр "tgq"). Тосты исчезают,
  // консоль — остаётся: по ней можно точно понять, где оборвался 18+-путь.
  function tgqLog(...args) {
    try {
      console.log("[tgq]", ...args);
    } catch (e) {
      /* ignore */
    }
  }
  function tgqWarn(...args) {
    try {
      console.warn("[tgq]", ...args);
    } catch (e) {
      /* ignore */
    }
  }

  // ==========================================================================
  // --- Пикабу 18+ / mode B: обход заглушки CDN (v1.3.0) ---------------------
  // ==========================================================================
  // Найденная причина «заглушек» (v0.4.61): CDN пикабу (DDoS-Guard) кэширует
  // ОТВЕТ ОРИГИНА на 7 дней (cache-control: max-age=604800) БЕЗ учёта cookies
  // (vary только Origin, Access-Control-*). Если хоть один аноним (бот,
  // краулер, предыдущая попытка скачивания) получил заглушку 18+ — она
  // кэшируется на edge-узле, и её получают ВСЕ, включая залогиненного
  // пользователя с включённым 18+. Поэтому браузер в новой вкладке получает
  // заглушку, а после ЛКМ (лайтбокс) — нормальную картинку (из кэша браузера).
  //
  // Решение:
  //   1) cache-bust: уникальный ?tgqcb=… к URL → edge-кэш промахивается (MISS)
  //      → запрос уходит на оригин → оригин видит cookies пользователя (18+)
  //      → отдаёт настоящий файл.
  //   2) Клиентский анализ GIF (парсер кадров): настоящий арт — много-кадровый,
  //      заглушка — одиночный белый кадр. Кандидаты сортируются «анимированные
  //      первыми», поэтому первым уходит настоящая картинка.
  //   3) Перебор кандидатов: если сервер всё же ответил «заглушка», пробуем
  //      следующие URL из DOM/производительности страницы.
  //   4) Автоматический «ЛКМ-сценарий»: если анимации не нашли — программно
  //      открываем лайтбокс (как пользователь ЛКМ), забираем его URL.
  //
  // Скачивание (mode B) остаётся в фоновом скрипте расширения — здесь только
  // подготовка URL-кандидатов и разбор ответов сервера.

  const MAX_CANDIDATE_PROBES = 6; // сколько URL анализировать (скачивая байты)
  const MAX_ENQUEUE_ATTEMPTS = 3; // сколько кандидатов отправлять серверу

  function sleep(ms) {
    return new Promise((r) => setTimeout(r, ms));
  }

  function isPikabuSite() {
    return /(^|\.)pikabu\.ru$/i.test(location.hostname);
  }

  // Ответ сервера «это заглушка 18+» (текстовое сообщение текущего бэкенда
  // и/или машина-читаемый error_code после обновления сервера).
  function isStubError(resp) {
    if (!resp || resp.ok) return false;
    if (resp.error_code === "nsfw_stub") return true;
    const msg = String(resp.error || "");
    return /[зЗ]аглушк|nsfw|18\+/i.test(msg);
  }

  // Уникальный query-параметр: ломает ключ кэша CDN (DDoS-Guard кэширует по
  // URL без учёта query → MISS → идём на оригина).
  function addCacheBust(url) {
    try {
      const u = new URL(url);
      u.searchParams.set(
        "tgqcb",
        Date.now().toString(36) + Math.floor(Math.random() * 1000000).toString(36)
      );
      return u.href;
    } catch (e) {
      return url;
    }
  }

  function isPikabuMediaUrl(u) {
    return /^https?:\/\/cs\d+\.pikabu\.ru\/s\/.+\.(gif|jpg|jpeg|png|webp|mp4|webm)(\?|#|$)/i.test(
      u || ""
    );
  }

  // Анонимная проба (без cookies) из контекста страницы: CORS у CDN пикабу
  // разрешён только БЕЗ credentials (access-control-allow-credentials не
  // отдаётся), поэтому «включённые» cookies здесь приложить нельзя — но нам
  // и не нужно: проба нужна лишь чтобы ОТЛИЧИТЬ заглушку (одиночный кадр)
  // от настоящего арта (много кадров) и отсортировать кандидатов.
  // Скачивание для отправки делает фоновый скрипт с host_permissions — там
  // cookies пользователя уходят в любом случае.
  // cache: "no-store" — НИКОГДА не пишем скачанное в HTTP-кэш браузера,
  // чтобы не «отравить» его заглушкой для последующих запросов.
  async function primeFetchBytes(url) {
    try {
      const resp = await fetch(url, {
        credentials: "omit",
        cache: "no-store",
        redirect: "follow",
      });
      if (!resp.ok) return null;
      const buf = await resp.arrayBuffer();
      if (!buf || buf.byteLength < 100) return null;
      return new Uint8Array(buf);
    } catch (e) {
      return null;
    }
  }

  // Мини-парсер GIF: считаем кадры (Image Descriptors). Проверен на заглушке
  // пикабу (1 кадр) и реальных артах (40–54 кадра).
  function gifFrameCount(bytes) {
    if (!bytes || bytes.length < 13) return -1;
    if (bytes[0] !== 0x47 || bytes[1] !== 0x49 || bytes[2] !== 0x46) return -1; // "GIF"
    // Header "GIFxxx" = байты 0..5. Logical Screen Descriptor = байты 6..12
    // (width2 байты6-7, height2 байты8-9, packed байт10, bg 11, aspect 12).
    const gPacked = bytes[10];
    let p = 13;
    if (gPacked & 0x80) {
      p += 3 * (1 << ((gPacked & 0x07) + 1)); // global color table
    }
    let frames = 0;
    let guard = 0;
    while (p < bytes.length && guard < 200000) {
      guard++;
      const b = bytes[p];
      if (b === 0x3b) break; // trailer
      if (b === 0x21) {
        // extension block: 0x21 label sub-blocks...
        let q = p + 2;
        while (q < bytes.length) {
          const len = bytes[q];
          if (len === 0) {
            q++;
            break;
          }
          q += 1 + len;
        }
        p = q;
        continue;
      }
      if (b === 0x2c) {
        // image descriptor: 0x2C left2 top2 w2 h2 packed (packed на p+9)
        frames++;
        let q = p + 10;
        const lPacked = bytes[p + 9];
        if (lPacked & 0x80) {
          q += 3 * (1 << ((lPacked & 0x07) + 1)); // local color table
        }
        q += 1; // LZW min code size byte
        while (q < bytes.length) {
          const len = bytes[q];
          if (len === 0) {
            q++;
            break;
          }
          q += 1 + len;
        }
        p = q;
        continue;
      }
      // неизвестный байт блока — дальше надёжно распарсить нельзя
      return frames > 0 ? frames : -2;
    }
    return frames;
  }

  // Все медиа-URL пикабу из article и из загрузок страницы (performance API):
  // тут оказывается и тот URL, который страница реально показала.
  function collectPikabuCandidateUrls(articleEl, excludeUrl) {
    const found = [];
    const push = (u) => {
      if (!u || typeof u !== "string") return;
      if (!isPikabuMediaUrl(u)) return;
      if (excludeUrl && u === excludeUrl) return;
      if (found.indexOf(u) === -1) found.push(u);
    };
    if (articleEl) {
      let els = [];
      try {
        els = articleEl.querySelectorAll("img, video, source, a[href]");
      } catch (e) {
        els = [];
      }
      for (const el of els) {
        push(el.getAttribute("src"));
        push(el.getAttribute("data-src"));
        push(el.getAttribute("data-large-image"));
        push(el.getAttribute("data-source"));
        push(el.getAttribute("data-poster"));
        if (el.tagName === "A") push(el.getAttribute("href"));
      }
    }
    try {
      const entries = performance.getEntriesByType("resource") || [];
      for (const e of entries) push(e.name);
    } catch (e) {
      /* performance API недоступен — не критично */
    }
    return found;
  }

  // Автоматический «ЛКМ-сценарий» пользователя: программно открываем
  // просмотр оригинала (лайтбокс), забираем его URL(ы), закрываем.
  async function simulateLightbox(articleEl) {
    const before = new Set();
    try {
      for (const e of performance.getEntriesByType("resource") || []) before.add(e.name);
    } catch (e) {
      /* ignore */
    }
    let target = null;
    try {
      target =
        (articleEl &&
          articleEl.querySelector(
            ".story__image img, .story__image, .player, img[src*='pikabu.ru'], video"
          )) ||
        null;
    } catch (e) {
      target = null;
    }
    if (!target) return [];
    try {
      target.dispatchEvent(
        new MouseEvent("click", { bubbles: true, cancelable: true, view: window })
      );
    } catch (e) {
      return [];
    }
    await sleep(1600);
    const urls = [];
    const push = (u) => {
      // Лайтбокс может отдать и «неканоничный» URL (pikabu.ru/... вместо
      // csN.pikabu.ru/s/...) — принимаем любой медиа-URL на доменах пикабу.
      if (
        u &&
        /^https:\/\/[a-z0-9.-]*pikabu\.ru\/.+\.(gif|jpg|jpeg|png|webp|mp4|webm)(\?|#|$)/i.test(u) &&
        urls.indexOf(u) === -1
      ) {
        urls.push(u);
      }
    };
    try {
      // Новые запросы, появившиеся после клика — лайтбокс грузит оригинал.
      for (const e of performance.getEntriesByType("resource") || []) {
        if (!before.has(e.name)) push(e.name);
      }
    } catch (e) {
      /* ignore */
    }
    // Крупные картинки вне article — вероятный img лайтбокса.
    let imgs = [];
    try {
      imgs = document.querySelectorAll("img");
    } catch (e) {
      imgs = [];
    }
    for (const im of imgs) {
      if (articleEl && articleEl.contains(im)) continue;
      const src = im.currentSrc || im.src;
      if (!src) continue;
      if (im.naturalWidth >= 400 || isPikabuMediaUrl(src)) push(src);
    }
    // Закрываем лайтбокс: Esc + возможные кнопки-кресты.
    try {
      const opts = { key: "Escape", code: "Escape", bubbles: true, cancelable: true };
      const owner = document.activeElement || document.body;
      owner.dispatchEvent(new KeyboardEvent("keydown", opts));
      document.dispatchEvent(new KeyboardEvent("keydown", opts));
    } catch (e) {
      /* ignore */
    }
    try {
      const closeBtn = document.querySelector(
        "[class*='lightbox'] [class*='close'], [class*='viewer'] [class*='close'], [class*='modal'] [class*='close']"
      );
      if (closeBtn) closeBtn.click();
    } catch (e) {
      /* ignore */
    }
    await sleep(250);
    return urls;
  }

  // Собирает упорядоченный список URL-кандидатов для mode B (пикабу).
  // GIF-кандидаты пробуются и сортируются: анимированные (настоящий арт)
  // получают высший приоритет, одиночный белый кадр (заглушка) — низший.
  async function resolvePikabuModeBCandidates(media, articleEl) {
    const mainUrl = media.url;
    if (!mainUrl || !/^https?:/i.test(mainUrl)) return [mainUrl].filter(Boolean);
    const isGif = /\.gif(\?|#|$)/i.test(mainUrl) || media.kind === "gif";
    const others = collectPikabuCandidateUrls(articleEl, mainUrl);
    tgqLog("pikabu-modeB: кандидатов из DOM/performance:", others.length);

    const ranked = [{ url: mainUrl, score: 0 }];
    if (isGif) {
      const probeList = [mainUrl].concat(others).slice(0, MAX_CANDIDATE_PROBES);
      for (const u of probeList) {
        const bytes = await primeFetchBytes(addCacheBust(u));
        if (!bytes) continue;
        const frames = gifFrameCount(bytes);
        // Заглушка = одиночный кадр → score 0 (ниже кандидатов лайтбокса).
        const score = frames > 1 ? 1000 + Math.min(frames, 999) : 0;
        tgqLog(
          "pikabu-modeB: проба",
          (u.split("/").pop() || "").slice(0, 44),
          "кадров=" + frames,
          "score=" + score
        );
        const existing = ranked.find((r) => r.url === u);
        if (existing) existing.score = score;
        else ranked.push({ url: u, score });
        if (frames > 1 && u === mainUrl) break; // основной URL уже настоящий
      }
      // Анимации не нашли — воспроизводим ручной «ЛКМ» пользователя.
      if (!ranked.some((r) => r.score > 1)) {
        tgqLog("pikabu-modeB: анимации не найдено — открываю лайтбокс (ЛКМ-сценарий)");
        try {
          const lbUrls = await simulateLightbox(articleEl);
          tgqLog("pikabu-modeB: лайтбокс дал URL:", lbUrls.length);
          for (const u of lbUrls) {
            if (!ranked.some((r) => r.url === u)) ranked.push({ url: u, score: 500 });
          }
        } catch (e) {
          /* ignore */
        }
      }
    } else {
      for (const u of others.slice(0, 4)) ranked.push({ url: u, score: 0 });
    }
    ranked.sort((a, b) => b.score - a.score);
    return ranked.map((r) => r.url);
  }

  // Отправка одного медиа в mode B (пикабу) с перебором кандидатов.
  // base — payload БЕЗ mediaUrl (добавится для каждой попытки).
  async function enqueueModeBWithCandidates(base, media, articleEl) {
    let candidates = [media.url];
    try {
      candidates = await resolvePikabuModeBCandidates(media, articleEl);
    } catch (e) {
      tgqWarn("pikabu-modeB: не удалось собрать кандидатов:", e);
    }
    try {
      tgqLog(
        "pikabu-modeB: порядок отправки:",
        candidates.map((u) => (u || "").split("/").pop().slice(0, 36)).join(" | ")
      );
    } catch (e) {
      /* ignore */
    }
    let resp = null;
    let attempts = 0;
    for (const url of candidates) {
      if (!url || attempts >= MAX_ENQUEUE_ATTEMPTS) break;
      attempts++;
      const payload = Object.assign({}, base, { mediaUrl: addCacheBust(url) });
      tgqLog("pikabu-modeB: ENQUEUE #" + attempts, "mediaUrl=" + payload.mediaUrl);
      try {
        resp = await chrome.runtime.sendMessage(payload);
      } catch (e) {
        resp = { ok: false, error: e && e.message ? e.message : "нет связи с фоновым скриптом" };
      }
      tgqLog("pikabu-modeB: ответ бота:", resp);
      if (resp && resp.ok) break;
      if (!isStubError(resp)) break; // другая ошибка — перебор не поможет
    }
    return resp;
  }

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
    if (!articleEl) return false;
    // Основные маркеры пикабу + широкий фолбэк по имени класса: пикабу
    // периодически меняет вёрстку заглушки (story__nsfw-stub и т.п.), и
    // если селектор отстал — кнопка вообще не появляется у 18+ поста.
    // unlockAdult-посты — только пикабу, ложные срабатывания маловероятны.
    return !!(
      articleEl.querySelector(
        ".story__nsfw-stub, .story__adult-overlay, [data-action='adult-confirm'], [class*='nsfw'], [class*='adult']"
      ) ||
      articleEl.querySelector("[class*='stub'], [class*='warning']")
    );
  }

  function tryUnlockAdult(articleEl) {
    const sel =
      "button[data-action='adult-confirm'], .story__adult-confirm-btn, .story__adult-overlay button:not(.tgq-btn)";
    // Сначала внутри article, затем по всему документу (пикабу иногда показывает
    // confirm-диалог как глобальную модалку, а не внутри поста).
    const btn =
      (articleEl && articleEl.querySelector(sel)) || document.querySelector(sel);
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
    const permalink = getArticlePermalink(articleEl, config);
    tgqLog("resolveMedia: старт — медиа в DOM:", media.length, "permalink:", permalink);
    if (media.length) return media;
    if (tryUnlockAdult(articleEl)) {
      tgqLog("resolveMedia: unlock-клик выполнен (кнопка 18+ найдена)");
      // Пауза на перерисовку/ленивую загрузку картинок после раскрытия:
      // две попытки с разной задержкой (медиа могут приехать позже).
      await new Promise((r) => setTimeout(r, 700));
      media = collectMediaInArticle(articleEl, config);
      if (media.length) {
        tgqLog("resolveMedia: после unlock (700мс) медиа:", media.length);
        return media;
      }
      await new Promise((r) => setTimeout(r, 1500));
      media = collectMediaInArticle(articleEl, config);
      if (media.length) {
        tgqLog("resolveMedia: после unlock (1500мс) медиа:", media.length);
        return media;
      }
      tgqWarn("resolveMedia: unlock-клик не дал медиа в DOM");
    } else {
      tgqLog("resolveMedia: unlock-клик невозможен — кнопка 18+ не найдена");
    }
    if (typeof config.fallbackResolve === "function" && permalink && /^https?:/.test(permalink)) {
      try {
        tgqShowToast("Получаю медиа через страницу поста (с вашими cookies)…", "info", 2500);
        tgqLog("resolveMedia: fallbackResolve →", permalink);
        const resolved = await config.fallbackResolve(permalink);
        const list = (resolved.mediaUrls || (resolved.fileUrl ? [resolved.fileUrl] : [])).map(
          (u) => ({ url: u, kind: mediaKind(null, u), viaFallback: true })
        );
        tgqLog("resolveMedia: fallback нашёл медиа:", list.length);
        if (list.length) {
          tgqShowToast(`Найдено медиа на странице поста: ${list.length}`, "ok", 2500);
          return list;
        }
      } catch (e) {
        tgqWarn("resolveMedia: fallbackResolve ошибка:", e && e.message ? e.message : e);
        tgqShowToast(
          e && e.message ? e.message : "Не удалось получить медиа со страницы поста",
          "err",
          8000
        );
        return [];
      }
    }
    if (isAdultStubArticle(articleEl)) {
      tgqWarn("resolveMedia: 18+-заглушка, медиа не получены");
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
  // articleEl передаётся для пикабу mode B (сбор URL-кандидатов из DOM поста).
  async function sendToQueueWithId(mediaList, permalink, queueId, sourceBtn, articleEl) {
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
      const mode = m.viaFallback ? "B" : isBlob ? "A" : config.mode;
      // base payload без mediaUrl — его проставит отправка (кандидаты пикабу).
      const base = {
        type: "ENQUEUE",
        pageUrl: permalink || location.href,
        mediaUrl: null,
        referer: config.needsReferer ? location.origin + "/" : null,
        tags: extractTags(config),
        title: document.title,
        site: location.hostname.replace(/^www\./, ""),
        mode,
        mediaGroupId: useMgid,
        queueId: queueId || null,
      };
      try {
        let resp;
        if (mode === "B" && m.url && isPikabuSite()) {
          resp = await enqueueModeBWithCandidates(base, m, articleEl || null);
        } else {
          base.mediaUrl = m.url;
          tgqLog(
            "sendToQueueWithId: mode=" + base.mode,
            "mediaUrl=" + (base.mediaUrl || "(null)"),
            "pageUrl=" + base.pageUrl
          );
          resp = await chrome.runtime.sendMessage(base);
          tgqLog("sendToQueueWithId: ответ бота:", resp);
        }
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
      sendToQueueWithId(media, permalink, queueId, btn, articleEl);
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
    const mode = media.viaFallback ? "B" : isBlobVideo ? "A" : config.mode;
    // blob: видео → mediaUrl=null + mode=A (бэкенд использует yt-dlp на permalink).
    // viaFallback (18+/NSFW пикабу) → mode=B: браузер скачает и зальёт сам.
    // pageUrl = permalink (НЕ location.href!) — иначе бот качает HTML ленты x.com/home.
    const base = {
      type: "ENQUEUE",
      pageUrl: permalink || location.href,
      mediaUrl: null,
      referer: config.needsReferer ? location.origin + "/" : null,
      tags: extractTags(config),
      title: document.title,
      site: location.hostname.replace(/^www\./, ""),
      mode,
      queueId: queueId || null,
    };
    tgqLog(
      "sendSingle: mode=" + mode,
      "mediaUrl=" + (media.url || "(null — yt-dlp)"),
      "pageUrl=" + base.pageUrl
    );

    try {
      let resp;
      if (mode === "B" && media.url && isPikabuSite()) {
        // Пикабу mode B: cache-bust + анализ GIF-кандидатов + перебор при «заглушке».
        resp = await enqueueModeBWithCandidates(base, media, articleEl || null);
      } else {
        base.mediaUrl = media.url;
        tgqLog("sendSingle: ENQUEUE mediaUrl=" + (base.mediaUrl || "(null)"));
        resp = await chrome.runtime.sendMessage(base);
        tgqLog("sendSingle: ответ бота:", resp);
      }
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
        if (isStubError(resp)) {
          tgqShowToast(
            "Пикабу отдаёт заглушку 18+ даже браузером. Проверьте: вы залогинены на pikabu.ru и включён показ 18+ в настройках профиля. Затем откройте пост заново и повторите.",
            "err",
            12000
          );
        } else {
          tgqShowToast(resp?.error || "Ошибка отправки", "err");
        }
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
        const mode = m.viaFallback ? "B" : isBlob ? "A" : config.mode;
        const base = {
          type: "ENQUEUE",
          // pageUrl = permalink (НЕ location.href) — иначе бот качает HTML ленты.
          pageUrl: permalink || location.href,
          mediaUrl: null,
          referer: config.needsReferer ? location.origin + "/" : null,
          tags: extractTags(config),
          title: document.title,
          site: location.hostname.replace(/^www\./, ""),
          mode,
          // media_group_id — одинаковый для всех выбранных медиа этого поста.
          // Бэкенд сохранит его; если у очереди group_albums=True, scheduler
          // отправит все посты с этим ID одним сообщением (send_media_group).
          mediaGroupId: useMgid,
        };
        try {
          let resp;
          if (mode === "B" && m.url && isPikabuSite()) {
            resp = await enqueueModeBWithCandidates(base, m, articleEl || null);
          } else {
            base.mediaUrl = m.url;
            tgqLog(
              "sendSelected: mode=" + base.mode,
              "mediaUrl=" + (base.mediaUrl || "(null)"),
              "pageUrl=" + base.pageUrl
            );
            resp = await chrome.runtime.sendMessage(base);
            tgqLog("sendSelected: ответ бота:", resp);
          }
          if (resp && resp.ok) {
            sent++;
            await markSent(m.url, permalink, resp.post_id);
          } else {
            failed++;
            tgqWarn("sendSelected: enqueue failed:", resp?.error);
          }
        } catch (e) {
          failed++;
          tgqWarn("sendSelected: enqueue error:", e);
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
