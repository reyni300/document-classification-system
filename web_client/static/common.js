// ДокАнализ — общее для страницы разбора (parse.html) и просмотра (view.html).
//
// Обе страницы ничего не распознают сами: они говорят с сервисом разбора
// через API /api/v1 (docs/parser_api.md в репозитории dokanaliz-parser).
// Страница разбора отправляет файл и получает ID разбора, страница просмотра
// по этому ID забирает и показывает результат.

const POLL_INTERVAL_MS = 2000;
const PARSE_ID_RE = /[0-9a-f]{32}/i;

function apiErrorText(body, httpStatus) {
  if (body && body.error) return typeof body.error === 'string' ? body.error : (body.error.message || body.error.code);
  return `HTTP ${httpStatus}`;
}

async function apiJson(url, options) {
  const r = await fetch(url, options);
  const body = await r.json().catch(() => ({}));
  if (!r.ok) { const e = new Error(apiErrorText(body, r.status)); e.httpStatus = r.status; throw e; }
  return body;
}

function parseStatusText(v) {
  if (v.status === 'queued') return v.queue_position ? `в очереди (№${v.queue_position})` : 'в очереди';
  if (v.status === 'processing') {
    const sec = v.started_at ? Math.max(0, Math.round((Date.now() - Date.parse(v.started_at)) / 1000)) : 0;
    return `распознаётся ${sec} с`;
  }
  if (v.status === 'done') return 'готово';
  if (v.status === 'failed') return 'ошибка';
  return v.status;
}

// Опрашиваем статус, пока разбор не закончится. Отдаёт ответ сервиса со
// статусом done (в нём уже лежит result) или бросает ошибку с журналом.
async function waitForParse(id, onProgress) {
  for (;;) {
    const v = await apiJson('/api/v1/parses/' + encodeURIComponent(id));
    if (v.status === 'done') return v;
    if (v.status === 'failed') {
      const e = new Error(v.error || 'Разбор не удался');
      e.logs = v.logs || [];
      throw e;
    }
    if (onProgress) onProgress(v);
    await new Promise(res => setTimeout(res, POLL_INTERVAL_MS));
  }
}

// Ссылка на просмотр одного разбора
function viewLink(parseId) {
  return `${location.origin}/view?id=${encodeURIComponent(parseId)}`;
}

// ID разбора из того, что вставил человек: сам ID или ссылка с ним
function extractParseId(text) {
  const m = String(text || '').match(PARSE_ID_RE);
  return m ? m[0].toLowerCase() : null;
}

// ID блока (p1-b42, p1-r3-l2-w5) из ссылки на блок: …/view?id=…&block=…
const BLOCK_ID_RE = /^p\d+-(?:b\d+|r\d+(?:-l\d+(?:-w\d+)?)?)$/i;
function extractBlockId(text) {
  const raw = String(text || '').trim();
  if (BLOCK_ID_RE.test(raw)) return raw.toLowerCase();
  try {
    const b = new URL(raw, location.origin).searchParams.get('block');
    return b && BLOCK_ID_RE.test(b) ? b.toLowerCase() : null;
  } catch (e) { return null; }
}

// Ссылка на просмотр разбора, а с block — сразу на этот блок
function viewLinkWithBlock(parseId, block) {
  return viewLink(parseId) + (block ? '&block=' + encodeURIComponent(block) : '');
}

function saveJsonFile(data, filename) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
}

// Результат разбора — ровно в том виде, в каком его отдаёт сервис
async function downloadResultJson(id, filename) {
  try {
    saveJsonFile(await apiJson('/api/v1/parses/' + encodeURIComponent(id) + '/result'),
                 `result_${filename || id}.json`);
    showToast('JSON сохранён', 'success');
  } catch (e) {
    showToast('JSON: ' + e.message, 'error');
  }
}

// Сырой JSON: что распознал OCR, до обработки (без полей, таблицы и связей).
// Сервис сохраняет его сразу после распознавания, поэтому скачать его можно,
// пока модель ещё разбирает документ.
async function downloadRawJson(id, filename) {
  try {
    saveJsonFile(await apiJson('/api/v1/parses/' + encodeURIComponent(id) + '/raw'),
                 `raw_${filename || id}.json`);
    showToast('Сырой JSON сохранён', 'success');
  } catch (e) {
    showToast('Сырой JSON: ' + e.message, 'error');
  }
}

function copyText(text, okMessage) {
  navigator.clipboard.writeText(text)
    .then(() => showToast(okMessage, 'success'))
    .catch(() => showToast(text, 'info'));
}

function setStatus(type, text) {
  const el = document.getElementById('globalStatus');
  if (!el) return;
  el.className = 'status-chip ' + type;
  el.innerHTML = `<div class="status-dot"></div><span>${escHtml(text)}</span>`;
}

function showToast(msg, type = 'info') {
  const c = document.getElementById('toastContainer');
  if (!c) return;
  const t = document.createElement('div');
  t.className = 'toast ' + type;
  const icons = { success: '✓', error: '✗', info: 'ℹ' };
  t.innerHTML = `<span>${icons[type] || ''}</span><span>${escHtml(msg)}</span>`;
  c.appendChild(t); setTimeout(() => t.remove(), 3500);
}

function escHtml(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function openModal(title, text) {
  document.getElementById('modalTitle').textContent = title;
  document.getElementById('modalBody').textContent = text;
  document.getElementById('modalOverlay').classList.add('open');
}
function closeModal() { document.getElementById('modalOverlay').classList.remove('open'); }
function closeModalOutside(e) { if (e.target === document.getElementById('modalOverlay')) closeModal(); }
