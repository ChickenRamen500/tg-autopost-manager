// Конфигурация по сайтам (ТЗ §9.3, переработано в Task 2-3-4-5-6-7).
//
// Ключевые понятия:
//   articleSelector      — селектор(ы) контейнера одного поста; в каждый контейнер
//                           инъецируется отдельная кнопка (одна кнопка на пост).
//   mediaSelector        — селектор(ы) медиа-элементов внутри поста (img/video/source/a).
//   mediaAttrPriority    — порядок чтения атрибутов (danbooru: data-file-url точнее src).
//   mediaHosts           — allowlist хостов медиа (фильтр мусорных tracker-URL).
//   needsReferer         — отправлять Referer на бэкенд (Pixiv/Gelbooru hotlink protection).
//   tagsSelector         — селектор тегов поста (для маршрутизации на бэкенде).
//   spa                   — включить MutationObserver для переинъекции (x.com/pikabu/pixiv).
//   buttonAnchorSelector — где внутри поста разместить кнопку (action bar / footer).
//   buttonPosition       — append | prepend | before | after относительно anchor.
//                           append  → в конец anchor (в action bar).
//                           prepend → в начало anchor.
//                           before  → перед anchor.
//                           after   → после anchor.
//   extractMediaUrls     — кастомная функция(articleEl, config) → string[]|null.
//                           Возвращает прямые URL медиа (Pixiv: оригиналы из JSON).
//   waitForLoad          — ждать img.complete && naturalWidth>0 перед извлечением URL.
//
// ВАЖНО: host_permissions в manifest.json должен покрывать mediaHosts —
// иначе fetch медиа в service worker блокируется CORS.

const SITE_CONFIGS = {
  "gelbooru.com": {
    mode: "A",
    // Контейнер одного поста (на странице просмотра — он один).
    articleSelector: "#image-container, #post-view, #container",
    // Прямая ссылка на оригинал: <a href="https://img3.gelbooru.com/images/...">
    // или <img id="image" src="...">.
    // singleMedia: на странице поста отправляем ВСЕГДА «оригинал» (только один
    // медиа на пост) — preferUrlPattern выбирает ссылку на /images/ (оригинал),
    // а не img#image (может быть sample у больших изображений).
    mediaSelector: "a[href*='img3.gelbooru.com/images/'], a[href*='/images/'], img#image",
    mediaAttrPriority: ["href", "src"],
    mediaHosts: ["gelbooru.com", "img3.gelbooru.com"],
    needsReferer: true,
    tagsSelector: "#tag-list li a[href*='tags='], .tag-list-section a",
    buttonAnchorSelector: ".image-container-notes, #image-container, #note-container",
    buttonPosition: "after",
    // Permalink: на странице поста — это canonical или location.href.
    permalinkSelector: "link[rel='canonical']",
    singleMedia: true,
    preferUrlPattern: "/images/",
    // Страница поиска (index.php?page=post&s=list): чекбоксы + кнопки на
    // thumbnail-preview, тулбар с кнопкой отправки и выбором очереди — НАД
    // thumbnail-container. Оригинальный URL каждого поста резолвится из HTML
    // страницы поста (dapi требует api_key с 2023 → HTTP 401) — полное
    // разрешение и верное расширение.
    resolveOriginal: resolveGelbooruOriginal,
    listPage: {
      // URL поиска: содержит page=post и s=list (или gallery); страница поста — s=view.
      matchUrl: /(\?|&)(s=list|s=gallery)/,
      containerSelector: "#thumbnail-container, .thumbnail-container",
      itemSelector: ".thumbnail-preview",
      linkSelector: "a[href*='page=post&s=view']",
      toolbarPosition: "before", // тулбар НАД контейнером
    },
  },
  "danbooru.donmai.us": {
    mode: "A",
    articleSelector: "#post-view, #c-posts article, #show-post",
    // Danbooru отдаёт data-original-url / data-file-url прямо на <img>; либо <a itemprop="contentURL">.
    mediaSelector: "a[itemprop='contentURL'], a[href$='/original'], #image-container img[id='image'], #show-post a[href]",
    mediaAttrPriority: ["data-file-url", "data-original-url", "src"],
    mediaHosts: ["donmai.us"],
    needsReferer: false,
    tagsSelector: ".tag-list a.search-tag, #tags-sidebar a.search-tag",
    buttonAnchorSelector: "#post-information, .image-container",
    buttonPosition: "before",
    permalinkSelector: "link[rel='canonical']",
  },
  "www.pixiv.net": {
    mode: "A",
    // Pixiv artwork detail page — один пост на страницу; main — корневой контейнер.
    articleSelector: "main, #root",
    // Главное изображение — <img> внутри div[role=presentation]. Для multi-page —
    // thumbnail strip справа. Но основные URL'ы мы достаём через extractMediaUrls().
    mediaSelector: "main img[src*='i.pximg.net'], div[role='presentation'] img, canvas",
    mediaAttrPriority: ["src", "data-src"],
    mediaHosts: ["pixiv.net", "i.pximg.net", "s.pximg.net"],
    needsReferer: true, // pixiv требует Referer: https://www.pixiv.net/
    tagsSelector: "a[data-gtm='new-work-tag-event-click'], a[href^='/tags/']",
    // Кастомный экстрактор — читает __NEXT_DATA__ JSON и достаёт оригиналы.
    extractMediaUrls: extractPixivUrls,
    waitForLoad: true,
    buttonAnchorSelector: "div[role='toolbar'], section [role='toolbar'], [role='group']",
    buttonPosition: "append",
    permalinkSelector: "link[rel='canonical']",
  },
  "pikabu.ru": {
    mode: "A",
    // На ленте — несколько постов (article.story). На странице поста — один.
    articleSelector: "article.story, .story, .post",
    // Фото: блоки .story__block_type_media / .player (современная вёрстка),
    // плюс легаси-селекторы. ВАЖНО: раньше в списке были только легаси-классы
    // (.story_image и т.п.) — на современном пикабу они не совпадают, и кнопка
    // появлялась только у видео (video/video source). Теперь фото ищутся
    // универсально (img внутри media-блока или с хостом картинок пикабу).
    mediaSelector: [
      ".story__block_type_media img",
      ".story__block .player img",
      ".player img",
      "img[data-large-image]",
      "img[src*='cs.pikabu.ru/post_img']",
      "img[src*='s.pikabu.ru/post_img']",
      ".story_image img",
      ".story-image__image img",
      "img.story__image",
      "video source",
      "video",
    ].join(", "),
    mediaAttrPriority: ["src", "data-src"],
    mediaHosts: ["pikabu.ru", "cs.pikabu.ru", "cs1.pikabu.ru", "cs2.pikabu.ru", "s.pikabu.ru"],
    // Отсекаем аватары/иконки/эмодзи — они тоже лежат на хостах пикабу.
    excludeUrlPattern: "(\\/ava\\/|\\/avatar|\\/emoji|favicon|logo|\\.svg(\\?|#|$))",
    needsReferer: false,
    spa: true,
    // 18+ посты: у анонимов пикабу вырезает медиа из HTML (в DOM только
    // заглушки .story__nsfw-stub). unlockAdult включает: кнопку и на заглушках,
    // программный клик «Показать контент 18+» (у залогиненных) и внятный
    // toast, если без входа медиа не получить.
    unlockAdult: true,
    tagsSelector: "a[href*='/tag/'], .story_tags a",
    buttonAnchorSelector: ".story__footer, .post__footer, .actions, .story-tools",
    buttonPosition: "append",
    // На ленте — ссылка на пост; на странице поста — canonical.
    permalinkSelector: "a.story__title-link, a[href*='/story/'], link[rel='canonical']",
  },
  "x.com": {
    mode: "B", // браузерное скачивание (cookies пользователя)
    // Один твит = один article. На ленте — много articles; в каждом своя кнопка.
    articleSelector: "article[data-testid='tweet'], article[role='article']",
    // Twitter: картинки — <img src="https://pbs.twimg.com/media/...">. Видео — blob: (HLS).
    mediaSelector: "img[src*='pbs.twimg.com/media'], video[poster]",
    mediaAttrPriority: ["src"],
    mediaHosts: ["pbs.twimg.com", "video.twimg.com"],
    needsReferer: false,
    spa: true,
    tagsSelector: "",
    // Action bar твита — div[role='group'] (reply/retweet/like/share).
    buttonAnchorSelector: "div[role='group'], [role='group']",
    buttonPosition: "append",
    // Permalink твита: a[href*='/status/'] — ссылка на время/дату или "Show this thread".
    permalinkSelector: "a[href*='/status/'], a[href*='/statuses/']",
  },
  "twitter.com": {
    mode: "B",
    articleSelector: "article[data-testid='tweet'], article[role='article']",
    mediaSelector: "img[src*='pbs.twimg.com/media'], video[poster]",
    mediaAttrPriority: ["src"],
    mediaHosts: ["pbs.twimg.com", "video.twimg.com"],
    needsReferer: false,
    spa: true,
    tagsSelector: "",
    buttonAnchorSelector: "div[role='group'], [role='group']",
    buttonPosition: "append",
    permalinkSelector: "a[href*='/status/'], a[href*='/statuses/']",
  },
};

// --- Хост текущей страницы без www (как в ТЗ §9.4) ---
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

  // <video>/<source> — src у video или первого source. blob: URL'ы игнорируем:
  // они указывают на локальный MediaSource (HLS/dash) и не скачиваются fetch.
  // (video poster отдельно обрабатывается в collectMediaInArticle.)
  if (tag === "video") {
    const src = mediaEl.getAttribute("src");
    if (src && !src.startsWith("blob:")) return _absMediaUrl(src, config);
    const sourceEl = mediaEl.querySelector("source");
    if (sourceEl) {
      const ssrc = sourceEl.getAttribute("src");
      if (ssrc && !ssrc.startsWith("blob:")) return _absMediaUrl(ssrc, config);
    }
    return null;
  }
  if (tag === "source") {
    const src = mediaEl.getAttribute("src");
    if (src && !src.startsWith("blob:")) return _absMediaUrl(src, config);
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
    let url = new URL(u, location.href).href;
    url = _upgradeTwitterQuality(url);
    url = _upgradePikabuQuality(url);
    return url;
  } catch (e) {
    return null;
  }
}

// Апгрейд качества изображений Pikabu до максимального размера.
// cs.pikabu.ru/post_img/medium|small|preview/... → /post_img/big/ (оригинал).
// «big» — самый крупный размер, который пикабу отдаёт по прямой ссылке.
function _upgradePikabuQuality(url) {
  try {
    if (!/(\.|^|\.)pikabu\.ru$/i.test(new URL(url).hostname.replace(/^www\./, "")) && !/pikabu\.ru/.test(url)) {
      return url;
    }
    if (!/\/post_img\//.test(url)) return url;
    return url.replace(/\/post_img\/(preview|small|medium)\//, "/post_img/big/");
  } catch {
    return url;
  }
}

// Апгрейд качества изображений Twitter до оригинала.
// pbs.twimg.com/media/...?format=jpg&name=small → name=orig
// Старый формат: /media/abc.jpg:small → /media/abc.jpg:orig
function _upgradeTwitterQuality(url) {
  try {
    const u = new URL(url);
    if (u.hostname !== "pbs.twimg.com") return url;
    const nameParam = u.searchParams.get("name");
    if (nameParam) {
      u.searchParams.set("name", "orig");
      return u.href;
    }
    const path = u.pathname;
    const colonIdx = path.lastIndexOf(":");
    if (colonIdx > path.lastIndexOf("/")) {
      u.pathname = path.substring(0, colonIdx) + ":orig";
      return u.href;
    }
    if (!u.searchParams.has("format")) {
      u.searchParams.set("format", "jpg");
    }
    u.searchParams.set("name", "orig");
    return u.href;
  } catch {
    return url;
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

// Тип медиа по элементу/URL — нужен для выбора иконки и обработки blob видео.
function mediaKind(el, url) {
  const tag = el?.tagName?.toLowerCase();
  if (tag === "video" || tag === "source") return "video";
  if (/\.(mp4|webm|gifv)(\?|#|$)/i.test(url || "")) return "video";
  if (/\.gif(\?|#|$)/i.test(url || "")) return "gif";
  return "img";
}

// --- Сбор всех медиа внутри одного поста (article) ---
// Возвращает массив { url, el, kind, poster }.
// url может быть null (blob: видео — отправляется как page URL mode B / yt-dlp).
function collectMediaInArticle(articleEl, config) {
  if (!articleEl || !config) return [];

  let excludeRe = null;
  if (config.excludeUrlPattern) {
    try {
      excludeRe = new RegExp(config.excludeUrlPattern, "i");
    } catch {
      excludeRe = null;
    }
  }

  const items = [];
  const seen = new Set();
  const push = (url, el, kind, poster) => {
    if (url) {
      let abs;
      try { abs = new URL(url, location.href).href; } catch { return; }
      if (!/^https?:/.test(abs)) return;
      // Мусорные URL (аватары, иконки, эмодзи) — отсекаем по excludeUrlPattern.
      if (excludeRe && excludeRe.test(abs)) return;
      if (seen.has(abs)) return;
      seen.add(abs);
      items.push({ url: abs, el, kind, poster: poster || null });
    } else if (kind === "video") {
      // Blob-видео без прямого URL — одно на пост, дедуп по poster/URL страницы.
      const k = "blob-video:" + (poster || location.href);
      if (seen.has(k)) return;
      seen.add(k);
      items.push({ url: null, el, kind, poster: poster || null });
    }
  };

  // 1) Кастомный экстрактор (Pixiv): JSON __NEXT_DATA__ → массив оригинальных URL.
  if (typeof config.extractMediaUrls === "function") {
    try {
      const urls = config.extractMediaUrls(articleEl, config);
      if (urls && urls.length) {
        for (const u of urls) push(u, null, "img");
        return _applySingleMedia(items, config);
      }
    } catch (e) {
      /* fall back to selectors */
    }
  }

  // 2) Общий путь: перебираем mediaSelector.
  if (config.mediaSelector) {
    for (const sel of config.mediaSelector.split(",").map((s) => s.trim())) {
      if (!sel) continue;
      let matched;
      try {
        matched = articleEl.querySelectorAll(sel);
      } catch (e) {
        continue; // невалидный селектор
      }
      matched.forEach((el) => {
        if (!el || !el.tagName) return;
        const tag = el.tagName.toLowerCase();

        // <video>: если src реальный http(s) — берём. Если blob: — отдаём как
        // "видео-ссылку" (url=null, poster для превью), бэкенд использует yt-dlp.
        if (tag === "video") {
          const src = el.getAttribute("src");
          const poster = el.getAttribute("poster");
          if (src && /^https?:/.test(src) && !src.startsWith("blob:")) {
            push(src, el, "video", poster);
          } else if (poster) {
            // blob: или нет src — представляем как "видео через yt-dlp".
            push(null, el, "video", poster);
          }
          return;
        }
        if (tag === "source") {
          const src = el.getAttribute("src");
          if (src && /^https?:/.test(src) && !src.startsWith("blob:")) {
            push(src, el, "video");
          }
          return;
        }
        // <img>/<a>/<meta>/<canvas>: обычный extractMediaUrl.
        const url = extractMediaUrl(el, config);
        if (!url) return;
        if (!isAllowedMediaHost(url, config)) return;
        push(url, el, mediaKind(el, url));
      });
    }
  }
  return _applySingleMedia(items, config);
}

// singleMedia: у поста может быть несколько кандидатов (оригинальная ссылка +
// inline <img>, часто это один и тот же контент). Оставляем РОВНО один —
// приоритет URL'у, совпадающему с preferUrlPattern (например /images/ у
// gelbooru — оригинальное хранилище), иначе первый кандидат.
function _applySingleMedia(items, config) {
  if (!config || !config.singleMedia || items.length <= 1) return items;
  let best = items[0];
  if (config.preferUrlPattern) {
    try {
      const re = new RegExp(config.preferUrlPattern, "i");
      const match = items.find((i) => i.url && re.test(i.url));
      if (match) best = match;
    } catch {
      /* invalid pattern — keep first */
    }
  }
  return [best];
}

// --- Теги поста (для маршрутизации на бэкенде по правилам ТЗ §2.2) ---
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

// --- Pixiv: оригинальные URL из встроенного JSON ---
// Современный Pixiv — Next.js SPA. В <script id="__NEXT_DATA__"> лежит JSON вида:
//   { props: { pageProps: { initialState: { entities: { illust: { images: {
//     "<id>": { illustId, pageCount, urls: { original, regular, ... } } } } } } } } }
// older: <meta name="preload-data" content='...'>.
// Мы просто обходим JSON и собираем все строки urls.original. Для multi-page
// (pageCount > 1) генерируем URL'ы для страниц 1..pageCount-1 (страница 0 уже
// указана в original).
function extractPixivUrls(articleEl, config) {
  const collected = [];
  const seen = new Set();
  const add = (u) => {
    if (u && /^https?:/.test(u) && !seen.has(u)) {
      seen.add(u);
      collected.push(u);
    }
  };

  // Источники JSON: __NEXT_DATA__ (Next.js) и meta[name=preload-data] (older).
  const sources = [];
  try {
    const nd = document.getElementById("__NEXT_DATA__");
    if (nd && nd.textContent) sources.push(nd.textContent);
  } catch (e) {}
  try {
    const pd = document.querySelector("meta[name='preload-data']");
    if (pd && pd.content) sources.push(pd.content);
  } catch (e) {}

  for (const raw of sources) {
    try {
      const json = JSON.parse(raw);
      const illusts = [];
      const walk = (obj) => {
        if (!obj || typeof obj !== "object") return;
        // Иллюстрация Pixiv характеризуется полем urls.original.
        if (obj.urls && typeof obj.urls.original === "string") {
          illusts.push(obj);
          return;
        }
        for (const v of Object.values(obj)) walk(v);
      };
      walk(json);
      for (const it of illusts) {
        const orig = it.urls.original;
        add(orig);
        const pc = Number(it.pageCount) || 1;
        if (pc > 1) {
          // original уже указывает на страницу 0 — генерируем _p1, _p2, ...
          const m = orig.match(/^(.*_p)(\d+)(\.\w+)$/);
          if (m) {
            for (let i = 1; i < pc; i++) add(`${m[1]}${i}${m[3]}`);
          }
        }
      }
    } catch (e) {
      /* not JSON or unexpected shape */
    }
  }
  if (collected.length) return collected;

  // Fallback: сканируем <img> в articleEl, апгрейдим превью → оригинал.
  // /c/600x600/img-master/img/.../12345_master1200.jpg → /img-original/img/.../12345.jpg
  // Внимание: расширение файла может отличаться (.png/.gif), но оставляем как в URL.
  const upgrade = (url) => {
    if (!url || !/i\.pximg\.net/.test(url)) return null;
    let u = url;
    u = u.replace(/\/c\/\d+x\d+\/img-master\//, "/img-original/");
    u = u.replace(/_master\d+/, "");
    return u !== url ? u : null;
  };
  const imgs = articleEl ? articleEl.querySelectorAll("img[src*='i.pximg.net'], img[data-src*='i.pximg.net']") : [];
  imgs.forEach((el) => {
    // Дождаться загрузки (Pixiv lazy-loads).
    const complete = el.complete && el.naturalWidth > 0;
    const src = el.getAttribute("src") || el.getAttribute("data-src");
    if (complete && src) {
      const u = upgrade(src);
      if (u) add(u);
    } else if (src && /img-master/.test(src)) {
      const u = upgrade(src);
      if (u) add(u);
    }
  });
  // Multi-page: thumbnail strip справа — <a href="i.pximg.net/...">.
  const thumbs = articleEl ? articleEl.querySelectorAll("a[href*='i.pximg.net']") : [];
  thumbs.forEach((el) => {
    const href = el.getAttribute("href");
    if (href) {
      const u = upgrade(href);
      if (u) add(u);
    }
  });
  return collected.length ? collected : null;
}

// --- Gelbooru: оригинал поста через HTML-страницу поста ---------------------
// ВНИМАНИЕ: dapi (page=dapi&s=post&q=index&json=1&id=...) с 2023 требует
// api_key+user_id — анонимные запросы получают HTTP 401. Поэтому резолвим
// оригинал из HTML страницы поста (публична, same-origin, cookies идут сами):
//   • a[href*='/images/']        — ссылка «Original image» (всегда оригинал);
//   • video/source[src]          — mp4/webm поста;
//   • img#image[src]             — показываемая картинка (может быть sample);
//   • meta[property='og:image']  — фолбэк.
// Приоритет отдаём /images/ — это хранилище оригиналов с полным разрешением
// и верным расширением (.png/.gif/.webp нельзя угадать из превью).
// Кэш резолвов в рамках жизни страницы (id → {fileUrl, tags}).
const gelbooruResolveCache = new Map();

async function resolveGelbooruOriginal(permalink) {
  const m = String(permalink || "").match(/[?&]id=(\d+)/);
  if (!m) throw new Error("gelbooru: не найден id поста в ссылке");
  const pid = m[1];
  if (gelbooruResolveCache.has(pid)) return gelbooruResolveCache.get(pid);

  const pageUrl = `${location.origin}/index.php?page=post&s=view&id=${pid}`;
  const resp = await fetch(pageUrl, { credentials: "include" });
  if (!resp.ok) throw new Error(`gelbooru: HTTP ${resp.status} на странице поста`);
  const doc = new DOMParser().parseFromString(await resp.text(), "text/html");

  const candidates = [];
  // 1) Прямая ссылка на оригинал (под изображением на странице поста).
  doc.querySelectorAll("a[href*='/images/']").forEach((a) => {
    const h = a.getAttribute("href");
    if (h) candidates.push(h);
  });
  // 2) Видео (mp4/webm).
  doc.querySelectorAll("video source[src], video[src]").forEach((v) => {
    const s = v.getAttribute("src");
    if (s) candidates.push(s);
  });
  // 3) Показываемая картинка (img#image; у больших изображений это sample).
  const img = doc.querySelector("img#image");
  if (img && img.getAttribute("src")) candidates.push(img.getAttribute("src"));
  // 4) og:image / twitter:image — последний фолбэк.
  const og = doc.querySelector("meta[property='og:image'], meta[name='twitter:image']");
  if (og && og.getAttribute("content")) candidates.push(og.getAttribute("content"));

  const abs = (u) => {
    try { return new URL(u, location.origin).href; } catch { return null; }
  };
  const isOrig = (u) => /^https?:/i.test(u) && /\/images\//i.test(u);
  const isMedia = (u) =>
    /^https?:/i.test(u) &&
    /(gelbooru\.com|img\d+\.gelbooru\.com)/i.test(new URL(u).hostname) &&
    !/\/(thumbnails?|samples?)\//i.test(u);

  let fileUrl = null;
  for (const c of candidates) {
    const u = abs(c);
    if (u && isOrig(u)) { fileUrl = u; break; }
  }
  if (!fileUrl) {
    for (const c of candidates) {
      const u = abs(c);
      if (u && isMedia(u)) { fileUrl = u; break; }
    }
  }
  if (!fileUrl) throw new Error("gelbooru: не удалось извлечь оригинал со страницы поста");

  // Теги поста (для маршрутизации на бэкенде).
  const tags = Array.from(
    doc.querySelectorAll(
      "#tag-list li a[href*='tags='], .tag-list-section a[href*='tags='], a.tag[href*='tags=']"
    )
  )
    .map((a) => (a.textContent || "").trim().toLowerCase())
    .filter(Boolean)
    .slice(0, 60);

  const result = { fileUrl, tags };
  gelbooruResolveCache.set(pid, result);
  return result;
}

// --- Извлечение permalink'а конкретного поста из article ---
// Важно для x.com/twitter/pikabu: на ленте location.href = ".../home" или ".../feed",
// но каждый пост имеет свой permalink (https://x.com/{user}/status/{id}).
// Бэкенду нужен именно permalink как pageUrl — иначе он пытается скачать HTML ленты.
// Также permalink используется как ключ дедупликации (для blob-видео, где mediaUrl=null).
function getArticlePermalink(articleEl, config) {
  if (!articleEl || !config) return location.href;

  // 1) Способ 1: config.permalinkSelector — CSS-селектор(ы) внутри article.
  if (config.permalinkSelector) {
    for (const sel of config.permalinkSelector.split(",").map((s) => s.trim())) {
      if (!sel) continue;
      try {
        const el = articleEl.querySelector(sel);
        if (!el) continue;
        // <link rel="canonical" href="..."> — читаем href.
        // <a href="..."> — читаем href.
        const href = el.getAttribute("href");
        if (!href) continue;
        // Относительные ссылки (/user/status/123) → абсолютные.
        const abs = new URL(href, location.origin).href;
        // Фильтр: только http(s), не якоря (#...) и не просто location.href.
        if (/^https?:/.test(abs) && !abs.endsWith("#")) {
          // Для Twitter: отрезаем query-параметры (?s=46, ?ref_src=...),
          // оставляем чистый permalink /user/status/id
          if (abs.includes("/status/") || abs.includes("/statuses/")) {
            try {
              const u = new URL(abs);
              u.search = "";
              return u.href;
            } catch {}
          }
          return abs;
        }
      } catch (e) {
        /* invalid selector — skip */
      }
    }
  }

  // 2) Способ 2: canonical link в <head> (для детальных страниц поста).
  try {
    const canonical = document.querySelector("link[rel='canonical']");
    if (canonical) {
      const href = canonical.getAttribute("href");
      if (href) return new URL(href, location.origin).href;
    }
  } catch {}

  // 3) Fallback: location.href (последний шанс — на детальной странице поста это ОК).
  return location.href;
}

// --- Экспорт для content.js и popup.js (через chrome.scripting.executeScript) ---
if (typeof globalThis !== "undefined") {
  globalThis.SITE_CONFIGS = SITE_CONFIGS;
  globalThis.getConfigForHostname = getConfigForHostname;
  globalThis.collectMediaInArticle = collectMediaInArticle;
  globalThis.extractMediaUrl = extractMediaUrl;
  globalThis.isAllowedMediaHost = isAllowedMediaHost;
  globalThis.extractTags = extractTags;
  globalThis.extractPixivUrls = extractPixivUrls;
  globalThis.mediaKind = mediaKind;
  globalThis.getArticlePermalink = getArticlePermalink;
  globalThis.resolveGelbooruOriginal = resolveGelbooruOriginal;
}
