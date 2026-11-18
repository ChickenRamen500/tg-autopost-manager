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

    /**
     * Фикс «обрезанного» dropdown внутри .table-responsive.
     * overflow-x:auto у table-responsive превращает overflow-y в auto →
     * выпадающее меню очередей обрезается границей контейнера (в «Входящих»,
     * когда строк мало и меню не помещается по высоте).
     * Решение: на show.bs.dropdown переносим .dropdown-menu в <body>
     * (Popper позиционирует его по-прежнему у кнопки), на hidden — возвращаем.
     * Делегированная обработка — работает на всех страницах.
     */
    function setupDropdownClippingFix() {
        var movedMenus = new Map(); // toggle → menu

        document.addEventListener('show.bs.dropdown', function (ev) {
            var toggle = ev.target;
            if (!(toggle instanceof Element)) return;
            var dropdown = toggle.closest('.dropdown');
            if (!dropdown || !toggle.closest('.table-responsive')) return;
            var menu = dropdown.querySelector('.dropdown-menu');
            if (!menu) return;
            movedMenus.set(toggle, menu);
            document.body.appendChild(menu);
        });

        document.addEventListener('hidden.bs.dropdown', function (ev) {
            var toggle = ev.target;
            if (!(toggle instanceof Element)) return;
            var menu = movedMenus.get(toggle);
            if (!menu) return;
            movedMenus.delete(toggle);
            var dropdown = toggle.closest('.dropdown');
            if (dropdown) {
                dropdown.appendChild(menu);
            } else {
                menu.remove();
            }
        });
    }

    /**
     * Hover-миниатюры для строк с data-preview='[{"url":"...","video":false},...]'.
     * Используется во «Входящих» (router сам формирует data-preview per group).
     * Плавающая панель возле курсора; видео — muted autoplay loop.
     */
    function setupRowPreview() {
        var panel = null;
        var showTimer = null;
        var currentRow = null;

        function ensurePanel() {
            if (panel) return panel;
            panel = document.createElement('div');
            panel.className = 'tgq-row-preview';
            panel.style.display = 'none';
            panel.setAttribute('aria-hidden', 'true');
            document.body.appendChild(panel);
            return panel;
        }

        function hidePanel() {
            if (showTimer) {
                clearTimeout(showTimer);
                showTimer = null;
            }
            currentRow = null;
            if (panel) {
                panel.style.display = 'none';
                panel.innerHTML = '';
            }
        }

        function renderPanel(items, ev) {
            var p = ensurePanel();
            p.innerHTML = '';
            var shown = items.slice(0, 4);
            shown.forEach(function (it) {
                var box = document.createElement('div');
                box.className = 'tgq-row-preview-item';
                if (it.video) {
                    var v = document.createElement('video');
                    v.src = it.url;
                    v.muted = true;
                    v.loop = true;
                    v.playsInline = true;
                    v.autoplay = true;
                    v.preload = 'metadata';
                    v.setAttribute('muted', ''); // для iOS
                    box.appendChild(v);
                } else {
                    var img = document.createElement('img');
                    img.src = it.url;
                    img.alt = '';
                    img.loading = 'lazy';
                    img.addEventListener('error', function () {
                        box.classList.add('tgq-preview-error');
                        img.remove();
                        var ph = document.createElement('div');
                        ph.className = 'tgq-preview-ph';
                        ph.textContent = '⚠';
                        box.appendChild(ph);
                    });
                    box.appendChild(img);
                }
                p.appendChild(box);
            });
            if (!p.childNodes.length) return;

            p.style.display = 'flex';
            // Позиционируем у курсора, не выходя за края окна.
            var pad = 14;
            var w = p.offsetWidth || 360;
            var h = p.offsetHeight || 200;
            var x = (ev.clientX || 0) + pad;
            var y = (ev.clientY || 0) + pad;
            if (x + w > window.innerWidth - 8) x = Math.max(8, ev.clientX - w - pad);
            if (y + h > window.innerHeight - 8) y = Math.max(8, ev.clientY - h - pad);
            p.style.left = x + 'px';
            p.style.top = y + 'px';
        }

        document.addEventListener('mouseover', function (ev) {
            var row = ev.target && ev.target.closest ? ev.target.closest('tr[data-preview]') : null;
            if (!row) return;
            if (row === currentRow) return;
            hidePanel();
            currentRow = row;
            var items;
            try {
                items = JSON.parse(row.getAttribute('data-preview') || '[]');
            } catch (e) {
                return;
            }
            if (!items || !items.length) return;
            showTimer = setTimeout(function () {
                showTimer = null;
                if (currentRow === row) renderPanel(items, ev);
            }, 180);
        });

        document.addEventListener('mouseout', function (ev) {
            var row = ev.target && ev.target.closest ? ev.target.closest('tr[data-preview]') : null;
            if (!row) return;
            var to = ev.relatedTarget;
            if (to && row.contains(to)) return; // переход внутри строки
            hidePanel();
        });

        // Уходим со страницы/меняем вкладку — прячем панель.
        document.addEventListener('visibilitychange', function () {
            if (document.hidden) hidePanel();
        });
        window.addEventListener('scroll', hidePanel, true);
    }

    // Init
    document.addEventListener('DOMContentLoaded', function () {
        setupConfirmForms();
        setupWorkerAutorefresh();
        showUrlToast();
        setupDropdownClippingFix();
        setupRowPreview();
    });

    // Экспорт для использования из консоли
    window.TGWeb = { showToast: showToast };
})();
