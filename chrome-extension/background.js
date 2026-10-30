// Service worker (Manifest V3): приём сообщений от content script и popup,
// отправка на бэкенд (ТЗ §9.5). Режим A — JSON quick_add; режим B — download+upload.
//
// MV3-нюансы:
//  - SW может «усыпать» между событиями — вся логика в обработчике onMessage,
//    ответ через sendResponse + return true (асинхронный канал).
//  - Настройки читаются из chrome.storage.sync при каждом запросе (не хардкод, ТЗ §9.2).
//  - Для скачивания файлов >~64 МБ используем streaming (resp.blob()) — MV3 не имеет
//    DOM URL.createObjectURL, но Blob пересылается в fetch как есть.

const DEFAULT_SETTINGS = {
  botUrl: "http://192.168.1.50:8000",
  secretToken: "",
  defaultQueueId: null, // null = маршрутизация по правилам / Входящие
};

async function getSettings(override) {
  const stored = await chrome.storage.sync.get(Object.keys(DEFAULT_SETTINGS));
  const s = { ...DEFAULT_SETTINGS, ...stored, ...(override || {}) };
  s.botUrl = (s.botUrl || "").replace(/\/+$/, ""); // без завершающего слеша
  return s;
}

function authHeaders(settings) {
  // токен кладём И в заголовок, и в body (бэкенд принимает оба варианта)
  return settings.secretToken ? { "X-Secret-Token": settings.secretToken } : {};
}

async function postQuickAdd(settings, payload) {
  const body = {
    url: payload.pageUrl,
    media_url: payload.mediaUrl,
    referer: payload.referer || null,
    title: payload.title || "",
    tags: payload.tags || [],
    site: payload.site || "",
    queue_id: settings.defaultQueueId || null,
    secret_token: settings.secretToken,
    // media_group_id — для объединения нескольких медиа одного поста в альбом.
    // Scheduler отправит все посты с одинаковым media_group_id одним сообщением
    // через send_media_group (если у очереди включён group_albums=True).
    media_group_id: payload.mediaGroupId || null,
  };
  const resp = await fetch(`${settings.botUrl}/api/quick_add`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders(settings) },
    body: JSON.stringify(body),
  });
  return normalizeResponse(resp);
}

async function postQuickAddUpload(settings, payload) {
  // Режим B: скачиваем медиа БРАУЗЕРОМ (cookies подставятся автоматически для x.com)
  // Защита от null/не-HTTP mediaUrl (BUG-3): popup отправляет null, если не смог извлечь URL
  if (!payload.mediaUrl || !/^https?:/i.test(payload.mediaUrl)) {
    return {
      ok: false,
      error: "Не удалось определить прямую ссылку медиа — откройте пост и используйте кнопку на странице",
    };
  }
  let mediaResp;
  try {
    mediaResp = await fetch(payload.mediaUrl, { credentials: "include" });
  } catch (e) {
    return { ok: false, error: "Не удалось скачать медиа в браузере: " + e.message };
  }
  if (!mediaResp.ok) {
    return { ok: false, error: `Скачивание медиа: HTTP ${mediaResp.status}` };
  }
  const blob = await mediaResp.blob();
  const MAX_MB = 200;
  if (blob.size > MAX_MB * 1024 * 1024) {
    return { ok: false, error: `Файл >${MAX_MB} МБ — киньте ссылку поста боту в ЛС (yt-dlp)` };
  }
  const fname = decodeURIComponent((payload.mediaUrl.split("/").pop() || "media").split("?")[0]) || "media";
  const fd = new FormData();
  fd.append("file", blob, fname);
  fd.append("url", payload.pageUrl || "");
  fd.append("title", payload.title || "");
  fd.append("tags", JSON.stringify(payload.tags || []));
  if (settings.defaultQueueId) fd.append("queue_id", String(settings.defaultQueueId));
  fd.append("secret_token", settings.secretToken);
  // media_group_id — для объединения нескольких медиа одного поста в альбом.
  if (payload.mediaGroupId) fd.append("media_group_id", payload.mediaGroupId);

  const resp = await fetch(`${settings.botUrl}/api/quick_add_upload`, {
    method: "POST",
    headers: { ...authHeaders(settings) }, // Content-Type ставит fetch (multipart boundary)
    body: fd,
  });
  return normalizeResponse(resp);
}

async function normalizeResponse(resp) {
  let data = null;
  try {
    data = await resp.json();
  } catch (e) {
    /* не-JSON ответ (например HTML ошибки прокси) */
  }
  if (resp.ok && data && data.success) {
    return { ok: true, duplicate: !!data.duplicate, post_id: data.post_id, message: data.message };
  }
  const detail = (data && (data.detail || data.message)) || `HTTP ${resp.status}`;
  return { ok: false, error: typeof detail === "string" ? detail : JSON.stringify(detail) };
}

async function handleEnqueue(payload) {
  const settings = await getSettings();
  if (!settings.botUrl) return { ok: false, error: "Не задан адрес бота (страница настроек)" };
  if (!settings.secretToken) return { ok: false, error: "Не задан секретный токен (страница настроек)" };

  // Режим берём из явного указателя content script'а; fallback — x.com/twitter → B
  let mode = payload.mode;
  if (!mode) {
    try {
      const host = new URL(payload.pageUrl).hostname;
      mode = host.includes("x.com") || host.includes("twitter.com") ? "B" : "A";
    } catch (e) {
      mode = "A";
    }
  }
  try {
    if (mode === "B") {
      return await postQuickAddUpload(settings, payload);
    }
    return await postQuickAdd(settings, payload);
  } catch (e) {
    // network error / bad URL / mixed content
    return { ok: false, error: "Сеть недоступна: " + e.message };
  }
}

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (!msg || !msg.type) return;

  if (msg.type === "ENQUEUE") {
    handleEnqueue(msg).then(sendResponse);
    return true; // асинхронный ответ
  }

  if (msg.type === "GET_QUEUES") {
    (async () => {
      const settings = await getSettings(msg.override);
      try {
        const u = `${settings.botUrl}/api/queues?secret_token=${encodeURIComponent(settings.secretToken)}`;
        const resp = await fetch(u, { headers: { ...authHeaders(settings) } });
        const data = await resp.json();
        sendResponse({ ok: resp.ok, queues: data.queues || [], error: resp.ok ? undefined : "check settings" });
      } catch (e) {
        sendResponse({ ok: false, queues: [], error: e.message });
      }
    })();
    return true;
  }

  if (msg.type === "TEST_CONNECTION") {
    (async () => {
      const settings = await getSettings(msg.override);
      try {
        const resp = await fetch(`${settings.botUrl}/health`);
        const healthOk = resp.ok;
        const q = await fetch(
          `${settings.botUrl}/api/queues?secret_token=${encodeURIComponent(settings.secretToken)}`,
          { headers: { ...authHeaders(settings) } }
        );
        sendResponse({ ok: healthOk && q.ok, health: resp.status, api: q.status });
      } catch (e) {
        sendResponse({ ok: false, error: e.message });
      }
    })();
    return true;
  }
});
