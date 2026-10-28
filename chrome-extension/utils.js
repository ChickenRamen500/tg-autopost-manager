// Общие хелперы для content script (прототип, ТЗ §9).
// Подключается ДО content.js в manifest.json ("js": ["site-config.js", "utils.js", "content.js"]).
(() => {
  "use strict";

  // Безопасная вставка HTML: экранирует все теги, разрешает только <b>, <i>, <br>.
  // Нужно потому, что error-сообщения бэкенда могут содержать текст пользователя
  // (название поста/теги) — без экранирования это XSS в контексте страницы (HIGH).
  function sanitizeHtml(str) {
    const escaped = String(str == null ? "" : str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
    return escaped
      .replace(/&lt;b&gt;/g, "<b>").replace(/&lt;\/b&gt;/g, "</b>")
      .replace(/&lt;i&gt;/g, "<i>").replace(/&lt;\/i&gt;/g, "</i>")
      .replace(/&lt;br\s*\/?&gt;/g, "<br>");
  }

  // Toast-уведомление в правом нижнем углу страницы (без inline-стилей — CSS-классы, CSP-safe).
  function showToast(message, type) {
    try {
      if (!document.body) return;
      let host = document.getElementById("tgq-toast-host");
      if (!host) {
        host = document.createElement("div");
        host.id = "tgq-toast-host";
        document.body.appendChild(host);
      }
      const div = document.createElement("div");
      div.className = "tgq-toast" + (type ? " tgq-" + type : "");
      div.innerHTML = sanitizeHtml(message); // safe: только b/i/br после экранирования
      host.appendChild(div);
      setTimeout(() => {
        div.classList.add("tgq-hide");
        setTimeout(() => div.remove(), 400);
      }, 3500);
    } catch (e) {
      /* toast не критичен */
    }
  }

  globalThis.tgqSanitizeHtml = sanitizeHtml;
  globalThis.tgqShowToast = showToast;
})();
