/* ==========================================================================
   TG Media Queue Bot — vanilla JS helpers (ТЗ §7, Task 4)
   - confirm dialogs на delete-формах
   - auto-refresh worker stats на дашборде (через /health/worker)
   - toast-уведомления после POST-actions (по ?toast=)
   Vanilla JS (no jQuery). Bootstrap 5 bundle уже загружен.
   ========================================================================== */
(function () {
    "use strict";

    /**
     * Подтверждение для всех форм с data-confirm="...".
     * Использует нативный confirm() — это работает везде, включая мобильные.
     */
    function setupConfirmForms() {
        var forms = document.querySelectorAll('form[data-confirm]');
        forms.forEach(function (form) {
            form.addEventListener('submit', function (e) {
                var msg = form.getAttribute('data-confirm');
                if (msg && !window.confirm(msg)) {
                    e.preventDefault();
                    e.stopPropagation();
                }
            });
        });
    }

    /**
     * Toast-контейнер для фидбэка (Bootstrap 5 toast).
     * Создаётся один раз, дальше в него кладутся сообщения.
     */
    function ensureToastContainer() {
        var existing = document.querySelector('.toast-container');
        if (existing) return existing;
        var div = document.createElement('div');
        div.className = 'toast-container';
        document.body.appendChild(div);
        return div;
    }

    function showToast(message, variant) {
        var container = ensureToastContainer();
        var variantClass = variant || 'info';
        var colorClass = {
            success: 'text-bg-success',
            error: 'text-bg-danger',
            warning: 'text-bg-warning',
            info: 'text-bg-primary'
        }[variantClass] || 'text-bg-primary';

        var wrapper = document.createElement('div');
        wrapper.className = 'toast align-items-center ' + colorClass + ' border-0';
        wrapper.setAttribute('role', 'alert');
        wrapper.setAttribute('aria-live', 'assertive');
        wrapper.setAttribute('aria-atomic', 'true');
        wrapper.innerHTML =
            '<div class="d-flex">' +
            '  <div class="toast-body">' + escapeHtml(message) + '</div>' +
            '  <button type="button" class="btn-close btn-close-white me-2 m-auto" data-bs-dismiss="toast" aria-label="Закрыть"></button>' +
            '</div>';
        container.appendChild(wrapper);
        if (window.bootstrap && window.bootstrap.Toast) {
            var t = new window.bootstrap.Toast(wrapper, { delay: 4000 });
            t.show();
            wrapper.addEventListener('hidden.bs.toast', function () {
                wrapper.remove();
            });
        }
    }

    function escapeHtml(s) {
        if (s === null || s === undefined) return '';
        return String(s)
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    /**
     * Auto-refresh worker stats на дашборде.
     * Дёргает /health/worker (он требует X-Secret-Token если SECRET_TOKEN задан;
     * для веб-страницы Basic auth в браузере не пробрасывается в fetch,
     * поэтому при 401 просто ничего не делаем — UI остаётся со старым значением).
     */
    function setupWorkerAutorefresh() {
        var statsBox = document.getElementById('worker-stats');
        var stateBadge = document.getElementById('worker-state-badge');
        if (!statsBox) return; // не на дашборде

        async function refresh() {
            try {
                var resp = await fetch('/health/worker', { credentials: 'same-origin' });
                if (!resp.ok) return; // 401/403 — просто пропускаем (auth не пройден в fetch)
                var data = await resp.json();
                if (!data || !data.task_queue) return;
                var tq = data.task_queue;
                setColValue(statsBox, 'Queued', tq.queued);
                setColValue(statsBox, 'In progress', tq.in_progress);
                setColValue(statsBox, 'Done', tq.done);
                setColValue(statsBox, 'Error', tq.error);
                if (stateBadge) {
                    var ok = data.status === 'ok';
                    stateBadge.className = 'badge ' + (ok ? 'bg-success' : 'bg-secondary');
                    stateBadge.textContent = ok ? 'запущен' : 'остановлен';
                }
            } catch (e) {
                // network error — тихо игнорируем (например, при offline)
            }
        }

        function setColValue(box, label, value) {
            var cols = box.querySelectorAll('.col');
            for (var i = 0; i < cols.length; i++) {
                var small = cols[i].querySelector('.small');
                var val = cols[i].querySelector('.fs-5');
                if (small && val && small.textContent.trim() === label) {
                    val.textContent = String(value);
                    return;
                }
            }
        }

        refresh();
        setInterval(refresh, 30000); // каждые 30 сек
    }

    /**
     * Show toast if URL has ?toast=...&variant=...
     * (Веб-роутер сейчас не проставляет этот параметр — заготовка на будущее.
     * Все действия возвращают redirect на referer, без toast-фидбэка.)
     */
    function showUrlToast() {
        var params = new URLSearchParams(window.location.search);
        var toast = params.get('toast');
        if (toast) {
            showToast(decodeURIComponent(toast), params.get('variant') || 'info');
        }
    }

    // Init
    document.addEventListener('DOMContentLoaded', function () {
        setupConfirmForms();
        setupWorkerAutorefresh();
        showUrlToast();
    });

    // Экспорт для использования из консоли
    window.TGWeb = { showToast: showToast };
})();
