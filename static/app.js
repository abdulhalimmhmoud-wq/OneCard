/* OneCard Platform — Shared JS Utilities */

// Business rule: all money values are shown as WHOLE numbers (no decimals)
function fmt(v) {
    if (v == null || isNaN(v)) return '--';
    return Math.round(Number(v)).toLocaleString();
}

function esc(s) {
    if (s == null) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

// ── CSRF: inject the session token into every POST form ─────
// (submit-event delegation also covers forms rendered dynamically by JS)
document.addEventListener('submit', e => {
    const form = e.target;
    if (!form || (form.method || '').toLowerCase() !== 'post') return;
    if (form.querySelector('input[name="_csrf"]')) return;
    const meta = document.querySelector('meta[name="csrf-token"]');
    if (!meta) return;
    const inp = document.createElement('input');
    inp.type = 'hidden';
    inp.name = '_csrf';
    inp.value = meta.content;
    form.appendChild(inp);
}, true);

// ── Flash auto-dismiss ──────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
    document.querySelectorAll('.flash').forEach(f => {
        setTimeout(() => { f.style.opacity = '0'; f.style.transform = 'translateY(-8px)'; setTimeout(() => f.remove(), 300); }, 5000);
    });
});

// ── Mobile sidebar toggle ───────────────────────────────────
function toggleSidebar() {
    document.querySelector('.sidebar')?.classList.toggle('open');
}

// ── CSV Export ──────────────────────────────────────────────
function downloadCSV(headers, rows, filename) {
    const csv = [headers.join(','), ...rows.map(r => r.map(v => `"${String(v || '').replace(/"/g, '""')}"`).join(','))].join('\n');
    const blob = new Blob([csv], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = filename; a.click();
    URL.revokeObjectURL(url);
}

// ── Multi-select dropdown (v8, portal-based since v11) ──────────
// Usage: const f = msCreate('merchantFilter', options, 'All Merchants', onChange);
//        f.getSelected() -> array (empty = no filter)
//
// The open panel is appended to <body> and positioned with position:fixed,
// anchored to the button via getBoundingClientRect(). This deliberately
// escapes the filter card's stacking context (cards use backdrop-filter,
// which traps z-index) and any parent overflow — the old absolutely-
// positioned panel was being clipped/hidden behind the results table.
function msCreate(elId, options, placeholder, onChange) {
    const host = document.getElementById(elId);
    if (!host) return { getSelected: () => [] };
    const selected = new Set();
    host.classList.add('ms-wrap');
    host.innerHTML = `<button type="button" class="ms-btn"><span class="ms-label">${esc(placeholder)}</span><span class="ms-caret">▾</span></button>`;
    const btn = host.querySelector('.ms-btn');
    const label = host.querySelector('.ms-label');

    // Panel lives on <body>, shared markup, created once
    const panel = document.createElement('div');
    panel.className = 'ms-panel';
    panel.innerHTML = `
        <input type="search" class="ms-search" placeholder="Search...">
        <div class="ms-actions"><a class="ms-clear">Clear</a></div>
        <div class="ms-list"></div>`;
    document.body.appendChild(panel);
    const list = panel.querySelector('.ms-list');
    const search = panel.querySelector('.ms-search');

    function renderList(q) {
        const ql = (q || '').toLowerCase();
        list.innerHTML = options
            .filter(o => !ql || String(o).toLowerCase().includes(ql))
            .map(o => `<label class="ms-item"><input type="checkbox" value="${esc(o)}" ${selected.has(o) ? 'checked' : ''}><span class="truncate">${esc(o)}</span></label>`)
            .join('') || '<div class="ms-empty">No matches</div>';
    }
    function refreshLabel() {
        if (!selected.size) { label.textContent = placeholder; btn.classList.remove('ms-active'); }
        else if (selected.size === 1) { label.textContent = [...selected][0]; btn.classList.add('ms-active'); }
        else { label.textContent = `${placeholder} (${selected.size})`; btn.classList.add('ms-active'); }
    }
    function position() {
        const r = btn.getBoundingClientRect();
        const vh = window.innerHeight;
        panel.style.left = r.left + 'px';
        panel.style.minWidth = r.width + 'px';
        // Flip above the button if there isn't room below
        const below = vh - r.bottom;
        if (below < 300 && r.top > below) {
            panel.style.top = 'auto';
            panel.style.bottom = (vh - r.top + 6) + 'px';
            panel.style.maxHeight = Math.min(340, r.top - 16) + 'px';
        } else {
            panel.style.bottom = 'auto';
            panel.style.top = (r.bottom + 6) + 'px';
            panel.style.maxHeight = Math.min(340, below - 16) + 'px';
        }
    }
    function open() {
        document.querySelectorAll('.ms-panel.open').forEach(p => p.classList.remove('open'));
        position();
        panel.classList.add('open');
        search.value = ''; renderList(); search.focus();
    }
    function close() { panel.classList.remove('open'); }

    btn.addEventListener('click', e => {
        e.stopPropagation();
        panel.classList.contains('open') ? close() : open();
    });
    panel.addEventListener('click', e => e.stopPropagation());
    search.addEventListener('input', () => renderList(search.value));
    list.addEventListener('change', e => {
        const v = e.target.value;
        if (e.target.checked) selected.add(v); else selected.delete(v);
        refreshLabel();
        if (onChange) onChange();
    });
    panel.querySelector('.ms-clear').addEventListener('click', () => {
        selected.clear(); renderList(search.value); refreshLabel();
        if (onChange) onChange();
    });
    document.addEventListener('click', close);
    window.addEventListener('resize', () => { if (panel.classList.contains('open')) position(); });
    window.addEventListener('scroll', () => { if (panel.classList.contains('open')) position(); }, true);
    renderList();
    return { getSelected: () => [...selected] };
}

// empty selection = "all" (no filtering)
function msMatch(selectedArr, value) {
    return !selectedArr.length || selectedArr.includes(value);
}

// ── Debounce ────────────────────────────────────────────────
function debounce(fn, delay = 300) {
    let timer;
    return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), delay); };
}
