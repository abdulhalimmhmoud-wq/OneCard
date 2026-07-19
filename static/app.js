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

// ── Multi-select dropdown (v8) ──────────────────────────────
// Usage: const f = msCreate('merchantFilter', options, 'All Merchants', onChange);
//        f.getSelected() -> array (empty = no filter)
function msCreate(elId, options, placeholder, onChange) {
    const host = document.getElementById(elId);
    if (!host) return { getSelected: () => [] };
    const selected = new Set();
    host.classList.add('ms-wrap');
    host.innerHTML = `
        <button type="button" class="ms-btn"><span class="ms-label">${esc(placeholder)}</span><span class="ms-caret">▾</span></button>
        <div class="ms-panel">
            <input type="search" class="ms-search" placeholder="Search...">
            <div class="ms-actions"><a class="ms-clear">Clear</a></div>
            <div class="ms-list"></div>
        </div>`;
    const btn = host.querySelector('.ms-btn');
    const panel = host.querySelector('.ms-panel');
    const list = host.querySelector('.ms-list');
    const search = host.querySelector('.ms-search');
    const label = host.querySelector('.ms-label');

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
    btn.addEventListener('click', e => {
        e.stopPropagation();
        document.querySelectorAll('.ms-panel.open').forEach(p => { if (p !== panel) p.classList.remove('open'); });
        panel.classList.toggle('open');
        if (panel.classList.contains('open')) { search.value = ''; renderList(); search.focus(); }
    });
    panel.addEventListener('click', e => e.stopPropagation());
    search.addEventListener('input', () => renderList(search.value));
    list.addEventListener('change', e => {
        const v = e.target.value;
        if (e.target.checked) selected.add(v); else selected.delete(v);
        refreshLabel();
        if (onChange) onChange();
    });
    host.querySelector('.ms-clear').addEventListener('click', () => {
        selected.clear(); renderList(search.value); refreshLabel();
        if (onChange) onChange();
    });
    document.addEventListener('click', () => panel.classList.remove('open'));
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
