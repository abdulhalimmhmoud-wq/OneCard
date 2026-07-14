/* OneCard Platform — Shared JS Utilities */

function fmt(v) {
    if (v == null || isNaN(v)) return '--';
    return Number(v).toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function esc(s) {
    if (s == null) return '';
    return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

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
