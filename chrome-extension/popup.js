// Popup: быстрый обзор очередей + ручная отправка текущей страницы (fallback,
// если content script не смог найти медиа — например на неподдерживаемом сайте).
"use strict";

const $ = (id) => document.getElementById(id);

function setStatus(text, ok) {
  const el = $("status");
  el.textContent = text;
  el.className = ok ? "ok" : "err";
}

async function loadQueues() {
  const ul = $("queues");
  const resp = await chrome.runtime.sendMessage({ type: "GET_QUEUES" });
  ul.textContent = "";
  if (!resp || !resp.ok) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = "Сервер недоступен: " + ((resp && resp.error) || "проверьте настройки ⚙");
    ul.appendChild(li);
    return;
  }
  if (!resp.queues.length) {
    const li = document.createElement("li");
    li.className = "muted";
    li.textContent = "Очередей пока нет";
    ul.appendChild(li);
    return;
  }
  for (const q of resp.queues) {
    const li = document.createElement("li");
    const name = document.createElement("span");
    name.textContent = q.name;
    const cnt = document.createElement("b");
    cnt.textContent = q.pending;
    li.appendChild(name);
    li.appendChild(cnt);
    ul.appendChild(li);
  }
}

async function getCurrentTab() {
  const tabs = await chrome.tabs.query({ active: true, currentWindow: true });
  return tabs[0];
}

async function enqueueCurrentPage() {
  const tab = await getCurrentTab();
  if (!tab || !tab.url) return setStatus("Нет активной вкладки", false);

  const config = getConfigForHostname(new URL(tab.url).hostname || "");
  // mediaUrl попробуем вытащить из вкладки (content script окружение недоступно
  // напрямую — используем chrome.scripting? Нет permissions. Fallback: без mediaUrl,
  // бот сам разберётся со страницей или yt-dlp).
  const payload = {
    type: "ENQUEUE",
    pageUrl: tab.url,
    mediaUrl: null,
    referer: config && config.needsReferer ? new URL(tab.url).origin + "/" : null,
    tags: [],
    title: tab.title || "",
    site: (new URL(tab.url).hostname || "").replace(/^www\./, ""),
    mode: config ? config.mode : "A",
  };
  setStatus("Отправка…", true);
  const resp = await chrome.runtime.sendMessage(payload);
  if (resp && resp.ok) {
    setStatus(resp.message || "OK ✓", true);
  } else {
    setStatus("Ошибка: " + ((resp && resp.error) || "неизвестно"), false);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  getCurrentTab().then((tab) => {
    if (tab) $("pageinfo").textContent = tab.title || tab.url || "";
  });
  loadQueues();
  $("enqueue").addEventListener("click", enqueueCurrentPage);
  $("options").addEventListener("click", () => chrome.runtime.openOptionsPage());
});
