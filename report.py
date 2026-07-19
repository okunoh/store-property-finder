"""
HTML レポート生成モジュール — コンパクトカード + ステータス管理
"""

import html
import json
from datetime import datetime
from typing import Optional


# ── ステータス定義 ────────────────────────────────────────────
STATUS_LIST = ["未調査", "調査中", "検討中", "見送り", "削除"]
STATUS_COLORS = {
    "未調査": ("#f1f3f4", "#5f6368"),
    "調査中": ("#e8f0fe", "#1967d2"),
    "検討中": ("#e6f4ea", "#1e8e3e"),
    "見送り": ("#fef3e2", "#e37400"),
    "削除":   ("#fce8e6", "#c5221f"),
}

# ── サイトバッジ色 ────────────────────────────────────────────
SITE_BADGE = {
    "アットホーム":        ("#fff3e0", "#e65100"),
    "HOMES":               ("#e3f2fd", "#1565c0"),
    "テンポスマート":      ("#e8f5e9", "#2e7d32"),
    "飲食店ドットコム":    ("#fce4ec", "#ad1457"),
    "店舗そのままオークション": ("#fff8e1", "#f57f17"),
    "テナント.biz":        ("#e0f2f1", "#00695c"),
    "テナントショップ":    ("#ede7f6", "#4527a0"),
    "テンポダス":          ("#e8f4ea", "#2e7d32"),
    "居抜き市場":          ("#fbe9e7", "#bf360c"),
    "店舗ネットワーク":    ("#e1f5fe", "#0277bd"),
}

PRIORITY_1_AREAS = [
    ("川崎市", "宮前区"),
    ("横浜市", "栄区"),
    ("川崎市", "幸区"),
    ("横浜市", "保土ケ谷区"),
    ("横浜市", "港北区"),
]

PRIORITY_2_AREAS = [
    ("横浜市", "青葉区"),
    ("横浜市", "瀬谷区"),
    ("横浜市", "港南区"),
    ("横浜市", "南区"),
    ("横浜市", "鶴見区"),
    ("横浜市", "金沢区"),
    ("横浜市", "戸塚区"),
    ("横浜市", "旭区"),
    ("横浜市", "神奈川区"),
    ("川崎市", "麻生区"),
    ("川崎市", "多摩区"),
]


def _esc(s) -> str:
    return html.escape(str(s) if s else "")


def _normalize_area_text(text: str) -> str:
    return (text or "").replace("神奈川県", "").replace(" ", "").replace("　", "")


def _priority_label(prop) -> str:
    text = _normalize_area_text(f"{prop.address} {prop.name} {prop.nearest_station}")
    for city, ward in PRIORITY_1_AREAS:
        if city in text and ward in text:
            return "優先1"
    for city, ward in PRIORITY_2_AREAS:
        if city in text and ward in text:
            return "優先2"
    return "優先3"


def _priority_sort_value(prop) -> int:
    return {"優先1": 0, "優先2": 1, "優先3": 2}.get(_priority_label(prop), 2)


def _fmt_rent(rent: Optional[int]) -> str:
    if not rent:
        return "要確認"
    if rent >= 10000:
        man = rent / 10000
        s = f"{man:.1f}"
        if s.endswith(".0"):
            s = s[:-2]
        return s + "万円"
    return f"{rent:,}円"


def _fmt_area(area: Optional[float]) -> str:
    if not area:
        return "—"
    tsubo = area / 3.30579
    return f"{area:.1f}㎡ ({tsubo:.1f}坪)"


def _fmt_walk(w: Optional[int]) -> str:
    return f"{w}分" if w else "—"


CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Hiragino Kaku Gothic ProN','Meiryo',sans-serif;background:#f0f2f5;color:#333;font-size:14px}
a{color:#1a73e8;text-decoration:none}a:hover{text-decoration:underline}

/* ヘッダー */
header{background:#1a73e8;color:#fff;padding:14px 24px;display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
header h1{font-size:1.2rem;font-weight:700}
.header-meta{font-size:0.8rem;opacity:.9;display:flex;gap:16px;align-items:center;flex-wrap:wrap}
.new-pill{background:rgba(255,255,255,.25);border-radius:12px;padding:3px 10px;font-weight:700;cursor:pointer;transition:background .15s}
.new-pill:hover,.new-pill.active{background:rgba(255,255,255,.45)}
.new-pill .cnt{font-size:1.05em}

/* サマリ */
.summary{background:#fff;margin:12px 20px 0;border-radius:10px;padding:14px 20px;display:flex;gap:24px;flex-wrap:wrap;box-shadow:0 1px 3px rgba(0,0,0,.08);align-items:center}
.stat{text-align:center}
.stat .num{font-size:1.6rem;font-weight:700;color:#1a73e8;line-height:1}
.stat .lbl{font-size:0.7rem;color:#888;margin-top:2px}
.stat.red .num{color:#e53935}

/* 検索条件 */
.conditions{background:#fff;margin:8px 20px 0;border-radius:8px;padding:10px 16px;font-size:0.78rem;color:#555;box-shadow:0 1px 3px rgba(0,0,0,.06)}
.conditions strong{color:#333}

/* ツールバー */
.toolbar{margin:10px 20px 0;display:flex;gap:8px;flex-wrap:wrap;align-items:center}
.tab-group{display:flex;gap:4px;flex-wrap:wrap}
.tab{padding:5px 12px;border-radius:16px;border:none;cursor:pointer;font-size:0.78rem;font-weight:600;background:#e0e0e0;color:#555;transition:all .15s}
.tab.active{background:#1a73e8;color:#fff}
.sep{width:1px;background:#ddd;height:24px;margin:0 4px}
.site-sel{padding:5px 8px;border:1px solid #ddd;border-radius:16px;font-size:0.78rem;background:#fff;cursor:pointer}
.btn-export{padding:5px 12px;border-radius:16px;border:1px solid #1a73e8;background:#fff;color:#1a73e8;font-size:0.78rem;font-weight:600;cursor:pointer;margin-left:auto}
.btn-export:hover{background:#e8f0fe}
.btn-sync{padding:5px 12px;border-radius:16px;border:1px solid #188038;background:#fff;color:#188038;font-size:0.78rem;font-weight:600;cursor:pointer}
.btn-sync:hover{background:#e6f4ea}
.btn-sync.syncing{opacity:.6;pointer-events:none}

/* グリッド */
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(270px,1fr));gap:10px;padding:10px 20px}

/* カード */
.card{background:#fff;border-radius:8px;padding:10px 12px;box-shadow:0 1px 3px rgba(0,0,0,.08);display:flex;flex-direction:column;gap:5px;border-left:3px solid #e0e0e0;transition:box-shadow .15s,border-color .15s;position:relative}
.card:hover{box-shadow:0 3px 10px rgba(0,0,0,.12)}
.card[data-status="未調査"]{border-left-color:#9e9e9e}
.card[data-status="調査中"]{border-left-color:#1967d2}
.card[data-status="検討中"]{border-left-color:#1e8e3e}
.card[data-status="見送り"]{border-left-color:#e37400}

/* カード — 新着バッジ */
.card[data-new="true"]::after{content:'NEW';position:absolute;top:7px;right:8px;background:#e53935;color:#fff;font-size:0.6rem;font-weight:700;padding:1px 6px;border-radius:3px}

/* カード内要素 */
.card-top{display:flex;align-items:center;gap:6px;padding-right:34px}
.site-badge{display:inline-block;font-size:0.65rem;font-weight:700;padding:1px 6px;border-radius:3px;white-space:nowrap;flex-shrink:0}
.priority-badge{display:inline-block;font-size:0.72rem;font-weight:800;padding:2px 7px;border-radius:5px;white-space:nowrap;flex-shrink:0;border:1px solid transparent}
.priority-badge.p1{background:#d93025;color:#fff;border-color:#b3261e}
.priority-badge.p2{background:#174ea6;color:#fff;border-color:#0b3d91}
.priority-badge.p3{background:#f1f3f4;color:#3c4043;border-color:#dadce0}
.card-name{font-size:0.82rem;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;flex:1}
.card-rent{font-size:1.05rem;font-weight:700;color:#c62828}
.card-specs{display:flex;gap:10px;flex-wrap:wrap}
.spec{display:flex;gap:2px;align-items:baseline;font-size:0.78rem}
.sk{color:#9e9e9e;font-size:0.68rem}
.sv{font-weight:600;color:#333}
.card-location{font-size:0.71rem;color:#888;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.card-footer{display:flex;align-items:center;justify-content:space-between;margin-top:2px}
.status-sel{font-size:0.72rem;padding:3px 6px;border:1px solid #ddd;border-radius:5px;cursor:pointer;background:#fff;max-width:90px}
.card-link{font-size:0.71rem;color:#1a73e8}
.note-input{width:100%;min-height:38px;resize:vertical;border:1px solid #e0e0e0;border-radius:6px;padding:6px 7px;font-family:inherit;font-size:0.72rem;line-height:1.35;background:#fafafa;color:#333}
.note-input:focus{outline:none;border-color:#1a73e8;background:#fff;box-shadow:0 0 0 2px rgba(26,115,232,.12)}
.note-input::placeholder{color:#aaa}

/* 見送りセクション */
.miokuri-section{margin:4px 20px 10px;border-radius:8px;overflow:hidden;border:1px solid #ffe0b2}
.miokuri-header{background:#fff3e0;padding:10px 16px;cursor:pointer;font-size:0.82rem;font-weight:700;color:#e37400;display:flex;align-items:center;gap:8px;user-select:none}
.miokuri-header::before{content:'▶';font-size:0.7rem;transition:transform .2s}
.miokuri-section.open .miokuri-header::before{transform:rotate(90deg)}
.miokuri-body{display:none;background:#fffaf5}
.miokuri-section.open .miokuri-body{display:block}

/* ゼロ件表示 */
.no-results{text-align:center;padding:40px 20px;color:#aaa;font-size:0.95rem;grid-column:1/-1}

footer{text-align:center;padding:16px;font-size:0.73rem;color:#aaa}
"""

JS = r"""
const STATUS_COLORS = {
  '未調査': ['#f1f3f4','#5f6368'],
  '調査中': ['#e8f0fe','#1967d2'],
  '検討中': ['#e6f4ea','#1e8e3e'],
  '見送り': ['#fef3e2','#e37400'],
  '削除':   ['#fce8e6','#c5221f'],
};

let curStatusFilter = 'all';
let curSiteFilter   = 'all';
let newOnlyMode     = false;

// ── ステータス読み書き ────────────────────────────────────────
function getStoredStatuses() {
  try { return JSON.parse(localStorage.getItem('store_prop_status') || '{}'); }
  catch { return {}; }
}
function saveStoredStatuses(s) {
  localStorage.setItem('store_prop_status', JSON.stringify(s));
}
function getStoredNotes() {
  try { return JSON.parse(localStorage.getItem('store_prop_notes') || '{}'); }
  catch { return {}; }
}
function saveStoredNotes(n) {
  localStorage.setItem('store_prop_notes', JSON.stringify(n));
}

// ── 初期化（ページロード時） ──────────────────────────────────
function initStatuses() {
  const stored  = getStoredStatuses();
  const prev    = (typeof PREV_STATUSES !== 'undefined') ? PREV_STATUSES : {};
  const mainGrid    = document.getElementById('main-grid');
  const miokuriGrid = document.getElementById('miokuri-grid');

  document.querySelectorAll('.card').forEach(card => {
    const key    = card.dataset.key;
    const status = stored[key] || prev[key] || '未調査';
    const notes  = getStoredNotes();
    const prevNotes = (typeof PREV_NOTES !== 'undefined') ? PREV_NOTES : {};
    const note = (notes[key] !== undefined) ? notes[key] : (prevNotes[key] || '');

    card.dataset.status = status;
    const sel = card.querySelector('.status-sel');
    if (sel) sel.value = status;
    const noteInput = card.querySelector('.note-input');
    if (noteInput) noteInput.value = note;

    if (status === '削除') {
      card.remove();
    } else if (status === '見送り') {
      miokuriGrid.appendChild(card);
    }
    // others: stay in main-grid
  });

  updateCounts();
  applyFilters();
}

// ── メモ保存 ─────────────────────────────────────────────────
function setNote(key, value) {
  const stored = getStoredNotes();
  const note = value.trimEnd();
  if (note) {
    stored[key] = note;
  } else {
    delete stored[key];
  }
  saveStoredNotes(stored);
}

// ── ステータス変更 ────────────────────────────────────────────
function setStatus(key, status) {
  const stored = getStoredStatuses();
  stored[key] = status;
  saveStoredStatuses(stored);

  const card = document.querySelector(`[data-key="${CSS.escape(key)}"]`);
  if (!card) return;
  card.dataset.status = status;
  const sel = card.querySelector('.status-sel');
  if (sel) sel.value = status;

  if (status === '削除') {
    card.style.opacity = '0';
    card.style.transition = 'opacity .25s';
    setTimeout(() => { card.remove(); updateCounts(); applyFilters(); }, 260);
    return;
  }

  const miokuriGrid = document.getElementById('miokuri-grid');
  const mainGrid    = document.getElementById('main-grid');
  if (status === '見送り') {
    miokuriGrid.appendChild(card);
    // 見送りセクションを開く
    document.getElementById('miokuri-section').classList.add('open');
  } else {
    // 見送りから戻す
    if (card.closest('#miokuri-grid')) mainGrid.appendChild(card);
  }

  updateCounts();
  applyFilters();
}

// ── カウント更新 ──────────────────────────────────────────────
function updateCounts() {
  const cards     = document.querySelectorAll('.card');
  const miokuri   = document.querySelectorAll('#miokuri-grid .card');
  const mainCards = document.querySelectorAll('#main-grid .card');

  document.getElementById('total-count').textContent  = cards.length;
  document.getElementById('miokuri-count').textContent = miokuri.length;

  // ステータスタブのバッジ更新
  const statusCounts = {};
  cards.forEach(c => {
    const st = c.dataset.status || '未調査';
    statusCounts[st] = (statusCounts[st] || 0) + 1;
  });
  document.querySelectorAll('.tab[data-status]').forEach(tab => {
    const st  = tab.dataset.status;
    const cnt = st === 'all' ? cards.length : (statusCounts[st] || 0);
    const badge = tab.querySelector('.tab-cnt');
    if (badge) badge.textContent = cnt ? ` (${cnt})` : '';
  });

  // 新着カウント
  const newCnt = document.querySelectorAll('.card[data-new="true"]').length;
  const newPill = document.getElementById('new-pill');
  if (newPill) newPill.querySelector('.cnt').textContent = newCnt;
}

// ── フィルタ適用 ──────────────────────────────────────────────
function applyFilters() {
  document.querySelectorAll('#main-grid .card').forEach(card => {
    let show = true;
    if (curStatusFilter !== 'all' && card.dataset.status !== curStatusFilter) show = false;
    if (curSiteFilter   !== 'all' && card.dataset.site   !== curSiteFilter)   show = false;
    if (newOnlyMode && card.dataset.new !== 'true') show = false;
    card.style.display = show ? '' : 'none';
  });

  // no-results表示制御
  const mainGrid = document.getElementById('main-grid');
  const visible  = [...mainGrid.querySelectorAll('.card')].filter(c => c.style.display !== 'none');
  let noRes = mainGrid.querySelector('.no-results');
  if (visible.length === 0) {
    if (!noRes) {
      noRes = document.createElement('div');
      noRes.className = 'no-results';
      noRes.textContent = '条件に合う物件はありません';
      mainGrid.appendChild(noRes);
    }
  } else {
    if (noRes) noRes.remove();
  }
}

// ── ステータスタブ ────────────────────────────────────────────
function filterStatus(status, el) {
  curStatusFilter = status;
  document.querySelectorAll('.tab[data-status]').forEach(t => t.classList.remove('active'));
  el.classList.add('active');
  applyFilters();
}

// ── サイトフィルタ ────────────────────────────────────────────
function filterSite(sel) {
  curSiteFilter = sel.value;
  applyFilters();
}

// ── 新着のみ ─────────────────────────────────────────────────
function toggleNew() {
  newOnlyMode = !newOnlyMode;
  const pill = document.getElementById('new-pill');
  pill.classList.toggle('active', newOnlyMode);
  applyFilters();
}

// ── 見送りアコーディオン ──────────────────────────────────────
function toggleMiokuri() {
  document.getElementById('miokuri-section').classList.toggle('open');
}

// ── ステータスエクスポート ────────────────────────────────────
function exportStatus() {
  const stored = getStoredStatuses();
  const blob   = new Blob([JSON.stringify(stored, null, 2)], {type: 'application/json'});
  const a      = document.createElement('a');
  a.href       = URL.createObjectURL(blob);
  a.download   = 'status.json';
  a.click();
  URL.revokeObjectURL(a.href);
}

// ── GitHub 同期 ───────────────────────────────────────────────
const GH_REPO = 'okunoh/store-property-finder';
const GH_STATUS_FILE = 'data/status.json';
const GH_NOTES_FILE = 'data/notes.json';

async function putGithubJson(path, data, token, messagePrefix) {
  const content = JSON.stringify(data, null, 2);
  const encoded = btoa(unescape(encodeURIComponent(content)));

  const getRes = await fetch(
    `https://api.github.com/repos/${GH_REPO}/contents/${path}`,
    { headers: { 'Authorization': `token ${token}`, 'Accept': 'application/vnd.github.v3+json' } }
  );
  let sha = null;
  if (getRes.ok) {
    const meta = await getRes.json();
    sha = meta.sha;
  }

  const body = {
    message: messagePrefix + ' ' + new Date().toLocaleString('ja-JP'),
    content: encoded,
  };
  if (sha) body.sha = sha;

  const putRes = await fetch(
    `https://api.github.com/repos/${GH_REPO}/contents/${path}`,
    {
      method: 'PUT',
      headers: {
        'Authorization': `token ${token}`,
        'Accept': 'application/vnd.github.v3+json',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify(body),
    }
  );

  if (!putRes.ok) {
    const err = await putRes.json();
    const e = new Error(err.message || 'GitHub update failed');
    e.status = putRes.status;
    throw e;
  }
}

async function githubSync() {
  const btn = document.getElementById('btn-sync');

  // トークン取得（初回のみ入力）
  let token = localStorage.getItem('gh_pat');
  if (!token) {
    token = prompt(
      'GitHub Personal Access Token を入力してください\n\n' +
      '取得方法: GitHub → Settings → Developer settings\n' +
      '→ Personal access tokens → Fine-grained tokens\n' +
      '→ Repository: store-property-finder\n' +
      '→ Permissions: Contents = Read and Write'
    );
    if (!token) return;
    localStorage.setItem('gh_pat', token.trim());
    token = token.trim();
  }

  btn.classList.add('syncing');
  btn.textContent = '🔄 同期中…';

  try {
    // localStorage + PREV_STATUSES をマージ（localStorageが優先）
    const localStatuses = getStoredStatuses();
    const prevStatuses  = (typeof PREV_STATUSES !== 'undefined') ? PREV_STATUSES : {};
    const mergedStatuses = Object.assign({}, prevStatuses, localStatuses);

    const localNotes = getStoredNotes();
    const prevNotes  = (typeof PREV_NOTES !== 'undefined') ? PREV_NOTES : {};
    const mergedNotes = Object.assign({}, prevNotes, localNotes);

    await putGithubJson(GH_STATUS_FILE, mergedStatuses, token, 'status: sync from browser');
    await putGithubJson(GH_NOTES_FILE, mergedNotes, token, 'notes: sync from browser');
    alert('✅ GitHubに同期しました。\nステータスとメモが保存されました。次回の自動実行後にほかの端末でも同じ状態になります。');
  } catch (e) {
    if (e.status === 401) {
      localStorage.removeItem('gh_pat');
      alert('❌ トークンが無効です。再度入力してください。\n（エラー: ' + e.message + '）');
    } else {
      alert('❌ 同期に失敗しました: ' + e.message);
    }
  } finally {
    btn.classList.remove('syncing');
    btn.textContent = '🔄 GitHub同期';
  }
}

// トークンをリセット
function resetToken() {
  if (confirm('保存済みのGitHub Tokenを削除しますか？')) {
    localStorage.removeItem('gh_pat');
    alert('削除しました。');
  }
}

window.addEventListener('DOMContentLoaded', initStatuses);
"""


def _card_html(prop, prev_status: str = "", note: str = "") -> str:
    bg, fg = SITE_BADGE.get(prop.site, ("#f3e5f5", "#6a1b9a"))
    is_new_attr = "true" if prop.is_new else "false"
    key = _esc(prop.unique_key)
    priority_label = _priority_label(prop)
    priority_badge = ""
    if priority_label == "優先1":
        priority_badge = '<span class="priority-badge p1">優先1</span>'
    elif priority_label == "優先2":
        priority_badge = '<span class="priority-badge p2">優先2</span>'
    else:
        priority_badge = '<span class="priority-badge p3">優先3</span>'

    location_parts = []
    if prop.nearest_station:
        location_parts.append(f"🚉 {prop.nearest_station[:20]}")
    if prop.address:
        location_parts.append(f"📍 {prop.address[:30]}")
    location_str = "　".join(location_parts) or "—"

    # ステータスオプション
    opts = ""
    for st in STATUS_LIST:
        sel = " selected" if st == prev_status else ""
        opts += f'<option{sel}>{_esc(st)}</option>'

    return f"""<div class="card" data-key="{key}" data-site="{_esc(prop.site)}" data-status="{_esc(prev_status or '未調査')}" data-new="{is_new_attr}">
  <div class="card-top">
    {priority_badge}
    <span class="site-badge" style="background:{bg};color:{fg}">{_esc(prop.site)}</span>
    <span class="card-name"><a href="{_esc(prop.url)}" target="_blank" rel="noopener">{_esc(prop.name or '物件詳細')}</a></span>
  </div>
  <div class="card-rent">{_esc(_fmt_rent(prop.rent))}</div>
  <div class="card-specs">
    <span class="spec"><span class="sk">面積</span> <span class="sv">{_esc(_fmt_area(prop.area))}</span></span>
    <span class="spec"><span class="sk">徒歩</span> <span class="sv">{_esc(_fmt_walk(prop.walk_minutes))}</span></span>
    <span class="spec"><span class="sk">階</span> <span class="sv">{_esc(prop.floor or '—')}</span></span>
  </div>
  <div class="card-location">{_esc(location_str)}</div>
  <textarea class="note-input" placeholder="メモ" oninput="setNote('{key}', this.value)">{_esc(note)}</textarea>
  <div class="card-footer">
    <select class="status-sel" onchange="setStatus('{key}', this.value)">{opts}</select>
    <a class="card-link" href="{_esc(prop.url)}" target="_blank" rel="noopener">詳細 →</a>
  </div>
</div>"""


def _fmt_rent_display(rent: Optional[int]) -> str:
    """Summary display formatting."""
    if not rent:
        return "—"
    if rent >= 10000:
        man = rent / 10000
        s = f"{man:.1f}"
        if s.endswith(".0"):
            s = s[:-2]
        return s + "万円"
    return f"{rent:,}円"


def generate_report(
    properties: list,
    config: dict,
    output_path: str,
    prev_keys: set = None,
    status_map: dict = None,
    notes_map: dict = None,
) -> None:
    prev_keys  = prev_keys or set()
    status_map = status_map or {}
    notes_map = notes_map or {}
    cfg_search = config.get("search", {})

    # 新着フラグ付与
    for p in properties:
        p.is_new = p.unique_key not in prev_keys

    new_count = sum(1 for p in properties if p.is_new)
    sites     = sorted(set(p.site for p in properties))
    now       = datetime.now().strftime("%Y年%m月%d日 %H:%M")

    # サマリ計算
    rent_vals = [p.rent for p in properties if p.rent]
    avg_rent  = int(sum(rent_vals) / len(rent_vals)) if rent_vals else 0
    area_vals = [p.area for p in properties if p.area]
    avg_area  = sum(area_vals) / len(area_vals) if area_vals else 0.0
    priority_counts = {
        "優先1": sum(1 for p in properties if _priority_label(p) == "優先1"),
        "優先2": sum(1 for p in properties if _priority_label(p) == "優先2"),
        "優先3": sum(1 for p in properties if _priority_label(p) == "優先3"),
    }

    # 検索条件テキスト
    conds = []
    if cfg_search.get("areas"):
        conds.append("エリア: " + "・".join(cfg_search["areas"]))
    if cfg_search.get("rent_max"):
        conds.append(f"賃料: {_fmt_rent_display(cfg_search['rent_max'])}以内")
    if cfg_search.get("area_min"):
        a_min = cfg_search["area_min"]
        tsubo = a_min / 3.30579
        conds.append(f"面積: {a_min}㎡({tsubo:.0f}坪)以上")
    if cfg_search.get("walk_minutes_max"):
        conds.append(f"徒歩: {cfg_search['walk_minutes_max']}分以内")
    if cfg_search.get("floor_1f_only"):
        conds.append("階数: 1F・2F対象")

    # PREV_STATUSES / PREV_NOTES 埋め込み用JSON
    prev_statuses_js = json.dumps(status_map, ensure_ascii=False)
    prev_notes_js = json.dumps(notes_map, ensure_ascii=False)

    # サイトフィルタ options
    site_opts = '<option value="all">すべてのサイト</option>'
    for s in sites:
        site_opts += f'<option value="{_esc(s)}">{_esc(s)}</option>'

    # ステータスタブ
    status_tabs = '<button class="tab active" data-status="all" onclick="filterStatus(\'all\',this)">すべて<span class="tab-cnt"></span></button>'
    for st in ["未調査", "調査中", "検討中"]:
        status_tabs += f'<button class="tab" data-status="{st}" onclick="filterStatus(\'{st}\',this)">{st}<span class="tab-cnt"></span></button>'

    # カード HTML 生成（優先度 → 新着 → サイト順）
    sorted_props = sorted(properties, key=lambda p: (_priority_sort_value(p), 0 if p.is_new else 1, p.site))
    main_cards = ""
    miokuri_cards = ""
    for p in sorted_props:
        st = status_map.get(p.unique_key, "未調査")
        note = notes_map.get(p.unique_key, "")
        c  = _card_html(p, prev_status=st, note=note)
        if st == "見送り":
            miokuri_cards += "\n" + c
        else:
            main_cards += "\n" + c

    if not main_cards:
        main_cards = '<div class="no-results">条件に合う物件が見つかりませんでした</div>'

    html_content = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>店舗物件候補 — {now}</title>
<style>{CSS}</style>
</head>
<body>

<header>
  <h1>🏪 店舗物件候補リスト</h1>
  <div class="header-meta">
    <span>{now}</span>
    <span id="new-pill" class="new-pill" onclick="toggleNew()" title="新着のみ表示">
      🆕 本日の新着 <span class="cnt">{new_count}</span>件
    </span>
  </div>
</header>

<div class="summary">
  <div class="stat"><div class="num" id="total-count">{len(properties)}</div><div class="lbl">総件数</div></div>
  <div class="stat red"><div class="num">{new_count}</div><div class="lbl">新着</div></div>
  <div class="stat"><div class="num">{len(sites)}</div><div class="lbl">サイト数</div></div>
  <div class="stat"><div class="num">{priority_counts["優先1"]}</div><div class="lbl">優先1</div></div>
  <div class="stat"><div class="num">{priority_counts["優先2"]}</div><div class="lbl">優先2</div></div>
  <div class="stat"><div class="num">{priority_counts["優先3"]}</div><div class="lbl">優先3</div></div>
  <div class="stat"><div class="num">{_fmt_rent_display(avg_rent)}</div><div class="lbl">平均賃料</div></div>
  <div class="stat"><div class="num">{avg_area:.0f}㎡</div><div class="lbl">平均面積</div></div>
</div>

<div class="conditions">
  <strong>検索条件:</strong> {" ／ ".join(conds) if conds else "指定なし"}
</div>

<div class="toolbar">
  <div class="tab-group">{status_tabs}</div>
  <div class="sep"></div>
  <select class="site-sel" onchange="filterSite(this)">{site_opts}</select>
  <button class="btn-export" onclick="exportStatus()">💾 ダウンロード</button>
  <button id="btn-sync" class="btn-sync" onclick="githubSync()">🔄 GitHub同期</button>
</div>

<div id="main-grid" class="grid">{main_cards}
</div>

<div id="miokuri-section" class="miokuri-section">
  <div class="miokuri-header" onclick="toggleMiokuri()">
    見送り物件 (<span id="miokuri-count">{miokuri_cards.count('class="card"')}</span>件)
  </div>
  <div class="miokuri-body">
    <div id="miokuri-grid" class="grid">{miokuri_cards}
    </div>
  </div>
</div>

<footer>店舗物件自動収集ツール — {now} 生成 ／ ステータスはブラウザに保存されます（「💾 ステータス保存」でdata/status.jsonにエクスポート可）</footer>

<script>
const PREV_STATUSES = {prev_statuses_js};
const PREV_NOTES = {prev_notes_js};
{JS}
</script>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html_content)
