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

// ── Debounce ────────────────────────────────────────────────
function debounce(fn, delay = 300) {
    let timer;
    return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), delay); };
}
