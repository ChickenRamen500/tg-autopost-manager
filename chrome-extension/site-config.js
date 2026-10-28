// Конфигурация по сайтам (ТЗ §9.3).
// mode "A" — бот скачивает сам по прямой ссылке (quick_add);
// mode "B" — расширение скачивает файл браузером с cookies (quick_add_upload).
//
// ВАЖНО: host_permissions в manifest.json должен покрывать mediaHosts —
// иначе fetch медиа в service worker блокируется CORS.
const SITE_CONFIGS = {
  "gelbooru.com": {
    mode: "A",
    // Ссылка на оригинал: <a href="https://img3.gelbooru.com/images/...">
    mediaSelector: "a[href*='/images/'], a[href*='img3.gelbooru.com']",
    mediaHosts: ["gelbooru.com", "img3.gelbooru.com"],
    needsReferer: true,
    tagsSelector: "#tag-list li a[href*='tags='], .tag-list-section a",
  },
  "danbooru.donmai.us": {
    mode: "A",
    // Danbooru отдаёт data-original-url прямо на <img>; ссылка "Original image"
    mediaSelector: "a[itemprop='contentURL'], a[href$='/original'], #image-container img[id='image'], #show-post a[href]",
    mediaAttrPriority: ["data-file-url", "src"],
    mediaHosts: ["donmai.us"],
    needsReferer: false,
    tagsSelector: ".tag-list a.search-tag, #tags-sidebar a.search-tag",
  },
  "www.pixiv.net": {
    mode: "A",
    // meta og:image указывает на i.pximg.net — оригинал берём из JSON на странице,
    // но для прототипа достаточно og:image (обычно это превью большого размера)
    mediaSelector: "meta[property='og:image']",
    mediaAttrPriority: ["content"],
    mediaHosts: ["pixiv.net", "i.pximg.net", "s.pximg.net"],
    needsReferer: true, // pixiv требует Referer: https://www.pixiv.net/
    tagsSelector: "a[data-gtm='new-work-tag-event-click'], style a[href^='/tags/']",
  },
  "pikabu.ru": {
    mode: "A",
    mediaSelector: ".story_image img, .story-image__image img, img.story__image",
    mediaAttrPriority: ["src"],
    mediaHosts: ["pikabu.ru", "cs.pikabu.ru", "boulanger"], // cs*.pikabu.ru
    needsReferer: false,
    spa: true, // подгрузка контента → нужен MutationObserver
    tagsSelector: "a[href*='/tag/']",
  },
  "x.com": {
    mode: "B", // браузерное скачивание (cookies пользователя)
    mediaSelector:
      "article img[src*='pbs.twimg.com/media'], article video, x-video video, article source[src*='video.twimg.com']",
    mediaHosts: ["pbs.twimg.com", "video.twimg.com"],
    needsReferer: false,
    spa: true,
    tagsSelector: "",
  },
  "twitter.com": {
    mode: "B",
    mediaSelector:
      "article img[src*='pbs.twimg.com/media'], article video, x-video video, article source[src*='video.twimg.com']",
    mediaHosts: ["pbs.twimg.com", "video.twimg.com"],
    needsReferer: false,
    spa: true,
    tagsSelector: "",
  },
};

// Хост текущей страницы без www (как в ТЗ §9.4)
function currentSiteKey() {
  return location.hostname.replace(/^www\./, "");
}

// Расширенный поиск конфига: точный ключ или endswith (m.pixiv.net и т.п.)
function getConfigForHostname(hostname) {
  const h = (hostname || "").replace(/^www\./, "");
  if (SITE_CONFIGS[h]) return SITE_CONFIGS[h];
  for (const key of Object.keys(SITE_CONFIGS)) {
    if (h.endsWith("." + key) || h === key) return SITE_CONFIGS[key];
  }
  // pixiv: www.pixiv.net нормализуется удалением www → pixiv.net отсутствует в таблице
  if (h.endsWith("pixiv.net")) return SITE_CONFIGS["www.pixiv.net"];
  return null;
}

// Извлекает прямой URL медиа из элемента, найденного по mediaSelector.
function extractMediaUrl(mediaEl, config) {
  if (!mediaEl) return null;
  const tag = mediaEl.tagName.toLowerCase();

  // <meta property="og:image" content="...">
  if (tag === "meta") return mediaEl.getAttribute("content");

  // <video>/<source> — src у video или первого source. blob: URL'ы игнорируем (BUG-21):
  // они указывают на локальный MediaSource (HLS/dash) и не скачиваются fetch без cookies.
  if (tag === "video") {
    const src = mediaEl.getAttribute("src");
    if (src && !src.startsWith("blob:")) return src;
    const sourceEl = mediaEl.querySelector("source");
    if (sourceEl) {
      const ssrc = sourceEl.getAttribute("src");
      if (ssrc && !ssrc.startsWith("blob:")) return ssrc;
    }
    return null;
  }
  if (tag === "source") {
    const src = mediaEl.getAttribute("src");
    if (src && !src.startsWith("blob:")) return src;
    return null;
  }

  // Приоритетные атрибуты (danbooru: data-file-url точнее, чем src превью)
  for (const attr of config.mediaAttrPriority || []) {
    const v = mediaEl.getAttribute(attr);
    if (v) return _absMediaUrl(v, config);
  }

  // <a href="...jpg"> — режим A booru
  if (tag === "a") {
    const href = mediaEl.getAttribute("href");
    if (href) return _absMediaUrl(href, config);
  }

  // <img src|data-src>
  const src = mediaEl.getAttribute("src") || mediaEl.getAttribute("data-src");
  return src ? _absMediaUrl(src, config) : null;
}

function _absMediaUrl(u, config) {
  try {
    const abs = new URL(u, location.href).href;
    // Для danbooru: если есть data-file-url — он и есть оригинал; иначе апгрейдим превью
    return abs;
  } catch (e) {
    return null;
  }
}

// Проверка, что URL ведёт на разрешённый хост медиа (BUG-12).
// mediaHosts — allowlist. Если пусто — пропускаем всё.
function isAllowedMediaHost(url, config) {
  try {
    const h = new URL(url).hostname;
    const allowed = config.mediaHosts || [];
    if (!allowed.length) return true; // no allowlist = allow all
    return allowed.some((a) => h === a || h.endsWith("." + a));
  } catch {
    return false;
  }
}

// Поиск лучшего кандидата-медиа на странице (первый совпавший селектор).
function findBestMedia(config) {
  if (!config || !config.mediaSelector) return null;
  for (const sel of config.mediaSelector.split(",").map((s) => s.trim())) {
    try {
      const el = document.querySelector(sel);
      if (el) {
        const url = extractMediaUrl(el, config);
        if (url && /^https?:/.test(url) && isAllowedMediaHost(url, config)) return { el, url };
      }
    } catch (e) {
      /* невалидный селектор — пропускаем */
    }
  }
  return null;
}

// Теги поста (для маршрутизации на бэкенде по правилам ТЗ §2.2)
function extractTags(config) {
  if (!config || !config.tagsSelector) return [];
  try {
    return Array.from(document.querySelectorAll(config.tagsSelector))
      .map((a) => (a.textContent || "").trim().toLowerCase())
      .filter(Boolean)
      .slice(0, 60);
  } catch (e) {
    return [];
  }
}

if (typeof globalThis !== "undefined") {
  globalThis.SITE_CONFIGS = SITE_CONFIGS;
  globalThis.getConfigForHostname = getConfigForHostname;
  globalThis.findBestMedia = findBestMedia;
  globalThis.extractMediaUrl = extractMediaUrl;
  globalThis.isAllowedMediaHost = isAllowedMediaHost;
  globalThis.extractTags = extractTags;
}
