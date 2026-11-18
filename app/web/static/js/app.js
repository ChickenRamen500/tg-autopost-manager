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
     *
     * Решение (v2): НЕ переносим DOM (первая версия с переносом в <body>
     * оказалась хрупкой), а переключаем Popper на strategy:'fixed' —
     * position:fixed не обрезается overflow-родителями. Плюс boundary:'viewport'
     * у preventOverflow/flip (меню всегда в пределах окна) и собственный
     * модификатор maxSize — меню выше окна получает внутренний скролл.
     * Делегированная обработка — работает на всех страницах.
     */
    function setupDropdownClippingFix() {
        if (!window.bootstrap || !window.bootstrap.Dropdown) return;

        document.addEventListener('show.bs.dropdown', function (ev) {
            var toggle = ev.target instanceof Element
                ? (ev.target.matches('[data-bs-toggle="dropdown"]') ? ev.target : ev.target.closest('[data-bs-toggle="dropdown"]'))
                : null;
            if (!toggle) return;
            // Только внутри скроллируемых таблиц-контейнеров (проблемная зона),
            // остальные dropdown'ы не трогаем.
            if (!toggle.closest('.table-responsive, .tgq-scroll-container')) return;

            var inst = window.bootstrap.Dropdown.getInstance(toggle) ||
                       window.bootstrap.Dropdown.getOrCreateInstance(toggle);
            if (!inst || !inst._config) return;
            // Перенос меню в <body> из старой версии — нейтрализуем, если уже применён.
            delete inst._config.popperConfig;
            inst._config.popperConfig = function (def) {
                var cfg = Object.assign({}, def || {});
                cfg.strategy = 'fixed';
                cfg.modifiers = (cfg.modifiers || []).filter(function (m) {
                    return !m || ['preventOverflow', 'flip', 'maxSize', 'applyMaxSize'].indexOf(m.name) === -1;
                });
                cfg.modifiers.push({ name: 'preventOverflow', options: { boundary: 'viewport', altBoundary: true } });
                cfg.modifiers.push({
                    name: 'flip',
                    options: {
                        boundary: 'viewport',
                        altBoundary: true,
                        fallbackPlacements: ['top-end', 'top-start', 'bottom-end', 'bottom-start']
                    }
                });
                // Меню выше окна → внутренний скролл. ВАЖНО: в @popperjs/core v2
                // НЕТ модификатора maxSize (это из floating-ui), поэтому считаем
                // доступную высоту сами: после flip (phase beforeWrite) берём
                // placement и прямоугольник кнопки в координатах окна.
                cfg.modifiers.push({
                    name: 'applyMaxSize',
                    enabled: true,
                    phase: 'beforeWrite',
                    fn: function (arg) {
                        var st = arg.state;
                        try {
                            var ref = st.elements && st.elements.reference;
                            if (!ref) return;
                            var r = ref.getBoundingClientRect();
                            var isTop = String(st.placement || '').indexOf('top') === 0;
                            var margin = 10;
                            var avail = isTop
                                ? r.top - margin
                                : window.innerHeight - r.bottom - margin;
                            // Минимум 140px, чтобы даже в стеснённых условиях меню было юзабельным.
                            avail = Math.max(140, avail);
                            st.styles.popper.maxHeight = Math.floor(avail) + 'px';
                            st.styles.popper.overflowY = 'auto';
                        } catch (e) { /* не критично */ }
                    }
                });
                return cfg;
            };
        });
    }

    /**
     * Hover-миниатюры для строк с data-preview='[{"url":"...","video":false},...]'.
     * Используется во «Входящих», «Постах», «Архиве» и на дашборде
     * (router сам формирует data-preview per строку).
     *
     * Зона показа ограничена ячейками <td class="tgq-preview-zone">
     * (колонки # / Тип / Источник) — пользователь просил, чтобы миниатюра
     * не выскакивала над кнопками «В очередь» и выпадающим списком очередей.
     * Плавающая панель возле курсора; видео — muted autoplay loop.
     */
    function setupRowPreview() {
        var panel = null;
        var showTimer = null;
        var currentRow = null;
        var currentZone = null;

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
            currentZone = null;
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
            // Только zone-ячейки (# / Тип / Источник) — не вся строка.
            var zone = ev.target && ev.target.closest ? ev.target.closest('td.tgq-preview-zone') : null;
            if (!zone) return;
            var row = zone.closest('tr[data-preview]');
            if (!row) return;
            if (row === currentRow && zone === currentZone) return;
            hidePanel();
            currentRow = row;
            currentZone = zone;
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
            var zone = ev.target && ev.target.closest ? ev.target.closest('td.tgq-preview-zone') : null;
            if (!zone || zone !== currentZone) return;
            var to = ev.relatedTarget;
            // Переход внутри той же zone-ячейки или в соседнюю zone-ячейку
            // той же строки — панель оставляем (mouseover перепозиционирует).
            if (to && to.closest && to.closest('td.tgq-preview-zone')) return;
            hidePanel();
        });

        // Открылся dropdown/модал — миниатюру прячем (не перекрывает меню).
        document.addEventListener('show.bs.dropdown', hidePanel);
        document.addEventListener('show.bs.modal', hidePanel);

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
