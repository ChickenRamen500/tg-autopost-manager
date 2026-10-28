// Страница настроек расширения (ТЗ §9.2): botUrl + secretToken + очередь по умолчанию.
"use strict";

const $ = (id) => document.getElementById(id);

async function loadSettings() {
  const s = await chrome.storage.sync.get(["botUrl", "secretToken", "defaultQueueId"]);
  if (s.botUrl) $("botUrl").value = s.botUrl;
  if (s.secretToken) $("secretToken").value = s.secretToken;
  return s;
}

function setStatus(text, ok) {
  const el = $("status");
  el.textContent = text;
  el.className = ok ? "ok" : "err";
}

async function saveSettings() {
  const botUrl = $("botUrl").value.trim().replace(/\/+$/, "");
  const secretToken = $("secretToken").value.trim();
  const defaultQueueId = $("queueSelect").value ? Number($("queueSelect").value) : null;
  if (!/^https?:\/\/.+/.test(botUrl)) {
    setStatus("Адрес должен начинаться с http:// или https://", false);
    return;
  }
  await chrome.storage.sync.set({ botUrl, secretToken, defaultQueueId });
  setStatus("Сохранено ✓", true);
}

async function loadQueues() {
  const sel = $("queueSelect");
  const current = sel.value;
  // сохраняем первый option
  while (sel.options.length > 1) sel.remove(1);
  const resp = await chrome.runtime.sendMessage({ type: "GET_QUEUES" });
  if (resp && resp.ok && Array.isArray(resp.queues)) {
    for (const q of resp.queues) {
      const opt = document.createElement("option");
      opt.value = String(q.id);
      opt.textContent = `${q.name}${q.channel_name ? " → " + q.channel_name : ""} (${q.pending} в ожидании)`;
      sel.appendChild(opt);
    }
    if (current) sel.value = current;
    setStatus(`Загружено очередей: ${resp.queues.length}`, true);
  } else {
    setStatus("Не удалось загрузить очереди: " + ((resp && resp.error) || "сервер недоступен"), false);
  }
}

async function testConnection() {
  setStatus("Проверяю…", true);
  const resp = await chrome.runtime.sendMessage({ type: "TEST_CONNECTION" });
  if (resp && resp.ok) {
    setStatus("Подключение OK ✓ (health + api/queues)", true);
  } else {
    setStatus(
      "Ошибка: " + ((resp && (resp.error || `health=${resp.health} api=${resp.api}`)) || "нет ответа"),
      false
    );
  }
}

document.addEventListener("DOMContentLoaded", async () => {
  await loadSettings();
  $("save").addEventListener("click", saveSettings);
  $("test").addEventListener("click", testConnection);
  $("reload").addEventListener("click", loadQueues);
  // автозагрузка очередей при открытых настройках
  loadQueues();
});
