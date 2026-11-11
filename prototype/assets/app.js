/* 统一日志分析平台 —— 共享壳层与交互
   原生 JS，无外部依赖（AI-003 / AI-006） */

(function () {
  'use strict';

  var ICONS = {
    overview: '<rect x="2" y="2" width="5" height="5" rx="1"/><rect x="9" y="2" width="5" height="5" rx="1"/><rect x="2" y="9" width="5" height="5" rx="1"/><rect x="9" y="9" width="5" height="5" rx="1"/>',
    search: '<circle cx="7" cy="7" r="4.4"/><path d="M10.4 10.4L14 14"/>',
    dashboard: '<path d="M2 13.5V9M6 13.5V5M10 13.5V7.5M14 13.5V2.5"/>',
    alarm: '<path d="M8 2a4 4 0 0 0-4 4v3l-1.4 2.4h10.8L12 9V6a4 4 0 0 0-4-4z"/><path d="M6.4 13a1.8 1.8 0 0 0 3.2 0"/>',
    rule: '<path d="M2 4h12M2 8h12M2 12h12"/><circle cx="5" cy="4" r="1.4"/><circle cx="10" cy="8" r="1.4"/><circle cx="7" cy="12" r="1.4"/>',
    source: '<ellipse cx="8" cy="3.6" rx="5.2" ry="1.9"/><path d="M2.8 3.6v8.8c0 1 2.3 1.9 5.2 1.9s5.2-.9 5.2-1.9V3.6"/><path d="M2.8 8c0 1 2.3 1.9 5.2 1.9s5.2-.9 5.2-1.9"/>',
    agent: '<rect x="2" y="2.6" width="12" height="4.4" rx="1"/><rect x="2" y="9" width="12" height="4.4" rx="1"/><path d="M4.4 4.8h.1M4.4 11.2h.1"/>',
    setting: '<circle cx="8" cy="8" r="2.4"/><path d="M8 1.6v1.6M8 12.8v1.6M1.6 8h1.6M12.8 8h1.6M3.5 3.5l1.1 1.1M11.4 11.4l1.1 1.1M12.5 3.5l-1.1 1.1M4.6 11.4l-1.1 1.1"/>',
    user: '<circle cx="8" cy="5.4" r="2.6"/><path d="M2.8 14c0-2.6 2.3-4.2 5.2-4.2s5.2 1.6 5.2 4.2"/>'
  };

  var MENU = [
    { group: '概览', items: [{ key: 'overview', label: '运行总览', href: 'overview.html', icon: 'overview' }] },
    { group: '日志分析', items: [
      { key: 'search', label: '日志检索', href: 'search.html', icon: 'search' },
      { key: 'dashboards', label: '仪表盘', href: 'dashboards.html', icon: 'dashboard' }
    ] },
    { group: '告警中心', items: [
      { key: 'alarms', label: '告警事件', href: 'alarms.html', icon: 'alarm' },
      { key: 'alarm-rules', label: '告警规则', href: 'alarm-rules.html', icon: 'rule' }
    ] },
    { group: '数据接入', items: [
      { key: 'data-sources', label: '数据源', href: 'data-sources.html', icon: 'source' },
      { key: 'agents', label: 'Agent 管理', href: 'agents.html', icon: 'agent' }
    ] },
    { group: '平台管理', items: [
      { key: 'settings', label: '用户与角色', href: 'settings.html', icon: 'setting' }
    ] }
  ];

  var TOP_NAV = [
    { key: 'overview', label: '概览', href: 'overview.html' },
    { key: 'search', label: '日志分析', href: 'search.html' },
    { key: 'alarms', label: '告警中心', href: 'alarms.html' },
    { key: 'data-sources', label: '数据接入', href: 'data-sources.html' },
    { key: 'settings', label: '平台管理', href: 'settings.html' }
  ];

  function icon(name, size) {
    return '<svg width="' + (size || 16) + '" height="' + (size || 16) + '" viewBox="0 0 16 16" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (ICONS[name] || '') + '</svg>';
  }

  function groupOf(key) {
    if (key === 'overview') return 'overview';
    if (key === 'search' || key === 'dashboards' || key === 'dashboard-detail') return 'search';
    if (key === 'alarms' || key === 'alarm-rules') return 'alarms';
    if (key === 'data-sources' || key === 'agents') return 'data-sources';
    return 'settings';
  }

  function renderShell() {
    var page = document.body.getAttribute('data-page') || '';
    var host = document.querySelector('[data-shell]');
    if (!host) return;

    var topNav = TOP_NAV.map(function (t) {
      var active = groupOf(page) === t.key ? ' is-active' : '';
      return '<a class="busi-topbar__nav-item' + active + '" href="' + t.href + '">' + t.label + '</a>';
    }).join('');

    host.outerHTML =
      '<header class="busi-topbar">' +
        '<div class="busi-topbar__left">' +
          '<a class="busi-brand" href="overview.html">' +
            '<span class="busi-brand__mark">' +
              '<img src="assets/logo.jpg" alt="国家电网标识">' +
            '</span>' +
            '<span class="busi-brand__name">统一日志分析平台</span>' +
          '</a>' +
          '<nav class="busi-topbar__nav" aria-label="产品导航">' + topNav + '</nav>' +
        '</div>' +
        '<div class="busi-topbar__right">' +
          '<span class="busi-brand__env">生产环境</span>' +
          '<button class="busi-topbar__icon" type="button" aria-label="全局搜索" data-toast="info" data-toast-text="全局搜索：支持菜单、仪表盘与检索语句">' + icon('search', 18) + '</button>' +
          '<button class="busi-topbar__icon" type="button" aria-label="通知，4 条未读" data-toast="warning" data-toast-text="4 条未读通知：2 条严重告警、2 条数据源异常">' + icon('alarm', 18) + '<span class="busi-topbar__badge">4</span></button>' +
          '<button class="busi-topbar__icon" type="button" aria-label="帮助中心" data-toast="info" data-toast-text="帮助中心：检索语法、接入指南与常见问题">' + icon('setting', 18) + '</button>' +
          '<div class="busi-topbar__user" role="button" tabindex="0" aria-label="账号菜单" data-toast="info" data-toast-text="账号菜单：个人信息 / 切换租户 / 退出登录">' +
            '<span class="busi-avatar">运</span>' +
            '<span class="busi-topbar__user-meta">' +
              '<span class="busi-topbar__user-name">运维管理员</span>' +
              '<span class="busi-topbar__user-role">总部生产 · 超级管理员</span>' +
            '</span>' +
          '</div>' +
        '</div>' +
      '</header>';

    var body = document.querySelector('.busi-body');
    if (!body) return;

    var navHtml = MENU.map(function (g) {
      var items = g.items.map(function (it) {
        var active = it.key === page ? ' is-active' : '';
        return '<a class="busi-sidebar__item' + active + '" href="' + it.href + '"' + (active ? ' aria-current="page"' : '') + '>' +
          icon(it.icon, 16) + '<span>' + it.label + '</span></a>';
      }).join('');
      return '<div class="busi-sidebar__group">' +
        '<div class="busi-sidebar__group-title">' + g.group + '</div>' + items + '</div>';
    }).join('');

    body.insertAdjacentHTML('afterbegin',
      '<aside class="busi-sidebar" aria-label="主导航">' + navHtml +
        '<div class="busi-sidebar__foot">当前租户：总部生产<br>Token V3.1.0 · 原型版本 P0.1</div>' +
      '</aside>');
  }

  /* ---------------- 浮层 ---------------- */
  var openLayers = [];

  function openLayer(el) {
    if (!el) return;
    var mask = document.querySelector('[data-mask="' + el.id + '"]');
    el.classList.add('is-open');
    if (mask) mask.classList.add('is-open');
    if (openLayers.indexOf(el) < 0) openLayers.push(el);
    document.body.style.overflow = 'hidden';
    var target = el.querySelector('input:not([type="hidden"]), select, textarea');
    if (target) { window.setTimeout(function () { target.focus(); }, 80); }
  }

  function closeLayer(el) {
    if (!el) return;
    var mask = document.querySelector('[data-mask="' + el.id + '"]');
    el.classList.remove('is-open');
    if (mask) mask.classList.remove('is-open');
    openLayers = openLayers.filter(function (x) { return x !== el; });
    if (!openLayers.length) document.body.style.overflow = '';
  }

  function closeTopLayer() {
    if (openLayers.length) closeLayer(openLayers[openLayers.length - 1]);
  }

  /* ---------------- 轻提示 ---------------- */
  function toast(text, type) {
    var wrap = document.querySelector('.busi-toast-wrap');
    if (!wrap) {
      wrap = document.createElement('div');
      wrap.className = 'busi-toast-wrap';
      document.body.appendChild(wrap);
    }
    var el = document.createElement('div');
    el.className = 'busi-toast busi-toast--' + (type || 'success');
    el.setAttribute('role', 'status');
    var mark = type === 'error' ? '!' : (type === 'success' ? '✓' : 'i');
    el.innerHTML = '<strong aria-hidden="true">' + mark + '</strong><span></span>';
    el.querySelector('span').textContent = text;
    wrap.appendChild(el);
    var ttl = type === 'error' ? 6000 : 2400;
    window.setTimeout(function () { if (el.parentNode) el.parentNode.removeChild(el); }, ttl);
  }

  /* ---------------- 表格勾选 ---------------- */
  function updateBatch(row) {
    var wrap = row.closest('.busi-table-wrap');
    if (!wrap) return;
    var rows = wrap.querySelectorAll('.busi-table__row');
    var checked = wrap.querySelectorAll('.busi-table__row .busi-check:checked');
    rows.forEach(function (r) { r.classList.toggle('is-selected', !!r.querySelector('.busi-check:checked')); });
    var bar = wrap.querySelector('.busi-table__batch');
    if (bar) {
      bar.classList.toggle('is-show', checked.length > 0);
      var count = bar.querySelector('[data-batch-count]');
      if (count) count.textContent = String(checked.length);
    }
    var all = wrap.querySelector('[data-check-all]');
    if (all) {
      all.checked = checked.length > 0 && checked.length === rows.length;
      all.indeterminate = checked.length > 0 && checked.length < rows.length;
    }
  }

  /* ---------------- 详情抽屉 ---------------- */
  function openDetail(drawerId, title, sourceRow) {
    var drawer = document.getElementById(drawerId);
    if (!drawer) return;
    var titleEl = drawer.querySelector('[data-drawer-title]');
    var body = drawer.querySelector('[data-drawer-body]');
    if (titleEl && title) titleEl.textContent = title;

    if (sourceRow && body) {
      var html = '<div class="busi-desc">';
      sourceRow.querySelectorAll('td[data-label]').forEach(function (td) {
        var v = (td.innerText || '').trim() || '--';
        html += '<div class="busi-desc__row"><div class="busi-desc__label">' + td.getAttribute('data-label') + '</div>' +
                '<div class="busi-desc__value">' + v.replace(/</g, '&lt;') + '</div></div>';
      });
      html += '</div>';
      var raw = sourceRow.getAttribute('data-raw');
      if (raw) {
        html += '<div><div class="busi-strong" style="margin-bottom:8px">原始日志</div>' +
                '<pre class="busi-code-block">' + raw.replace(/</g, '&lt;') + '</pre></div>';
      }
      body.innerHTML = html;
    }
    openLayer(drawer);
  }

  /* ---------------- 事件绑定 ---------------- */
  function bind() {
    document.addEventListener('click', function (e) {
      var t = e.target;

      var modalBtn = t.closest('[data-modal-open]');
      if (modalBtn) { openLayer(document.getElementById(modalBtn.getAttribute('data-modal-open'))); return; }

      var drawerBtn = t.closest('[data-drawer-open]');
      if (drawerBtn) { openDetail(drawerBtn.getAttribute('data-drawer-open'), drawerBtn.getAttribute('data-drawer-title')); return; }

      var toastBtn = t.closest('[data-toast]');
      if (toastBtn) {
        toast(toastBtn.getAttribute('data-toast-text') || '操作已完成', toastBtn.getAttribute('data-toast'));
        return;
      }

      var cBtn = t.closest('[data-modal-close], [data-drawer-close]');
      if (cBtn) { closeLayer(cBtn.closest('.busi-modal, .busi-drawer')); return; }

      var mask = t.closest('.busi-mask');
      if (mask) {
        var layer = document.getElementById(mask.getAttribute('data-mask'));
        closeLayer(layer);
        return;
      }

      var sw = t.closest('.busi-switch');
      if (sw) {
        var on = sw.classList.toggle('is-checked');
        sw.setAttribute('aria-checked', on ? 'true' : 'false');
        toast(on ? '已启用，配置即时生效' : '已停用，相关任务不再执行', on ? 'success' : 'warning');
        return;
      }

      var tab = t.closest('.busi-tabs__item');
      if (tab) {
        var tabs = tab.closest('.busi-tabs');
        tabs.querySelectorAll('.busi-tabs__item').forEach(function (x) { x.classList.remove('is-active'); x.setAttribute('aria-selected', 'false'); });
        tab.classList.add('is-active');
        tab.setAttribute('aria-selected', 'true');
        var scope = tab.closest('[data-tab-scope]');
        if (scope) {
          var name = tab.getAttribute('data-tab');
          scope.querySelectorAll('[data-panel]').forEach(function (p) {
            p.classList.toggle('busi-is-hidden', p.getAttribute('data-panel') !== name);
          });
        }
        return;
      }

      var pageBtn = t.closest('.busi-pagination__item');
      if (pageBtn && !pageBtn.hasAttribute('disabled')) {
        pageBtn.parentNode.querySelectorAll('.busi-pagination__item').forEach(function (x) { x.classList.remove('is-active'); });
        if (!pageBtn.classList.contains('busi-pagination__item--arrow')) pageBtn.classList.add('is-active');
        return;
      }

      var reset = t.closest('[data-filter-reset]');
      if (reset) {
        var filter = reset.closest('.busi-filter, .busi-search__query');
        if (filter) {
          filter.querySelectorAll('input, select, textarea').forEach(function (f) {
            if (f.tagName === 'SELECT') f.selectedIndex = 0; else f.value = '';
          });
        }
        toast('已重置筛选条件并重新查询', 'info');
        return;
      }

      var loadBtn = t.closest('[data-loading-btn]');
      if (loadBtn && !loadBtn.classList.contains('is-loading')) {
        e.preventDefault();
        loadBtn.classList.add('is-loading');
        loadBtn.setAttribute('aria-busy', 'true');
        var done = loadBtn.getAttribute('data-done') || '操作已完成';
        window.setTimeout(function () {
          loadBtn.classList.remove('is-loading');
          loadBtn.removeAttribute('aria-busy');
          toast(done, 'success');
        }, 900);
        return;
      }

      var row = t.closest('.busi-table__row');
      if (row && row.hasAttribute('data-detail')) {
        if (t.closest('button, a, input, select')) return;
        openDetail(row.getAttribute('data-detail'), row.getAttribute('data-detail-title') || '详情', row);
      }
    });

    document.addEventListener('change', function (e) {
      var t = e.target;
      if (t.matches('[data-check-all]')) {
        var wrap = t.closest('.busi-table-wrap');
        if (wrap) {
          wrap.querySelectorAll('.busi-table__row .busi-check').forEach(function (c) { c.checked = t.checked; });
          updateBatch(wrap.querySelector('.busi-table__row') || wrap);
        }
        return;
      }
      if (t.matches('.busi-table__row .busi-check')) { updateBatch(t); return; }
      if (t.matches('[data-range]')) {
        toast('时间范围已切换为：' + t.options[t.selectedIndex].text, 'info');
      }
    });

    document.addEventListener('submit', function (e) {
      var form = e.target;
      if (form.matches('[data-login]')) {
        e.preventDefault();
        var btn = form.querySelector('button[type="submit"]');
        if (btn) { btn.classList.add('is-loading'); btn.setAttribute('aria-busy', 'true'); }
        window.setTimeout(function () { window.location.href = 'overview.html'; }, 700);
        return;
      }
      if (form.matches('[data-form]')) {
        e.preventDefault();
        var layer = form.closest('.busi-modal, .busi-drawer');
        toast(form.getAttribute('data-done') || '已保存', 'success');
        closeLayer(layer);
      }
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') { closeTopLayer(); return; }
      if (e.key !== 'Tab') return;
      var top = openLayers[openLayers.length - 1];
      if (!top) return;
      var items = top.querySelectorAll('a[href], button:not([disabled]), input:not([type="hidden"]), select, textarea, [tabindex]:not([tabindex="-1"])');
      if (!items.length) return;
      var first = items[0];
      var last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
    });
  }

  renderShell();
  bind();
})();
