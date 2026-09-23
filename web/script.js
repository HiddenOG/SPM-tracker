// ── THEME ─────────────────────────────────────────────────────────────────────
var _theme = localStorage.getItem('spm_theme') || 'auto';
(function() {
  var r = document.documentElement;
  if (_theme === 'dark')       r.setAttribute('data-theme', 'dark');
  else if (_theme === 'light') r.setAttribute('data-theme', 'light');
  else                         r.removeAttribute('data-theme');
})();

function setTheme(t) {
  _theme = t;
  localStorage.setItem('spm_theme', t);
  var r = document.documentElement;
  if (t === 'dark')       r.setAttribute('data-theme', 'dark');
  else if (t === 'light') r.setAttribute('data-theme', 'light');
  else                    r.removeAttribute('data-theme');
  document.querySelectorAll('.theme-btn').forEach(function(btn) {
    btn.classList.toggle('active', btn.getAttribute('data-theme') === t);
  });
}

function _syncThemeBtns() {
  document.querySelectorAll('.theme-btn').forEach(function(btn) {
    btn.classList.toggle('active', btn.getAttribute('data-theme') === _theme);
  });
}

// ── Auth ──────────────────────────────────────────────────────────────────────
var _authHeader     = localStorage.getItem('spm_auth') || '';
var _currentUser    = null;
var _sseSource      = null;
var _unreadMentions = [];
try { _currentUser = JSON.parse(localStorage.getItem('spm_user') || 'null'); } catch(e) {}

function authFetch(url, opts) {
  opts = opts || {};
  opts.headers = Object.assign({}, opts.headers, _authHeader ? {Authorization: _authHeader} : {});
  return fetch(url, opts).then(function(res) {
    if (res.status === 401 && _authHeader) {
      // Token expired or invalid — force back to login
      logout();
    }
    return res;
  });
}

// ── State ─────────────────────────────────────────────────────────────────────
var ORDERS = [];
var _activeFilters = new Set(['all']); // multi-select stage chips
var currentSubFilter = 'all';
var _lastSync = null;
var _pollTimer = null;
var POLL_MS = 30000;

var _page = 1;
var PER_PAGE = 100;
var _otdPage = 1;
var OTD_PER_PAGE = 100;
var _filtered = [];
var _sortCol = 'notification_received_at';
var _sortDir = 'asc';
var _otdSortCol = 'notification_received_at';
var _otdSortDir = 'desc';
var _nlngOtdFilter       = 'all';
var _nlngOtdDeliveredSub = 'all';
var _nlngOtdPeriodFilter = 'all';
var _nlngOtdPage         = 1;
var _nlngOtdSortCol      = 'notification_received_at';
var _nlngOtdSortDir      = 'desc';
var _selected     = new Set();
var _nlngSelected = new Set();

// sidebar resize state
var _sbWidth = 192;
var _sbCollapsed = false;

// ── Column definitions ────────────────────────────────────────────────────────
var COLS = [
  { key:'buyer_po_number',               hdr:'Chevron PO',                cls:'td-po mono td-sticky', sticky:true },
  { key:'_live_status',                  hdr:'State',                     cls:'',                     isLiveStatus:true },
  { key:'po_amount',                     hdr:'Value',                     cls:'td-val',               isAmt:true  },
  { key:'notification_received_at',      hdr:'Notified',                  cls:'td-dt',                isDate:true },
  { key:'order_submitted_on',            hdr:'Submitted',                 cls:'td-dt',                isDate:true },
  { key:'order_line_items',              hdr:'Items',                     cls:'td-items', isLineItems:true        },
  { key:'req_number',                    hdr:'REQ#',                      cls:'td-ref mono'                       },
  { key:'buyer_name',                    hdr:'Buyer/Requester',           cls:''                                  },
  { key:'required_delivery_date',        hdr:'Req. Delivery',             cls:'td-dt',                isDate:true },
  { key:'_po_promised',                  hdr:'PO Promised',               cls:'td-dt',                isPoPromised:true },
  { key:'po_destination',                hdr:'Destination',               cls:''                                  },
  { key:'transportation',                hdr:'Transport',                 cls:''                                  },
  { key:'acknowledgment_status',         hdr:'Ack Status',                cls:''                                  },
  { key:'acknowledged_at',               hdr:'Acknowledged',              cls:'td-dt',                isDate:true },
  { key:'sent_to_warehouse_at',          hdr:'Sent to Wh.',               cls:'td-dt',                isDate:true },
  { key:'warehouse_routing_raw',         hdr:'Routing Note',              cls:'td-ref td-trunc',      isRoutingRaw:true },
  { key:'stock_check_completed_at',      hdr:'Stock Check',               cls:'td-dt',                isDate:true },
  { key:'stock_check_raw',               hdr:'Stock Notes',               cls:'td-ref td-trunc'                   },
  { key:'spm_po_number',                 hdr:'SPM PO',                    cls:'td-ref mono td-trunc'              },
  { key:'spm_po_sent_at',                hdr:'SPM PO Sent',               cls:'td-dt',                isDate:true },
  { key:'so_number',                     hdr:'SO Number',                 cls:'td-ref mono'                       },
  { key:'_so_items',                     hdr:'SO Items',                  cls:'td-items', isSoItems:true          },
  { key:'promised_date',                 hdr:'Promised Dispatch',         cls:'td-dt',                isDate:true },
  { key:'so_received_at',                hdr:'SO Received',               cls:'td-dt',                isDate:true },
  { key:'so_sent_to_warehouse_at',       hdr:'SO → Wh.',             cls:'td-dt',                isDate:true },
  { key:'flex_dispatch_ready_at',        hdr:'Dispatch Ready',            cls:'td-dt',                isDate:true },
  { key:'dispatch_instructions_sent_at', hdr:'Dispatch Instr.',           cls:'td-dt',                isDate:true },
  { key:'ready_for_dispatch_at',         hdr:'To Shipping Co.',           cls:'td-dt',                isDate:true },
  { key:'dispatched_at',                 hdr:'Shipping Co. Rcvd',         cls:'td-dt',                isDate:true },
  { key:'delivery_requested_at',         hdr:'Delivery Request',          cls:'td-dt',                isDate:true },
  { key:'delivered_at',                  hdr:'Delivered',                 cls:'td-dt',                isDate:true },
  { key:'overall_status',               hdr:'Status',                    cls:'',                     isStatus:true }
];

// ── Stage map ─────────────────────────────────────────────────────────────────
var STAGE_MAP = {
  new:                                  {lbl:'New',                      cls:'sp-n'},
  pending_acknowledgment:               {lbl:'Pending ack',              cls:'sp-n'},
  acknowledged:                         {lbl:'Acknowledged',             cls:'sp-n'},
  awaiting_warehouse_stock_check:       {lbl:'Stock check',              cls:'sp-n'},
  stock_check_needs_review:             {lbl:'Review needed',            cls:'sp-w'},
  stock_check_complete:                 {lbl:'Stock OK',                 cls:'sp-n'},
  pricing:                              {lbl:'Pricing',                  cls:'sp-n'},
  po_sent:                              {lbl:'SPM PO sent',              cls:'sp-r'},
  awaiting_supplier_so:                 {lbl:'Awaiting SO',              cls:'sp-r'},
  supplier_acknowledged:                {lbl:'SO received',              cls:'sp-r'},
  dispatch_packed_awaiting_instruction: {lbl:'Packed – awaiting instr.', cls:'sp-w'},
  dispatch_instruction_sent:            {lbl:'Instr. sent',              cls:'sp-w'},
  so_sent_to_warehouse:                 {lbl:'SO to warehouse',          cls:'sp-b'},
  ready_for_dispatch:                   {lbl:'Collection arranged',      cls:'sp-b'},
  dispatched:                           {lbl:'With shipping co.',         cls:'sp-b'},
  delivery_requested:                   {lbl:'Delivery requested',       cls:'sp-b'},
  delivered:                            {lbl:'Delivered',                cls:'sp-ok'},
  waybill_received:                     {lbl:'Waybill received',         cls:'sp-ok'},
  invoiced:                             {lbl:'Invoiced',                 cls:'sp-ok'},
  paid:                                 {lbl:'Paid',                     cls:'sp-ok'},
  closed:                               {lbl:'Closed',                   cls:'sp-ok'},
  cancelled:                            {lbl:'Cancelled',                cls:'sp-crit'}
};

var CLOSED_STATUSES = new Set(['delivered','waybill_received','invoiced','paid','closed','cancelled']);

// Single source of truth for the STATE column. Every call site used to test
// `overall_status === 'delivered'` on its own, so any other terminal status
// (cancelled, closed, paid, invoiced, waybill_received) still rendered as a
// green "Live" pill.
function isClosedOrder(o) {
  if (!o) return false;
  return CLOSED_STATUSES.has(o.overall_status) || !!o.delivered_at;
}
var DISPATCH_STAGES = ['dispatch_packed_awaiting_instruction','dispatch_instruction_sent','so_sent_to_warehouse','ready_for_dispatch','dispatched'];

// Pipeline order (must match STATUS_RANK in sync.py)
var PIPELINE_ORDER = [
  'new','pending_acknowledgment','acknowledged',
  'awaiting_warehouse_stock_check','stock_check_needs_review','stock_check_complete',
  'pricing','po_sent','awaiting_supplier_so','supplier_acknowledged',
  'dispatch_packed_awaiting_instruction','dispatch_instruction_sent',
  'so_sent_to_warehouse','ready_for_dispatch','dispatched','delivery_requested',
  'delivered','waybill_received','invoiced','paid','closed','cancelled'
];

// ── Stock check raw helper ────────────────────────────────────────────────────

// Removes the quoted thread from an email reply body, keeping only the fresh lines
function stripEmailThread(text) {
  if (!text) return text;
  var lines = text.split(/\r?\n/);
  var cut = lines.length;
  for (var i = 0; i < lines.length; i++) {
    var ln = lines[i].trim();
    // "On [date] ... wrote:" — start of quoted thread
    if (/^On .{5,100}wrote:\s*$/i.test(ln)) { cut = i; break; }
    // Lines prefixed with > are quoted
    if (ln.startsWith('>')) { cut = i; break; }
  }
  return lines.slice(0, cut).join('\n').trim();
}

function extractStockRaw(v) {
  if (v == null || v === '') return null;

  function fromObj(o) {
    if (!o || typeof o !== 'object') return null;
    // Confident clean result — just show the summary (e.g. "PO not in stock")
    if (!o.needs_human_review && o.confidence === 'high' && o.summary) {
      return String(o.summary).trim();
    }
    // Unclear / deferred — strip quoted thread and show just the fresh reply
    if (o.raw_body) return stripEmailThread(String(o.raw_body).trim());
    // Old entry with no raw_body — strip error noise from summary
    if (o.summary) {
      var s = String(o.summary).trim().replace(/^Interpretation deferred:\s*/i, '');
      if (/^Error code:|^'type':|^\{/.test(s)) return 'Awaiting re-check';
      return s || null;
    }
    return null;
  }

  if (typeof v === 'object') return fromObj(v);
  var s = String(v).trim();
  if (s.startsWith('{')) {
    try { return fromObj(JSON.parse(s)) || s; } catch(e) {}
  }
  return s;
}

// Returns true when the warehouse reply has SOME items not in stock but ALSO some in stock
function isPartialStock(o) {
  var raw = extractStockRaw(o.stock_check_raw);
  if (!raw) return false;
  var lo = raw.toLowerCase();
  var hasIn    = lo.includes('in stock') || lo.includes('rfd') || lo.includes('in-stock');
  var hasOut   = lo.includes('not in stock') || lo.includes('not instock') || lo.includes('out of stock') || lo.includes('no stock');
  return hasIn && hasOut;
}

// Returns true when ALL (or the overall) reply indicates not in stock
function isNotInStock(o) {
  if (isPartialStock(o)) return false; // partial — handled separately
  var raw = extractStockRaw(o.stock_check_raw);
  if (!raw) return false;
  var lo = raw.toLowerCase();
  return lo.includes('not in stock') || lo.includes('not instock')
      || lo.includes('out of stock') || lo.includes('no stock')
      || lo.includes('unavailable')  || lo.includes('not available');
}

// ── Stage chip definitions ────────────────────────────────────────────────────
// subs: array of {key, label} — 'dynamic_months' means build from data at render time
var CHIP_STAGES = [
  {key:'all',                                  label:'All'},
  {key:'live',                                 label:'Live'},
  {key:'closed',                               label:'Closed'},
  {key:'awaiting_warehouse_stock_check',       label:'Stock check'},
  {key:'stock_check_needs_review',             label:'Review needed'},
  {key:'stock_check_complete',                 label:'Stock OK'},
  {key:'po_sent',                              label:'SPM PO sent'},
  {key:'awaiting_supplier_so',                 label:'Awaiting SO'},
  {key:'supplier_acknowledged',                label:'SO received'},
  {key:'dispatch_packed_awaiting_instruction', label:'Packed – awaiting instr.'},

  {key:'ready_for_dispatch',                   label:'Collection arranged'},
  {key:'dispatched',                           label:'With shipping co.'},
  {key:'delivery_requested',                   label:'Delivery requested'},
  {key:'delivered',                            label:'Delivered'},
  {key:'cancelled',                            label:'Cancelled'}
];

// ── Client switcher ───────────────────────────────────────────────────────────
var _activeClient = 'chevron'; // 'chevron' | 'nlng' | 'seplat'

// ── NLNG data + state ─────────────────────────────────────────────────────────
var NLNG_ORDERS     = [];
var _nlngActiveFilter = 'all';
var _nlngSubFilter    = 'all';
var _nlngReceivedFrom = '';   // 'YYYY-MM-DD' — nlng Received from
var _nlngReceivedTo   = '';   // 'YYYY-MM-DD' — nlng Received to
var _nlngPage         = 1;
var _nlngFiltered     = [];
var _nlngSortCol      = 'notification_received_at';
var _nlngSortDir      = 'desc';

var NLNG_COLS = [
  { key:'po_number',                     hdr:'NLNG PO',              cls:'td-po mono td-sticky', sticky:true },
  { key:'_nlng_live',                    hdr:'State',                cls:'',  isNlngLive:true },
  { key:'net_value',                     hdr:'Value',                cls:'td-val', isNlngAmt:true },
  { key:'notification_received_at',      hdr:'Notified',             cls:'td-dt', isDate:true },
  { key:'nlng_order_line_items',         hdr:'Items',                cls:'td-items', isNlngItems:true },
  { key:'contact_name',                  hdr:'Buyer/Requester',      cls:'' },
  { key:'enquiry_number',                hdr:'ENQ#',                 cls:'td-ref mono', isNlngEnq:true },
  { key:'required_delivery_date',        hdr:'RDD',                  cls:'td-dt', isDate:true },
  { key:'_nlng_ack',                     hdr:'Ack Status',           cls:'', isNlngAck:true },
  { key:'sent_to_warehouse_at',          hdr:'Sent to Wh.',          cls:'td-dt', isDate:true },
  { key:'warehouse_routing_raw',         hdr:'Routing Note',         cls:'td-ref td-trunc', isRoutingRaw:true },
  { key:'stock_check_completed_at',      hdr:'Stock Check',          cls:'td-dt', isDate:true },
  { key:'stock_check_raw',               hdr:'Stock Notes',          cls:'td-ref td-trunc' },
  { key:'spm_po_sent_at',                hdr:'SPM PO Sent',          cls:'td-dt', isDate:true },
  { key:'spm_po_number',                 hdr:'SPM PO',               cls:'td-ref mono td-trunc' },
  { key:'so_number',                     hdr:'SO Number',            cls:'td-ref mono' },
  { key:'_nlng_so_items',                hdr:'SO Items',             cls:'td-items', isNlngSoItems:true },
  { key:'promised_date',                 hdr:'Promised Dispatch',    cls:'td-dt', isDate:true },
  { key:'so_received_at',                hdr:'SO Received',          cls:'td-dt', isDate:true },
  { key:'so_sent_to_warehouse_at',       hdr:'SO → Wh.',        cls:'td-dt', isDate:true },
  { key:'flex_dispatch_ready_at',        hdr:'Dispatch Ready',       cls:'td-dt', isDate:true },
  { key:'dispatch_instructions_sent_at', hdr:'Dispatch Instr.',      cls:'td-dt', isDate:true },
  { key:'ready_for_dispatch_at',         hdr:'To Shipping Co.',      cls:'td-dt', isDate:true },
  { key:'dispatched_at',                 hdr:'Shipping Co. Rcvd',    cls:'td-dt', isDate:true },
  { key:'delivered_at',                  hdr:'Delivered',            cls:'td-dt', isDate:true },
  { key:'overall_status',                hdr:'Status',               cls:'', isNlngStatus:true }
];

var NLNG_STAGE_MAP = {
  notification_received:                 {lbl:'Notified',                 cls:'sp-n'},
  awaiting_warehouse_stock_check:        {lbl:'Sent to warehouse',        cls:'sp-n'},
  stock_check_complete:                  {lbl:'Stock check done',         cls:'sp-n'},
  po_sent:                               {lbl:'SPM PO sent',              cls:'sp-r'},
  awaiting_supplier_so:                  {lbl:'Awaiting SO',              cls:'sp-r'},
  supplier_acknowledged:                 {lbl:'SO received',              cls:'sp-r'},
  so_sent_to_warehouse:                  {lbl:'SO to warehouse',          cls:'sp-b'},
  dispatch_packed_awaiting_instruction:  {lbl:'Packed – awaiting instr.', cls:'sp-w'},
  dispatch_instruction_sent:             {lbl:'Instr. sent',              cls:'sp-w'},
  ready_for_dispatch:                    {lbl:'With shipping co.',         cls:'sp-b'},
  dispatched:                            {lbl:'Shipped',                  cls:'sp-b'},
  delivered:                             {lbl:'Delivered',                cls:'sp-ok'}
};

var NLNG_CHIP_STAGES = [
  { key:'all',                                  label:'All' },
  { key:'live',                                 label:'Live' },
  { key:'closed',                               label:'Closed' },
  { key:'awaiting_warehouse_stock_check',       label:'Stock check' },
  { key:'stock_check_complete',                 label:'Stock OK' },
  { key:'po_sent',                              label:'SPM PO sent' },
  { key:'awaiting_supplier_so',                 label:'Awaiting SO' },
  { key:'supplier_acknowledged',                label:'SO received' },
  { key:'so_sent_to_warehouse',                 label:'SO → Warehouse' },
  { key:'dispatch_packed_awaiting_instruction', label:'Packed – awaiting instr.' },
  { key:'dispatch_instruction_sent',            label:'Instr. sent' },
  { key:'ready_for_dispatch',                   label:'With shipping co.' },
  { key:'dispatched',                           label:'Shipped' },
  { key:'delivered',                            label:'Delivered' },
];

var NLNG_PIPELINE = [
  'notification_received','awaiting_warehouse_stock_check','stock_check_complete',
  'po_sent','awaiting_supplier_so','supplier_acknowledged','so_sent_to_warehouse',
  'dispatch_packed_awaiting_instruction','dispatch_instruction_sent',
  'ready_for_dispatch','dispatched','delivered'
];

function applyNlngStageFilter(o, stageKey) {
  if (stageKey === 'all')    return true;
  if (stageKey === 'live')   return o.overall_status !== 'delivered' && !o.delivered_at;
  if (stageKey === 'closed') return o.overall_status === 'delivered' || !!o.delivered_at;
  return o.overall_status === stageKey;
}

// Build month sub-filters dynamically from order data for a given date field
function buildMonthSubs(dateField) {
  var seen = {};
  var now = new Date();
  for (var i = 0; i < ORDERS.length; i++) {
    var v = ORDERS[i][dateField];
    if (!v) continue;
    var d = new Date(v);
    var key = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2,'0');
    seen[key] = d.toLocaleDateString('en-GB', {month:'short', year:'numeric'});
  }
  var subs = [{key:'all', label:'All'}];
  // Time-range shortcuts
  subs.push({key:'24h',    label:'Last 24 hrs'});
  subs.push({key:'week',   label:'This week'});
  subs.push({key:'2wk',    label:'2 weeks'});
  subs.push({key:'3wk',    label:'3 weeks'});
  var thisMonKey = now.getFullYear() + '-' + String(now.getMonth()+1).padStart(2,'0');
  subs.push({key:'month',  label:'This month'});
  // Past months from data, newest first
  var keys = Object.keys(seen).sort().reverse();
  for (var k = 0; k < keys.length; k++) {
    if (keys[k] !== thisMonKey) subs.push({key: keys[k], label: seen[keys[k]]});
  }
  return subs;
}

// Resolve CHIP_STAGES subs (expand dynamic month refs)
function getStage(key) {
  for (var i = 0; i < CHIP_STAGES.length; i++) {
    if (CHIP_STAGES[i].key === key) return CHIP_STAGES[i];
  }
  return null;
}
function getSubs(stage) {
  if (!stage || !stage.subs) return [];
  if (typeof stage.subs === 'string' && stage.subs.startsWith('months:')) {
    return buildMonthSubs(stage.subs.slice(7));
  }
  return stage.subs;
}

// ── Formatters ────────────────────────────────────────────────────────────────
function fmt(v) {
  if (v == null) return '<span class="td-null">&mdash;</span>';
  return Number(v) >= 1e6
    ? '$' + (Number(v) / 1e6).toFixed(1) + 'M'
    : '$' + Number(v).toLocaleString();
}

function fmtTs(v) {
  if (!v) return '<span class="td-null">&mdash;</span>';
  try {
    var d = new Date(v);
    var datePart = d.toLocaleDateString('en-GB', {day:'2-digit', month:'short', year:'2-digit'});
    var hasTime = String(v).includes('T') || (String(v).includes(' ') && String(v).length > 11);
    if (hasTime) {
      var timePart = d.toLocaleTimeString('en-GB', {hour:'2-digit', minute:'2-digit'});
      return datePart + ' <span class="ts-t">' + timePart + '</span>';
    }
    return datePart;
  } catch(e) { return String(v); }
}

function htmlEscape(s) {
  if (s == null) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
function n(v) {
  return (v != null && v !== '') ? htmlEscape(v) : '<span class="td-null">&mdash;</span>';
}

// ── Data loading ──────────────────────────────────────────────────────────────
async function loadOrders() {
  try {
    var res = await authFetch('/api/orders');
    if (!res.ok) throw new Error('Server error ' + res.status);
    var data = await res.json();
    if (data.error) throw new Error(data.error);
    ORDERS = data;
    _lastSync = new Date();
    buildStatusChips();    // also rebuilds orders period chips (month chips from data)
    buildOtdPeriodChips(); // rebuild OTD period chips
    filterOrders(true);
    updateDashboard();
    if (document.getElementById('page-delays').classList.contains('active')) renderOTD();
  } catch(e) {
    console.error('loadOrders:', e);
    var em = document.getElementById('ot-empty');
    if (em) { em.textContent = 'Could not load data: ' + e.message; em.classList.remove('hidden'); }
    var sub = document.getElementById('dash-sub');
    if (sub) sub.textContent = 'Error: ' + e.message;
  }
}

function startPolling() {
  if (_pollTimer) clearInterval(_pollTimer);
  _pollTimer = setInterval(function() {
    loadOrders();
    loadAlerts();
    loadUnreadCount();
    checkMentions();
  }, POLL_MS);
}

function toggleFunnelLabel(el) { el.classList.toggle('tapped'); }

// ── Role-based access ─────────────────────────────────────────────────────────
var ROLE_PAGES = {
  admin:       ['dashboard','orders','suppliers','delays','reports','quotes','purchase-orders','stock','products','settings','team','messages'],
  procurement: ['dashboard','orders','suppliers','delays','reports','quotes','purchase-orders','stock','products','settings','messages'],
  expeditor:   ['dashboard','orders','delays','stock','products','settings','messages'],
  // The warehouse is who actually walks the shelves and counts, so stock is
  // theirs before it is anyone else's.
  warehouse:   ['dashboard','orders','delays','stock','products','settings','messages'],
  accounts:    ['dashboard','orders','delays','reports','stock','settings','messages'],
};

function applyRoleVisibility() {
  var role    = (_currentUser && _currentUser.role) || 'procurement';
  var allowed = new Set(ROLE_PAGES[role] || ROLE_PAGES['procurement']);
  // Show/hide nav items
  document.querySelectorAll('.nav[data-p]').forEach(function(el) {
    el.style.display = allowed.has(el.dataset.p) ? '' : 'none';
  });
  // Team nav is admin-only
  var teamNav = document.getElementById('nav-team');
  if (teamNav) teamNav.style.display = (role === 'admin') ? '' : 'none';
  // A group header carries no data-p of its own, so it is shown only when at
  // least one of the rows underneath it is allowed.
  document.querySelectorAll('.nav-sub').forEach(function(sub) {
    var visible = Array.prototype.slice.call(sub.querySelectorAll('.nav[data-p]'))
      .some(function(child) { return allowed.has(child.dataset.p); });
    var header = document.getElementById(sub.id.replace('nav-sub-', 'nav-') + '-group');
    if (header) header.style.display = visible ? '' : 'none';
    if (!visible) sub.classList.remove('open');
  });
}

// ── Team page ─────────────────────────────────────────────────────────────────
var TEAM_USERS  = [];
var _AV_PALETTE = ['#7c3aed','#1d4ed8','#0369a1','#047857','#b45309','#9f1239','#0e7490','#4338ca'];

function _avColor(str) {
  var h = 0;
  for (var i = 0; i < str.length; i++) h = (h * 31 + str.charCodeAt(i)) & 0xffff;
  return _AV_PALETTE[h % _AV_PALETTE.length];
}

function _esc(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

function _relTime(iso) {
  if (!iso) return 'Never';
  var diff = Date.now() - new Date(iso).getTime();
  var m = Math.floor(diff / 60000);
  if (m < 2)   return 'Just now';
  if (m < 60)  return m + 'm ago';
  var h = Math.floor(m / 60);
  if (h < 24)  return h + 'h ago';
  var d = Math.floor(h / 24);
  if (d < 7)   return d + 'd ago';
  if (d < 30)  return Math.floor(d/7) + 'w ago';
  return new Date(iso).toLocaleDateString('en-GB', {day:'numeric',month:'short',year:'2-digit'});
}

async function loadUsers() {
  try {
    var res = await authFetch('/api/users');
    if (!res.ok) return;
    TEAM_USERS = await res.json();
    renderTeam();
  } catch(e) { console.error('loadUsers:', e); }
}

function renderTeam() {
  var list  = document.getElementById('team-list');
  var count = document.getElementById('team-count');
  if (!list) return;
  if (count) count.textContent = TEAM_USERS.length;
  if (!TEAM_USERS.length) {
    list.innerHTML = '<div style="padding:2.5rem;text-align:center;color:var(--t3);font-size:13px">No users yet</div>';
    return;
  }
  list.innerHTML = TEAM_USERS.map(function(u) {
    var name     = u.full_name || u.email.split('@')[0];
    var parts    = name.trim().split(/\s+/);
    var initials = parts.length >= 2
      ? (parts[0][0] + parts[parts.length-1][0]).toUpperCase()
      : name.slice(0,2).toUpperCase();
    var color    = _avColor(u.id || u.email);
    var isMe     = _currentUser && (_currentUser.id === u.id || (_currentUser.email === u.email && !DEPT_EMAILS[u.role]));
    var isAdmin  = u.role === 'admin';
    var logTime  = _relTime(u.last_login_at);
    return '<div class="team-row">'
      + '<div class="team-av" style="background:' + color + '">' + initials + '</div>'
      + '<div class="team-info">'
      +   '<div class="team-name">' + _esc(u.full_name || name)
      +     (isMe ? '<span class="team-you">you</span>' : '')
      +   '</div>'
      +   '<div class="team-email">' + _esc(u.email) + '</div>'
      + '</div>'
      + '<div class="team-right">'
      +   '<span class="rbadge ' + _esc(u.role) + '">' + _esc(u.role) + '</span>'
      +   '<span class="team-login">' + logTime + '</span>'
      +   (isMe ? '' : '<button title="Preview as this role" onclick="enterPreview(\'' + _esc(u.role) + '\',\'' + _esc(u.full_name || u.email) + '\')" style="background:none;border:none;cursor:pointer;color:var(--t3);padding:3px;border-radius:3px;display:flex;align-items:center" onmouseover="this.style.color=\'var(--t1)\'" onmouseout="this.style.color=\'var(--t3)\'"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg></button>')
      +   '<button title="Reset password" onclick="openResetPwModal(\'' + _esc(u.id) + '\',\'' + _esc(u.full_name || u.email) + '\')" style="background:none;border:none;cursor:pointer;color:var(--t3);padding:3px;border-radius:3px;display:flex;align-items:center" onmouseover="this.style.color=\'var(--t1)\'" onmouseout="this.style.color=\'var(--t3)\'">'
      +   '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0110 0v4"/></svg></button>'
      +   '<label class="tog" title="' + (u.is_active ? 'Deactivate' : 'Activate') + '">'
      +     '<input type="checkbox"' + (u.is_active ? ' checked' : '')
      +     (isMe ? ' disabled' : '')
      +     ' onchange="toggleUserActive(\'' + _esc(u.id) + '\', this.checked)">'
      +     '<span class="tog-sl"></span>'
      +   '</label>'
      +   (!isMe && !isAdmin ? '<button title="Remove user" onclick="openDeleteUserModal(\'' + _esc(u.id) + '\',\'' + _esc(u.full_name || name) + '\')" style="background:none;border:none;cursor:pointer;color:var(--crit);opacity:0.6;padding:3px;border-radius:3px;display:flex;align-items:center" onmouseover="this.style.opacity=\'1\'" onmouseout="this.style.opacity=\'0.6\'"><svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="3 6 5 6 21 6"/><path d="M19 6l-1 14H6L5 6"/><path d="M10 11v6M14 11v6"/><path d="M9 6V4h6v2"/></svg></button>' : '')
      + '</div>'
      + '</div>';
  }).join('');
}

async function toggleUserActive(userId, active) {
  try {
    await authFetch('/api/users/' + userId, {
      method: 'PATCH',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({is_active: active}),
    });
    var u = TEAM_USERS.find(function(x){ return x.id === userId; });
    if (u) u.is_active = active;
  } catch(e) { console.error('toggleUserActive:', e); }
}

// ── Role preview ──────────────────────────────────────────────────────────────
var _realAdminRole = null;

function enterPreview(role, displayName) {
  if (!_currentUser) return;
  _realAdminRole = _currentUser.role;
  _currentUser.role = role;
  var bar = document.getElementById('preview-bar');
  var lbl = document.getElementById('preview-bar-label');
  if (bar) bar.classList.add('on');
  if (lbl) lbl.textContent = 'Previewing as ' + displayName + ' (' + role + ')';
  applyRoleVisibility();
  var allowed = ROLE_PAGES[role] || [];
  showPage(allowed[0] || 'dashboard');
}

function exitPreview() {
  if (!_realAdminRole) return;
  _currentUser.role = _realAdminRole;
  _realAdminRole = null;
  var bar = document.getElementById('preview-bar');
  if (bar) bar.classList.remove('on');
  applyRoleVisibility();
  showPage('team');
}

// ── Reset password ────────────────────────────────────────────────────────────
var _resetPwUserId = null;

function openResetPwModal(userId, userName) {
  _resetPwUserId = userId;
  document.getElementById('rp-username').textContent = userName;
  document.getElementById('rp-pw').value = '';
  var err = document.getElementById('rp-error');
  err.style.display = 'none'; err.textContent = '';
  document.getElementById('reset-pw-modal').classList.remove('hidden');
  document.getElementById('rp-pw').focus();
}

function closeResetPwModal() {
  document.getElementById('reset-pw-modal').classList.add('hidden');
  _resetPwUserId = null;
}

async function submitResetPw() {
  var pw    = document.getElementById('rp-pw').value;
  var errEl = document.getElementById('rp-error');
  var btn   = document.getElementById('rp-submit-btn');
  errEl.style.display = 'none';
  if (!pw || pw.length < 6) {
    errEl.textContent = 'Password must be at least 6 characters.';
    errEl.style.display = 'block';
    return;
  }
  btn.disabled = true; btn.textContent = 'Saving…';
  try {
    var res = await authFetch('/api/users/' + _resetPwUserId, {
      method: 'PATCH',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({password: pw}),
    });
    var data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Failed');
    closeResetPwModal();
  } catch(e) {
    errEl.textContent = e.message || 'Could not reset password.';
    errEl.style.display = 'block';
  } finally {
    btn.disabled = false; btn.textContent = 'Set password';
  }
}

// ── To: multi-select ──────────────────────────────────────────────────────────
var _ROLE_COLORS = {
  warehouse:'#78350f', procurement:'#1e3a8a', expeditor:'#064e3b',
  accounts:'#1f2937', admin:'#3b0764'
};
var _ROLE_TEXT = {
  warehouse:'#fde68a', procurement:'#bfdbfe', expeditor:'#a7f3d0',
  accounts:'#d1fae5', admin:'#e9d5ff'
};

function toggleToDrop(e) {
  e.stopPropagation();
  var drop    = document.getElementById('to-drop');
  var trigger = document.getElementById('to-trigger');
  var isOpen  = drop.classList.contains('open');
  drop.classList.toggle('open', !isOpen);
  trigger.classList.toggle('open', !isOpen);
}

function getSelectedRoles() {
  return Array.from(document.querySelectorAll('#to-drop input[type=checkbox]:checked')).map(function(cb){ return cb.value; });
}

function setSelectedRoles(roles) {
  document.querySelectorAll('#to-drop input[type=checkbox]').forEach(function(cb) {
    cb.checked = roles.indexOf(cb.value) !== -1;
  });
  updateToChips();
}

function updateToChips() {
  var roles   = getSelectedRoles();
  var trigger = document.getElementById('to-trigger');
  var ph      = document.getElementById('to-ph');
  // Remove existing chips
  trigger.querySelectorAll('.to-chip').forEach(function(c){ c.remove(); });
  if (!roles.length) {
    ph.style.display = '';
  } else {
    ph.style.display = 'none';
    roles.forEach(function(r) {
      var chip = document.createElement('span');
      chip.className = 'to-chip';
      chip.style.background = _ROLE_COLORS[r] || '#374151';
      chip.style.color      = _ROLE_TEXT[r]   || '#fff';
      chip.innerHTML = _esc(r.charAt(0).toUpperCase() + r.slice(1))
        + '<button class="to-chip-x" onclick="removeRole(\'' + r + '\',event)" title="Remove">×</button>';
      trigger.insertBefore(chip, ph);
    });
  }
}

function removeRole(role, e) {
  e.stopPropagation();
  var cb = document.querySelector('#to-drop input[value="' + role + '"]');
  if (cb) { cb.checked = false; updateToChips(); }
}

// Close dropdown when clicking outside
document.addEventListener('click', function(e) {
  if (!document.getElementById('to-sel').contains(e.target)) {
    document.getElementById('to-drop').classList.remove('open');
    document.getElementById('to-trigger').classList.remove('open');
  }
});

// ── Compose message ───────────────────────────────────────────────────────────
var _composeOrderId     = null;
var _composeData        = {}; // keyed by order id, avoids apostrophe issues in onclick
var _composeOrderClient = null;
var _composePdfUrl      = null;
var _composeAttachFile  = null;

function onComposeFileChosen(input) {
  var file = input.files && input.files[0];
  if (!file) return;
  if (file.size > 10 * 1024 * 1024) {
    alert('File too large — maximum is 10 MB.');
    input.value = '';
    return;
  }
  _composeAttachFile = file;
  document.getElementById('compose-attach-name').textContent = file.name;
  document.getElementById('compose-attach-pill').style.display = '';
}

function clearComposeAttach() {
  _composeAttachFile = null;
  var inp = document.getElementById('compose-file-input');
  if (inp) inp.value = '';
  document.getElementById('compose-attach-pill').style.display = 'none';
  document.getElementById('compose-attach-name').textContent = '';
}

function openCompose(opts) {
  opts = opts || {};
  _composeOrderId     = opts.orderId     || null;
  _composeOrderClient = opts.orderClient || null;
  _composePdfUrl      = opts.pdfUrl      || null;
  setSelectedRoles(opts.toRole ? [opts.toRole] : []);
  document.getElementById('compose-subject').value = opts.subject || '';
  document.getElementById('compose-body').value    = opts.body    || '';
  var errEl = document.getElementById('compose-error');
  errEl.style.display = 'none'; errEl.textContent = '';
  // PO reference block
  var poRef = document.getElementById('compose-po-ref');
  if (opts.poFields && Object.keys(opts.poFields).length) {
    poRef.style.display = '';
    // Render each field individually; put Required By + Destination side by side
    var pf = opts.poFields;
    var paired = ['Required By', 'Destination'];
    var hasPair = pf['Required By'] || pf['Destination'];
    var html = '';
    Object.keys(pf).forEach(function(k) {
      if (paired.indexOf(k) !== -1) return; // handled below
      html += '<div class="cpo-field"><div class="cpo-label">' + _esc(k) + '</div>'
            + '<div class="cpo-val">' + _esc(pf[k] || '—') + '</div></div>';
    });
    if (hasPair) {
      html += '<div class="cpo-row">';
      paired.forEach(function(k) {
        html += '<div><div class="cpo-label">' + _esc(k) + '</div>'
              + '<div class="cpo-val">' + _esc(pf[k] || '—') + '</div></div>';
      });
      html += '</div>';
    }
    document.getElementById('compose-po-fields').innerHTML = html;
    // PDF attachment
    var pdfAttach = document.getElementById('compose-pdf-attach');
    var pdfNone   = document.getElementById('compose-pdf-none');
    var pdfLink   = document.getElementById('compose-pdf-link');
    var pdfName   = document.getElementById('compose-pdf-name');
    if (opts.pdfUrl) {
      var fn = 'PO_' + (pf['PO Number'] || 'document') + '.pdf';
      pdfName.textContent = fn;
      pdfLink.href = opts.pdfUrl;
      pdfLink.setAttribute('download', fn);
      pdfAttach.style.display = '';
      pdfNone.style.display   = 'none';
    } else {
      pdfAttach.style.display = 'none';
      pdfNone.style.display   = '';
    }
  } else { poRef.style.display = 'none'; }
  document.getElementById('compose-modal').classList.remove('hidden');
  document.getElementById('compose-body').focus();
}

function closeCompose() {
  document.getElementById('compose-modal').classList.add('hidden');
  setSelectedRoles([]);
  _composeOrderId = null; _composeOrderClient = null; _composePdfUrl = null;
  clearComposeAttach();
}

async function sendMessage() {
  var roles   = getSelectedRoles();
  var subject = document.getElementById('compose-subject').value.trim();
  var body    = document.getElementById('compose-body').value.trim();
  var errEl   = document.getElementById('compose-error');
  var btn     = document.getElementById('compose-send-btn');
  errEl.style.display = 'none';
  if (!roles.length || !subject || !body) {
    errEl.textContent = 'Recipient, subject and message are all required.';
    errEl.style.display = 'block';
    return;
  }
  btn.disabled = true; btn.textContent = 'Sending…';
  var attachUrl = null, attachName = null;
  try {
    // Upload attachment first if one was chosen
    if (_composeAttachFile) {
      btn.textContent = 'Uploading file…';
      var fd  = new FormData();
      fd.append('file', _composeAttachFile, _composeAttachFile.name);
      var upRes  = await authFetch('/api/messages/upload', {method: 'POST', body: fd});
      var upData = await upRes.json();
      if (!upRes.ok) throw new Error((upData && upData.detail) || 'File upload failed');
      var ud  = (upData && upData.data) || upData;
      attachUrl  = ud.url;
      attachName = ud.name;
      btn.textContent = 'Sending…';
    }
    // Send one message per selected role
    await Promise.all(roles.map(function(role) {
      return authFetch('/api/messages', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          to_role:         role,
          subject:         subject,
          body:            body,
          message_type:    _composeOrderId ? 'availability_check' : 'general',
          order_id:        _composeOrderId,
          order_client:    _composeOrderClient,
          po_pdf_url:      _composePdfUrl    || undefined,
          attachment_url:  attachUrl         || undefined,
          attachment_name: attachName        || undefined,
        }),
      }).then(function(res) {
        return res.json().then(function(data) {
          if (!res.ok) throw new Error(data.error || 'Failed sending to ' + role);
        });
      });
    }));
    closeCompose();
  } catch(e) {
    errEl.textContent = e.message || 'Could not send message.';
    errEl.style.display = 'block';
  } finally {
    btn.disabled = false; btn.textContent = 'Send Message';
  }
}

async function loadUnreadCount() {
  try {
    var role = (_currentUser && _currentUser.role) || '';
    var res  = await authFetch('/api/messages/unread_count?role=' + encodeURIComponent(role));
    if (!res.ok) return;
    var data = await res.json();
    var badge = document.getElementById('msg-badge');
    if (badge) {
      badge.textContent    = data.count;
      badge.style.display  = data.count > 0 ? '' : 'none';
    }
  } catch(e) {}
}

// ── Delete user ───────────────────────────────────────────────────────────────
var _deleteUserId = null;

function openDeleteUserModal(userId, userName) {
  _deleteUserId = userId;
  document.getElementById('du-username').textContent = userName;
  var err = document.getElementById('du-error');
  err.style.display = 'none'; err.textContent = '';
  document.getElementById('delete-user-modal').classList.remove('hidden');
}

function closeDeleteUserModal() {
  document.getElementById('delete-user-modal').classList.add('hidden');
  _deleteUserId = null;
}

async function confirmDeleteUser() {
  if (!_deleteUserId) return;
  var btn   = document.getElementById('du-confirm-btn');
  var errEl = document.getElementById('du-error');
  btn.disabled = true; btn.textContent = 'Removing…';
  try {
    var res = await authFetch('/api/users/' + _deleteUserId, { method: 'DELETE' });
    var data = await res.json();
    if (!res.ok) throw new Error(data.error || 'Failed');
    closeDeleteUserModal();
    TEAM_USERS = TEAM_USERS.filter(function(u){ return u.id !== _deleteUserId; });
    renderTeam();
  } catch(e) {
    errEl.textContent = e.message || 'Could not remove user.';
    errEl.style.display = 'block';
    btn.disabled = false; btn.textContent = 'Remove';
  }
}

// ── Add user role → email helpers ─────────────────────────────────────────────
function onAuRoleChange() {
  var role     = document.getElementById('au-role').value;
  var display  = document.getElementById('au-dept-email-display');
  var emailIn  = document.getElementById('au-email');
  if (role && role !== 'admin') {
    var deptEmail = DEPT_EMAILS[role] || '';
    if (display) { display.textContent = deptEmail; display.style.display = ''; }
    if (emailIn) emailIn.style.display = 'none';
  } else if (role === 'admin') {
    if (display) display.style.display = 'none';
    if (emailIn) { emailIn.style.display = ''; emailIn.value = ''; }
  } else {
    if (display) display.style.display = 'none';
    if (emailIn) emailIn.style.display = 'none';
  }
}

function openAddUserModal() {
  ['au-name','au-email','au-pw'].forEach(function(id){ document.getElementById(id).value = ''; });
  document.getElementById('au-role').value = '';
  var display = document.getElementById('au-dept-email-display');
  var emailIn = document.getElementById('au-email');
  if (display) display.style.display = 'none';
  if (emailIn) emailIn.style.display = 'none';
  var err = document.getElementById('au-error');
  err.style.display = 'none'; err.textContent = '';
  document.getElementById('add-user-modal').classList.remove('hidden');
  document.getElementById('au-name').focus();
}

function closeAddUserModal() {
  document.getElementById('add-user-modal').classList.add('hidden');
}

async function submitAddUser() {
  var name  = document.getElementById('au-name').value.trim();
  var email = (document.getElementById('au-email').value || '').trim();
  var role  = document.getElementById('au-role').value;
  var pw    = document.getElementById('au-pw').value;
  var errEl = document.getElementById('au-error');
  var btn   = document.getElementById('au-submit-btn');
  errEl.style.display = 'none';
  // For non-admin roles, email is set server-side from role; admin requires explicit email
  if (!role || !pw || (role === 'admin' && !email)) {
    errEl.textContent = role === 'admin' ? 'Email, role, and password are required.' : 'Role and password are required.';
    errEl.style.display = 'block';
    return;
  }
  if (pw.length < 6) {
    errEl.textContent = 'Password must be at least 6 characters.';
    errEl.style.display = 'block';
    return;
  }
  btn.disabled = true; btn.textContent = 'Creating…';
  try {
    var payload = {full_name: name, role: role, password: pw};
    if (role === 'admin' && email) payload.email = email;
    var res = await authFetch('/api/users', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(payload),
    });
    var data = await res.json();
    if (!res.ok) { throw new Error(data.error || 'Failed'); }
    closeAddUserModal();
    loadUsers();
  } catch(e) {
    errEl.textContent = e.message || 'Could not create user.';
    errEl.style.display = 'block';
  } finally {
    btn.disabled = false; btn.textContent = 'Create account';
  }
}

// ── Exchange rate cache ────────────────────────────────────────────────────────
var _ngnRate      = null;   // NGN per 1 USD
var _ngnRateAt    = 0;      // timestamp of last fetch
var _NGN_TTL      = 3600000; // refresh rate every 1 hour

async function getNgnRate() {
  if (_ngnRate && (Date.now() - _ngnRateAt) < _NGN_TTL) return _ngnRate;
  try {
    var res  = await fetch('https://open.er-api.com/v6/latest/USD');
    var data = await res.json();
    if (data && data.rates && data.rates.NGN) {
      _ngnRate   = data.rates.NGN;
      _ngnRateAt = Date.now();
    }
  } catch(e) { console.warn('Exchange rate fetch failed:', e); }
  // Fallback only if the rate fetch fails. Kept in step with the suppliers
  // page's FX_RATES so the two pages cannot disagree; refreshed 12 Sep 2026.
  return _ngnRate || 1327;
}

// ── Dashboard ─────────────────────────────────────────────────────────────────
async function updateDashboard() {
  var ngnPerUsd = await getNgnRate();

  // Chevron — convert NGN po_amount values to USD before summing
  var chActive    = ORDERS.filter(function(o){ return !CLOSED_STATUSES.has(o.overall_status); });
  var chDelivered = ORDERS.filter(function(o){ return CLOSED_STATUSES.has(o.overall_status); });
  var chSoRcvd    = ORDERS.filter(function(o){ return o.so_received_at; });
  var chTotalVal  = ORDERS.reduce(function(s,o) {
    var v = Number(o.po_amount) || 0;
    return s + (o.po_currency === 'NGN' ? v / ngnPerUsd : v);
  }, 0);

  // NLNG — convert NGN values to USD before summing
  var nlngActive    = NLNG_ORDERS.filter(function(o){ return o.overall_status !== 'delivered' && !o.delivered_at; });
  var nlngDelivered = NLNG_ORDERS.filter(function(o){ return o.overall_status === 'delivered' || !!o.delivered_at; });
  var nlngSoRcvd    = NLNG_ORDERS.filter(function(o){ return o.so_received_at; });
  var nlngTotalVal  = NLNG_ORDERS.reduce(function(s,o) {
    var v = Number(o.net_value) || 0;
    return s + (o.currency === 'NGN' ? v / ngnPerUsd : v);
  }, 0);

  // Combined totals
  var totalActive    = chActive.length + nlngActive.length;
  var totalDelivered = chDelivered.length + nlngDelivered.length;
  var totalSoRcvd    = chSoRcvd.length + nlngSoRcvd.length;
  var totalVal       = chTotalVal + nlngTotalVal;

  _set('kpi-active',       totalActive);
  _set('kpi-active-note',  chActive.length + ' Chevron · ' + nlngActive.length + ' NLNG');
  _set('kpi-value',        totalVal >= 1e6 ? '$' + (totalVal/1e6).toFixed(1) + 'M' : '$' + totalVal.toLocaleString());
  _set('kpi-value-note',   'Chevron + NLNG · NGN converted at $1=₦' + Math.round(ngnPerUsd).toLocaleString());
  _set('kpi-delivered',    totalDelivered);
  _set('kpi-delivered-note', chDelivered.length + ' Chevron · ' + nlngDelivered.length + ' NLNG');
  _set('kpi-spm-pos',      totalSoRcvd);
  _set('kpi-spm-pos-note', chSoRcvd.length + ' Chevron · ' + nlngSoRcvd.length + ' NLNG');

  // 2026 supply pipeline — combined Chevron + NLNG
  _set('fun-notified',   ORDERS.length + NLNG_ORDERS.length);
  _set('fun-spm',        ORDERS.filter(function(o){ return o.spm_po_sent_at; }).length + NLNG_ORDERS.filter(function(o){ return o.spm_po_sent_at; }).length);
  _set('fun-so',         chSoRcvd.length + nlngSoRcvd.length);
  _set('fun-live',   (ORDERS.filter(function(o){ return !CLOSED_STATUSES.has(o.overall_status); }).length + NLNG_ORDERS.filter(function(o){ return o.overall_status !== 'delivered' && !o.delivered_at; }).length));
  _set('fun-closed', (ORDERS.filter(function(o){ return CLOSED_STATUSES.has(o.overall_status); }).length + NLNG_ORDERS.filter(function(o){ return o.overall_status === 'delivered' || !!o.delivered_at; }).length));

  var syncTime = _lastSync
    ? _lastSync.toLocaleTimeString('en-GB', {hour:'2-digit', minute:'2-digit'})
    : '...';
  _set('dash-sub', totalActive + ' active orders · Chevron + NLNG · last sync ' + syncTime);

  renderRecentOrders();
  buildActivityFeed();
  // Only redraw reports charts when the user is on the reports page.
  // updateReports() clears and redraws all charts simultaneously; running it
  // every 30s poll while the user is interacting with filters or dropdowns
  // looks like a full page refresh. showPage('reports') triggers it instead.
  var _rptPg = document.getElementById('page-reports');
  if (_rptPg && _rptPg.classList.contains('active')) updateReports();
}

// ── Reports page — all functions live in reports.js ──────────────────────────



// ── Old reports functions below are shadowed by reports.js (loaded after) ────

function _rptBucketCh(o) {
  var s = o.overall_status || '';
  for (var i = 0; i < RPT_STAGES.length; i++) {
    if (RPT_STAGES[i].statuses && RPT_STAGES[i].statuses.has(s)) return i;
  }
  return 3; // Pending
}

function _rptBucketNl(o) {
  if (o.overall_status === 'delivered' || !!o.delivered_at) return 4;
  var s = o.overall_status || '';
  for (var i = 0; i < 3; i++) {
    if (RPT_STAGES[i].statuses && RPT_STAGES[i].statuses.has(s)) return i;
  }
  return 3; // Pending
}

function _rptMonthBuckets(orders, dateField, year) {
  var now = new Date();
  var months = [];

  if (year) {
    // Full Jan–Dec grid for the selected year
    for (var i = 0; i < 12; i++) {
      months.push({ y: year, m: i, lbl: new Date(year, i, 1).toLocaleString('en', { month: 'short' }), v: 0 });
    }
  } else {
    // Past 8 months rolling window
    for (var i = 7; i >= 0; i--) {
      var d = new Date(now.getFullYear(), now.getMonth() - i, 1);
      months.push({ y: d.getFullYear(), m: d.getMonth(), lbl: d.toLocaleString('en', { month: 'short' }), v: 0 });
    }
  }

  orders.forEach(function(o) {
    var raw = o[dateField]; if (!raw) return;
    var d = new Date(raw); if (isNaN(d)) return;
    for (var j = 0; j < months.length; j++) {
      if (d.getFullYear() === months[j].y && d.getMonth() === months[j].m) { months[j].v++; break; }
    }
  });
  return months;
}

function _rptOtd(delivered, liField) {
  var liOtd = 0, liLate = 0, totalDays = 0, lateDays = 0, minDays = Infinity;
  delivered.forEach(function(o) {
    var cls   = otdClass(o);
    var n     = (o[liField || 'order_line_items'] || []).length || 1;
    var del   = o.delivered_at             ? new Date(o.delivered_at)             : null;
    var start = o.notification_received_at ? new Date(o.notification_received_at) : null;
    if (del && start && !isNaN(del) && !isNaN(start)) {
      var days = Math.round((del - start) / 86400000);
      if (days > 0) { totalDays += days; if (days < minDays) minDays = days; }
    }
    if (cls === 'del-otd') {
      liOtd += n;
    } else if (cls === 'del-late') {
      liLate += n;
      var rdd = o.required_delivery_date ? new Date(o.required_delivery_date) : null;
      if (del && rdd && !isNaN(del) && !isNaN(rdd)) lateDays += Math.round((del - rdd) / 86400000);
    }
  });
  var liTotal = liOtd + liLate;
  return {
    onTime: liOtd, late: liLate, total: liTotal,
    score:       liTotal     > 0 ? Math.round(liOtd / liTotal * 100)       : null,
    avgDays:     delivered.length > 0 ? Math.round(totalDays / delivered.length) : null,
    avgLateDays: liLate      > 0 ? Math.round(lateDays / liLate)           : null,
    minDays:     minDays < Infinity ? minDays : null
  };
}

function _rptTopBuyers(orders, nameField, n) {
  var counts = {};
  orders.forEach(function(o) {
    var name = (o[nameField] || '').trim() || 'Unknown';
    counts[name] = (counts[name] || 0) + 1;
  });
  return Object.keys(counts)
    .map(function(k) { return { name: k, count: counts[k] }; })
    .sort(function(a, b) { return b.count - a.count; })
    .slice(0, n || 5);
}

// ── SVG helpers ───────────────────────────────────────────────────────────────
var _rptNS = 'http://www.w3.org/2000/svg';
function _rptSvgEl(tag, attrs) {
  var el = document.createElementNS(_rptNS, tag);
  Object.keys(attrs || {}).forEach(function(k) { el.setAttribute(k, attrs[k]); });
  return el;
}
function _rptSvgTxt(txt, attrs, cls) {
  var el = _rptSvgEl('text', attrs);
  if (cls) el.setAttribute('class', cls);
  el.textContent = txt;
  return el;
}

// ── Tooltip ───────────────────────────────────────────────────────────────────
var _rptTip = null, _rptTipLbl, _rptTipVal;
function _rptEnsureTip() {
  if (_rptTip) return;
  _rptTip = document.createElement('div');
  _rptTip.id = 'rpt-tip';
  _rptTipLbl = document.createElement('div'); _rptTipLbl.id = 'rpt-tip-lbl';
  _rptTipVal = document.createElement('div'); _rptTipVal.id = 'rpt-tip-val';
  _rptTip.appendChild(_rptTipLbl); _rptTip.appendChild(_rptTipVal);
  document.body.appendChild(_rptTip);
}
function _rptShowTip(e, lbl, val) {
  _rptTipLbl.textContent = lbl; _rptTipVal.textContent = val;
  _rptTip.classList.add('on');
  var x = e.clientX + 14, y = e.clientY - 10;
  _rptTip.style.left = Math.min(x, window.innerWidth - (_rptTip.offsetWidth || 120) - 8) + 'px';
  _rptTip.style.top  = Math.max(y, 8) + 'px';
}
function _rptHideTip() { _rptTip.classList.remove('on'); }

// ── Donut chart ───────────────────────────────────────────────────────────────
function _rptDrawDonut(containerId, legendId, values) {
  var cont = document.getElementById(containerId);
  var leg  = document.getElementById(legendId);
  if (!cont || !leg) return;
  cont.innerHTML = ''; leg.innerHTML = '';

  var W = 190, cx = 95, cy = 95, R = 70, sw = 17;
  var circ = 2 * Math.PI * R;
  var total = values.reduce(function(s, v) { return s + v; }, 0);
  var svg = _rptSvgEl('svg', { viewBox:'0 0 '+W+' '+W, width:W, height:W });
  svg.style.minWidth = W + 'px';

  var bg = _rptSvgEl('circle', { cx:cx, cy:cy, r:R, fill:'none', 'stroke-width':sw });
  bg.style.stroke = _rptCssVar('--s2');
  svg.appendChild(bg);

  var offset = circ / 4; // 12 o'clock
  RPT_STAGES.forEach(function(stage, i) {
    var v = values[i];
    if (!v) { return; }
    var arc = (v / total) * circ;
    var gap = total > 1 ? 2 : 0;
    var c = _rptSvgEl('circle', {
      cx:cx, cy:cy, r:R, fill:'none',
      'stroke-dasharray': (arc - gap) + ' ' + (circ - arc + gap),
      'stroke-dashoffset': offset,
      'stroke-width': sw, 'stroke-linecap':'butt'
    });
    c.style.stroke = _rptCssVar(stage.ck);
    c.style.cursor = 'pointer';
    (function(s, v2, pct) {
      c.addEventListener('mousemove', function(e) { _rptShowTip(e, s.label, v2 + ' orders · ' + pct + '%'); });
      c.addEventListener('mouseleave', _rptHideTip);
    })(stage, v, Math.round(v / total * 100));
    svg.appendChild(c);
    offset -= arc;
  });

  svg.appendChild(_rptSvgTxt(total, { x:cx, y:cy+8, 'text-anchor':'middle' }, 'rch-dv'));
  svg.appendChild(_rptSvgTxt('TOTAL POs', { x:cx, y:cy+22, 'text-anchor':'middle' }, 'rch-dl'));
  cont.appendChild(svg);

  // Legend (required for ≥2 series)
  RPT_STAGES.forEach(function(stage, i) {
    var row = document.createElement('div'); row.className = 'rpt-leg-row';
    var dot = document.createElement('span'); dot.className = 'rpt-leg-dot';
    dot.style.background = _rptCssVar(stage.ck);
    var lbl = document.createElement('span'); lbl.className = 'rpt-leg-l';
    lbl.appendChild(dot); lbl.appendChild(document.createTextNode(stage.label));
    var val = document.createElement('span'); val.className = 'rpt-leg-v';
    val.textContent = values[i];
    row.appendChild(lbl); row.appendChild(val);
    leg.appendChild(row);
  });
}

// ── Monthly bar chart ─────────────────────────────────────────────────────────
function _rptDrawBars(containerId, months, fillColor, tipLabel) {
  tipLabel = tipLabel || 'POs received';
  var cont = document.getElementById(containerId);
  if (!cont) return;
  cont.innerHTML = '';

  var VW = 380, VH = 158, ml = 28, mr = 6, mt = 10, mb = 24;
  var pw = VW - ml - mr, ph = VH - mt - mb;
  var maxV = Math.max.apply(null, months.map(function(m) { return m.v; }));
  if (!maxV) maxV = 1;
  var topTick = Math.ceil(maxV / 5) * 5 || 5;
  var svg = _rptSvgEl('svg', { viewBox:'0 0 '+VW+' '+VH, width:'100%', preserveAspectRatio:'xMidYMid meet' });

  for (var t = 0; t <= topTick; t += 5) {
    var y = mt + ph - (t / topTick) * ph;
    var gl = _rptSvgEl('line', { x1:ml, x2:ml+pw, y1:y, y2:y, 'stroke-width':'1' });
    gl.style.stroke = t === 0 ? _rptCssVar('--t3') : _rptCssVar('--s2');
    svg.appendChild(gl);
    if (t > 0) {
      svg.appendChild(_rptSvgTxt(t, { x:ml-4, y:y, 'text-anchor':'end', 'dominant-baseline':'central', 'font-variant-numeric':'tabular-nums' }, 'rch-ax'));
    }
  }

  var slotW = pw / months.length;
  var barW  = Math.min(22, slotW * 0.55);
  months.forEach(function(m, i) {
    var x    = ml + i * slotW + (slotW - barW) / 2;
    var barH = Math.max((m.v / topTick) * ph, 2);
    var yBar = mt + ph - barH;
    var rect = _rptSvgEl('rect', { x:x, y:yBar, width:barW, height:barH, rx:3 });
    rect.style.fill = fillColor; rect.style.opacity = '0.85'; rect.style.cursor = 'pointer';
    (function(mo) {
      rect.addEventListener('mousemove', function(e) { _rptShowTip(e, mo.lbl, mo.v + ' ' + tipLabel); });
      rect.addEventListener('mouseleave', _rptHideTip);
    })(m);
    svg.appendChild(rect);
    var skipLbl = months.length > 12 && i % 2 !== 0;
    if (!skipLbl) svg.appendChild(_rptSvgTxt(m.lbl, { x:x+barW/2, y:mt+ph+16, 'text-anchor':'middle' }, 'rch-ax'));
    if (m.v === maxV || i === months.length - 1) {
      svg.appendChild(_rptSvgTxt(m.v, { x:x+barW/2, y:yBar-5, 'text-anchor':'middle', 'font-weight':'700' }, 'rch-tip'));
    }
  });

  // Line overlay connecting bar tops
  var linePts = months.map(function(m, i) {
    var cx = ml + i * slotW + slotW / 2;
    var cy = mt + ph - (m.v / topTick) * ph;
    return cx + ',' + cy;
  }).join(' ');
  var lineEl = _rptSvgEl('polyline', { points: linePts, fill: 'none', stroke: fillColor, 'stroke-width': '1.5', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', opacity: '0.55' });
  svg.appendChild(lineEl);

  var ringColor = _rptCssVar('--s1');
  months.forEach(function(m, i) {
    var cx = ml + i * slotW + slotW / 2;
    var cy = mt + ph - (m.v / topTick) * ph;
    var dot = _rptSvgEl('circle', { cx: cx, cy: cy, r: '3.5' });
    dot.style.fill        = fillColor;
    dot.style.stroke      = ringColor;
    dot.style.strokeWidth = '1.5';
    (function(mo) {
      dot.addEventListener('mousemove', function(e) { _rptShowTip(e, mo.lbl, mo.v + ' ' + tipLabel); });
      dot.addEventListener('mouseleave', _rptHideTip);
    })(m);
    svg.appendChild(dot);
  });

  cont.appendChild(svg);
}

// ── Delivery performance ──────────────────────────────────────────────────────
function _rptDrawPerf(containerId, otd) {
  var cont = document.getElementById(containerId);
  if (!cont) return;
  var mt = otd.onTime + otd.late;
  var onPct  = mt > 0 ? Math.round(otd.onTime / mt * 100) : 0;
  var latePct = mt > 0 ? 100 - onPct : 0;
  cont.innerHTML = [
    '<div class="rpt-perf-rows">',
      '<div>',
        '<div class="rpt-perf-hd">',
          '<span class="rpt-perf-status"><span class="rpt-perf-icon" style="background:var(--ok)"></span>On time</span>',
          '<span class="rpt-perf-count">'+otd.onTime+' orders &middot; '+onPct+'%</span>',
        '</div>',
        '<div class="rpt-track"><div class="rpt-fill" style="width:'+onPct+'%;background:var(--ok)"></div></div>',
      '</div>',
      '<div>',
        '<div class="rpt-perf-hd">',
          '<span class="rpt-perf-status"><span class="rpt-perf-icon" style="background:var(--crit)"></span>Late</span>',
          '<span class="rpt-perf-count">'+otd.late+' orders &middot; '+latePct+'%</span>',
        '</div>',
        '<div class="rpt-track"><div class="rpt-fill" style="width:'+latePct+'%;background:var(--crit)"></div></div>',
      '</div>',
    '</div>',
    '<div class="rpt-metrics">',
      (otd.avgDays    != null ? '<div class="rpt-metric"><span class="rpt-metric-lbl">Avg days PO → delivery</span><span class="rpt-metric-val">'+otd.avgDays+' days</span></div>' : ''),
      (otd.avgLateDays!= null ? '<div class="rpt-metric"><span class="rpt-metric-lbl">Avg overdue (late orders)</span><span class="rpt-metric-val" style="color:var(--warn)">'+otd.avgLateDays+' days</span></div>' : ''),
      (otd.minDays    != null ? '<div class="rpt-metric"><span class="rpt-metric-lbl">Fastest delivery</span><span class="rpt-metric-val" style="color:var(--ok)">'+otd.minDays+' days</span></div>' : ''),
      (mt === 0 ? '<div class="rpt-metric"><span class="rpt-metric-lbl" style="color:var(--t3)">No closed orders with RDD and delivery date on record yet</span></div>' : ''),
    '</div>'
  ].join('');
}

// ── Top buyers bar chart ──────────────────────────────────────────────────────
function _rptDrawBuyers(containerId, buyers, fillColor) {
  var cont = document.getElementById(containerId);
  if (!cont) return;
  cont.innerHTML = '';
  if (!buyers.length) {
    cont.innerHTML = '<div style="padding:.5rem 0;color:var(--t3);font-size:12px">No requester data available</div>';
    return;
  }
  var rowH = 28, namePx = 130, countPx = 28, padR = 6;
  var VW = 340, VH = rowH * buyers.length + 4, barArea = VW - namePx - countPx - padR;
  var maxCount = buyers[0].count;
  var svg = _rptSvgEl('svg', { viewBox:'0 0 '+VW+' '+VH, width:'100%' });

  buyers.forEach(function(b, i) {
    var y    = i * rowH + 4;
    var barW = (b.count / maxCount) * barArea;
    var track = _rptSvgEl('rect', { x:namePx, y:y+9, width:barArea, height:8, rx:4 });
    track.style.fill = _rptCssVar('--s2');
    svg.appendChild(track);
    var fill = _rptSvgEl('rect', { x:namePx, y:y+9, width:barW, height:8, rx:4 });
    fill.style.fill = fillColor; fill.style.opacity = '0.85'; fill.style.cursor = 'pointer';
    (function(b2) {
      fill.addEventListener('mousemove', function(e) { _rptShowTip(e, b2.name, b2.count + ' POs'); });
      fill.addEventListener('mouseleave', _rptHideTip);
    })(b);
    svg.appendChild(fill);
    var short = b.name.length > 19 ? b.name.slice(0, 18) + '…' : b.name;
    svg.appendChild(_rptSvgTxt(short, { x:namePx-5, y:y+17, 'text-anchor':'end', 'dominant-baseline':'central' }, 'rch-ax'));
    svg.appendChild(_rptSvgTxt(b.count, { x:namePx+barArea+5, y:y+17, 'dominant-baseline':'central', 'font-weight':'700', 'font-variant-numeric':'tabular-nums' }, 'rch-tip'));
  });
  cont.appendChild(svg);
}

// ── Main updateReports ────────────────────────────────────────────────────────
function updateReports() {
  _rptEnsureTip();

  // ── Chevron ──
  var chClosed = ORDERS.filter(function(o) { return CLOSED_STATUSES.has(o.overall_status); });
  var chActive = ORDERS.filter(function(o) { return !CLOSED_STATUSES.has(o.overall_status); });
  var otdCh = _rptOtd(chClosed, 'order_line_items');

  _set('rpt-ch-total',     ORDERS.length);
  _set('rpt-ch-active',    chActive.length);
  _set('rpt-ch-delivered', chClosed.length);

  var chOtdEl = document.getElementById('rpt-ch-otd');
  if (chOtdEl) {
    chOtdEl.textContent = otdCh.score != null ? otdCh.score + '%' : '—';
    chOtdEl.style.color  = otdCh.score == null ? '' : otdCh.score >= 90 ? 'var(--ok)' : otdCh.score >= 75 ? 'var(--warn)' : 'var(--crit)';
  }
  _set('rpt-ch-otd-note', otdCh.total > 0 ? otdCh.onTime + ' / ' + otdCh.total + ' line items' : 'On-time delivery rate');

  var chBuckets = [0,0,0,0,0];
  ORDERS.forEach(function(o) { chBuckets[_rptBucketCh(o)]++; });
  _rptDrawDonut('rpt-donut-ch', 'rpt-leg-ch', chBuckets);

  _rptRedrawBars('ch');

  _rptDrawPerf('rpt-perf-ch', otdCh);
  _rptDrawBuyers('rpt-buyers-ch', _rptTopBuyers(ORDERS, 'buyer_name', 5), _rptCssVar('--accent'));

  // ── NLNG ──
  var nlClosed = NLNG_ORDERS.filter(function(o) { return o.overall_status === 'delivered' || !!o.delivered_at; });
  var nlActive = NLNG_ORDERS.filter(function(o) { return o.overall_status !== 'delivered' && !o.delivered_at; });
  var otdNl = _rptOtd(nlClosed, 'nlng_order_line_items');

  _set('rpt-nl-total',     NLNG_ORDERS.length);
  _set('rpt-nl-active',    nlActive.length);
  _set('rpt-nl-delivered', nlClosed.length);

  var nlOtdEl = document.getElementById('rpt-nl-otd');
  if (nlOtdEl) {
    nlOtdEl.textContent = otdNl.score != null ? otdNl.score + '%' : '—';
    nlOtdEl.style.color  = otdNl.score == null ? '' : otdNl.score >= 90 ? 'var(--ok)' : otdNl.score >= 75 ? 'var(--warn)' : 'var(--crit)';
  }
  _set('rpt-nl-otd-note', otdNl.total > 0 ? otdNl.onTime + ' / ' + otdNl.total + ' line items' : 'On-time delivery rate');

  var nlBuckets = [0,0,0,0,0];
  NLNG_ORDERS.forEach(function(o) { nlBuckets[_rptBucketNl(o)]++; });
  _rptDrawDonut('rpt-donut-nl', 'rpt-leg-nl', nlBuckets);

  _rptRedrawBars('nl');

  _rptDrawPerf('rpt-perf-nl', otdNl);
  _rptDrawBuyers('rpt-buyers-nl', _rptTopBuyers(NLNG_ORDERS, 'contact_name', 5), _rptCssVar('--ch1'));
}

function _set(id, val) {
  var el = document.getElementById(id);
  if (el) el.textContent = val;
}

// ── Recent orders card ────────────────────────────────────────────────────────
function renderRecentOrders() {
  var el = document.getElementById('recent-orders');
  if (!el) return;
  var top5 = ORDERS.slice().sort(function(a,b){
    var at = a.notification_received_at || ''; var bt = b.notification_received_at || '';
    return bt > at ? 1 : bt < at ? -1 : 0;
  }).slice(0, 5);
  if (!top5.length) {
    el.innerHTML = '<div style="padding:1.5rem;text-align:center;color:var(--t3);font-size:13px">No orders yet</div>';
    return;
  }
  var dotColor = function(o) {
    return CLOSED_STATUSES.has(o.overall_status) ? 'var(--ok)'
      : DISPATCH_STAGES.includes(o.overall_status) ? 'var(--warn)' : 'var(--accent)';
  };
  el.innerHTML = top5.map(function(o) {
    return '<div class="or">'
      + '<div class="or-dot" style="background:' + dotColor(o) + '"></div>'
      + '<div class="or-info">'
      + '<div class="or-po mono">' + (o.buyer_po_number || '&mdash;') + '</div>'
      + '<div class="or-desc">' + (function(){ var li=Array.isArray(o.order_line_items)&&o.order_line_items.length?o.order_line_items[0].description:null; return (li||o.extracted_description||'').slice(0,52)||'&mdash;'; })() + '</div>'
      + '</div>'
      + '<div class="or-val mono">' + (o.po_amount ? (o.po_currency === 'NGN' ? '₦' : '$') + Number(o.po_amount).toLocaleString() : '&mdash;') + '</div>'
      + '</div>';
  }).join('');
}

// ── Activity feed ─────────────────────────────────────────────────────────────
var ACTIVITY_DEFS = [
  { f:'delivered_at',                  col:'var(--ok)',     msg: function(o){ return 'Delivered &mdash; <span class="mono">' + o.buyer_po_number + '</span>'; } },
  { f:'delivery_requested_at',         col:'var(--ok)',     msg: function(o){ return 'Delivery requested &mdash; <span class="mono">' + o.buyer_po_number + '</span>'; } },
  { f:'dispatched_at',                 col:'var(--warn)',   msg: function(o){ return 'Shipping co. received &mdash; <span class="mono">' + (o.so_number || o.buyer_po_number) + '</span>'; } },
  { f:'ready_for_dispatch_at',         col:'var(--warn)',   msg: function(o){ return 'Dispatched to shipping co. &mdash; <span class="mono">' + (o.so_number || o.buyer_po_number) + '</span>'; } },
  { f:'dispatch_instructions_sent_at', col:'var(--warn)',   msg: function(o){ return 'Dispatch instructions sent for <span class="mono">' + (o.so_number || o.buyer_po_number) + '</span>'; } },
  { f:'flex_dispatch_ready_at',        col:'var(--warn)',   msg: function(o){ return 'Dispatch ready &mdash; <span class="mono">' + (o.so_number || o.buyer_po_number) + '</span> packed'; } },
  { f:'so_received_at',                col:'var(--accent)', msg: function(o){ return 'SO received &mdash; <span class="mono">' + (o.so_number || '?') + '</span> for <span class="mono">' + o.buyer_po_number + '</span>'; } },
  { f:'spm_po_sent_at',                col:'var(--accent)', msg: function(o){ var r = o.spm_po_number || '?'; return 'SPM PO sent &mdash; ref <span class="mono">' + (r.length > 20 ? r.slice(0,20) + '…' : r) + '</span>'; } },
  { f:'notification_received_at',      col:'var(--t3)',     msg: function(o){ return 'Chevron PO <span class="mono">' + o.buyer_po_number + '</span> received'; } }
];

function buildActivityFeed() {
  var el = document.getElementById('activity-feed');
  if (!el) return;
  var events = [];
  for (var oi = 0; oi < ORDERS.length; oi++) {
    var o = ORDERS[oi];
    for (var di = 0; di < ACTIVITY_DEFS.length; di++) {
      var def = ACTIVITY_DEFS[di];
      if (o[def.f]) events.push({ ts: new Date(o[def.f]), col: def.col, html: def.msg(o) });
    }
  }
  events.sort(function(a,b){ return b.ts - a.ts; });
  var top = events.slice(0, 8);
  if (!top.length) {
    el.innerHTML = '<div style="padding:1.5rem;text-align:center;color:var(--t3);font-size:13px">No activity yet</div>';
    return;
  }
  el.innerHTML = top.map(function(ev, i) {
    return '<div class="ai">'
      + '<div class="ai-track">'
      + '<div class="ai-dot" style="background:' + ev.col + '"></div>'
      + (i < top.length - 1 ? '<div class="ai-line"></div>' : '')
      + '</div>'
      + '<div class="ai-body">'
      + '<div class="ai-text">' + ev.html + '</div>'
      + '<div class="ai-time">' + ev.ts.toLocaleDateString('en-GB',{day:'2-digit',month:'short'}) + ' &middot; ' + ev.ts.toLocaleTimeString('en-GB',{hour:'2-digit',minute:'2-digit'}) + '</div>'
      + '</div>'
      + '</div>';
  }).join('');
}

// ── Build stage filter chips (status row only) ────────────────────────────────
function buildStatusChips() {
  var el = document.getElementById('status-chips');
  if (!el) return;
  var html = '';
  for (var i = 0; i < CHIP_STAGES.length; i++) {
    var st = CHIP_STAGES[i];
    html += '<div class="chip' + (_activeFilters.has(st.key) ? ' on' : '') + '" data-f="' + st.key + '" onclick="setF(this,\'' + st.key + '\')">' + st.label + '</div>';
  }
  el.innerHTML = html;
}

// ── Build + render sub-filter chips for the selected stage ────────────────────
function buildSubFilters(stageKey) {
  var bar   = document.getElementById('orders-sub-bar');
  var subEl = document.getElementById('sub-filter-chips');
  if (!bar || !subEl) return;

  var stage = getStage(stageKey);
  var subs  = getSubs(stage);

  if (!subs || subs.length === 0) {
    bar.classList.add('hidden');
    return;
  }

  bar.classList.remove('hidden');
  currentSubFilter = 'all';

  var html = '';
  for (var i = 0; i < subs.length; i++) {
    var sub = subs[i];
    html += '<button class="otd-chip' + (sub.key === currentSubFilter ? ' on' : '') + '" data-sub="' + sub.key + '" onclick="setSubFilter(this,\'' + sub.key + '\')">' + sub.label + '</button>';
  }
  subEl.innerHTML = html;
}

function setSubFilter(el, sub) {
  currentSubFilter = sub;
  document.querySelectorAll('#sub-filter-chips .otd-chip').forEach(function(c){ c.classList.remove('on'); });
  el.classList.add('on');
  filterOrders();
}

// ── Orders table — column headers ─────────────────────────────────────────────
function buildHeaders() {
  var hd = document.getElementById('ot-head-row');
  if (!hd) return;
  var html = '<th class="th-cb"><input type="checkbox" id="cb-all" style="cursor:pointer;accent-color:var(--accent)"></th>';
  for (var i = 0; i < COLS.length; i++) {
    var col = COLS[i];
    var cls = col.sticky ? 'td-sticky' : '';
    var align = col.key === 'po_amount' ? ' style="text-align:right"' : '';
    html += '<th class="' + cls + '" data-col="' + col.key + '"' + align + '>' + col.hdr + ' <span class="sort-ind"></span></th>';
  }
  html += '<th style="width:32px"></th>'; // message action column
  hd.innerHTML = html;
}

function updateSortHeaders() {
  var ths = document.querySelectorAll('#ot-head-row th[data-col]');
  for (var i = 0; i < ths.length; i++) {
    var th = ths[i];
    th.classList.remove('sort-asc','sort-desc');
    if (th.dataset.col === _sortCol) {
      th.classList.add(_sortDir === 'asc' ? 'sort-asc' : 'sort-desc');
    }
  }
}

// ── Period filter state ───────────────────────────────────────────────────────
var _receivedFrom       = '';   // 'YYYY-MM-DD' — orders table Received from
var _receivedTo         = '';   // 'YYYY-MM-DD' — orders table Received to
var _otdPeriodFilter    = 'all';
var _otdDeliveredSub    = 'all'; // 'all' | 'del-otd' | 'del-late'

function applyPeriodFilter(o, filter) {
  if (!filter || filter === 'all') return true;
  var ts = o.notification_received_at;
  if (!ts) return false;
  var d   = new Date(ts);
  var now = new Date();
  var sp  = filter.slice(4); // strip 'rcv_'
  if (sp === '24h')   return (now - d) <= 86400000;
  if (sp === 'week')  return (now - d) <= 7  * 86400000;
  if (sp === '2wk')   return (now - d) <= 14 * 86400000;
  if (sp === 'month') return (now - d) <= 30 * 86400000;
  if (sp.length === 7) { var yr = +sp.slice(0,4), mo = +sp.slice(5,7) - 1; return d.getFullYear() === yr && d.getMonth() === mo; }
  return true;
}

// Build [{key, label}] period chip items from ORDERS data
function _periodItems(dataArr) {
  var data = dataArr || ORDERS;
  var items = [
    {key:'all',       label:'All'},
    {key:'rcv_24h',   label:'24 hrs'},
    {key:'rcv_week',  label:'1 wk'},
    {key:'rcv_2wk',   label:'2 wks'},
    {key:'rcv_month', label:'1 mo'}
  ];
  var seen = {};
  for (var i = 0; i < data.length; i++) {
    var v = data[i].notification_received_at;
    if (!v) continue;
    var d = new Date(v);
    var mk = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
    seen[mk] = d.toLocaleDateString('en-GB', {month:'short', year:'2-digit'});
  }
  Object.keys(seen).sort().reverse().forEach(function(k) {
    items.push({key:'rcv_' + k, label:seen[k]});
  });
  return items;
}

function _periodLabel(key, dataArr) {
  if (!key || key === 'all') return 'All';
  var items = _periodItems(dataArr);
  for (var i = 0; i < items.length; i++) { if (items[i].key === key) return items[i].label; }
  return 'All';
}

// ── Generic date-range picker helpers ────────────────────────────────────────
function drFmtLabel(from, to) {
  if (!from && !to) return 'All ▾';
  var s = function(d) { var p = d.split('-'); return p[1] + '/' + p[2]; };
  if (from && to)  return s(from) + ' – ' + s(to) + ' ▾';
  if (from)        return s(from) + ' → ▾';
  return '→ ' + s(to) + ' ▾';
}

function toggleDrPicker(wrapId) {
  var pop = document.getElementById(wrapId + '-pop');
  if (!pop) return;
  var opening = pop.classList.contains('hidden');
  document.querySelectorAll('.dr-pop').forEach(function(p) { p.classList.add('hidden'); });
  if (opening) pop.classList.remove('hidden');
}

document.addEventListener('click', function(e) {
  if (!e.target.closest('.dr-wrap')) {
    document.querySelectorAll('.dr-pop').forEach(function(p) { p.classList.add('hidden'); });
  }
});

// ── Orders — Received range ───────────────────────────────────────────────────
function onOrdersPeriodChange() {
  _receivedFrom = (document.getElementById('orders-period-from') || {}).value || '';
  _receivedTo   = (document.getElementById('orders-period-to')   || {}).value || '';
  var btn = document.getElementById('orders-period-btn');
  if (btn) btn.textContent = drFmtLabel(_receivedFrom, _receivedTo);
  filterOrders();
}

function clearOrdersPeriod() {
  _receivedFrom = ''; _receivedTo = '';
  var f = document.getElementById('orders-period-from');
  var t = document.getElementById('orders-period-to');
  if (f) f.value = '';
  if (t) t.value = '';
  var btn = document.getElementById('orders-period-btn');
  if (btn) btn.textContent = 'All ▾';
  document.querySelectorAll('.dr-pop').forEach(function(p) { p.classList.add('hidden'); });
  filterOrders();
}

function applyOrdersReceivedRange(o) {
  if (!_receivedFrom && !_receivedTo) return true;
  var ts = o.notification_received_at;
  if (!ts) return false;
  var d = String(ts).slice(0, 10);
  if (_receivedFrom && d < _receivedFrom) return false;
  if (_receivedTo   && d > _receivedTo)   return false;
  return true;
}

function buildOtdPeriodChips() {
  var menu = document.getElementById('otd-period-menu');
  var btn  = document.getElementById('otd-period-btn');
  if (!menu) return;
  menu.innerHTML = _periodItems().map(function(c) {
    return '<div class="period-dd-item' + (_otdPeriodFilter === c.key ? ' on' : '')
      + '" onclick="setOtdPeriodFilter(\'' + c.key + '\')">' + c.label + '</div>';
  }).join('');
  if (btn) btn.textContent = _periodLabel(_otdPeriodFilter) + ' ▾';
}

function toggleOtdPeriodDd() {
  var menu = document.getElementById('otd-period-menu');
  if (menu) menu.classList.toggle('hidden');
}

function setOtdPeriodFilter(f) {
  _otdPeriodFilter = f;
  var menu = document.getElementById('otd-period-menu');
  if (menu) menu.classList.add('hidden');
  buildOtdPeriodChips();
  _otdPage = 1;
  renderOTD();
}

function setOtdDeliveredSub(sf) {
  _otdDeliveredSub = sf;
  document.querySelectorAll('#otd-del-sub-bar .otd-chip').forEach(function(c) {
    c.classList.toggle('on', c.dataset.sf === sf);
  });
  _otdPage = 1;
  renderOTD();
}

// ── NLNG OTD TRACKER ─────────────────────────────────────────────────────────

var NLNG_OTD_STAGES = [
  { key:'notification_received_at',      lbl:'Received'    },
  { key:'sent_to_warehouse_at',          lbl:'To Warehouse' },
  { key:'stock_check_completed_at',      lbl:'Stock ✓'     },
  { key:'spm_po_sent_at',                lbl:'PO → Flex'   },
  { key:'so_received_at',                lbl:'SO Rcvd'     },
  { key:'so_sent_to_warehouse_at',       lbl:'WH Fwd'      },
  { key:'flex_dispatch_ready_at',        lbl:'Packed'      },
  { key:'dispatch_instructions_sent_at', lbl:'Instr Sent'  },
  { key:'ready_for_dispatch_at',         lbl:'Coll. Arr.'  },
  { key:'dispatched_at',                 lbl:'Dispatched'  },
  { key:'delivered_at',                  lbl:'Delivered'   }
];

function buildNlngOtdTimeline(o) {
  var lastDone = -1;
  NLNG_OTD_STAGES.forEach(function(sf, i) { if (o[sf.key]) lastDone = i; });
  var currIdx = lastDone + 1;
  if (lastDone === NLNG_OTD_STAGES.length - 1) currIdx = lastDone;
  var h = '';
  NLNG_OTD_STAGES.forEach(function(sf, i) {
    var val    = o[sf.key];
    var isDone = !!val;
    var isCurr = i === currIdx && !isDone;
    var dotCls = isCurr ? 'curr' : (isDone ? 'done' : 'pend');
    h += '<div class="otn"><div class="otd-dot ' + dotCls + '"></div>';
    h += '<div class="otl-lbl"><div class="nm">' + sf.lbl + '</div>';
    if (val)       h += '<div class="dt">' + fmtOtdShort(val) + '</div>';
    else if (isCurr) h += '<div class="cu">now</div>';
    h += '</div></div>';
    if (i < NLNG_OTD_STAGES.length - 1) {
      var nextVal = o[NLNG_OTD_STAGES[i+1].key];
      var dur = (val && nextVal) ? fmtDur(new Date(nextVal) - new Date(val))
              : (val && i === lastDone && !o.delivered_at) ? fmtDur(new Date() - new Date(val))
              : '…';
      var lineCls = (isDone && !isCurr) ? 'done' : (isCurr ? 'curr' : '');
      h += '<div class="otc"><div class="otl-line ' + lineCls + '"></div>';
      h += '<div class="otl-dur">' + dur + '</div></div>';
    }
  });
  return h;
}

function buildNlngOtdExpand(o) {
  var age  = o.notification_received_at ? fmtDur(new Date() - new Date(o.notification_received_at)) : '—';
  var done = NLNG_OTD_STAGES.filter(function(sf){ return !!o[sf.key]; }).length;
  var h = '<div class="oxi"><div class="oxi-ttl">Stage Timeline — elapsed time between each pipeline step</div>';
  h += '<div class="otl">' + buildNlngOtdTimeline(o) + '</div>';
  h += '<div class="oxm">';
  h += '<div class="oxmi"><div class="k">Pipeline Age</div><div class="v">' + age + '</div></div>';
  h += '<div class="oxmi"><div class="k">Stages Done</div><div class="v">' + done + ' / ' + NLNG_OTD_STAGES.length + '</div></div>';
  if (o.notification_received_at) h += '<div class="oxmi"><div class="k">Received</div><div class="v">'    + fmtOtdDate(o.notification_received_at) + '</div></div>';
  if (o.required_delivery_date)   h += '<div class="oxmi"><div class="k">Required By</div><div class="v">' + fmtOtdDate(o.required_delivery_date)   + '</div></div>';
  if (o.promised_date)            h += '<div class="oxmi"><div class="k">Promised Date</div><div class="v">' + fmtOtdDate(o.promised_date)           + '</div></div>';
  if (o.delivered_at)             h += '<div class="oxmi"><div class="k">Delivered</div><div class="v">'    + fmtOtdDate(o.delivered_at)             + '</div></div>';
  h += '</div></div>';
  return h;
}

function updateNlngOtdSortHeaders() {
  var ths = document.querySelectorAll('#nlng-otd-head-row th[data-col]');
  for (var i = 0; i < ths.length; i++) {
    var th = ths[i];
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.dataset.col === _nlngOtdSortCol) {
      th.classList.add(_nlngOtdSortDir === 'asc' ? 'sort-asc' : 'sort-desc');
    }
  }
}

function sortNlngOtdBy(col) {
  if (_nlngOtdSortCol === col) { _nlngOtdSortDir = _nlngOtdSortDir === 'asc' ? 'desc' : 'asc'; }
  else { _nlngOtdSortCol = col; _nlngOtdSortDir = 'asc'; }
  updateNlngOtdSortHeaders();
  _nlngOtdPage = 1;
  renderNlngOTD();
}

function initNlngOtdSortEvents() {
  var hd = document.getElementById('nlng-otd-head-row');
  if (!hd) return;
  hd.addEventListener('click', function(e) {
    var th = e.target.closest('th[data-col]');
    if (th) sortNlngOtdBy(th.dataset.col);
  });
  hd.style.cursor = 'pointer';
  updateNlngOtdSortHeaders();
}

function buildNlngOtdPeriodChips() {
  var menu = document.getElementById('nlng-otd-period-menu');
  var btn  = document.getElementById('nlng-otd-period-btn');
  if (!menu) return;
  menu.innerHTML = _periodItems(NLNG_ORDERS).map(function(c) {
    return '<div class="period-dd-item' + (_nlngOtdPeriodFilter === c.key ? ' on' : '')
      + '" onclick="setNlngOtdPeriodFilter(\'' + c.key + '\')">' + c.label + '</div>';
  }).join('');
  if (btn) btn.textContent = _periodLabel(_nlngOtdPeriodFilter, NLNG_ORDERS) + ' ▾';
}

function toggleNlngOtdPeriodDd() {
  var menu = document.getElementById('nlng-otd-period-menu');
  if (menu) menu.classList.toggle('hidden');
}

function setNlngOtdPeriodFilter(f) {
  _nlngOtdPeriodFilter = f;
  var menu = document.getElementById('nlng-otd-period-menu');
  if (menu) menu.classList.add('hidden');
  buildNlngOtdPeriodChips();
  _nlngOtdPage = 1;
  renderNlngOTD();
}

function setNlngOtdDeliveredSub(sf) {
  _nlngOtdDeliveredSub = sf;
  document.querySelectorAll('#nlng-otd-del-sub-bar .otd-chip').forEach(function(c) {
    c.classList.toggle('on', c.dataset.sf === sf);
  });
  _nlngOtdPage = 1;
  renderNlngOTD();
}

function setNlngOtdFilter(f) {
  _nlngOtdFilter = f;
  _nlngOtdDeliveredSub = 'all';
  _nlngOtdPage = 1;
  document.querySelectorAll('#nlng-otd-chips .otd-chip').forEach(function(c) {
    c.classList.toggle('on', c.dataset.f === f);
  });
  var subBar = document.getElementById('nlng-otd-del-sub-bar');
  if (subBar) {
    subBar.classList.toggle('hidden', f !== 'delivered');
    subBar.querySelectorAll('.otd-chip').forEach(function(c) {
      c.classList.toggle('on', c.dataset.sf === 'all');
    });
  }
  renderNlngOTD();
}

function renderNlngOTD() {
  if (_otdIsInteracting()) { _otdPendingRender = true; return; }
  updateNlngOtdSortHeaders();
  var orders = NLNG_ORDERS;
  var tbody = document.getElementById('nlng-otd-body');
  if (!tbody) return;
  if (!orders || !orders.length) {
    tbody.innerHTML = '<tr><td colspan="13" style="text-align:center;padding:3rem;color:var(--t3)">No NLNG orders loaded</td></tr>';
    return;
  }

  var counts = {};
  var rows   = [];

  orders.forEach(function(o, idx) {
    var cls = otdClass(o);
    var lastTs = null;
    for (var i = NLNG_OTD_STAGES.length - 1; i >= 0; i--) {
      if (o[NLNG_OTD_STAGES[i].key]) { lastTs = o[NLNG_OTD_STAGES[i].key]; break; }
    }
    var inStage = (lastTs && !o.delivered_at) ? fmtDur(new Date() - new Date(lastTs)) : '—';
    var rcvdAgo = o.notification_received_at ? fmtDur(new Date() - new Date(o.notification_received_at)) : '—';
    var pastStock = !!o.spm_po_sent_at;
    var stageLbl;
    if (!pastStock && isPartialStock(o)) {
      stageLbl = 'Partial stock';
    } else if (!pastStock && isNotInStock(o)) {
      stageLbl = 'Not in stock';
    } else if (!pastStock && o.overall_status === 'stock_check_needs_review') {
      var rawSnip = extractStockRaw(o.stock_check_raw);
      stageLbl = rawSnip ? 'Review: ' + rawSnip.replace(/[\r\n]+/g,' ').slice(0, 28) + '…' : 'Needs review';
    } else {
      stageLbl = (NLNG_STAGE_MAP[o.overall_status] || {}).lbl || (o.overall_status || '—');
    }
    var lastTsMs = lastTs ? new Date(lastTs).getTime() : 0;
    rows.push({ cls:cls, idx:idx, o:o, inStage:inStage, rcvdAgo:rcvdAgo, stageLbl:stageLbl, lastTsMs:lastTsMs });
  });

  var _nlngOtdQ = ((document.getElementById('nlng-otd-q') || {}).value || '').trim().toLowerCase();
  if (_nlngOtdQ) {
    rows = rows.filter(function(row) {
      var o = row.o;
      return (o.po_number || '').toLowerCase().indexOf(_nlngOtdQ) >= 0
          || (o.description || '').toLowerCase().indexOf(_nlngOtdQ) >= 0
          || (o.spm_po_number || '').toLowerCase().indexOf(_nlngOtdQ) >= 0;
    });
  }

  // Apply period filter before counting so chips reflect the active window
  var _now = new Date();
  var periodRows = _nlngOtdPeriodFilter === 'all' ? rows : rows.filter(function(row) {
    var rts = row.o.notification_received_at;
    if (!rts) return false;
    var rd = new Date(rts);
    var sp = _nlngOtdPeriodFilter.slice(4);
    if (sp === '24h')   return (_now - rd) <= 86400000;
    if (sp === 'week')  return (_now - rd) <= 7  * 86400000;
    if (sp === '2wk')   return (_now - rd) <= 14 * 86400000;
    if (sp === 'month') return (_now - rd) <= 30 * 86400000;
    if (sp.length === 7) { var yr2 = +sp.slice(0,4), mo2 = +sp.slice(5,7)-1; return rd.getFullYear() === yr2 && rd.getMonth() === mo2; }
    return true;
  });
  periodRows.forEach(function(row) { counts[row.cls] = (counts[row.cls] || 0) + 1; });

  var OTD_CLS_ORDER = {'on-track':1,'at-risk':2,'overdue':3,'critical':4,'del-otd':5,'del-late':6,'no-date':7};
  periodRows.sort(function(a, b) {
    var col = _nlngOtdSortCol;
    var dir = _nlngOtdSortDir === 'asc' ? 1 : -1;
    var av, bv;
    if (col === '_in_stage') { av = a.lastTsMs; bv = b.lastTsMs; return (av - bv) * dir; }
    if (col === '_li_count') {
      av = (a.o.nlng_order_line_items || []).length || 1;
      bv = (b.o.nlng_order_line_items || []).length || 1;
      return (av - bv) * dir;
    }
    if (col === '_otd') {
      av = OTD_CLS_ORDER[a.cls] || 9; bv = OTD_CLS_ORDER[b.cls] || 9;
      return (av - bv) * dir;
    }
    if (col === '_days_left') {
      av = getOtdDate(a.o); bv = getOtdDate(b.o);
      av = av ? new Date(av).getTime() : 0; bv = bv ? new Date(bv).getTime() : 0;
      return (av - bv) * dir;
    }
    if (col === '_gap') {
      av = a.o.promised_date ? new Date(a.o.promised_date).getTime() : 0;
      bv = b.o.promised_date ? new Date(b.o.promised_date).getTime() : 0;
      return (av - bv) * dir;
    }
    if (col === 'overall_status') {
      av = a.stageLbl || ''; bv = b.stageLbl || '';
      return av.localeCompare(bv, undefined, {sensitivity:'base'}) * dir;
    }
    av = a.o[col] != null ? a.o[col] : '';
    bv = b.o[col] != null ? b.o[col] : '';
    return String(av).localeCompare(String(bv), undefined, {numeric:true, sensitivity:'base'}) * dir;
  });

  var el = document.getElementById('nlng-otd-c-ok');   if (el) el.textContent = counts['on-track'] || 0;
  el = document.getElementById('nlng-otd-c-risk');      if (el) el.textContent = counts['at-risk']  || 0;
  el = document.getElementById('nlng-otd-c-crit');      if (el) el.textContent = (counts['overdue'] || 0) + (counts['critical'] || 0);
  el = document.getElementById('nlng-otd-c-del');       if (el) el.textContent = (counts['del-otd'] || 0) + (counts['del-late'] || 0);

  // NLNG OTD score — weighted by line item count
  var nlngLiOtd = 0, nlngLiLate = 0;
  periodRows.forEach(function(row) {
    if (row.cls !== 'del-otd' && row.cls !== 'del-late') return;
    var n = (row.o.nlng_order_line_items || []).length || 1;
    if (row.cls === 'del-otd') nlngLiOtd += n; else nlngLiLate += n;
  });
  var nlngLiTotal = nlngLiOtd + nlngLiLate;
  var nlngScoreCard = document.getElementById('nlng-otd-score-card');
  el = document.getElementById('nlng-otd-c-score');
  if (el) el.textContent = nlngLiTotal ? Math.round(nlngLiOtd / nlngLiTotal * 100) + '%' : '—';
  el = document.getElementById('nlng-otd-c-score-sub');
  if (el) el.textContent = nlngLiTotal ? nlngLiOtd + ' / ' + nlngLiTotal + ' line items' : 'no deliveries yet';
  if (nlngScoreCard) {
    nlngScoreCard.classList.remove('c-ok', 'c-warn', 'c-crit', 'c-grey');
    if (!nlngLiTotal)                          nlngScoreCard.classList.add('c-grey');
    else if (nlngLiOtd / nlngLiTotal >= 0.9)   nlngScoreCard.classList.add('c-ok');
    else if (nlngLiOtd / nlngLiTotal >= 0.7)   nlngScoreCard.classList.add('c-warn');
    else                                        nlngScoreCard.classList.add('c-crit');
  }

  var visRows = periodRows.filter(function(row) {
    if (_nlngOtdFilter !== 'all') {
      var cls = row.cls;
      if (_nlngOtdFilter === 'on-track'  && cls !== 'on-track')  return false;
      if (_nlngOtdFilter === 'at-risk'   && cls !== 'at-risk')   return false;
      if (_nlngOtdFilter === 'overdue'   && cls !== 'overdue' && cls !== 'critical') return false;
      if (_nlngOtdFilter === 'critical'  && cls !== 'critical')  return false;
      if (_nlngOtdFilter === 'delivered') {
        if (cls !== 'del-otd' && cls !== 'del-late') return false;
        if (_nlngOtdDeliveredSub === 'del-otd'  && cls !== 'del-otd')  return false;
        if (_nlngOtdDeliveredSub === 'del-late' && cls !== 'del-late') return false;
      }
    }
    return true;
  });

  var nlngOtdPages = Math.max(1, Math.ceil(visRows.length / OTD_PER_PAGE));
  if (_nlngOtdPage > nlngOtdPages) _nlngOtdPage = 1;
  var pageStart = (_nlngOtdPage - 1) * OTD_PER_PAGE;
  var pageRows  = visRows.slice(pageStart, pageStart + OTD_PER_PAGE);

  var html = '';
  pageRows.forEach(function(row) {
    var promised = row.o.promised_date ? fmtOtdDate(row.o.promised_date) : '<span style="color:var(--t3)">—</span>';
    html += '<tr class="odr ' + row.cls + '" data-idx="' + row.idx + '" data-cls="' + row.cls + '">';
    html += '<td style="width:32px"><button class="oxbtn" data-idx="' + row.idx + '">▶</button></td>';
    // NLNG PO — Gmail search link (same pattern as Chevron)
    var gmailSearchNlngPO = 'https://mail.google.com/mail/?authuser=specialpiping%40gmail.com#search/' + encodeURIComponent(row.o.po_number || '');
    var poVal = row.o.po_number || '—';
    html += '<td><div class="odr-po"><a href="' + gmailSearchNlngPO + '" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="Search Gmail for this PO" style="color:inherit;text-decoration:none;border-bottom:1px dotted var(--accent)">' + poVal + '</a></div>'
          + (row.o.net_value ? '<div class="odr-amt">' + (row.o.currency||'USD') + ' ' + parseFloat(row.o.net_value).toLocaleString('en-US',{maximumFractionDigits:0}) + '</div>' : '')
          + '</td>';
    // SO Number — Gmail search link
    if (row.o.so_number) {
      var gmailSearchNlngSO = 'https://mail.google.com/mail/?authuser=specialpiping%40gmail.com#search/' + encodeURIComponent(row.o.so_number);
      html += '<td><div class="odr-po" style="font-size:11px"><a href="' + gmailSearchNlngSO + '" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="Search Gmail for ' + row.o.so_number + '" style="color:inherit;text-decoration:none;border-bottom:1px dotted var(--accent)">' + row.o.so_number + '</a></div></td>';
    } else {
      html += '<td><span style="color:var(--t3)">—</span></td>';
    }
    html += '<td title="' + (row.o.contact_name||'') + '" style="max-width:110px;overflow:hidden;text-overflow:ellipsis">' + n(row.o.contact_name) + '</td>';
    html += '<td><div class="odc">' + fmtOtdDate(row.o.notification_received_at) + '<div class="odc-ago">' + row.rcvdAgo + ' ago</div></div></td>';
    html += '<td><div class="odc">' + fmtOtdDate(row.o.required_delivery_date) + '</div></td>';
    html += '<td class="c">' + dlCellHtml(row.o) + '</td>';
    html += '<td><div class="odc">' + promised + '</div></td>';
    html += '<td>' + gapCellHtml(row.o) + '</td>';
    html += '<td><span class="osp" title="' + row.stageLbl + '">' + row.stageLbl + '</span></td>';
    html += '<td><span class="otin">' + row.inStage + '</span></td>';
    var nlngLiCnt = (row.o.nlng_order_line_items || []).length || 1;
    html += '<td class="c">' + liCountCellHtml(nlngLiCnt) + '</td>';
    html += '<td><span class="obd ' + row.cls + '">' + otdLabel(row.cls) + '</span></td>';
    html += '</tr>';
    html += '<tr class="oxr hidden" data-idx="' + row.idx + '"><td colspan="13">' + buildNlngOtdExpand(row.o) + '</td></tr>';
  });

  if (!pageRows.length) {
    tbody.innerHTML = '<tr><td colspan="13" style="text-align:center;padding:3rem;color:var(--t3)">No orders match the current filter</td></tr>';
  } else {
    tbody.innerHTML = html;
  }

  var pg = document.getElementById('nlng-otd-pagination');
  if (pg) {
    pg.innerHTML =
      '<div class="pg-l">'
      + '<button class="pg-btn" id="nlng-otd-pg-prev"' + (_nlngOtdPage <= 1 ? ' disabled' : '') + '>← Prev</button>'
      + '<span>Page <strong>' + _nlngOtdPage + '</strong> of <strong>' + nlngOtdPages + '</strong></span>'
      + '<button class="pg-btn" id="nlng-otd-pg-next"' + (_nlngOtdPage >= nlngOtdPages ? ' disabled' : '') + '>Next →</button>'
      + '</div>'
      + '<div class="pg-r">'
      + '<span class="pg-rows">' + OTD_PER_PAGE + ' rows</span>'
      + '<span class="pg-count">' + visRows.length + ' record' + (visRows.length !== 1 ? 's' : '') + '</span>'
      + '</div>';
    var pp = document.getElementById('nlng-otd-pg-prev');
    var pn = document.getElementById('nlng-otd-pg-next');
    if (pp) pp.addEventListener('click', function() { if (_nlngOtdPage > 1) { _nlngOtdPage--; renderNlngOTD(); } });
    if (pn) pn.addEventListener('click', function() { if (_nlngOtdPage < nlngOtdPages) { _nlngOtdPage++; renderNlngOTD(); } });
  }

  tbody.querySelectorAll('.oxbtn').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      var idx = btn.dataset.idx;
      var xr  = tbody.querySelector('tr.oxr[data-idx="' + idx + '"]');
      var open = !xr.classList.contains('hidden');
      if (open) { xr.classList.add('hidden');    btn.textContent = '▶'; btn.classList.remove('open'); if (!_otdIsInteracting()) _otdFlushPending(); }
      else       { xr.classList.remove('hidden'); btn.textContent = '▼'; btn.classList.add('open'); }
    });
  });
  tbody.querySelectorAll('tr.odr').forEach(function(tr) {
    tr.addEventListener('click', function(e) {
      if (e.target.closest('.oxbtn')) return;
      tr.querySelector('.oxbtn').click();
    });
  });
}

// ── NLNG table functions ──────────────────────────────────────────────────────

function buildNlngHeaders() {
  var hd = document.getElementById('nlng-head-row');
  if (!hd) return;
  var html = '<th class="th-cb"><input type="checkbox" id="nlng-cb-all" style="cursor:pointer;accent-color:var(--accent)"></th>';
  for (var i = 0; i < NLNG_COLS.length; i++) {
    var col = NLNG_COLS[i];
    var cls = col.sticky ? 'td-sticky' : '';
    var align = col.isNlngAmt ? ' style="text-align:right"' : '';
    html += '<th class="' + cls + '" data-nc="' + col.key + '"' + align + '>' + col.hdr + ' <span class="sort-ind"></span></th>';
  }
  html += '<th style="width:32px"></th>';
  hd.innerHTML = html;
  // Sort on column click — assign (not addEventListener) to avoid duplicate listeners on refresh
  hd.onclick = function(e) {
    var th = e.target.closest('th[data-nc]');
    if (!th) return;
    var col = th.dataset.nc;
    if (_nlngSortCol === col) { _nlngSortDir = _nlngSortDir === 'asc' ? 'desc' : 'asc'; }
    else { _nlngSortCol = col; _nlngSortDir = 'asc'; }
    document.querySelectorAll('#nlng-head-row th').forEach(function(t){ t.classList.remove('sort-asc','sort-desc'); });
    th.classList.add(_nlngSortDir === 'asc' ? 'sort-asc' : 'sort-desc');
    filterNlng(true);
  };
  hd.addEventListener('change', function(e) {
    if (e.target.id !== 'nlng-cb-all') return;
    var start = (_nlngPage - 1) * PER_PAGE;
    var pageRows = _nlngFiltered.slice(start, start + PER_PAGE);
    pageRows.forEach(function(o) {
      if (e.target.checked) _nlngSelected.add(o.id);
      else _nlngSelected.delete(o.id);
    });
    renderNlngTable();
    updateNlngSelectUI();
  });
}

function buildNlngStatusChips() {
  var el = document.getElementById('nlng-status-chips');
  if (!el) return;
  var html = '';
  for (var i = 0; i < NLNG_CHIP_STAGES.length; i++) {
    var st = NLNG_CHIP_STAGES[i];
    html += '<div class="chip' + (_nlngActiveFilter === st.key ? ' on' : '') + '" data-nf="' + st.key + '" onclick="setNlngF(\'' + st.key + '\')">' + st.label + '</div>';
  }
  el.innerHTML = html;
}

function buildNlngSubFilters(stageKey) {
  var bar = document.getElementById('nlng-sub-bar');
  var subEl = document.getElementById('nlng-sub-chips');
  if (!bar || !subEl) return;
  var stage = null;
  for (var i = 0; i < NLNG_CHIP_STAGES.length; i++) {
    if (NLNG_CHIP_STAGES[i].key === stageKey) { stage = NLNG_CHIP_STAGES[i]; break; }
  }
  var subs = stage ? stage.subs : [];
  if (!subs || subs.length === 0) { bar.classList.add('hidden'); return; }
  bar.classList.remove('hidden');
  _nlngSubFilter = 'all';
  var html = '';
  for (var j = 0; j < subs.length; j++) {
    var sub = subs[j];
    html += '<button class="otd-chip' + (sub.key === 'all' ? ' on' : '') + '" data-ns="' + sub.key + '" onclick="setNlngSubFilter(this,\'' + sub.key + '\')">' + sub.label + '</button>';
  }
  subEl.innerHTML = html;
}

function setNlngSubFilter(el, sub) {
  _nlngSubFilter = sub;
  document.querySelectorAll('#nlng-sub-chips .otd-chip').forEach(function(c){ c.classList.remove('on'); });
  el.classList.add('on');
  filterNlng();
}

function setNlngF(f) {
  _nlngActiveFilter = f;
  document.querySelectorAll('#nlng-status-chips .chip').forEach(function(c){
    c.classList.toggle('on', c.dataset.nf === f);
  });
  var bar = document.getElementById('nlng-sub-bar');
  if (bar) bar.classList.add('hidden');
  filterNlng();
}

// ── NLNG — Received range ─────────────────────────────────────────────────────
function onNlngPeriodChange() {
  _nlngReceivedFrom = (document.getElementById('nlng-period-from') || {}).value || '';
  _nlngReceivedTo   = (document.getElementById('nlng-period-to')   || {}).value || '';
  var btn = document.getElementById('nlng-period-btn');
  if (btn) btn.textContent = drFmtLabel(_nlngReceivedFrom, _nlngReceivedTo);
  filterNlng();
}

function clearNlngPeriod() {
  _nlngReceivedFrom = ''; _nlngReceivedTo = '';
  var f = document.getElementById('nlng-period-from');
  var t = document.getElementById('nlng-period-to');
  if (f) f.value = '';
  if (t) t.value = '';
  var btn = document.getElementById('nlng-period-btn');
  if (btn) btn.textContent = 'All ▾';
  document.querySelectorAll('.dr-pop').forEach(function(p) { p.classList.add('hidden'); });
  filterNlng();
}

// ── NLNG — Promised range ─────────────────────────────────────────────────────
var _nlngPromisedFrom = '';
var _nlngPromisedTo   = '';

function onNlngPromisedChange() {
  _nlngPromisedFrom = (document.getElementById('nlng-promised-from') || {}).value || '';
  _nlngPromisedTo   = (document.getElementById('nlng-promised-to')   || {}).value || '';
  var btn = document.getElementById('nlng-promised-btn');
  if (btn) btn.textContent = drFmtLabel(_nlngPromisedFrom, _nlngPromisedTo);
  filterNlng();
}

function clearNlngPromised() {
  _nlngPromisedFrom = ''; _nlngPromisedTo = '';
  var f = document.getElementById('nlng-promised-from');
  var t = document.getElementById('nlng-promised-to');
  if (f) f.value = '';
  if (t) t.value = '';
  var btn = document.getElementById('nlng-promised-btn');
  if (btn) btn.textContent = 'All ▾';
  document.querySelectorAll('.dr-pop').forEach(function(p) { p.classList.add('hidden'); });
  filterNlng();
}

function applyNlngPromisedRange(o) {
  if (!_nlngPromisedFrom && !_nlngPromisedTo) return true;
  var ts = o.promised_date;
  if (!ts) return false;
  var d = String(ts).slice(0, 10);
  if (_nlngPromisedFrom && d < _nlngPromisedFrom) return false;
  if (_nlngPromisedTo   && d > _nlngPromisedTo)   return false;
  return true;
}

function applyNlngReceivedRange(o) {
  if (!_nlngReceivedFrom && !_nlngReceivedTo) return true;
  var ts = o.notification_received_at;
  if (!ts) return false;
  var d = String(ts).slice(0, 10);
  if (_nlngReceivedFrom && d < _nlngReceivedFrom) return false;
  if (_nlngReceivedTo   && d > _nlngReceivedTo)   return false;
  return true;
}

function filterNlng(keepPage) {
  var q = ((document.getElementById('nlng-q') || {}).value || '').toLowerCase();
  var rows = NLNG_ORDERS.filter(function(o) {
    var mq = !q || (function() {
      var keys = Object.keys(o);
      for (var ki = 0; ki < keys.length; ki++) {
        var v = o[keys[ki]];
        if (v == null || typeof v === 'object') continue;
        if (String(v).toLowerCase().includes(q)) return true;
      }
      if (Array.isArray(o.nlng_order_line_items)) {
        for (var li = 0; li < o.nlng_order_line_items.length; li++) {
          var desc = o.nlng_order_line_items[li].description || '';
          if (desc.toLowerCase().includes(q)) return true;
        }
      }
      return false;
    })();
    var mf  = (_nlngActiveFilter === 'all') ? true : applyNlngStageFilter(o, _nlngActiveFilter);
    var mso = !_nlngSoFilter || !!o.so_number;
    return mq && mf && mso && applyNlngReceivedRange(o) && applyNlngPromisedRange(o);
  });
  if (_nlngSortCol) {
    rows = rows.slice().sort(function(a, b) {
      var av = a[_nlngSortCol]; var bv = b[_nlngSortCol];
      if (av == null) av = ''; if (bv == null) bv = '';
      var cmp = String(av).localeCompare(String(bv), undefined, {numeric:true, sensitivity:'base'});
      return _nlngSortDir === 'asc' ? cmp : -cmp;
    });
  }
  _nlngFiltered = rows;
  if (!keepPage) { _nlngPage = 1; _nlngSelected.clear(); updateNlngSelectUI(); }
  else if (_nlngPage > Math.ceil(rows.length / PER_PAGE)) _nlngPage = 1;
  renderNlngTable();
  renderNlngPagination();
}

function renderNlngTable() {
  if (_ncIsInteracting()) { _ncPendingRender = true; return; }
  var start = (_nlngPage - 1) * PER_PAGE;
  var pageRows = _nlngFiltered.slice(start, start + PER_PAGE);
  var tb = document.getElementById('nlng-body');
  var em = document.getElementById('nlng-empty');
  if (!tb) return;
  if (!pageRows.length) {
    tb.innerHTML = '';
    if (em) { em.textContent = NLNG_ORDERS.length ? 'No orders match your filter' : 'No NLNG orders yet'; em.classList.remove('hidden'); }
    return;
  }
  if (em) em.classList.add('hidden');

  var html = '';
  for (var ri = 0; ri < pageRows.length; ri++) {
    var o = pageRows[ri];
    var sm = NLNG_STAGE_MAP[o.overall_status] || {lbl: o.overall_status || '—', cls:'sp-n'};
    var _noid = _esc(o.id||'');
    var _nlngBtnHtml = '<span onclick="event.stopPropagation()" style="display:inline-flex;align-items:center;gap:4px;margin-right:7px;vertical-align:middle;flex-shrink:0">'
      + '<button class="act-btn act-story" title="View email story for this PO" onclick="openStoryDrawer(\'' + _noid + '\',\'nlng\')">&#128214; Story</button>'
      + '<button class="nc-msg-btn" title="Send message" onclick="openCompose(_composeData[\'' + _noid + '\'])">'
      + '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>'
      + '</button>'
      + '</span>';
    var isSel = _nlngSelected.has(o.id);
    html += '<tr class="' + (isSel ? 'row-sel' : '') + '" data-id="' + o.id + '">';
    html += '<td class="td-cb"><input type="checkbox" class="nlng-row-cb"' + (isSel ? ' checked' : '') + '></td>';
    for (var ci = 0; ci < NLNG_COLS.length; ci++) {
      var col = NLNG_COLS[ci];
      var cls = col.cls || '';
      if (col.key === 'po_number') {
        var poUrl = o.pdf_url;
        html += '<td class="' + cls + '" style="white-space:nowrap">' + _nlngBtnHtml;
        if (poUrl) {
          html += '<a href="' + poUrl + '" target="_blank" rel="noopener" onclick="event.stopPropagation()" style="color:inherit;text-decoration:none;border-bottom:1px dotted var(--accent)" download>' + (o.po_number || '—') + '</a>';
        } else { html += (o.po_number || '—'); }
        html += '</td>';
      } else if (col.isNlngLive) {
        var isDone = isClosedOrder(o);
        html += '<td class="' + cls + '"><span class="stage-pill ' + (isDone ? 'sp-closed' : 'sp-live') + '"><span class="d"></span>' + (isDone ? 'Closed' : 'Live') + '</span></td>';
      } else if (col.isNlngStatus) {
        html += '<td class="' + cls + '"><span class="stage-pill ' + sm.cls + '"><span class="d"></span>' + sm.lbl + '</span></td>';
      } else if (col.isNlngAmt) {
        var val = o.net_value; var cur = o.currency || 'USD';
        if (val == null) { html += '<td class="' + cls + '"><span class="td-null">&mdash;</span></td>'; }
        else {
          var fv = Number(val) >= 1e6 ? (Number(val)/1e6).toFixed(1) + 'M' : Number(val).toLocaleString();
          html += '<td class="' + cls + '">' + cur + ' ' + fv + '</td>';
        }
      } else if (col.isDate) {
        html += '<td class="' + cls + '">' + fmtTs(o[col.key]) + '</td>';
      } else if (col.isNlngAck) {
        html += '<td class="' + cls + '"><span class="stage-pill sp-n"><span class="d"></span>Acknowledged</span></td>';
      } else if (col.isRoutingRaw) {
        var rtTxt = (o.warehouse_routing_raw || '').trim();
        var rtSafe = rtTxt ? rtTxt.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;') : '';
        var rtPrev = rtTxt ? rtSafe.replace(/\n+/g,' ').slice(0,55) + (rtSafe.length > 55 ? '…' : '') : '';
        html += '<td class="' + cls + '" title="' + rtSafe.slice(0,200) + '">' + (rtTxt ? rtPrev : '<span class="td-null">&mdash;</span>') + '</td>';
      } else if (col.key === 'stock_check_raw') {
        var rawTxt = extractStockRaw(o.stock_check_raw);
        var safeTxt = rawTxt ? rawTxt.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;') : '';
        var prev = rawTxt ? safeTxt.replace(/\n+/g,' ').slice(0,55) + (safeTxt.length > 55 ? '…' : '') : '';
        html += '<td class="' + cls + '" title="' + safeTxt + '">' + (rawTxt ? prev : '<span class="td-null">&mdash;</span>') + '</td>';
      } else if (col.isNlngItems) {
        var liArr = Array.isArray(o.nlng_order_line_items) ? o.nlng_order_line_items : [];
        if (liArr.length === 0) {
          html += '<td class="' + cls + '"><span class="td-null">&mdash;</span></td>';
        } else {
          var liSorted = liArr.slice().sort(function(a,b){ return (a.item_no||0)-(b.item_no||0); });
          var liFirst = liSorted[0].description || '';
          var liPrev = liFirst.slice(0,45).replace(/&/g,'&amp;').replace(/</g,'&lt;');
          var liBadge = liArr.length > 1 ? '<span class="li-badge">+' + (liArr.length-1) + '</span>' : '';
          html += '<td class="' + cls + '" title="' + liArr.length + ' item' + (liArr.length!==1?'s':'') + '">'
            + liPrev + (liFirst.length > 45 ? '…' : '') + liBadge + '</td>';
        }
      } else if (col.key === 'so_number') {
        var soNum = o.so_number;
        var soUrl = o.so_pdf_url;
        if (!soNum) { html += '<td class="' + cls + '"><span class="td-null">&mdash;</span></td>'; }
        else if (soUrl) {
          html += '<td class="' + cls + '"><a href="' + soUrl + '" target="_blank" rel="noopener" onclick="event.stopPropagation()" style="color:inherit;text-decoration:none;border-bottom:1px dotted var(--accent)">' + soNum + '</a></td>';
        } else {
          html += '<td class="' + cls + '">' + soNum + '</td>';
        }
      } else if (col.isNlngEnq) {
        var eqv = o.enquiry_number;
        var eqDisplay = eqv ? String(eqv) : '<span class="td-null" style="font-style:italic;font-size:10px">click to add</span>';
        html += '<td class="' + cls + '" title="Click to edit ENQ#" style="cursor:text">' + eqDisplay + '</td>';
      } else if (col.isNlngSoItems) {
        var soArr = o.so_number ? (o.so_line_items || []) : [];
        if (soArr.length === 0) {
          html += '<td class="' + cls + '"><span class="td-null">&mdash;</span></td>';
        } else {
          var soFirst = soArr[0].item_number || '';
          var soPrev = soFirst.slice(0,30).replace(/&/g,'&amp;').replace(/</g,'&lt;');
          var soBadge = soArr.length > 1 ? '<span class="li-badge">+' + (soArr.length-1) + '</span>' : '';
          var soDates = [];
          soArr.forEach(function(li){ if (li.despatch_date && soDates.indexOf(li.despatch_date) < 0) soDates.push(li.despatch_date); });
          var soDateStr = soDates.length === 1 ? ' <span class="ts-t">' + fmtOtdShort(soDates[0]) + '</span>'
                        : soDates.length > 1 ? ' <span class="ts-t">' + soDates.length + ' dates</span>' : '';
          html += '<td class="' + cls + '">' + soPrev + (soFirst.length > 30 ? '…' : '') + soBadge + soDateStr + '</td>';
        }
      } else {
        var rawv = (o[col.key] != null && o[col.key] !== '') ? String(o[col.key]) : '';
        var safev = rawv.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
        html += '<td class="' + cls + '" title="' + safev + '">' + n(o[col.key]) + '</td>';
      }
    }
    var nlngPoFields = {};
    if (o.po_number)              nlngPoFields['NLNG PO']    = o.po_number;
    if (o.contact_name)           nlngPoFields['Buyer']      = o.contact_name;
    if (o.required_delivery_date) nlngPoFields['Required By']= fmtTs(o.required_delivery_date);
    if (o.net_value)              nlngPoFields['Value']      = (o.currency || 'USD') + ' ' + Number(o.net_value).toLocaleString();
    var _nlngBodyLines = Object.keys(nlngPoFields).map(function(k){ return k + ': ' + nlngPoFields[k]; });
    _composeData[o.id] = {orderId: o.id, orderClient: 'nlng', toRole: 'warehouse',
      subject: 'Availability check — NLNG PO ' + (o.po_number || ''),
      body: _nlngBodyLines.join('\n') + '\n\nPlease confirm stock availability for the above order.',
      poFields: nlngPoFields,
      pdfUrl: o.pdf_url || null};
    html += '</tr>';
  }
  tb.innerHTML = html;
  updateNlngCbAll();
}

function renderNlngPagination() {
  renderPgBar(
    'nlng-pagination', _nlngPage, _nlngFiltered.length, PER_PAGE,
    'nlng-pg-prev', 'nlng-pg-next',
    function() { if (_nlngPage > 1) { _nlngPage--; renderNlngTable(); renderNlngPagination(); } },
    function() { var p = Math.max(1, Math.ceil(_nlngFiltered.length / PER_PAGE)); if (_nlngPage < p) { _nlngPage++; renderNlngTable(); renderNlngPagination(); } }
  );
}

async function loadNlngOrders() {
  try {
    var res = await authFetch('/api/nlng_orders');
    if (!res.ok) throw new Error('Server error ' + res.status);
    var data = await res.json();
    if (data.error) throw new Error(data.error);
    NLNG_ORDERS = data;
    buildNlngHeaders();
    buildNlngStatusChips(); // also rebuilds period chips
    filterNlng(true);
    updateDashboard(); // refresh combined KPIs now that NLNG data is ready
  } catch(e) {
    console.error('loadNlngOrders:', e);
    var em = document.getElementById('nlng-empty');
    if (em) { em.textContent = 'Could not load NLNG data: ' + e.message; em.classList.remove('hidden'); }
  }
}

// ── Client switcher ───────────────────────────────────────────────────────────
var _CLIENT_LABELS = {chevron:'Chevron', nlng:'NLNG', seplat:'SEPLAT / MOBILE'};
var _CLIENT_SUBS   = {
  chevron:'All tracked Chevron purchase orders',
  nlng:'All tracked NLNG purchase orders',
  seplat:'SEPLAT / MOBILE purchase orders'
};

function toggleClientSwDd() {
  var menu = document.getElementById('client-sw-menu');
  if (menu) menu.classList.toggle('hidden');
}

function switchClient(c) {
  _activeClient = c;
  var btn = document.getElementById('client-sw-btn');
  if (btn) btn.textContent = (_CLIENT_LABELS[c] || c) + ' ▾';
  var menu = document.getElementById('client-sw-menu');
  if (menu) menu.classList.add('hidden');
  // Update active state in dropdown items
  document.querySelectorAll('#client-sw-menu .period-dd-item').forEach(function(item) {
    var txt = item.textContent.replace(' ▾','').trim().toLowerCase();
    item.classList.toggle('on', txt === (_CLIENT_LABELS[c] || c).toLowerCase());
  });
  document.getElementById('view-chevron').style.display = c === 'chevron' ? '' : 'none';
  document.getElementById('view-nlng').style.display    = c === 'nlng'    ? '' : 'none';
  document.getElementById('view-seplat').style.display  = c === 'seplat'  ? '' : 'none';
  var sub = document.getElementById('orders-ph-sub');
  if (sub) sub.textContent = _CLIENT_SUBS[c] || '';

  // Toggle OTD views on the Delays page
  var otdCh = document.getElementById('otd-view-chevron');
  var otdNl = document.getElementById('otd-view-nlng');
  if (otdCh) otdCh.style.display = c === 'nlng' ? 'none' : '';
  if (otdNl) otdNl.style.display = c === 'nlng' ? ''     : 'none';
  var delSub = document.getElementById('delays-ph-sub');
  if (delSub) delSub.textContent = c === 'nlng'
    ? 'On-time delivery status for all active NLNG purchase orders'
    : 'On-time delivery status for all active Chevron purchase orders';

  if (c === 'nlng') {
    if (NLNG_ORDERS.length === 0) {
      loadNlngOrders().then(function() { buildNlngOtdPeriodChips(); renderNlngOTD(); });
    } else {
      filterNlng();
      if (document.getElementById('page-delays').classList.contains('active')) {
        buildNlngOtdPeriodChips();
        renderNlngOTD();
      }
    }
  } else if (c === 'chevron') {
    filterOrders();
    if (document.getElementById('page-delays').classList.contains('active')) {
      buildOtdPeriodChips();
      renderOTD();
    }
  }
}

// ── Filter + sort ─────────────────────────────────────────────────────────────
var _soFilter        = false;
var _nlngSoFilter    = false;
var _promisedFrom       = '';   // 'YYYY-MM-DD' | '' — orders table PO Promised from
var _promisedTo         = '';   // 'YYYY-MM-DD' | '' — orders table PO Promised to

function toggleSoFilter() {
  _soFilter = !_soFilter;
  var btn = document.getElementById('so-filter-btn');
  if (btn) btn.classList.toggle('on', _soFilter);
  filterOrders();
}

function toggleNlngSoFilter() {
  _nlngSoFilter = !_nlngSoFilter;
  var btn = document.getElementById('nlng-so-filter-btn');
  if (btn) btn.classList.toggle('on', _nlngSoFilter);
  filterNlng();
}

// Orders table: date-range filter on PO Promised date (from line items)
function applyOrdersPromisedRange(o) {
  if (!_promisedFrom && !_promisedTo) return true;
  var ts = getPoPromisedDate(o);
  if (!ts) return false;
  var d = String(ts).slice(0, 10);
  if (_promisedFrom && d < _promisedFrom) return false;
  if (_promisedTo   && d > _promisedTo)   return false;
  return true;
}

// ── Orders — Promised range ───────────────────────────────────────────────────
function onOrdersPromisedChange() {
  _promisedFrom = (document.getElementById('orders-promised-from') || {}).value || '';
  _promisedTo   = (document.getElementById('orders-promised-to')   || {}).value || '';
  var btn = document.getElementById('orders-promised-btn');
  if (btn) btn.textContent = drFmtLabel(_promisedFrom, _promisedTo);
  filterOrders();
}

function clearOrdersPromised() {
  _promisedFrom = ''; _promisedTo = '';
  var f = document.getElementById('orders-promised-from');
  var t = document.getElementById('orders-promised-to');
  if (f) f.value = '';
  if (t) t.value = '';
  var btn = document.getElementById('orders-promised-btn');
  if (btn) btn.textContent = 'All ▾';
  document.querySelectorAll('.dr-pop').forEach(function(p) { p.classList.add('hidden'); });
  filterOrders();
}

// NLNG table: month-key filter on promised_date (kept as a dropdown)
function applyPromisedFilter(o, filter) {
  if (!filter || filter === 'all') return true;
  var ts = getPoPromisedDate(o) || o.required_delivery_date;
  if (!ts) return false;
  var d   = new Date(ts);
  var now = new Date();
  var sp  = filter.slice(4); // strip 'prd_'
  if (sp === 'over')  return d < now;
  if (sp === 'this')  { return d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth(); }
  if (sp === 'next')  { var nm = new Date(now.getFullYear(), now.getMonth() + 1, 1); return d.getFullYear() === nm.getFullYear() && d.getMonth() === nm.getMonth(); }
  if (sp === '3mo')   return d >= now && (d - now) <= 90 * 86400000;
  if (sp.length === 7) { var yr = +sp.slice(0,4), mo = +sp.slice(5,7) - 1; return d.getFullYear() === yr && d.getMonth() === mo; }
  return true;
}

function _promisedPeriodItems(dataArr) {
  var data  = dataArr || ORDERS;
  var items = [
    {key:'all',      label:'All'},
    {key:'prd_over', label:'Overdue'},
    {key:'prd_this', label:'This month'},
    {key:'prd_next', label:'Next month'},
    {key:'prd_3mo',  label:'3 months'},
  ];
  var seen = {};
  for (var i = 0; i < data.length; i++) {
    var v = getPoPromisedDate(data[i]) || data[i].required_delivery_date;
    if (!v) continue;
    var d = new Date(v);
    var mk = d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0');
    seen[mk] = d.toLocaleDateString('en-GB', {month:'short', year:'2-digit'});
  }
  Object.keys(seen).sort().reverse().forEach(function(k) { items.push({key:'prd_' + k, label:seen[k]}); });
  return items;
}

function _promisedLabel(key, dataArr) {
  if (!key || key === 'all') return 'All';
  var items = _promisedPeriodItems(dataArr);
  for (var i = 0; i < items.length; i++) { if (items[i].key === key) return items[i].label; }
  return 'All';
}



function filterOrders(keepPage) {
  var q = ((document.getElementById('ot-q') || {}).value || '').toLowerCase();
  var rows = ORDERS.filter(function(o) {
    var mq = !q || (function() {
      // Search every scalar field on the order object
      var keys = Object.keys(o);
      for (var ki = 0; ki < keys.length; ki++) {
        var v = o[keys[ki]];
        if (v == null || typeof v === 'object') continue;
        if (String(v).toLowerCase().includes(q)) return true;
      }
      // Also search nested line item descriptions
      if (Array.isArray(o.order_line_items)) {
        for (var li = 0; li < o.order_line_items.length; li++) {
          var desc = o.order_line_items[li].description || '';
          if (desc.toLowerCase().includes(q)) return true;
        }
      }
      return false;
    })();
    var mf = _activeFilters.has('all')
      || (_activeFilters.has('live')   && !CLOSED_STATUSES.has(o.overall_status))
      || (_activeFilters.has('closed') &&  CLOSED_STATUSES.has(o.overall_status))
      || _activeFilters.has(o.overall_status);
    var mso = !_soFilter || !!o.so_number;
    return mq && mf && mso && applyOrdersReceivedRange(o) && applyOrdersPromisedRange(o);
  });

  if (_sortCol) {
    rows = rows.slice().sort(function(a, b) {
      var av, bv;
      if (_sortCol === '_po_promised') {
        av = getPoPromisedDate(a) || '';
        bv = getPoPromisedDate(b) || '';
      } else {
        av = a[_sortCol]; bv = b[_sortCol];
        if (av == null) av = ''; if (bv == null) bv = '';
      }
      var cmp = String(av).localeCompare(String(bv), undefined, {numeric:true, sensitivity:'base'});
      return _sortDir === 'asc' ? cmp : -cmp;
    });
  }

  _filtered = rows;
  if (!keepPage) _page = 1;
  else if (_page > Math.ceil(rows.length / PER_PAGE)) _page = 1; // clamp if result set shrank
  _selected.clear();
  updateSelectUI();
  renderTable();
  renderPagination();
}

// ── Render table page ─────────────────────────────────────────────────────────
function renderTable() {
  if (_ncIsInteracting()) { _ncPendingRender = true; return; }
  var start    = (_page - 1) * PER_PAGE;
  var pageRows = _filtered.slice(start, start + PER_PAGE);
  var tb = document.getElementById('ot-body');
  var em = document.getElementById('ot-empty');

  if (!pageRows.length) {
    tb.innerHTML = '';
    em.textContent = ORDERS.length ? 'No orders match your filter' : 'No orders yet — run the parser to populate data';
    em.classList.remove('hidden');
    updateCbAll();
    return;
  }
  em.classList.add('hidden');

  var html = '';
  for (var ri = 0; ri < pageRows.length; ri++) {
    var o = pageRows[ri];
    var po = o.buyer_po_number || '';
    var isSel = _selected.has(po);
    // Status: stock annotations only while still in stock-check phase (before SPM PO sent)
    var pastStockPhase = !!o.spm_po_sent_at;
    var partialStock   = !pastStockPhase && isPartialStock(o);
    var notInStock     = !pastStockPhase && !partialStock && isNotInStock(o);
    var m;
    if (notInStock) {
      m = {lbl:'Not in stock', cls:'sp-crit'};
    } else if (partialStock) {
      m = {lbl:'Partial stock', cls:'sp-w'};
    } else if (!pastStockPhase && o.overall_status === 'stock_check_needs_review') {
      var rawSnip = extractStockRaw(o.stock_check_raw);
      m = rawSnip ? {lbl:'Review: ' + rawSnip.replace(/[\r\n]+/g,' ').slice(0,28) + '…', cls:'sp-w'} : {lbl:'Needs review', cls:'sp-w'};
    } else {
      m = STAGE_MAP[o.overall_status] || {lbl: o.overall_status || '—', cls:'sp-n'};
    }

    var _oid = _esc(o.id||'');
    var _btnHtml = '<span onclick="event.stopPropagation()" style="display:inline-flex;align-items:center;gap:4px;margin-right:7px;vertical-align:middle;flex-shrink:0">'
      + '<button class="act-btn act-story" title="View email story for this PO" onclick="openStoryDrawer(\'' + _oid + '\',\'chevron\')">&#128214; Story</button>'
      + '<button class="nc-msg-btn" title="Send message" onclick="openCompose(_composeData[\'' + _oid + '\'])">'
      + '<svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>'
      + '</button>'
      + '</span>';
    html += '<tr class="' + (isSel ? 'row-sel' : '') + '" data-po="' + po.replace(/"/g,'&quot;') + '">';
    html += '<td class="td-cb"><input type="checkbox" class="row-cb"' + (isSel ? ' checked' : '') + '></td>';
    for (var ci = 0; ci < COLS.length; ci++) {
      var col = COLS[ci];
      var cls = col.cls || '';
      if (col.key === 'buyer_po_number') {
        var poUrl = o.pdf_url;
        if (poUrl) {
          html += '<td class="' + cls + '" title="Click to open PO PDF" style="white-space:nowrap">'
            + _btnHtml
            + '<a href="' + poUrl + '" target="_blank" rel="noopener" onclick="event.stopPropagation()" style="color:inherit;text-decoration:none;border-bottom:1px dotted var(--accent)" download>' + po + '</a>'
            + '</td>';
        } else {
          html += '<td class="' + cls + '" style="white-space:nowrap">' + _btnHtml + po + '</td>';
        }
      } else if (col.key === 'so_number') {
        var soVal = o.so_number;
        var soUrl = o.so_pdf_url;
        if (soVal && soUrl) {
          html += '<td class="' + cls + '" title="Click to open SO PDF">'
            + '<a href="' + soUrl + '" target="_blank" rel="noopener" onclick="event.stopPropagation()" style="color:inherit;text-decoration:none;border-bottom:1px dotted var(--t3)" download>' + soVal + '</a>'
            + '</td>';
        } else {
          html += '<td class="' + cls + '">' + n(soVal) + '</td>';
        }
      } else if (col.isLiveStatus) {
        var isDone = isClosedOrder(o);
        var lLbl = isDone ? 'Closed' : 'Live';
        var lCls = isDone ? 'sp-closed' : 'sp-live';
        html += '<td class="' + cls + '"><span class="stage-pill ' + lCls + '"><span class="d"></span>' + lLbl + '</span></td>';
      } else if (col.isStatus) {
        html += '<td class="' + cls + '"><span class="stage-pill ' + m.cls + '"><span class="d"></span>' + m.lbl + '</span></td>';
      } else if (col.isAmt) {
        var _sym = o.po_currency === 'NGN' ? '₦' : '$';
        var _av = o[col.key];
        var _afmt = _av == null ? '<span class="td-null">&mdash;</span>' : (Number(_av) >= 1e6 ? _sym + (Number(_av)/1e6).toFixed(1) + 'M' : _sym + Number(_av).toLocaleString());
        html += '<td class="' + cls + '">' + _afmt + '</td>';
      } else if (col.isDate) {
        html += '<td class="' + cls + '">' + fmtTs(o[col.key]) + '</td>';
      } else if (col.isPoPromised) {
        html += '<td class="' + cls + '">' + fmtTs(getPoPromisedDate(o)) + '</td>';
      } else if (col.isRoutingRaw) {
        var rtTxt = (o.warehouse_routing_raw || '').trim();
        var rtSafe = rtTxt ? rtTxt.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;') : '';
        var rtPreview = rtTxt ? rtSafe.replace(/\n+/g,' ').slice(0, 55) + (rtSafe.length > 55 ? '…' : '') : '';
        html += '<td class="' + cls + '" title="' + rtSafe.slice(0,200) + '">' + (rtTxt ? rtPreview : '<span class="td-null">&mdash;</span>') + '</td>';
      } else if (col.key === 'stock_check_raw') {
        // Extract text from object/string — avoid [object Object]
        var rawTxt = extractStockRaw(o.stock_check_raw);
        var safeTxt = rawTxt ? rawTxt.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;') : '';
        var preview = rawTxt ? safeTxt.replace(/\n+/g,' ').slice(0, 55) + (safeTxt.length > 55 ? '…' : '') : '';
        html += '<td class="' + cls + '" title="' + safeTxt + '">' + (rawTxt ? preview : '<span class="td-null">&mdash;</span>') + '</td>';
      } else if (col.key === 'req_number') {
        var rqv = o.req_number;
        var rqDisplay = rqv ? String(rqv) : '<span class="td-null" style="font-style:italic;font-size:10px">click to add</span>';
        html += '<td class="' + cls + '" title="Click to edit REQ#" style="cursor:text">' + rqDisplay + '</td>';
      } else if (col.isLineItems) {
        var liArr = Array.isArray(o.order_line_items) ? o.order_line_items : [];
        if (liArr.length === 0) {
          var fb = (o.extracted_description || '');
          var fbSafe = fb.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
          var fbPrev = fbSafe.slice(0, 50) + (fbSafe.length > 50 ? '…' : '');
          html += '<td class="' + cls + '">' + (fbPrev || '<span class="td-null">&mdash;</span>') + '</td>';
        } else {
          var liSorted = liArr.slice().sort(function(a,b){ return (a.line_no||0)-(b.line_no||0); });
          var liFirst = (liSorted[0].description || '');
          var liPreview = liFirst.slice(0, 45).replace(/&/g,'&amp;').replace(/</g,'&lt;');
          var liDots = liFirst.length > 45 ? '…' : '';
          var liBadge = liArr.length > 1 ? '<span class="li-badge">+' + (liArr.length-1) + '</span>' : '';
          html += '<td class="' + cls + '" title="' + liArr.length + ' line item' + (liArr.length!==1?'s':'') + ' — click to expand">'
            + liPreview + liDots + liBadge + '</td>';
        }
      } else if (col.isSoItems) {
        var soArr = o.so_number ? (o.so_line_items || []) : [];
        if (soArr.length === 0) {
          html += '<td class="' + cls + '"><span class="td-null">&mdash;</span></td>';
        } else {
          var soFirst = soArr[0].item_number || '';
          var soPreview = soFirst.slice(0, 30).replace(/&/g,'&amp;').replace(/</g,'&lt;');
          var soDots = soFirst.length > 30 ? '…' : '';
          var soBadge = soArr.length > 1 ? '<span class="li-badge">+' + (soArr.length - 1) + '</span>' : '';
          var soDates = [];
          soArr.forEach(function(li) { if (li.despatch_date && soDates.indexOf(li.despatch_date) < 0) soDates.push(li.despatch_date); });
          var soDateStr = soDates.length === 1 ? ' <span class="ts-t">' + fmtOtdShort(soDates[0]) + '</span>'
                        : soDates.length > 1   ? ' <span class="ts-t">' + soDates.length + ' dates</span>' : '';
          html += '<td class="' + cls + '" title="' + soArr.length + ' SO line item' + (soArr.length !== 1 ? 's' : '') + ' — click to expand">'
            + soPreview + soDots + soBadge + soDateStr + '</td>';
        }
      } else {
        var rawv = (o[col.key] != null && o[col.key] !== '') ? String(o[col.key]) : '';
        var safev = rawv.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/"/g,'&quot;');
        html += '<td class="' + cls + '" title="' + safev + '">' + n(o[col.key]) + '</td>';
      }
    }
    // Send message action
    var poFields = {};
    if (o.buyer_po_number)          poFields['PO Number']    = o.buyer_po_number;
    if (o.buyer_name)               poFields['Buyer']        = o.buyer_name;
    if (o.extracted_description)    poFields['Description']  = o.extracted_description;
    if (o.required_delivery_date)   poFields['Required By']  = fmtTs(o.required_delivery_date);
    if (o.po_destination)           poFields['Destination']  = o.po_destination;
    if (o.po_amount)                poFields['Amount']       = (o.po_currency === 'NGN' ? '₦' : '$') + parseFloat(o.po_amount).toLocaleString('en-US',{maximumFractionDigits:0});
    var _poBodyLines = Object.keys(poFields).map(function(k){return k+': '+poFields[k];});
    _composeData[o.id] = {orderId:o.id, orderClient:'chevron', toRole:'warehouse',
      subject:'Availability check — PO ' + (o.buyer_po_number || ''),
      body: _poBodyLines.join('\n') + '\n\nPlease confirm stock availability for the above order.',
      poFields:poFields,
      pdfUrl: o.pdf_url || null};
    html += '</tr>';
  }
  tb.innerHTML = html;
  updateCbAll();
}

// ── Shared pagination renderer (reusable for every client table) ───────────────
function renderPgBar(containerId, page, filteredLen, perPage, prevId, nextId, onPrev, onNext) {
  var el = document.getElementById(containerId);
  if (!el) return;
  var pages = Math.max(1, Math.ceil(filteredLen / perPage));
  var total = filteredLen;
  el.innerHTML =
    '<div class="pg-l">'
    + '<button class="pg-btn" id="' + prevId + '"' + (page <= 1 ? ' disabled' : '') + '>← Prev</button>'
    + '<span>Page <strong>' + page + '</strong> of <strong>' + pages + '</strong></span>'
    + '<button class="pg-btn" id="' + nextId + '"' + (page >= pages ? ' disabled' : '') + '>Next →</button>'
    + '</div>'
    + '<div class="pg-r">'
    + '<span class="pg-rows">' + perPage + ' rows</span>'
    + '<span class="pg-count">' + total + ' record' + (total !== 1 ? 's' : '') + '</span>'
    + '</div>';
  var prevBtn = document.getElementById(prevId);
  var nextBtn = document.getElementById(nextId);
  if (prevBtn) prevBtn.addEventListener('click', onPrev);
  if (nextBtn) nextBtn.addEventListener('click', onNext);
}

// ── Pagination bar ────────────────────────────────────────────────────────────
function renderPagination() {
  renderPgBar(
    'ot-pagination', _page, _filtered.length, PER_PAGE,
    'pg-prev', 'pg-next',
    function() { if (_page > 1) { _page--; renderTable(); renderPagination(); } },
    function() { var p = Math.max(1, Math.ceil(_filtered.length / PER_PAGE)); if (_page < p) { _page++; renderTable(); renderPagination(); } }
  );
}

// ── Sorting ───────────────────────────────────────────────────────────────────
function sortBy(col) {
  if (_sortCol === col) {
    _sortDir = _sortDir === 'asc' ? 'desc' : 'asc';
  } else {
    _sortCol = col; _sortDir = 'asc';
  }
  updateSortHeaders();
  filterOrders();
}

function updateOtdSortHeaders() {
  var ths = document.querySelectorAll('#otd-head-row th[data-col]');
  for (var i = 0; i < ths.length; i++) {
    var th = ths[i];
    th.classList.remove('sort-asc', 'sort-desc');
    if (th.dataset.col === _otdSortCol) {
      th.classList.add(_otdSortDir === 'asc' ? 'sort-asc' : 'sort-desc');
    }
  }
}

function sortOtdBy(col) {
  if (_otdSortCol === col) {
    _otdSortDir = _otdSortDir === 'asc' ? 'desc' : 'asc';
  } else {
    _otdSortCol = col; _otdSortDir = 'asc';
  }
  updateOtdSortHeaders();
  _otdPage = 1;
  renderOTD();
}

// ── Row selection ─────────────────────────────────────────────────────────────
function updateCbAll() {
  var cbAll = document.getElementById('cb-all');
  if (!cbAll) return;
  var start = (_page - 1) * PER_PAGE;
  var pageRows = _filtered.slice(start, start + PER_PAGE);
  if (!pageRows.length) { cbAll.checked = false; cbAll.indeterminate = false; return; }
  var selCount = pageRows.filter(function(o){ return _selected.has(o.buyer_po_number || ''); }).length;
  cbAll.indeterminate = selCount > 0 && selCount < pageRows.length;
  cbAll.checked = selCount === pageRows.length;
}

function updateSelectUI() {
  var btn = document.getElementById('btn-export');
  if (btn) btn.textContent = _selected.size ? 'Export (' + _selected.size + ')' : 'Export';
}

function updateNlngCbAll() {
  var cbAll = document.getElementById('nlng-cb-all');
  if (!cbAll) return;
  var start = (_nlngPage - 1) * PER_PAGE;
  var pageRows = _nlngFiltered.slice(start, start + PER_PAGE);
  if (!pageRows.length) { cbAll.checked = false; cbAll.indeterminate = false; return; }
  var selCount = pageRows.filter(function(o){ return _nlngSelected.has(o.id); }).length;
  cbAll.indeterminate = selCount > 0 && selCount < pageRows.length;
  cbAll.checked = selCount === pageRows.length;
}

function updateNlngSelectUI() {
  var btn = document.getElementById('nlng-btn-export');
  if (btn) btn.textContent = _nlngSelected.size ? 'Export (' + _nlngSelected.size + ')' : 'Export';
}

// ── Export CSV ────────────────────────────────────────────────────────────────
// Turn any stored value into something readable in a spreadsheet.
// Several columns (stock_check_raw most visibly) hold a JSON object, and
// String({...}) yields the literal text "[object Object]" — which is what the
// Stock Notes column was exporting. The table view and the cell popup already
// run these through extractStockRaw; the export did not.
function _csvValue(v) {
  if (v == null) return '';
  if (typeof v !== 'object') return String(v);
  var text = (typeof extractStockRaw === 'function') ? extractStockRaw(v) : '';
  if (text) return String(text);
  try { return JSON.stringify(v); } catch (e) { return ''; }
}

function exportCSV() {
  var rows = _selected.size
    ? ORDERS.filter(function(o){ return _selected.has(o.buyer_po_number || ''); })
    : _filtered;
  var keys = COLS.map(function(c){ return c.key; });
  var hdrs = COLS.map(function(c){ return c.hdr; });
  var lines = [hdrs.map(function(h){ return '"' + h.replace(/"/g,'""') + '"'; }).join(',')];
  for (var i = 0; i < rows.length; i++) {
    var o = rows[i];
    lines.push(keys.map(function(k){
      var v;
      if (k === 'order_line_items') {
        var liExp = Array.isArray(o[k]) ? o[k] : [];
        v = liExp.map(function(li){ return (li.line_no||'') + '. ' + (li.description||'') + (li.quantity?' x'+li.quantity:''); }).join(' | ');
      } else {
        v = _csvValue(o[k]);
      }
      return '"' + v.replace(/"/g,'""') + '"';
    }).join(','));
  }
  var blob = new Blob([lines.join('\n')], {type:'text/csv'});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'orders-' + new Date().toISOString().slice(0,10) + '.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

// ── Export NLNG CSV ───────────────────────────────────────────────────────────
function exportNlngCSV() {
  var rows = _nlngSelected.size
    ? NLNG_ORDERS.filter(function(o){ return _nlngSelected.has(o.id); })
    : _nlngFiltered;
  var hdrs = NLNG_COLS.map(function(c){ return c.hdr; });
  var lines = [hdrs.map(function(h){ return '"' + h.replace(/"/g,'""') + '"'; }).join(',')];
  for (var i = 0; i < rows.length; i++) {
    var o = rows[i];
    lines.push(NLNG_COLS.map(function(col) {
      var v;
      if (col.isNlngLive) {
        v = isClosedOrder(o) ? 'Closed' : 'Live';
      } else if (col.isNlngAck) {
        v = 'Acknowledged';
      } else if (col.isNlngItems) {
        var liArr = Array.isArray(o.nlng_order_line_items) ? o.nlng_order_line_items : [];
        v = liArr.slice().sort(function(a,b){ return (a.item_no||0)-(b.item_no||0); })
          .map(function(li){ return (li.item_no||'') + '. ' + (li.description||'') + (li.quantity ? ' x'+li.quantity+' '+(li.uom||'') : ''); })
          .join(' | ');
      } else if (col.isNlngSoItems) {
        var soArr = Array.isArray(o.so_line_items) ? o.so_line_items : [];
        v = soArr.map(function(li){ return (li.item_number||'') + (li.qty ? ' x'+li.qty+' '+(li.uom||'') : '') + (li.despatch_date ? ' ['+li.despatch_date+']' : ''); })
          .join(' | ');
      } else if (col.isNlngAmt) {
        v = o.net_value != null ? String(o.net_value) + ' ' + (o.currency || 'USD') : '';
      } else {
        v = _csvValue(o[col.key]);
      }
      return '"' + v.replace(/"/g,'""') + '"';
    }).join(','));
  }
  var blob = new Blob([lines.join('\n')], {type:'text/csv'});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'nlng-orders-' + new Date().toISOString().slice(0,10) + '.csv';
  a.click();
  URL.revokeObjectURL(a.href);
}

// ── Cell expand modal ─────────────────────────────────────────────────────────
function showCell(colHdr, value) {
  document.getElementById('cell-col-name').textContent = colHdr;
  // Handle object values (e.g. stock_check_raw returned as JSON object)
  var display;
  if (value == null || value === '') {
    display = '— (empty)';
  } else if (typeof value === 'object') {
    var extracted = extractStockRaw(value);
    display = extracted || JSON.stringify(value, null, 2);
  } else {
    display = String(value);
  }
  document.getElementById('cell-val').textContent = display;
  document.getElementById('cell-modal').classList.remove('hidden');
}

// ── Email Story Drawer ─────────────────────────────────────────────────────────
var _drawerOrderId = null;
var _drawerType    = null;

function openStoryDrawer(orderId, orderType, tab) {
  _drawerOrderId = orderId;
  _drawerType    = orderType || 'chevron';
  var type  = _drawerType;
  var order = (type === 'nlng' ? NLNG_ORDERS : ORDERS).find(function(o){ return o.id === orderId; });
  var poNum = (order && (order.buyer_po_number || order.po_number)) ? (order.buyer_po_number || order.po_number) : orderId;
  document.getElementById('story-po-num').textContent  = poNum;
  document.getElementById('story-po-desc').textContent = (order && order.extracted_description) ? order.extracted_description : '';
  var strip = document.getElementById('story-meta-strip');
  strip.innerHTML = '';
  if (order) {
    var meta = type === 'nlng' ? [
      {l:'PO Number', v: order.po_number || '—'},
      {l:'Status',    v: order.overall_status || '—'},
      {l:'Doc Date',  v: order.document_date ? String(order.document_date).slice(0,10) : '—'},
      {l:'RDD',       v: order.required_delivery_date ? String(order.required_delivery_date).slice(0,10) : '—'},
    ] : [
      {l:'Vendor', v: order.vendor_name || '—'},
      {l:'Status', v: order.status || '—'},
      {l:'PO Date', v: order.po_date ? fmtTs(order.po_date) : '—'},
      {l:'RDD', v: order.required_delivery_date ? String(order.required_delivery_date).slice(0,10) : '—'},
    ];
    meta.forEach(function(m){
      strip.innerHTML += '<div class="story-mi"><span class="story-ml">' + _esc(m.l) + '</span><span class="story-mv">' + _esc(m.v) + '</span></div>';
    });
  }
  document.getElementById('story-overlay').classList.add('show');
  document.getElementById('story-drawer').classList.add('open');
  document.body.style.overflow = 'hidden';
  switchDrawerTab(tab || 'activity');
}

function switchDrawerTab(tab) {
  var isActivity = tab === 'activity';
  document.getElementById('tab-activity').classList.toggle('active', isActivity);
  document.getElementById('tab-story').classList.toggle('active', !isActivity);
  document.getElementById('panel-activity').classList.toggle('drawer-panel-hidden', !isActivity);
  document.getElementById('panel-story').classList.toggle('drawer-panel-hidden', isActivity);
  document.getElementById('story-po-label').textContent = isActivity ? 'PO Activity' : 'PO Story';
  if (isActivity) {
    loadActivity(_drawerOrderId, _drawerType);
  } else {
    document.getElementById('story-scroll').innerHTML = '<div class="se-loading"><div class="ai-status" style="justify-content:center"><div class="ai-status-dots"><span></span><span></span><span></span></div>Please wait, creating PO story…</div></div>';
    document.getElementById('story-ai-card').style.display = 'none';
    document.getElementById('ai-msgs').innerHTML = '';
    document.getElementById('ai-input').value    = '';
    document.getElementById('ai-input').disabled = true;
    document.getElementById('ai-send').disabled  = true;
    loadStoryEmails(_drawerOrderId, _drawerType);
  }
}

function closeStoryDrawer() {
  document.getElementById('story-overlay').classList.remove('show');
  document.getElementById('story-drawer').classList.remove('open');
  document.body.style.overflow = '';
}

// ── Activity tab ───────────────────────────────────────────────────────────────
var _actOid  = null;
var _actType = null;
var _actAvatarPalette = ['#0EA5E9','#8B5CF6','#10B981','#F59E0B','#EF4444','#EC4899','#F97316','#06B6D4'];

function _actAvatarColor(name) {
  var h = 0;
  for (var i = 0; i < (name || '').length; i++) h = ((h << 5) - h) + (name || '').charCodeAt(i);
  return _actAvatarPalette[Math.abs(h) % _actAvatarPalette.length];
}

function _actRelTime(iso) {
  if (!iso) return '';
  try {
    var diff = Math.floor((Date.now() - new Date(iso)) / 60000);
    if (diff < 1)    return 'just now';
    if (diff < 60)   return diff + 'm ago';
    if (diff < 1440) return Math.floor(diff / 60) + 'h ago';
    return new Date(iso).toLocaleDateString(undefined, {day:'2-digit', month:'short'});
  } catch(e) { return ''; }
}

function loadActivity(orderId, type) {
  _actOid  = orderId;
  _actType = type || 'chevron';
  var feed = document.getElementById('act-feed');
  if (!feed) return;
  feed.innerHTML = '<div class="act-loading">Loading…</div>';
  authFetch('/api/comments?order_id=' + encodeURIComponent(orderId) + '&type=' + encodeURIComponent(_actType))
    .then(function(r){ return r.ok ? r.json() : Promise.reject(r.status); })
    .then(function(d){ renderActivityFeed(Array.isArray(d) ? d : (d.data || [])); })
    .catch(function(){ feed.innerHTML = '<div class="act-empty">Could not load activity.</div>'; });
}

function renderActivityFeed(items) {
  var feed = document.getElementById('act-feed');
  if (!feed) return;
  if (!items || !items.length) {
    feed.innerHTML = '<div class="act-empty">No activity yet. Write the first note below.</div>';
    return;
  }
  var myId   = _currentUser && (_currentUser.id || _currentUser.sub);
  var isAdmin = _currentUser && _currentUser.role === 'admin';
  feed.innerHTML = items.map(function(item) {
    if (item.is_system_event) {
      return '<div class="act-sys">'
        + '<span class="act-sys-icon">&#9881;</span>'
        + '<span class="act-sys-text">' + _esc(item.body) + '</span>'
        + '<span class="act-sys-time">' + _actRelTime(item.created_at) + '</span>'
        + '</div>';
    }
    var name     = item.author_name || 'Unknown';
    var initials = name.split(/\s+/).filter(Boolean).map(function(w){ return w[0]; }).join('').slice(0, 2).toUpperCase() || '?';
    var color    = _actAvatarColor(name);
    var bodyHtml = _esc(item.body).replace(/@(\w+)/g, '<span class="act-mention">@$1</span>');
    var canEdit  = myId && item.user_id === myId;
    var canDel   = canEdit || isAdmin;
    var id       = _esc(item.id);
    var actions  = (canEdit || canDel)
      ? '<div class="act-actions">'
        + (canEdit ? '<button class="act-act-btn" onclick="actStartEdit(\'' + id + '\')" title="Edit">&#9998;</button>' : '')
        + (canDel  ? '<button class="act-act-btn act-del-btn" onclick="actDelete(\'' + id + '\')" title="Delete">&#10005;</button>' : '')
        + '</div>'
      : '';
    return '<div class="act-item" id="actitem-' + id + '">'
      + '<div class="act-avatar" style="background:' + color + '">' + _esc(initials) + '</div>'
      + '<div class="act-body">'
      + '<div class="act-meta">'
      + '<span class="act-author">' + _esc(name) + '</span>'
      + (item.author_role ? '<span class="act-role">' + _esc(item.author_role) + '</span>' : '')
      + '<span class="act-time">' + _actRelTime(item.created_at) + '</span>'
      + actions
      + '</div>'
      + '<div class="act-text" id="acttext-' + id + '">' + bodyHtml + '</div>'
      + '</div></div>';
  }).join('');
  feed.scrollTop = feed.scrollHeight;
}

function actStartEdit(id) {
  var textEl = document.getElementById('acttext-' + id);
  if (!textEl || textEl.querySelector('textarea')) return; // already editing
  var current = textEl.textContent;
  textEl.innerHTML = '<textarea class="act-edit-ta">' + _esc(current) + '</textarea>'
    + '<div class="act-edit-actions">'
    + '<button class="act-save-btn" onclick="actSaveEdit(\'' + id + '\')">Save</button>'
    + '<button class="act-cancel-btn" onclick="loadActivity(_actOid,_actType)">Cancel</button>'
    + '</div>';
  var ta = textEl.querySelector('textarea');
  ta.focus();
  ta.setSelectionRange(ta.value.length, ta.value.length);
}

function actSaveEdit(id) {
  var textEl = document.getElementById('acttext-' + id);
  var ta     = textEl && textEl.querySelector('textarea');
  if (!ta) return;
  var text = ta.value.trim();
  if (!text) return;
  authFetch('/api/comments/' + encodeURIComponent(id), {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({body: text}),
  })
  .then(function(r){ if (!r.ok) throw new Error(); loadActivity(_actOid, _actType); })
  .catch(function(){ alert('Could not save — please try again.'); });
}

function actDelete(id) {
  if (!confirm('Delete this note?')) return;
  authFetch('/api/comments/' + encodeURIComponent(id), {method: 'DELETE'})
  .then(function(r){ if (!r.ok) throw new Error(); loadActivity(_actOid, _actType); })
  .catch(function(){ alert('Could not delete — please try again.'); });
}

function postActivity() {
  var input = document.getElementById('act-input');
  var btn   = document.getElementById('act-send');
  var text  = (input.value || '').trim();
  if (!text || !_actOid) return;
  btn.disabled   = true;
  input.disabled = true;
  authFetch('/api/comments', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({order_id: _actOid, type: _actType || 'chevron', body: text})
  })
  .then(function(r){
    if (!r.ok) throw new Error('post failed');
    input.value = '';
    _actHideDrop();
    return loadActivity(_actOid, _actType);
  })
  .catch(function(){ alert('Could not send — please try again.'); })
  .finally(function(){ btn.disabled = false; input.disabled = false; input.focus(); });
}

// ── @mention autocomplete ──────────────────────────────────────────────────────
var _actMentionRoles = [
  {role:'warehouse',   label:'Warehouse'},
  {role:'procurement', label:'Procurement'},
  {role:'admin',       label:'Admin'},
  {role:'expeditor',   label:'Expeditor'},
  {role:'accounts',    label:'Accounts'},
];

function actInputChanged(e) {
  var ta     = e.target;
  var pos    = ta.selectionStart;
  var before = ta.value.slice(0, pos);
  var atIdx  = before.lastIndexOf('@');
  if (atIdx === -1) { _actHideDrop(); return; }
  var partial = before.slice(atIdx + 1);
  if (/\s/.test(partial)) { _actHideDrop(); return; }
  var matches = _actMentionRoles.filter(function(r) {
    return r.role.startsWith(partial.toLowerCase());
  });
  if (!matches.length) { _actHideDrop(); return; }
  _actShowDrop(matches, atIdx, partial, ta);
}

function _actShowDrop(matches, atIdx, partial, ta) {
  var drop = document.getElementById('act-mention-drop');
  if (!drop) return;
  drop._atIdx   = atIdx;
  drop._partial = partial;
  drop._ta      = ta;
  drop.innerHTML = matches.map(function(m, i) {
    return '<div class="act-md-item' + (i === 0 ? ' active' : '') + '" data-role="' + m.role + '"'
      + ' onmousedown="actPickMention(event,\'' + m.role + '\')">'
      + '<span class="act-md-at">@</span>'
      + '<span class="act-md-role">' + m.label + '</span>'
      + '</div>';
  }).join('');
  drop.style.display = '';
}

function _actHideDrop() {
  var drop = document.getElementById('act-mention-drop');
  if (drop) drop.style.display = 'none';
}

function actPickMention(e, role) {
  e.preventDefault();
  var drop = document.getElementById('act-mention-drop');
  if (!drop || !drop._ta) return;
  var ta      = drop._ta;
  var before  = ta.value.slice(0, drop._atIdx);
  var after   = ta.value.slice(drop._atIdx + 1 + (drop._partial || '').length);
  ta.value    = before + '@' + role + ' ' + after;
  var newPos  = drop._atIdx + role.length + 2;
  ta.setSelectionRange(newPos, newPos);
  ta.focus();
  _actHideDrop();
}

function actInputKeydown(e) {
  var drop = document.getElementById('act-mention-drop');
  if (drop && drop.style.display !== 'none') {
    if (e.key === 'Escape') { e.preventDefault(); _actHideDrop(); return; }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      var items  = Array.from(drop.querySelectorAll('.act-md-item'));
      var idx    = items.findIndex(function(el){ return el.classList.contains('active'); });
      items.forEach(function(el){ el.classList.remove('active'); });
      idx = e.key === 'ArrowDown' ? Math.min(idx + 1, items.length - 1) : Math.max(idx - 1, 0);
      if (items[idx]) items[idx].classList.add('active');
      return;
    }
    if (e.key === 'Enter' || e.key === 'Tab') {
      var active = drop.querySelector('.act-md-item.active');
      if (active) { e.preventDefault(); actPickMention({preventDefault:function(){}}, active.dataset.role); return; }
    }
  }
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); postActivity(); }
}

// ── Team Notes ────────────────────────────────────────────────────────────────
var NC_MAX = 20;

function _ncInitials() {
  if (!_currentUser) return 'ME';
  var name = (_currentUser.full_name || _currentUser.email || '').trim();
  return name.split(/\s+/).filter(Boolean).map(function(w){ return w[0]; }).join('').slice(0, 2).toUpperCase() || 'ME';
}

var _ncActiveOid = null;
var _ncPendingRender = false; // set when a render was skipped because a dropdown/thread was open

function _ncIsInteracting() {
  return _ncActiveOid !== null;
}

function _ncFlushPending() {
  if (!_ncPendingRender) return;
  _ncPendingRender = false;
  if (_activeClient === 'nlng') filterNlng(true); else filterOrders(true);
}

var _otdPendingRender = false; // set when OTD render was skipped because a row was expanded

function _otdIsInteracting() {
  return !!document.querySelector('tr.oxr:not(.hidden)');
}

function _otdFlushPending() {
  if (!_otdPendingRender) return;
  _otdPendingRender = false;
  if (_activeClient === 'nlng') renderNlngOTD(); else renderOTD();
}

var _ncSendSvg = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>';
var _ncNoteSvg = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 01-2 2H7l-4 4V5a2 2 0 012-2h14a2 2 0 012 2z"/></svg>';

function ncToggleMenu(oid, type) {
  var portal = document.getElementById('nc-portal-menu');
  var trig   = document.getElementById('nctrig-' + oid);
  if (!portal || !trig) return;
  var isOpen = _ncActiveOid === oid && portal.classList.contains('nc-open');
  // close current
  portal.classList.remove('nc-open');
  if (_ncActiveOid) { var pt = document.getElementById('nctrig-' + _ncActiveOid); if (pt) pt.classList.remove('nc-open'); }
  _ncActiveOid = null;
  if (isOpen) return; // was already open — just toggled shut
  // build portal content
  var count   = ncCountComments(oid);
  var badgeCls = 'nc-badge' + (count === 0 ? ' nc-badge-zero' : '');
  portal.innerHTML =
    '<button class="nc-act-item" onclick="openCompose(_composeData[\'' + oid + '\']);ncCloseMenu(\'' + oid + '\')">'
    + _ncSendSvg + '<span class="nc-act-item-label">Send Message</span></button>'
    + '<div class="nc-act-item-sep"></div>'
    + '<button class="nc-act-item" onclick="ncCloseMenu(\'' + oid + '\');openStoryDrawer(\'' + oid + '\',\'' + type + '\',\'activity\')">'
    + _ncNoteSvg + '<span class="nc-act-item-label">Activity</span>'
    + '<span class="' + badgeCls + '" id="ncbadge-' + oid + '">' + count + '</span></button>';
  // position and show
  var rect = trig.getBoundingClientRect();
  var mw   = 162;
  var left = rect.left;
  if (left + mw > window.innerWidth - 4) left = window.innerWidth - mw - 4;
  if (left < 4) left = 4;
  portal.style.top  = (rect.bottom + 4) + 'px';
  portal.style.left = left + 'px';
  portal.classList.add('nc-open');
  trig.classList.add('nc-open');
  _ncActiveOid = oid;
}
function ncCloseMenu(oid) {
  _ncActiveOid = null;
  if (!_ncIsInteracting()) _ncFlushPending();
}

function ncOpenNotes(oid, type) {
  ncCloseMenu(oid);
  // Close any other open thread before opening this one
  document.querySelectorAll('.nc-thread-row:not(.nc-closed)').forEach(function(openRow) {
    if (openRow.id !== 'ncthread-' + oid) {
      var prevOid = openRow.id.replace('ncthread-', '');
      openRow.classList.add('nc-closed');
      var prevTrig = document.getElementById('nctrig-' + prevOid);
      if (prevTrig) prevTrig.classList.remove('nc-notes-open');
    }
  });
  var row  = document.getElementById('ncthread-' + oid);
  var trig = document.getElementById('nctrig-' + oid);
  if (!row) return;
  var opening = row.classList.contains('nc-closed');
  row.classList.toggle('nc-closed');
  if (trig) trig.classList.toggle('nc-notes-open', opening);
  if (!opening && !_ncIsInteracting()) _ncFlushPending();
  if (opening) {
    if (row.dataset.loaded !== '1') {
      row.dataset.loaded = '1';
      ncLoadComments(oid, type);
    } else {
      var list = document.getElementById('nclist-' + oid);
      if (list) list.scrollTop = list.scrollHeight;
    }
  }
}

function ncLoadComments(oid, type) {
  authFetch('/api/comments?order_id=' + encodeURIComponent(oid) + '&type=' + encodeURIComponent(type))
    .then(function(r){ return r.ok ? r.json() : Promise.reject(r.status); })
    .then(function(data){ ncRender(oid, data); })
    .catch(function(){
      var list = document.getElementById('nclist-' + oid);
      if (list) list.innerHTML = '<div class="nc-empty-cmnt">Could not load notes.</div>';
    });
}

function ncRender(oid, comments) {
  var list = document.getElementById('nclist-' + oid);
  if (!list) return;
  if (!comments || !comments.length) {
    list.innerHTML = '<div class="nc-empty-cmnt">No notes yet. Add the first one below.</div>';
    ncUpdateBadge(oid, 0);
    ncUpdatePost(oid);
    return;
  }
  list.innerHTML = comments.map(function(c){
    return ncCommentHTML(c.id, c.author_name, c.author_role, c.body, c.created_at, oid);
  }).join('');
  ncUpdateBadge(oid, comments.length);
  ncUpdatePost(oid);
  list.scrollTop = list.scrollHeight;
}

function ncCommentHTML(id, name, role, text, ts, oid) {
  var parts    = (name || '??').split(/\s+/).filter(Boolean);
  var initials = parts.map(function(w){ return w[0]; }).join('').slice(0, 2).toUpperCase();
  var avCls    = ncAvColor(name);
  return '<div class="nc-comment" data-cid="' + _esc(String(id)) + '">'
    + '<div class="nc-av ' + avCls + '">' + _esc(initials) + '</div>'
    + '<div class="nc-c-body">'
    + '<div class="nc-c-meta">'
    + '<span class="nc-c-name">' + _esc(name) + '</span>'
    + '<span class="nc-c-role">' + _esc(role) + '</span>'
    + '<span class="nc-c-time">' + ncFmtTime(ts) + '</span>'
    + '<div class="nc-c-actions">'
    + '<button class="nc-c-act" onclick="ncStartEdit(this)" title="Edit"><i class="ri-pencil-line"></i></button>'
    + '<button class="nc-c-act nc-del" onclick="ncDelete(this,\'' + _esc(String(id)) + '\',\'' + _esc(oid) + '\')" title="Delete"><i class="ri-delete-bin-line"></i></button>'
    + '</div></div>'
    + '<div class="nc-c-text">' + _esc(text) + '</div>'
    + '</div></div>';
}

function ncAvColor(name) {
  var cols = ['nc-av-red', 'nc-av-teal', 'nc-av-blue', 'nc-av-purple'];
  var n = 0;
  for (var i = 0; i < (name || '').length; i++) n += (name || '').charCodeAt(i);
  return cols[n % cols.length];
}

function ncFmtTime(ts) {
  if (!ts) return '';
  try {
    var diff = Math.floor((Date.now() - new Date(ts)) / 60000);
    if (diff < 1)    return 'just now';
    if (diff < 60)   return diff + 'm ago';
    if (diff < 1440) return Math.floor(diff / 60) + 'h ago';
    return new Date(ts).toLocaleDateString(undefined, {day: '2-digit', month: 'short'});
  } catch(e) { return ''; }
}

function ncUpdateBadge(oid, n) {
  var el = document.getElementById('ncbadge-' + oid);
  if (!el) return;
  el.textContent = n;
  el.classList.toggle('nc-badge-zero', n === 0);
}

function ncCountComments(oid) {
  var list = document.getElementById('nclist-' + oid);
  return list ? list.querySelectorAll('.nc-comment').length : 0;
}

function ncUpdatePost(oid) {
  var ta   = document.getElementById('ncta-' + oid);
  var btn  = document.getElementById('ncpost-' + oid);
  var warn = document.getElementById('ncwarn-' + oid);
  if (!ta || !btn) return;
  var atMax = ncCountComments(oid) >= NC_MAX;
  if (warn) warn.style.display = atMax ? 'block' : 'none';
  btn.disabled = atMax || !ta.value.trim();
}

function ncAutoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 86) + 'px';
}

function ncPost(oid, type) {
  var ta  = document.getElementById('ncta-' + oid);
  var btn = document.getElementById('ncpost-' + oid);
  if (!ta || !ta.value.trim() || ncCountComments(oid) >= NC_MAX) return;
  if (btn && btn.disabled) return;
  if (btn) btn.disabled = true;
  var body = ta.value.trim();
  authFetch('/api/comments', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({order_id: oid, type: type, body: body})
  })
  .then(function(r){ return r.ok ? r.json() : Promise.reject(r.status); })
  .then(function(c){
    var list = document.getElementById('nclist-' + oid);
    if (!list) return;
    var empty = list.querySelector('.nc-empty-cmnt');
    if (empty) empty.remove();
    list.insertAdjacentHTML('beforeend', ncCommentHTML(c.id, c.author_name, c.author_role, c.body, c.created_at, oid));
    ta.value = ''; ta.style.height = 'auto';
    ncUpdateBadge(oid, ncCountComments(oid));
    ncUpdatePost(oid);
    list.scrollTop = list.scrollHeight;
  })
  .catch(function(){ if (btn) btn.disabled = false; alert('Failed to post note. Please try again.'); });
}

function ncDelete(btn, cid, oid) {
  var comment = btn.closest('.nc-comment');
  if (!comment || comment.dataset.deleting) return;
  comment.dataset.deleting = '1';
  comment.style.opacity = '0.4';
  authFetch('/api/comments/' + encodeURIComponent(cid), {method: 'DELETE'})
  .then(function(r){ if (!r.ok) throw r.status; })
  .then(function(){
    comment.remove();
    var list = document.getElementById('nclist-' + oid);
    if (list && !list.querySelector('.nc-comment'))
      list.innerHTML = '<div class="nc-empty-cmnt">No notes yet. Add the first one below.</div>';
    ncUpdateBadge(oid, ncCountComments(oid));
    ncUpdatePost(oid);
  })
  .catch(function(){
    delete comment.dataset.deleting;
    comment.style.opacity = '';
    alert('Could not delete note. Please try again.');
  });
}

function ncStartEdit(btn) {
  var c      = btn.closest('.nc-comment');
  var textEl = c.querySelector('.nc-c-text');
  if (c.querySelector('.nc-edit-area')) return;
  textEl.style.display = 'none';
  textEl.insertAdjacentHTML('afterend',
    '<textarea class="nc-edit-area" rows="2">' + _esc(textEl.textContent) + '</textarea>'
    + '<div class="nc-edit-btns">'
    + '<button class="nc-edit-cancel" onclick="ncCancelEdit(this)">Cancel</button>'
    + '<button class="nc-edit-save" onclick="ncSaveEdit(this)">Save</button>'
    + '</div>');
  var area = c.querySelector('.nc-edit-area');
  area.focus(); area.selectionStart = area.selectionEnd = area.value.length;
  ncAutoResize(area);
  area.addEventListener('input', function(){ ncAutoResize(area); });
}

function ncCancelEdit(btn) {
  var c = btn.closest('.nc-comment');
  c.querySelector('.nc-c-text').style.display = '';
  c.querySelector('.nc-edit-area').remove();
  c.querySelector('.nc-edit-btns').remove();
}

function ncSaveEdit(btn) {
  if (btn.disabled) return;
  var c    = btn.closest('.nc-comment');
  var area = c.querySelector('.nc-edit-area');
  var text = area.value.trim();
  if (!text) return;
  btn.disabled = true;
  var cid  = c.dataset.cid;
  authFetch('/api/comments/' + encodeURIComponent(cid), {
    method: 'PATCH',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({body: text})
  })
  .then(function(r){ return r.ok ? r.json() : Promise.reject(r.status); })
  .then(function(){
    var textEl = c.querySelector('.nc-c-text');
    textEl.textContent = text; textEl.style.display = '';
    area.remove(); c.querySelector('.nc-edit-btns').remove();
    var timeEl = c.querySelector('.nc-c-time');
    if (timeEl) timeEl.textContent = 'edited just now';
  })
  .catch(function(){ btn.disabled = false; alert('Could not save edit. Please try again.'); });
}

function loadStoryEmails(orderId, orderType) {
  var type = orderType || 'chevron';
  authFetch('/api/emails?order_id=' + encodeURIComponent(orderId) + '&type=' + encodeURIComponent(type))
    .then(function(r){ return r.ok ? r.json() : Promise.reject(r.status); })
    .then(function(data){
      renderStoryEmails(data);
      if (data && data.length > 0) loadStorySummary(orderId, type);
    })
    .catch(function(err){
      document.getElementById('story-scroll').innerHTML =
        '<div class="se-empty"><div class="se-empty-icon">&#9888;</div>' +
        '<div class="se-empty-title">Could not load emails</div>' +
        '<div class="se-empty-sub">Error: ' + _esc(String(err)) + '</div></div>';
    });
}

var _storyOrderId   = '';
var _storyOrderType = '';
var _storyChatHistory = [];  // [{role,content}, ...]
var _chatInflight = false;   // prevents double-sends

function loadStorySummary(orderId, orderType) {
  _storyOrderId   = orderId;
  _storyOrderType = orderType || 'chevron';
  _storyChatHistory = [];
  var card = document.getElementById('story-ai-card');
  var msgs = document.getElementById('ai-msgs');
  card.style.display = 'block';
  msgs.innerHTML = '<div class="ai-msg ai-msg-ai" id="ai-stream-out"></div>';
  document.getElementById('ai-input').disabled = true;
  document.getElementById('ai-send').disabled  = true;

  var full = '';
  var el   = document.getElementById('ai-stream-out');
  var url  = '/api/emails/summarize?order_id=' + encodeURIComponent(orderId) + '&type=' + encodeURIComponent(_storyOrderType);

  fetch(url, {headers: _authHeader ? {Authorization: _authHeader} : {}})
    .then(function(r) {
      if (!r.ok) throw new Error(r.status);
      var reader  = r.body.getReader();
      var decoder = new TextDecoder();
      var buf = '';
      function pump() {
        return reader.read().then(function(result) {
          if (result.done) return;
          buf += decoder.decode(result.value, {stream: true});
          var parts = buf.split('\n\n');
          buf = parts.pop();
          parts.forEach(function(part) {
            var line = part.trim();
            if (!line.startsWith('data: ')) return;
            var d = line.slice(6);
            if (d === '[DONE]') return;
            try {
              var parsed = JSON.parse(d);
              if (parsed.status) {
                if (!full && el) {
                  el.innerHTML = '<div class="ai-status"><div class="ai-status-dots"><span></span><span></span><span></span></div>' + _esc(parsed.status) + '</div>';
                }
              } else if (parsed.c) {
                if (!full && el) el.innerHTML = '';
                full += parsed.c;
                if (el) el.textContent = full;
              }
            } catch(e) {}
          });
          return pump();
        });
      }
      return pump();
    })
    .then(function() {
      _storyChatHistory.push({role:'assistant', content: full});
      document.getElementById('ai-input').disabled = false;
      document.getElementById('ai-send').disabled  = false;
      document.getElementById('ai-input').focus();
    })
    .catch(function() {
      card.style.display = 'none';
    });
}

function sendStoryChat() {
  if (_chatInflight) return;  // block double-sends
  var input = document.getElementById('ai-input');
  var question = input.value.trim();
  if (!question || !_storyOrderId) return;
  _chatInflight = true;
  input.value = '';
  document.getElementById('ai-send').disabled = true;
  input.disabled = true;

  var msgs = document.getElementById('ai-msgs');
  msgs.insertAdjacentHTML('beforeend', '<div class="ai-msg ai-msg-user">' + _esc(question) + '</div>');
  msgs.insertAdjacentHTML('beforeend', '<div class="ai-msg ai-loading" id="ai-typing">Thinking…</div>');
  msgs.scrollTop = msgs.scrollHeight;

  _storyChatHistory.push({role:'user', content: question});

  var replyFull = '';
  var controller = new AbortController();
  var timeoutId  = setTimeout(function() { controller.abort(); }, 45000);

  fetch('/api/emails/chat', {
    method: 'POST',
    signal: controller.signal,
    headers: Object.assign({'Content-Type':'application/json'}, _authHeader ? {Authorization: _authHeader} : {}),
    body: JSON.stringify({order_id: _storyOrderId, type: _storyOrderType, messages: _storyChatHistory})
  })
    .then(function(r) {
      if (!r.ok) throw new Error(r.status);
      var typing  = document.getElementById('ai-typing');
      if (typing) { typing.id = 'ai-reply-stream'; typing.textContent = ''; typing.className = 'ai-msg ai-msg-ai'; }
      var replyEl = document.getElementById('ai-reply-stream');
      var reader  = r.body.getReader();
      var decoder = new TextDecoder();
      var buf = '';
      function pump() {
        return reader.read().then(function(result) {
          if (result.done) return;
          buf += decoder.decode(result.value, {stream: true});
          var parts = buf.split('\n\n');
          buf = parts.pop();
          parts.forEach(function(part) {
            var line = part.trim();
            if (!line.startsWith('data: ')) return;
            var d = line.slice(6);
            if (d === '[DONE]') return;
            try {
              var c = JSON.parse(d).c || '';
              if (c === '__RATE_LIMIT__') { replyFull = '__RATE_LIMIT__'; return; }
              replyFull += c;
            } catch(e) {}
            if (replyEl) { replyEl.textContent = replyFull; msgs.scrollTop = msgs.scrollHeight; }
          });
          return pump();
        });
      }
      return pump();
    })
    .then(function() {
      var el = document.getElementById('ai-reply-stream');
      if (replyFull === '__RATE_LIMIT__') {
        // Rate limited — pop user message so retry is clean, show specific message
        if (_storyChatHistory.length && _storyChatHistory[_storyChatHistory.length - 1].role === 'user') {
          _storyChatHistory.pop();
        }
        if (el) el.outerHTML = '<div class="ai-msg ai-msg-ai" style="opacity:.6">Groq rate limit hit — wait ~30 seconds and try again.</div>';
      } else if (replyFull) {
        _storyChatHistory.push({role:'assistant', content: replyFull});
      } else {
        // Empty response — pop the unanswered user message so retry starts clean
        if (_storyChatHistory.length && _storyChatHistory[_storyChatHistory.length - 1].role === 'user') {
          _storyChatHistory.pop();
        }
        if (el) el.outerHTML = '<div class="ai-msg ai-msg-ai" style="opacity:.6">No response — please try again in a moment.</div>';
      }
      if (el) el.removeAttribute('id');
      msgs.scrollTop = msgs.scrollHeight;
    })
    .catch(function(err){
      var typing = document.getElementById('ai-typing') || document.getElementById('ai-reply-stream');
      var msg = err && err.name === 'AbortError' ? 'Request timed out — please try again.' : 'Could not get a response — please try again.';
      if (typing) typing.outerHTML = '<div class="ai-msg ai-msg-ai" style="opacity:.6">' + msg + '</div>';
      // Remove the unanswered user message from history so a retry is clean
      if (_storyChatHistory.length && _storyChatHistory[_storyChatHistory.length - 1].role === 'user') {
        _storyChatHistory.pop();
      }
    })
    .finally(function(){
      clearTimeout(timeoutId);
      _chatInflight = false;
      document.getElementById('ai-send').disabled = false;
      input.disabled = false;
      input.focus();
    });
}

function renderStoryEmails(emails) {
  var scroll = document.getElementById('story-scroll');
  if (!emails || emails.length === 0) {
    scroll.innerHTML =
      '<div class="se-empty">' +
      '<div class="se-empty-icon">&#128214;</div>' +
      '<div class="se-empty-title">No emails captured yet</div>' +
      '<div class="se-empty-sub">Emails related to this PO will appear here as they arrive through the listeners.</div>' +
      '</div>';
    return;
  }
  // Sort oldest-first; treat null received_at as epoch 0 so backfilled rows
  // (no date) sort to the top rather than the bottom.
  var sorted = emails.slice().sort(function(a, b){
    var ta = a.received_at ? new Date(a.received_at).getTime() : 0;
    var tb = b.received_at ? new Date(b.received_at).getTime() : 0;
    return ta - tb;
  });
  var tl = '<div class="story-tl">';
  sorted.forEach(function(e, idx){
    var dir = (e.direction || 'in').toLowerCase();
    var cls = dir === 'out' ? 'out' : (dir === 'sys' ? 'sys' : '');
    var initials = storyInitials(e.from_address || '');
    var dirLabel = dir === 'out' ? 'SENT' : (dir === 'sys' ? 'SYSTEM' : 'RECEIVED');
    // fmtTs returns HTML (contains <span>), so do NOT escape it
    var ts = e.received_at ? fmtTs(e.received_at) : '';
    var subj = e.subject || '(no subject)';
    var plainBody = _stripHtml(e.body_text || '');
    var preview = plainBody.slice(0, 120);
    var body = plainBody ? _esc(plainBody) : '';
    var hasBody = !!body;
    tl += '<div class="se ' + cls + '">';
    tl += '<div class="se-node"></div>';
    tl += '<div class="se-card" id="se-' + idx + '"' + (hasBody ? ' onclick="toggleStoryCard(this)"' : '') + '>';
    tl += '<div class="se-head">';
    tl += '<div class="se-av">' + _esc(initials) + '</div>';
    tl += '<div class="se-meta">';
    tl += '<div class="se-r1"><span class="se-from">' + _esc(e.from_address || '—') + '</span><span class="se-time">' + ts + '</span></div>';
    tl += '<div class="se-subj">' + _esc(subj) + '</div>';
    tl += '<div class="se-r3"><span class="se-dir">' + dirLabel + '</span><span class="se-preview">' + _esc(preview) + '</span>';
    if (hasBody) tl += '<span class="se-chev">&#9660;</span>';
    tl += '</div>';
    tl += '</div></div>';
    if (hasBody) tl += '<div class="se-body">' + body + '</div>';
    tl += '</div></div>';
  });
  tl += '</div>';
  scroll.innerHTML = tl;
}

function toggleStoryCard(card) {
  card.classList.toggle('open');
}

function _stripHtml(s) {
  if (!s) return '';
  try {
    var doc = new DOMParser().parseFromString(s, 'text/html');
    return (doc.body.textContent || '').replace(/\s+/g, ' ').trim();
  } catch(e) {
    return s.replace(/<[^>]*>/g, ' ').replace(/\s+/g, ' ').trim();
  }
}

function storyInitials(addr) {
  var name = addr.split('@')[0].replace(/[._-]+/, ' ');
  var parts = name.trim().split(/\s+/);
  if (parts.length >= 2) return (parts[0][0] + parts[parts.length-1][0]).toUpperCase();
  return name.slice(0,2).toUpperCase() || '??';
}

function showLineItems(order) {
  var items = Array.isArray(order.order_line_items) ? order.order_line_items : [];
  document.getElementById('cell-col-name').textContent = 'Line Items — ' + (order.buyer_po_number || '');
  if (items.length === 0) {
    document.getElementById('cell-val').textContent = order.extracted_description || '— (empty)';
  } else {
    var sorted = items.slice().sort(function(a,b){ return (a.line_no||0)-(b.line_no||0); });
    var lines = sorted.map(function(item, i) {
      var no = item.line_no || (i + 1);
      var desc = item.description || '(no description)';
      var qty = item.quantity ? '  ×' + item.quantity : '';
      var pd = item.promised_date ? '  [promised ' + String(item.promised_date).slice(0,10) + ']' : '';
      var dd = item.required_delivery_date ? '  [rdd ' + String(item.required_delivery_date).slice(0,10) + ']' : '';
      dd = pd + dd;
      return no + '.  ' + desc + qty + dd;
    });
    document.getElementById('cell-val').textContent = lines.join('\n\n');
  }
  document.getElementById('cell-modal').classList.remove('hidden');
}

function showSoItems(order) {
  var items = order.so_line_items || [];
  document.getElementById('cell-col-name').textContent = 'SO Items — ' + (order.so_number || '');
  if (items.length === 0) {
    document.getElementById('cell-val').textContent = '— (no SO line items)';
  } else {
    var lines = items.map(function(li, i) {
      var no   = 'Line ' + (li.line_no || (i + 1));
      var item = li.item_number || '(no item number)';
      var qty  = li.qty != null ? '  x' + li.qty + ' ' + (li.uom || '') : '';
      var dd   = li.despatch_date ? '  [dispatch ' + String(li.despatch_date).slice(0, 10) + ']' : '';
      var val  = li.extended_price != null ? '  $' + parseFloat(li.extended_price).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}) : '';
      return no + '.  ' + item + qty + dd + val;
    });
    document.getElementById('cell-val').textContent = lines.join('\n\n');
  }
  document.getElementById('cell-modal').classList.remove('hidden');
}

function showNlngLineItems(order) {
  var items = Array.isArray(order.nlng_order_line_items) ? order.nlng_order_line_items : [];
  document.getElementById('cell-col-name').textContent = 'Line Items — ' + (order.po_number || '');
  if (items.length === 0) {
    document.getElementById('cell-val').textContent = '— (empty)';
  } else {
    var sorted = items.slice().sort(function(a,b){ return (a.item_no||0)-(b.item_no||0); });
    var lines = sorted.map(function(item, i) {
      var no   = item.item_no || (i + 1);
      var desc = item.description || '(no description)';
      var qty  = item.quantity  ? '  ×' + item.quantity + ' ' + (item.uom || '')  : '';
      var dd   = item.delivery_date ? '  [due ' + String(item.delivery_date).slice(0,10) + ']' : '';
      var code = item.mesc_code ? '  [MESC: ' + item.mesc_code + ']' : '';
      var val  = item.net_amount != null ? '  ' + (order.currency || '') + ' ' + parseFloat(item.net_amount).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}) : '';
      return no + '.  ' + desc + qty + dd + code + val;
    });
    document.getElementById('cell-val').textContent = lines.join('\n\n');
  }
  document.getElementById('cell-modal').classList.remove('hidden');
}

function showNlngSoItems(order) {
  var items = order.so_line_items || [];
  document.getElementById('cell-col-name').textContent = 'SO Items — ' + (order.so_number || '');
  if (items.length === 0) {
    document.getElementById('cell-val').textContent = '— (no SO line items)';
  } else {
    var lines = items.map(function(li, i) {
      var no   = 'Line ' + (li.line_no || (i + 1));
      var item = li.item_number || '(no item number)';
      var qty  = li.qty != null ? '  x' + li.qty + ' ' + (li.uom || '') : '';
      var dd   = li.despatch_date ? '  [dispatch ' + String(li.despatch_date).slice(0, 10) + ']' : '';
      var val  = li.extended_price != null ? '  $' + parseFloat(li.extended_price).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2}) : '';
      return no + '.  ' + item + qty + dd + val;
    });
    document.getElementById('cell-val').textContent = lines.join('\n\n');
  }
  document.getElementById('cell-modal').classList.remove('hidden');
}

function closeCell() {
  document.getElementById('cell-modal').classList.add('hidden');
}

// ── Table event delegation ────────────────────────────────────────────────────
function initOtdSortEvents() {
  var otdHeadRow = document.getElementById('otd-head-row');
  if (!otdHeadRow) return;
  otdHeadRow.addEventListener('click', function(e) {
    var th = e.target.closest('th[data-col]');
    if (th) sortOtdBy(th.dataset.col);
  });
  otdHeadRow.style.cursor = 'pointer';
  updateOtdSortHeaders();
}

function initTableEvents() {
  var table  = document.getElementById('orders-table');
  var thead  = table.querySelector('thead');
  var tbody  = document.getElementById('ot-body');

  // Sort by column header click
  thead.addEventListener('click', function(e) {
    var th = e.target.closest('th[data-col]');
    if (th) sortBy(th.dataset.col);
  });

  // Select-all checkbox
  thead.addEventListener('change', function(e) {
    if (e.target.id !== 'cb-all') return;
    var start = (_page - 1) * PER_PAGE;
    var pageRows = _filtered.slice(start, start + PER_PAGE);
    pageRows.forEach(function(o) {
      var po = o.buyer_po_number || '';
      if (e.target.checked) _selected.add(po);
      else _selected.delete(po);
    });
    renderTable();
    updateSelectUI();
  });

  // Row checkbox
  tbody.addEventListener('change', function(e) {
    if (!e.target.classList.contains('row-cb')) return;
    var tr = e.target.closest('tr');
    var po = tr ? tr.dataset.po : null;
    if (!po) return;
    if (e.target.checked) _selected.add(po);
    else _selected.delete(po);
    if (tr) tr.classList.toggle('row-sel', e.target.checked);
    updateCbAll();
    updateSelectUI();
  });

  // Cell click → expand modal (skip checkbox column)
  tbody.addEventListener('click', function(e) {
    if (e.target.closest('.td-cb') || e.target.closest('.row-cb')) return;
    var td = e.target.closest('td');
    if (!td) return;
    var tr = td.closest('tr');
    if (!tr) return;
    var po = tr.dataset.po;
    var order = null;
    for (var i = 0; i < ORDERS.length; i++) {
      if (ORDERS[i].buyer_po_number === po) { order = ORDERS[i]; break; }
    }
    if (!order) return;
    var cells = Array.from(tr.cells);
    var colIdx = cells.indexOf(td) - 1; // -1 for checkbox cell
    if (colIdx < 0 || colIdx >= COLS.length) return;
    var col = COLS[colIdx];
    if (col.key === 'req_number') {
      openEditReq(order);
    } else if (col.isLineItems) {
      showLineItems(order);
    } else if (col.isSoItems) {
      showSoItems(order);
    } else if (col.isRoutingRaw) {
      var rtFull = (order.warehouse_routing_raw || '').trim();
      showCell('Routing Note — ' + (order.buyer_po_number || ''), rtFull || '— (no routing note)');
    } else {
      showCell(col.hdr, order[col.key]);
    }
  });
}

// ── NLNG table event delegation ───────────────────────────────────────────────
function initNlngTableEvents() {
  var tbody = document.getElementById('nlng-body');
  if (!tbody) return;

  // Row checkbox
  tbody.addEventListener('change', function(e) {
    if (!e.target.classList.contains('nlng-row-cb')) return;
    var tr = e.target.closest('tr');
    var rowId = tr ? tr.dataset.id : null;
    if (!rowId) return;
    if (e.target.checked) _nlngSelected.add(rowId);
    else _nlngSelected.delete(rowId);
    if (tr) tr.classList.toggle('row-sel', e.target.checked);
    updateNlngCbAll();
    updateNlngSelectUI();
  });

  // Cell expand
  tbody.addEventListener('click', function(e) {
    if (e.target.closest('.td-cb') || e.target.closest('.nlng-row-cb')) return;
    var td = e.target.closest('td');
    if (!td) return;
    var tr = td.closest('tr');
    if (!tr) return;
    var rowId = tr.dataset.id;
    var order = null;
    for (var i = 0; i < NLNG_ORDERS.length; i++) {
      if (String(NLNG_ORDERS[i].id) === rowId) { order = NLNG_ORDERS[i]; break; }
    }
    if (!order) return;
    var cells = Array.from(tr.cells);
    var colIdx = cells.indexOf(td) - 1; // -1 for checkbox cell
    if (colIdx < 0 || colIdx >= NLNG_COLS.length) return;
    var col = NLNG_COLS[colIdx];

    if (col.key === 'po_number') {
      if (e.target.tagName === 'A') return;
      showCell(col.hdr, order.po_number);
    } else if (col.isNlngEnq) {
      openEditNlngEnq(order);
    } else if (col.key === 'so_number') {
      if (e.target.tagName === 'A') return;
      showCell(col.hdr, order.so_number);
    } else if (col.isNlngItems) {
      showNlngLineItems(order);
    } else if (col.isNlngSoItems) {
      showNlngSoItems(order);
    } else if (col.isRoutingRaw) {
      var rtFull = (order.warehouse_routing_raw || '').trim();
      showCell('Routing Note — ' + (order.po_number || ''), rtFull || '— (no routing note)');
    } else if (col.key === 'stock_check_raw') {
      var rawTxt = extractStockRaw(order.stock_check_raw);
      showCell('Stock Notes — ' + (order.po_number || ''), rawTxt || (typeof order.stock_check_raw === 'object' ? JSON.stringify(order.stock_check_raw, null, 2) : (order.stock_check_raw || '—')));
    } else if (col.isNlngLive) {
      var isDone = isClosedOrder(order);
      showCell(col.hdr, isDone ? 'Closed' : 'Live');
    } else if (col.isNlngAck) {
      showCell(col.hdr, 'Acknowledged');
    } else if (col.isNlngStatus) {
      var sm = NLNG_STAGE_MAP[order.overall_status] || {lbl: order.overall_status || '—'};
      showCell(col.hdr, sm.lbl);
    } else if (col.isNlngAmt) {
      var amtDisplay = order.net_value != null
        ? (order.currency || '') + ' ' + Number(order.net_value).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2})
        : '—';
      showCell(col.hdr, amtDisplay);
    } else {
      showCell(col.hdr, order[col.key]);
    }
  });
}

// ── Sidebar toggle ────────────────────────────────────────────────────────────
function toggleSidebar() {
  _sbCollapsed = !_sbCollapsed;
  var sb = document.getElementById('sidebar');
  sb.classList.toggle('collapsed', _sbCollapsed);
  document.documentElement.style.setProperty('--sb-w', _sbCollapsed ? '54px' : _sbWidth + 'px');
}

// ── Mobile sidebar ────────────────────────────────────────────────────────────
function initMobileSidebar() {
  var hamburger = document.getElementById('btn-hamburger');
  var backdrop  = document.getElementById('mob-sb-backdrop');
  if (!hamburger || !backdrop) return;

  function openSidebar()  { document.body.classList.add('mob-sb-open'); }
  function closeSidebar() { document.body.classList.remove('mob-sb-open'); }

  hamburger.addEventListener('click', function() {
    document.body.classList.toggle('mob-sb-open');
  });
  backdrop.addEventListener('click', closeSidebar);

  // Close when navigating on mobile
  document.querySelectorAll('.sidebar .nav').forEach(function(nav) {
    nav.addEventListener('click', function() {
      if (window.innerWidth <= 768) closeSidebar();
    });
  });

  // Close on Escape
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') closeSidebar();
  });
}

// ── Sidebar resize ────────────────────────────────────────────────────────────
function initSidebarResize() {
  var handle  = document.getElementById('sb-resize');
  var toggle  = document.getElementById('sb-toggle');
  if (!handle || !toggle) return;

  toggle.addEventListener('click', toggleSidebar);

  var dragging = false;
  handle.addEventListener('mousedown', function(e) {
    if (_sbCollapsed) return;
    dragging = true;
    handle.classList.add('dragging');
    document.body.style.cursor = 'col-resize';
    document.body.style.userSelect = 'none';
    e.preventDefault();
  });
  document.addEventListener('mousemove', function(e) {
    if (!dragging || _sbCollapsed) return;
    _sbWidth = Math.max(160, Math.min(320, e.clientX));
    document.documentElement.style.setProperty('--sb-w', _sbWidth + 'px');
  });
  document.addEventListener('mouseup', function() {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove('dragging');
    document.body.style.cursor = '';
    document.body.style.userSelect = '';
  });
}

// ── Navigation ────────────────────────────────────────────────────────────────
function showPage(name) {
  document.querySelectorAll('.page').forEach(function(p){ p.classList.remove('active'); });
  document.querySelectorAll('.nav').forEach(function(v){ v.classList.remove('active'); });
  var pg = document.getElementById('page-' + name);
  if (pg) pg.classList.add('active');
  document.querySelectorAll('.nav[data-p="' + name + '"]').forEach(function(v){ v.classList.add('active'); });
  var sw = document.getElementById('client-sw-dd');
  if (sw) sw.style.display = (['orders','delays'].indexOf(name) !== -1) ? '' : 'none';
  if (name === 'orders') { if (_activeClient === 'nlng') filterNlng(); else filterOrders(); }
  if (name === 'suppliers') loadFlexitallicHistory();
  if (name === 'team') {
    if (!_currentUser || _currentUser.role !== 'admin') return;
    loadUsers();
  }
  if (name === 'messages') {
    var frame = document.getElementById('msg-iframe');
    if (frame) {
      var authMsg = {type:'spm_auth', token:_authHeader, user:_currentUser};
      if (!frame.getAttribute('src')) {
        frame.onload = function() {
          frame.contentWindow.postMessage(authMsg, window.location.origin);
        };
        frame.src = '/messages';
      } else {
        // Already loaded — resend auth so preview role changes take effect
        try { frame.contentWindow.postMessage(authMsg, window.location.origin); } catch(e) {}
      }
    }
  }
  if (name === 'delays') {
    if (_activeClient === 'nlng') {
      buildNlngOtdPeriodChips();
      renderNlngOTD();
    } else {
      buildOtdPeriodChips();
      renderOTD();
    }
  }
  if (name === 'quotes') {
    _applyQuoteCompanyUI();
    loadQuotes();
  }
  if (name === 'purchase-orders') {
    loadPurchaseOrders();
  }
  if (name === 'reports') {
    updateReports();
  }
}

function setF(el, f) {
  _activeFilters.clear();
  _activeFilters.add(f === 'all' ? 'all' : f);
  document.querySelectorAll('#status-chips .chip').forEach(function(c){
    c.classList.toggle('on', _activeFilters.has(c.dataset.f));
  });
  filterOrders();
}

document.querySelectorAll('.nav[data-p]').forEach(function(navEl) {
  navEl.addEventListener('click', function(){ showPage(navEl.dataset.p); });
});

document.getElementById('goto-orders').addEventListener('click', function(){ showPage('orders'); });

// Cell modal — click backdrop to close
document.getElementById('cell-modal').addEventListener('click', function(e) {
  if (e.target === this) closeCell();
});
document.getElementById('add-user-modal').addEventListener('click', function(e) {
  if (e.target === this) closeAddUserModal();
});
document.getElementById('reset-pw-modal').addEventListener('click', function(e) {
  if (e.target === this) closeResetPwModal();
});
document.getElementById('cell-close-btn').addEventListener('click', closeCell);
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeCell();
});

// ── REQ# / ENQ# inline edit ──────────────────────────────────────────────────
var _editOrderId   = null;
var _editOrderPO   = null;
var _editNlngId    = null;
var _editNlngMode  = false;

function openEditNlngEnq(order) {
  _editNlngMode = true;
  _editNlngId   = order.id;
  document.getElementById('edit-col-label').textContent = 'ENQ#';
  document.getElementById('edit-po-label').textContent  = 'NLNG PO: ' + (order.po_number || '');
  var inp = document.getElementById('edit-req-input');
  inp.value = order.enquiry_number || '';
  inp.placeholder = 'e.g. ENQ-12345';
  document.getElementById('edit-error').style.display = 'none';
  document.getElementById('edit-modal').classList.remove('hidden');
  inp.focus(); inp.select();
}

function openEditReq(order) {
  _editNlngMode = false;
  _editOrderId = order.id;
  _editOrderPO = order.buyer_po_number;
  document.getElementById('edit-col-label').textContent = 'REQ#';
  document.getElementById('edit-po-label').textContent = 'Chevron PO: ' + (order.buyer_po_number || '');
  var inp = document.getElementById('edit-req-input');
  inp.value = order.req_number || '';
  inp.placeholder = 'e.g. REQ0612726';
  document.getElementById('edit-error').style.display = 'none';
  document.getElementById('edit-modal').classList.remove('hidden');
  inp.focus();
  inp.select();
}

function closeEditReq() {
  document.getElementById('edit-modal').classList.add('hidden');
  _editOrderId = null;
}

async function saveEditReq() {
  if (_editNlngMode) { await _saveNlngEnq(); return; }
  if (!_editOrderId) return;
  var val = document.getElementById('edit-req-input').value.trim();
  var errEl = document.getElementById('edit-error');
  errEl.style.display = 'none';
  var saveBtn = document.getElementById('edit-save-btn');
  saveBtn.disabled = true;
  saveBtn.textContent = 'Saving…';
  try {
    var res = await authFetch('/api/orders/' + _editOrderId + '/req_number', {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({req_number: val || null})
    });
    var data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Server error');
    for (var i = 0; i < ORDERS.length; i++) {
      if (ORDERS[i].id === _editOrderId) {
        ORDERS[i].req_number = val || null;
        break;
      }
    }
    filterOrders();
    closeEditReq();
  } catch(e) {
    errEl.textContent = 'Error: ' + e.message;
    errEl.style.display = 'block';
  } finally {
    saveBtn.disabled = false;
    saveBtn.textContent = 'Save';
  }
}

async function _saveNlngEnq() {
  if (!_editNlngId) return;
  var val = document.getElementById('edit-req-input').value.trim();
  var errEl = document.getElementById('edit-error');
  errEl.style.display = 'none';
  var saveBtn = document.getElementById('edit-save-btn');
  saveBtn.disabled = true;
  saveBtn.textContent = 'Saving…';
  try {
    var res = await authFetch('/api/nlng_orders/' + _editNlngId + '/enquiry_number', {
      method: 'PATCH',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enquiry_number: val || null})
    });
    var data = await res.json();
    if (!res.ok || data.error) throw new Error(data.error || 'Server error');
    for (var i = 0; i < NLNG_ORDERS.length; i++) {
      if (NLNG_ORDERS[i].id === _editNlngId) {
        NLNG_ORDERS[i].enquiry_number = val || null;
        break;
      }
    }
    filterNlng(true);
    closeEditReq();
  } catch(e) {
    errEl.textContent = 'Error: ' + e.message;
    errEl.style.display = 'block';
  } finally {
    saveBtn.disabled = false;
    saveBtn.textContent = 'Save';
  }
}

document.getElementById('edit-save-btn').addEventListener('click', saveEditReq);
document.getElementById('edit-cancel-btn').addEventListener('click', closeEditReq);
document.getElementById('edit-close-btn').addEventListener('click', closeEditReq);
document.getElementById('edit-modal').addEventListener('click', function(e) {
  if (e.target === this) closeEditReq();
});
document.getElementById('edit-req-input').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') saveEditReq();
  if (e.key === 'Escape') closeEditReq();
});

// ── Login ─────────────────────────────────────────────────────────────────────
function _launchApp() {
  ['screen-welcome','screen-department','screen-login'].forEach(function(id){
    var el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });
  document.getElementById('screen-app').style.display  = 'flex';
  // Populate sidebar with real user info
  if (_currentUser) {
    var nameParts = (_currentUser.name || _currentUser.email || '').split(' ');
    var initials  = nameParts.length >= 2
      ? (nameParts[0][0] + nameParts[nameParts.length - 1][0]).toUpperCase()
      : (_currentUser.name || _currentUser.email || '??').slice(0, 2).toUpperCase();
    var roleLabel = (_currentUser.role || '').charAt(0).toUpperCase() + (_currentUser.role || '').slice(1);
    document.getElementById('sb-av').textContent    = initials;
    document.getElementById('sb-uname').textContent = _currentUser.name || _currentUser.email;
    document.getElementById('sb-urole').textContent = roleLabel;
    var tbAv = document.querySelector('.tb-av');
    if (tbAv) tbAv.textContent = initials;
  }
  applyRoleVisibility();
  _syncThemeBtns();
  if (_currentUser && _currentUser.role === 'admin') loadUsers();
  buildStatusChips();
  buildHeaders();
  initTableEvents();
  initNlngTableEvents();
  initOtdSortEvents();
  initNlngOtdSortEvents();
  initSidebarResize();
  initMobileSidebar();
  document.getElementById('btn-refresh').addEventListener('click', loadOrders);
  document.getElementById('btn-export').addEventListener('click', exportCSV);
  document.getElementById('nlng-refresh').addEventListener('click', loadNlngOrders);
  document.getElementById('nlng-btn-export').addEventListener('click', exportNlngCSV);
  loadOrders();
  loadNlngOrders();
  loadAlerts();
  loadUnreadCount();
  checkMentions();
  initSSE();
  startPolling();
  _initSW();
}

// ── Department → email mapping (mirrors server DEPT_EMAILS) ──────────────────
var DEPT_EMAILS = {
  procurement: 'specialpiping@gmail.com',
  warehouse:   'spmwarehouse22@gmail.com',
  accounts:    'accounts@specialpipingltd.com',
  expeditor:   'etsano@specialpipingltd.com',
};
var _selectedDept = '';

// ── Welcome screen animation ──────────────────────────────────────────────────
function _startWelcome() {
  var welcome = document.getElementById('screen-welcome');
  if (!welcome) { _showDeptScreen(); return; }
  welcome.style.display = '';
  // Animate the title text revealing left-to-right via clip-path
  var title = document.getElementById('welcome-title');
  if (title) {
    title.style.clipPath = 'inset(0 100% 0 0)';
    title.style.opacity  = '1';
    // Small delay so the logo fades in first
    setTimeout(function() {
      title.style.transition = 'clip-path 1.2s cubic-bezier(0.22,0.61,0.36,1)';
      title.style.clipPath   = 'inset(0 0% 0 0)';
    }, 400);
  }
  // Transition to department screen after 2.8s
  setTimeout(function() {
    welcome.style.transition = 'opacity 0.5s ease';
    welcome.style.opacity    = '0';
    setTimeout(function() {
      welcome.style.display = 'none';
      _showDeptScreen();
    }, 500);
  }, 2800);
}

function _showDeptScreen() {
  var dept = document.getElementById('screen-department');
  if (dept) {
    dept.style.display  = '';
    dept.style.opacity  = '0';
    dept.style.transition = 'opacity 0.4s ease';
    setTimeout(function() { dept.style.opacity = '1'; }, 20);
  }
}

function selectDept(dept) {
  _selectedDept = dept;
  var deptScreen  = document.getElementById('screen-department');
  var loginScreen = document.getElementById('screen-login');
  if (deptScreen) deptScreen.style.display = 'none';
  if (loginScreen) {
    loginScreen.style.display = '';
    loginScreen.style.opacity = '0';
    loginScreen.style.transition = 'opacity 0.35s ease';
    setTimeout(function() { loginScreen.style.opacity = '1'; }, 20);
  }
  // Update dept badge
  var badge = document.getElementById('login-dept-badge');
  var LABELS = {admin:'Admin',procurement:'Procurement',warehouse:'Warehouse',accounts:'Accounts',expeditor:'Expeditor'};
  if (badge) badge.textContent = LABELS[dept] || dept;
  // Show/hide admin email vs dept email rows
  var adminRow = document.getElementById('admin-email-row');
  var deptRow  = document.getElementById('dept-email-row');
  var emailDisp = document.getElementById('dept-email-display');
  if (dept === 'admin') {
    if (adminRow) adminRow.style.display = '';
    if (deptRow)  deptRow.style.display  = 'none';
  } else {
    if (adminRow) adminRow.style.display = 'none';
    if (deptRow)  deptRow.style.display  = '';
    if (emailDisp) emailDisp.textContent = DEPT_EMAILS[dept] || '';
  }
  // Reset form
  var pwEl = document.getElementById('pw');
  var unEl = document.getElementById('un');
  var emEl = document.getElementById('em');
  if (pwEl) pwEl.value = '';
  if (unEl) unEl.value = '';
  if (emEl) emEl.value = '';
  var errEl = document.getElementById('login-err-msg');
  if (errEl) { errEl.textContent = 'SPM Procurement · Authorised personnel only'; errEl.style.color = ''; }
  setTimeout(function() { if (unEl) unEl.focus(); else if (emEl) emEl.focus(); }, 50);
}

function goBackToDept() {
  var loginScreen = document.getElementById('screen-login');
  var deptScreen  = document.getElementById('screen-department');
  if (loginScreen) loginScreen.style.display = 'none';
  if (deptScreen)  { deptScreen.style.display = ''; deptScreen.style.opacity = '1'; }
  _selectedDept = '';
}

document.getElementById('btn-in').addEventListener('click', async function() {
  var username = (document.getElementById('un')  || {}).value || '';
  var password = (document.getElementById('pw')  || {}).value || '';
  var email    = (document.getElementById('em')  || {}).value || '';
  var errEl    = document.getElementById('login-err-msg');
  var btn      = this;
  username = username.trim();
  var SIGN_SVG = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>';
  btn.innerHTML = 'Signing in&hellip;';
  btn.disabled  = true;
  try {
    var payload = { department: _selectedDept, username: username, password: password };
    if (_selectedDept === 'admin') payload.email = email.trim().toLowerCase();
    var res  = await fetch('/api/auth/login', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify(payload),
    });
    var data = await res.json();
    if (!res.ok) {
      errEl.textContent = data.error || 'Invalid credentials.';
      errEl.style.color = 'var(--crit)';
      btn.innerHTML     = 'Sign in ' + SIGN_SVG;
      btn.disabled      = false;
      return;
    }
    _authHeader  = 'Bearer ' + data.token;
    _currentUser = data.user;
    localStorage.setItem('spm_auth',  _authHeader);
    localStorage.setItem('spm_user',  JSON.stringify(data.user));
    _launchApp();
  } catch(e) {
    errEl.textContent = 'Could not reach server.';
    errEl.style.color = 'var(--crit)';
    btn.innerHTML     = 'Sign in ' + SIGN_SVG;
    btn.disabled      = false;
  }
});

// Enter key submits login
document.getElementById('pw').addEventListener('keydown', function(e) {
  if (e.key === 'Enter') document.getElementById('btn-in').click();
});

function logout() {
  localStorage.removeItem('spm_auth');
  localStorage.removeItem('spm_user');
  _authHeader   = '';
  _currentUser  = null;
  _selectedDept = '';
  _realAdminRole = null;
  // Stop background timers and SSE so they don't keep firing unauthenticated requests
  if (_sseSource)   { try { _sseSource.close(); } catch(e){}  _sseSource   = null; }
  if (_pollTimer)   { clearInterval(_pollTimer);               _pollTimer   = null; }
  if (_swPingTimer) { clearInterval(_swPingTimer); _swPingTimer = null; }
  var previewBar = document.getElementById('preview-bar');
  if (previewBar) previewBar.classList.remove('on');
  // Go back to department selection without a full page reload
  var app  = document.getElementById('screen-app');
  var dept = document.getElementById('screen-department');
  if (app)  app.style.display  = 'none';
  if (dept) { dept.style.display = ''; dept.style.opacity = '1'; }
}

// Auto-launch if a valid token is already stored; otherwise show welcome
if (_authHeader && _currentUser) {
  _launchApp();
} else {
  _startWelcome();
}

// ── OTD TRACKER ──────────────────────────────────────────────────────────────

var _otdFilter = 'all';

var OTD_STAGES = [
  { key:'notification_received_at',      lbl:'Received'    },
  { key:'order_submitted_on',            lbl:'Submitted'   },
  { key:'sent_to_warehouse_at',          lbl:'Warehouse'   },
  { key:'stock_check_completed_at',      lbl:'Stock ✓' },
  { key:'spm_po_sent_at',                lbl:'PO → Flex' },
  { key:'so_received_at',                lbl:'SO Rcvd'     },
  { key:'so_sent_to_warehouse_at',       lbl:'WH Fwd'      },
  { key:'flex_dispatch_ready_at',        lbl:'Packed'      },
  { key:'dispatch_instructions_sent_at', lbl:'Instr Sent'  },
  { key:'ready_for_dispatch_at',         lbl:'Coll. Arr.'  },
  { key:'dispatched_at',                 lbl:'Dispatched'  },
  { key:'delivery_requested_at',         lbl:'Del. Req.'   },
  { key:'delivered_at',                  lbl:'Delivered'   }
];

function _dateMidnight(s) {
  // Parse any date or datetime string as LOCAL midnight to avoid the UTC-midnight trap.
  // "2026-07-14" and "2026-07-14T09:00:00Z" both become the same local midnight.
  if (!s) return null;
  var p = String(s).slice(0, 10).split('-');
  return new Date(+p[0], +p[1] - 1, +p[2]);
}

// Latest promised_date across line items (order complete only when last item delivered).
function getPoPromisedDate(o) {
  var lids = (o.order_line_items || []).filter(function(li) { return li.promised_date; });
  if (!lids.length) return null;
  return lids.reduce(function(max, li) { return li.promised_date > max ? li.promised_date : max; }, lids[0].promised_date);
}

// OTD benchmark date: PO promised date (Chevron line items) → required delivery date.
// NLNG has no per-line-item promised date, so it always scores against required_delivery_date.
function getOtdDate(o) {
  return getPoPromisedDate(o) || o.required_delivery_date;
}

function otdDaysLeft(o) {
  var d = getOtdDate(o);
  if (!d || o.delivered_at) return null;
  var now = new Date(); now.setHours(0, 0, 0, 0);
  return Math.round((_dateMidnight(d) - now) / 86400000);
}

function otdClass(o) {
  if (o.delivered_at) {
    var d = getOtdDate(o);
    if (!d) return 'del-otd';
    return _dateMidnight(o.delivered_at) <= _dateMidnight(d) ? 'del-otd' : 'del-late';
  }
  var dl = otdDaysLeft(o);
  if (dl === null) return 'no-date';
  if (dl < 0)  return 'overdue';
  if (dl < 7)  return 'critical';
  if (dl < 30) return 'at-risk';
  return 'on-track';
}

function otdLabel(cls) {
  return {
    'on-track':'On Track', 'at-risk':'At Risk', 'overdue':'Overdue',
    'critical':'Critical', 'del-otd':'✓ OTD', 'del-late':'Late Delivery', 'no-date':'No Date'
  }[cls] || cls;
}

function fmtDur(ms) {
  if (!ms || isNaN(ms) || ms < 0) return '—';
  var m = Math.floor(ms/60000), h = Math.floor(m/60), d = Math.floor(h/24), mo = Math.floor(d/30);
  m %= 60; h %= 24; d %= 30;
  if (mo >= 2)  return mo + 'mo ' + d + 'd';
  if (d >= 1)   return (mo ? mo + 'mo ' : '') + d + 'd' + (h ? ' ' + h + 'h' : '');
  if (h >= 1)   return h + 'h ' + m + 'm';
  return m + 'm';
}

function fmtOtdDate(s) {
  if (!s) return '—';
  return new Date(s).toLocaleDateString('en-GB', {day:'numeric', month:'short', year:'2-digit'});
}

function fmtOtdShort(s) {
  if (!s) return '';
  return String(s).slice(5,10).replace('-','/');
}

function dlCellHtml(o) {
  var otdDate = getOtdDate(o);
  if (o.delivered_at && otdDate) {
    var diff = Math.round((_dateMidnight(otdDate) - _dateMidnight(o.delivered_at)) / 86400000);
    if (diff >= 0) return '<div class="odl grey"><div class="n">✓ OTD</div><div class="u">+' + diff + 'd early</div></div>';
    return '<div class="odl warn"><div class="n">Late</div><div class="u">' + Math.abs(diff) + 'd after</div></div>';
  }
  var dl = otdDaysLeft(o);
  if (dl === null) return '<div class="odl grey"><div class="n">—</div><div class="u">no date</div></div>';
  if (dl < 0)  return '<div class="odl crit"><div class="n">–' + Math.abs(dl) + 'd</div><div class="u">overdue</div></div>';
  if (dl < 7)  return '<div class="odl crit"><div class="n">' + dl + 'd</div><div class="u">critical</div></div>';
  if (dl < 30) return '<div class="odl warn"><div class="n">' + dl + 'd</div><div class="u">remaining</div></div>';
  return '<div class="odl ok"><div class="n">' + dl + 'd</div><div class="u">remaining</div></div>';
}


function gapCellHtml(o) {
  if (!o.promised_date || !o.required_delivery_date) return '<span class="ogp z">—</span>';
  var g = Math.round((new Date(o.promised_date) - new Date(o.required_delivery_date)) / 86400000);
  if (g < 0) return '<span class="ogp e">' + Math.abs(g) + 'd early</span>';
  if (g > 0) return '<span class="ogp l">+' + g + 'd late</span>';
  return '<span class="ogp z">same day</span>';
}

function liCountCellHtml(count) {
  var cls = count >= 10 ? 'crit' : count >= 4 ? 'warn' : 'grey';
  return '<div class="odl ' + cls + '"><div class="n">' + count + '</div><div class="u">' + (count === 1 ? 'item' : 'items') + '</div></div>';
}

function buildOtdTimeline(o) {
  var lastDone = -1;
  OTD_STAGES.forEach(function(sf, i) { if (o[sf.key]) lastDone = i; });
  var currIdx = lastDone + 1;
  if (lastDone === OTD_STAGES.length - 1) currIdx = lastDone;
  var h = '';
  OTD_STAGES.forEach(function(sf, i) {
    var val    = o[sf.key];
    var isDone = !!val;
    var isCurr = i === currIdx && !isDone;
    var dotCls = isCurr ? 'curr' : (isDone ? 'done' : 'pend');
    h += '<div class="otn"><div class="otd-dot ' + dotCls + '"></div>';
    h += '<div class="otl-lbl"><div class="nm">' + sf.lbl + '</div>';
    if (val)      h += '<div class="dt">' + fmtOtdShort(val) + '</div>';
    else if (isCurr) h += '<div class="cu">now</div>';
    h += '</div></div>';
    if (i < OTD_STAGES.length - 1) {
      var nextVal = o[OTD_STAGES[i+1].key];
      var dur = (val && nextVal) ? fmtDur(new Date(nextVal) - new Date(val))
                : (val && i === lastDone && !o.delivered_at) ? fmtDur(new Date() - new Date(val))
                : '…';
      var lineCls = (isDone && !isCurr) ? 'done' : (isCurr ? 'curr' : '');
      h += '<div class="otc"><div class="otl-line ' + lineCls + '"></div>';
      h += '<div class="otl-dur">' + dur + '</div></div>';
    }
  });
  return h;
}

function buildOtdExpand(o) {
  var age  = o.notification_received_at ? fmtDur(new Date() - new Date(o.notification_received_at)) : '—';
  var done = OTD_STAGES.filter(function(sf){ return !!o[sf.key]; }).length;
  var h = '<div class="oxi"><div class="oxi-ttl">Stage Timeline — elapsed time between each pipeline step</div>';
  h += '<div class="otl">' + buildOtdTimeline(o) + '</div>';
  h += '<div class="oxm">';
  h += '<div class="oxmi"><div class="k">Pipeline Age</div><div class="v">' + age + '</div></div>';
  h += '<div class="oxmi"><div class="k">Stages Done</div><div class="v">' + done + ' / ' + OTD_STAGES.length + '</div></div>';
  if (o.notification_received_at) h += '<div class="oxmi"><div class="k">Received</div><div class="v">' + fmtOtdDate(o.notification_received_at) + '</div></div>';
  if (o.required_delivery_date)   h += '<div class="oxmi"><div class="k">Required By</div><div class="v">' + fmtOtdDate(o.required_delivery_date) + '</div></div>';
  var ppd = getPoPromisedDate(o);
  if (ppd)                        h += '<div class="oxmi"><div class="k">PO Promised</div><div class="v">' + fmtOtdDate(ppd) + '</div></div>';
  if (o.promised_date)            h += '<div class="oxmi"><div class="k">SO Promised</div><div class="v">' + fmtOtdDate(o.promised_date) + '</div></div>';
  if (o.delivered_at)             h += '<div class="oxmi"><div class="k">Delivered</div><div class="v">' + fmtOtdDate(o.delivered_at) + '</div></div>';
  h += '</div></div>';
  return h;
}

function setOtdFilter(f) {
  _otdFilter = f;
  _otdDeliveredSub = 'all';
  _otdPage = 1;
  document.querySelectorAll('#otd-chips .otd-chip').forEach(function(c) {
    c.classList.toggle('on', c.dataset.f === f);
  });
  var subBar = document.getElementById('otd-del-sub-bar');
  if (subBar) {
    subBar.classList.toggle('hidden', f !== 'delivered');
    // reset sub-chip active state
    subBar.querySelectorAll('.otd-chip').forEach(function(c) {
      c.classList.toggle('on', c.dataset.sf === 'all');
    });
  }
  renderOTD();
}

function renderOTD() {
  if (_otdIsInteracting()) { _otdPendingRender = true; return; }
  updateOtdSortHeaders();
  var orders = ORDERS;
  var tbody = document.getElementById('otd-body');
  if (!tbody) return;
  if (!orders || !orders.length) {
    tbody.innerHTML = '<tr><td colspan="14" style="text-align:center;padding:3rem;color:var(--t3)">No orders loaded</td></tr>';
    return;
  }

  var counts = {};
  var rows   = [];

  orders.forEach(function(o, idx) {
    var cls = otdClass(o);
    var lastTs = null;
    for (var i = OTD_STAGES.length - 1; i >= 0; i--) {
      if (o[OTD_STAGES[i].key]) { lastTs = o[OTD_STAGES[i].key]; break; }
    }
    var inStage  = (lastTs && !o.delivered_at) ? fmtDur(new Date() - new Date(lastTs)) : '—';
    var rcvdAgo  = o.notification_received_at ? fmtDur(new Date() - new Date(o.notification_received_at)) : '—';
    var stageLbl;
    var pastStock = !!o.spm_po_sent_at;
    if (!pastStock && isPartialStock(o)) {
      stageLbl = 'Partial stock';
    } else if (!pastStock && isNotInStock(o)) {
      stageLbl = 'Not in stock';
    } else if (!pastStock && o.overall_status === 'stock_check_needs_review') {
      var rawSnip = extractStockRaw(o.stock_check_raw);
      stageLbl = rawSnip ? 'Review: ' + rawSnip.replace(/[\r\n]+/g,' ').slice(0, 28) + '…' : 'Needs review';
    } else {
      stageLbl = (STAGE_MAP[o.overall_status] || {}).lbl || (o.overall_status || '—');
    }
    var lastTsMs = lastTs ? new Date(lastTs).getTime() : 0;
    rows.push({ cls:cls, idx:idx, o:o, inStage:inStage, rcvdAgo:rcvdAgo, stageLbl:stageLbl, lastTsMs:lastTsMs });
  });

  var _otdQ = ((document.getElementById('otd-q') || {}).value || '').trim().toLowerCase();
  if (_otdQ) {
    rows = rows.filter(function(row) {
      var o = row.o;
      return (o.buyer_po_number || '').toLowerCase().indexOf(_otdQ) >= 0
          || (o.extracted_description || '').toLowerCase().indexOf(_otdQ) >= 0
          || (o.spm_po_number || '').toLowerCase().indexOf(_otdQ) >= 0;
    });
  }

  // Apply period filter before counting so chips reflect the active window
  var _now = new Date();
  var periodRows = _otdPeriodFilter === 'all' ? rows : rows.filter(function(row) {
    var rts = row.o.notification_received_at;
    if (!rts) return false;
    var rd = new Date(rts);
    var sp = _otdPeriodFilter.slice(4);
    if (sp === '24h')   return (_now - rd) <= 86400000;
    if (sp === 'week')  return (_now - rd) <= 7  * 86400000;
    if (sp === '2wk')   return (_now - rd) <= 14 * 86400000;
    if (sp === 'month') return (_now - rd) <= 30 * 86400000;
    if (sp.length === 7) { var yr2 = +sp.slice(0,4), mo2 = +sp.slice(5,7)-1; return rd.getFullYear() === yr2 && rd.getMonth() === mo2; }
    return true;
  });
  periodRows.forEach(function(row) { counts[row.cls] = (counts[row.cls] || 0) + 1; });

  var OTD_CLS_ORDER = {'on-track':1,'at-risk':2,'overdue':3,'critical':4,'del-otd':5,'del-late':6,'no-date':7};
  periodRows.sort(function(a, b) {
    var col = _otdSortCol;
    var dir = _otdSortDir === 'asc' ? 1 : -1;
    var av, bv;
    if (col === '_in_stage') {
      av = a.lastTsMs; bv = b.lastTsMs;
      return (av - bv) * dir;
    }
    if (col === '_li_count') {
      av = (a.o.order_line_items || []).length || 1;
      bv = (b.o.order_line_items || []).length || 1;
      return (av - bv) * dir;
    }
    if (col === '_otd') {
      av = OTD_CLS_ORDER[a.cls] || 9; bv = OTD_CLS_ORDER[b.cls] || 9;
      return (av - bv) * dir;
    }
    if (col === '_po_promised') {
      av = getPoPromisedDate(a.o); bv = getPoPromisedDate(b.o);
      av = av ? new Date(av).getTime() : 0; bv = bv ? new Date(bv).getTime() : 0;
      return (av - bv) * dir;
    }
    if (col === '_days_left') {
      av = getOtdDate(a.o); bv = getOtdDate(b.o);
      av = av ? new Date(av).getTime() : 0; bv = bv ? new Date(bv).getTime() : 0;
      return (av - bv) * dir;
    }
    if (col === '_gap') {
      av = a.o.promised_date ? new Date(a.o.promised_date).getTime() : 0;
      bv = b.o.promised_date ? new Date(b.o.promised_date).getTime() : 0;
      return (av - bv) * dir;
    }
    if (col === 'overall_status') {
      av = a.stageLbl || ''; bv = b.stageLbl || '';
      return av.localeCompare(bv, undefined, {sensitivity:'base'}) * dir;
    }
    av = a.o[col] != null ? a.o[col] : '';
    bv = b.o[col] != null ? b.o[col] : '';
    return String(av).localeCompare(String(bv), undefined, {numeric:true, sensitivity:'base'}) * dir;
  });

  var el = document.getElementById('otd-c-ok');   if (el) el.textContent = counts['on-track'] || 0;
  el = document.getElementById('otd-c-risk');      if (el) el.textContent = counts['at-risk']  || 0;
  el = document.getElementById('otd-c-crit');      if (el) el.textContent = (counts['overdue'] || 0) + (counts['critical'] || 0);
  el = document.getElementById('otd-c-del');       if (el) el.textContent = (counts['del-otd'] || 0) + (counts['del-late'] || 0);

  // Chevron OTD score — weighted by line item count, not PO count
  var liOtd = 0, liLate = 0;
  periodRows.forEach(function(row) {
    if (row.cls !== 'del-otd' && row.cls !== 'del-late') return;
    var n = (row.o.order_line_items || []).length || 1;
    if (row.cls === 'del-otd') liOtd += n; else liLate += n;
  });
  var liTotal = liOtd + liLate;
  var scoreCard = document.getElementById('otd-score-card');
  el = document.getElementById('otd-c-score');
  if (el) el.textContent = liTotal ? Math.round(liOtd / liTotal * 100) + '%' : '—';
  el = document.getElementById('otd-c-score-sub');
  if (el) el.textContent = liTotal ? liOtd + ' / ' + liTotal + ' line items' : 'no deliveries yet';
  if (scoreCard) {
    scoreCard.classList.remove('c-ok', 'c-warn', 'c-crit', 'c-grey');
    if (!liTotal)                    scoreCard.classList.add('c-grey');
    else if (liOtd / liTotal >= 0.9) scoreCard.classList.add('c-ok');
    else if (liOtd / liTotal >= 0.7) scoreCard.classList.add('c-warn');
    else                             scoreCard.classList.add('c-crit');
  }

  var visRows = periodRows.filter(function(row) {
    if (_otdFilter !== 'all') {
      var cls = row.cls;
      if (_otdFilter === 'on-track'  && cls !== 'on-track')  return false;
      if (_otdFilter === 'at-risk'   && cls !== 'at-risk')   return false;
      if (_otdFilter === 'overdue'   && cls !== 'overdue' && cls !== 'critical') return false;
      if (_otdFilter === 'critical'  && cls !== 'critical')  return false;
      if (_otdFilter === 'delivered') {
        if (cls !== 'del-otd' && cls !== 'del-late') return false;
        if (_otdDeliveredSub === 'del-otd'  && cls !== 'del-otd')  return false;
        if (_otdDeliveredSub === 'del-late' && cls !== 'del-late') return false;
      }
    }
    return true;
  });
  var otdPages = Math.max(1, Math.ceil(visRows.length / OTD_PER_PAGE));
  if (_otdPage > otdPages) _otdPage = 1;
  var pageStart = (_otdPage - 1) * OTD_PER_PAGE;
  var pageRows  = visRows.slice(pageStart, pageStart + OTD_PER_PAGE);

  var html = '';
  pageRows.forEach(function(row) {
    var promised = row.o.promised_date ? fmtOtdDate(row.o.promised_date) : '<span style="color:var(--t3)">—</span>';
    html += '<tr class="odr ' + row.cls + '" data-idx="' + row.idx + '" data-cls="' + row.cls + '">';
    html += '<td style="width:32px"><button class="oxbtn" data-idx="' + row.idx + '">▶</button></td>';
    var gmailSearchPO = 'https://mail.google.com/mail/?authuser=specialpiping%40gmail.com#search/' + encodeURIComponent(row.o.buyer_po_number || '');
    html += '<td><div class="odr-po"><a href="' + gmailSearchPO + '" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="Search Gmail for this PO" style="color:inherit;text-decoration:none;border-bottom:1px dotted var(--accent)">' + n(row.o.buyer_po_number) + '</a></div>'
          + (row.o.po_amount ? '<div class="odr-amt">' + (row.o.po_currency === 'NGN' ? '₦' : '$') + parseFloat(row.o.po_amount).toLocaleString('en-US',{maximumFractionDigits:0}) + '</div>' : '')
          + '</td>';
    // SO Number — Gmail search by SO number
    if (row.o.so_number) {
      var gmailSearchSO = 'https://mail.google.com/mail/?authuser=specialpiping%40gmail.com#search/' + encodeURIComponent(row.o.so_number);
      html += '<td><div class="odr-po" style="font-size:11px"><a href="' + gmailSearchSO + '" target="_blank" rel="noopener" onclick="event.stopPropagation()" title="Search Gmail for ' + row.o.so_number + '" style="color:inherit;text-decoration:none;border-bottom:1px dotted var(--accent)">' + row.o.so_number + '</a></div></td>';
    } else {
      html += '<td><span style="color:var(--t3)">—</span></td>';
    }
    html += '<td title="' + (row.o.buyer_name||'') + '" style="max-width:105px;overflow:hidden;text-overflow:ellipsis">' + n(row.o.buyer_name) + '</td>';
    html += '<td title="' + (row.o.po_destination||'') + '" style="max-width:100px;overflow:hidden;text-overflow:ellipsis">' + n(row.o.po_destination) + '</td>';
    html += '<td><div class="odc">' + fmtOtdDate(row.o.notification_received_at) + '<div class="odc-ago">' + row.rcvdAgo + ' ago</div></div></td>';
    html += '<td><div class="odc">' + fmtOtdDate(row.o.required_delivery_date) + '</div></td>';
    html += '<td><div class="odc">' + fmtOtdDate(getPoPromisedDate(row.o)) + '</div></td>';
    html += '<td class="c">' + dlCellHtml(row.o) + '</td>';
    html += '<td><div class="odc">' + promised + '</div></td>';
    html += '<td>' + gapCellHtml(row.o) + '</td>';
    html += '<td><span class="osp" title="' + row.stageLbl + '">' + row.stageLbl + '</span></td>';
    html += '<td><span class="otin">' + row.inStage + '</span></td>';
    var chevLiCnt = (row.o.order_line_items || []).length || 1;
    html += '<td class="c">' + liCountCellHtml(chevLiCnt) + '</td>';
    html += '<td><span class="obd ' + row.cls + '">' + otdLabel(row.cls) + '</span></td>';
    html += '</tr>';
    html += '<tr class="oxr hidden" data-idx="' + row.idx + '"><td colspan="15">' + buildOtdExpand(row.o) + '</td></tr>';
  });
  if (!pageRows.length) {
    tbody.innerHTML = '<tr><td colspan="15" style="text-align:center;padding:3rem;color:var(--t3)">No orders match the current filter</td></tr>';
  } else {
    tbody.innerHTML = html;
  }

  // Pagination bar
  var pg = document.getElementById('otd-pagination');
  if (pg) {
    pg.innerHTML =
      '<div class="pg-l">'
      + '<button class="pg-btn" id="otd-pg-prev"' + (_otdPage <= 1 ? ' disabled' : '') + '>← Prev</button>'
      + '<span>Page <strong>' + _otdPage + '</strong> of <strong>' + otdPages + '</strong></span>'
      + '<button class="pg-btn" id="otd-pg-next"' + (_otdPage >= otdPages ? ' disabled' : '') + '>Next →</button>'
      + '</div>'
      + '<div class="pg-r">'
      + '<span class="pg-rows">' + OTD_PER_PAGE + ' rows</span>'
      + '<span class="pg-count">' + visRows.length + ' record' + (visRows.length !== 1 ? 's' : '') + '</span>'
      + '</div>';
    var pp = document.getElementById('otd-pg-prev');
    var pn = document.getElementById('otd-pg-next');
    if (pp) pp.addEventListener('click', function() { if (_otdPage > 1) { _otdPage--; renderOTD(); } });
    if (pn) pn.addEventListener('click', function() { if (_otdPage < otdPages) { _otdPage++; renderOTD(); } });
  }

  tbody.querySelectorAll('.oxbtn').forEach(function(btn) {
    btn.addEventListener('click', function(e) {
      e.stopPropagation();
      var idx = btn.dataset.idx;
      var xr  = tbody.querySelector('tr.oxr[data-idx="' + idx + '"]');
      var open = !xr.classList.contains('hidden');
      if (open) { xr.classList.add('hidden');    btn.textContent = '▶'; btn.classList.remove('open'); if (!_otdIsInteracting()) _otdFlushPending(); }
      else       { xr.classList.remove('hidden'); btn.textContent = '▼'; btn.classList.add('open'); }
    });
  });
  tbody.querySelectorAll('tr.odr').forEach(function(tr) {
    tr.addEventListener('click', function(e) {
      if (e.target.closest('.oxbtn')) return;
      tr.querySelector('.oxbtn').click();
    });
  });
}

// ── NOTIFICATIONS ─────────────────────────────────────────────────────────────

// Track which alert IDs have already triggered a sound so we don't repeat
var _seenAlertIds = (function() {
  try { return new Set(JSON.parse(localStorage.getItem('spm_seen_alerts') || '[]')); }
  catch(e) { return new Set(); }
})();
var _alertData = null; // last /api/alerts response

async function loadAlerts() {
  try {
    var res = await authFetch('/api/alerts');
    if (!res.ok) return;
    _alertData = await res.json();

    // Sound + track new PO arrivals and delivery requests
    var incoming = (_alertData.new_pos || []).concat(_alertData.delivery_requests || []);
    var hasNew = false;
    incoming.forEach(function(item) {
      if (!_seenAlertIds.has(item.id)) { hasNew = true; _seenAlertIds.add(item.id); }
    });
    if (hasNew) {
      _playAlertSound('info');
      try { localStorage.setItem('spm_seen_alerts', JSON.stringify([..._seenAlertIds].slice(-300))); } catch(e) {}
    }

    checkOTDAlerts(); // rebuild bell panel with fresh data
  } catch(e) { console.error('loadAlerts:', e); }
}

// ── SSE real-time feed ─────────────────────────────────────────────────────────
function initSSE() {
  if (!_authHeader) return;
  if (_sseSource) { try { _sseSource.close(); } catch(e){} }
  var token = _authHeader.replace(/^Bearer\s+/, '');
  _sseSource = new EventSource('/api/sse?token=' + encodeURIComponent(token));
  _sseSource.addEventListener('new_comment', function(e) {
    try {
      var d = JSON.parse(e.data);
      // Refresh activity feed if the drawer is showing this PO's activity
      if (_actOid === d.order_id) {
        var actPanel = document.getElementById('panel-activity');
        if (actPanel && !actPanel.classList.contains('drawer-panel-hidden')) {
          loadActivity(_actOid, _actType);
        }
      }
      // If we're mentioned, refresh the notification count
      var c = d.comment || {};
      if (_currentUser && Array.isArray(c.mentioned_roles) && c.mentioned_roles.indexOf(_currentUser.role) !== -1) {
        _unreadMentions.push(c);
        checkOTDAlerts();
      }
    } catch(err) {}
  });
}

function checkMentions() {
  if (!_currentUser) return;
  var uid   = _currentUser.id || '';
  var since = localStorage.getItem('mention_seen_' + uid) || '';
  var url   = '/api/notifications/mentions' + (since ? '?since=' + encodeURIComponent(since) : '');
  authFetch(url)
    .then(function(r){ return r.ok ? r.json() : Promise.reject(); })
    .then(function(d){
      _unreadMentions = Array.isArray(d) ? d : (d.data || []);
      checkOTDAlerts();
    })
    .catch(function(){});
}

function _markMentionsRead() {
  if (!_currentUser) return;
  var uid = _currentUser.id || '';
  localStorage.setItem('mention_seen_' + uid, new Date().toISOString());
  _unreadMentions = [];
}

function checkOTDAlerts() {
  // Gather critical/at-risk from both Chevron and NLNG
  var critical = [], atRisk = [];
  (ORDERS || []).forEach(function(o) {
    var cls = otdClass(o);
    if (cls === 'critical' || cls === 'overdue') critical.push({po: o.buyer_po_number, buyer: o.buyer_name, dl: otdDaysLeft(o)});
    else if (cls === 'at-risk') atRisk.push({po: o.buyer_po_number, buyer: o.buyer_name, dl: otdDaysLeft(o)});
  });
  (NLNG_ORDERS || []).forEach(function(o) {
    var cls = otdClass(o);
    if (cls === 'critical' || cls === 'overdue') critical.push({po: o.po_number, buyer: 'NLNG', dl: otdDaysLeft(o)});
    else if (cls === 'at-risk') atRisk.push({po: o.po_number, buyer: 'NLNG', dl: otdDaysLeft(o)});
  });

  var newPos     = _alertData ? (_alertData.new_pos  || []) : [];
  var delReqs    = _alertData ? (_alertData.delivery_requests || []) : [];
  var suspEmails = _alertData ? (_alertData.suspicious_emails || []) : [];

  var total = critical.length + atRisk.length + newPos.length + delReqs.length + _unreadMentions.length + suspEmails.length;
  var pip   = document.getElementById('notif-pip');
  if (pip) {
    pip.style.display = total > 0 ? 'block' : 'none';
    pip.textContent   = total > 0 ? (total > 9 ? '9+' : String(total)) : '';
  }

  // Browser push + sound for newly-critical OTD orders
  if (critical.length > 0 && 'Notification' in window) {
    if (Notification.permission === 'default') {
      Notification.requestPermission();
    } else if (Notification.permission === 'granted') {
      var newCrit = critical.filter(function(o) { return !localStorage.getItem('otd_notif_' + o.po); });
      if (newCrit.length > 0) {
        try {
          new Notification('SPM — ' + newCrit.length + ' critical order' + (newCrit.length > 1 ? 's' : ''), {
            body: newCrit.map(function(o) { return o.po; }).join(', '),
            icon: '/logo.png',
          });
          _playAlertSound('critical');
        } catch(e) {}
        newCrit.forEach(function(o) { localStorage.setItem('otd_notif_' + o.po, '1'); });
      }
    }
  }

  var bodyEl = document.getElementById('notif-body');
  if (!bodyEl) return;
  var html = '';

  if (_unreadMentions.length) {
    html += '<div class="notif-sec-lbl">Mentions</div>';
    _unreadMentions.forEach(function(m) {
      var oid  = m.order_id || m.nlng_order_id || '';
      var type = m.order_id ? 'chevron' : 'nlng';
      var snippet = (m.body || '').slice(0, 60) + ((m.body || '').length > 60 ? '…' : '');
      html += '<div class="notif-item info" style="cursor:pointer" onclick="closeNotifPanel();openStoryDrawer(\'' + _esc(oid) + '\',\'' + type + '\',\'activity\')">'
            + '<div class="ni-ttl">@' + _esc((_currentUser && _currentUser.role) || 'you') + ' · ' + _esc(m.author_name || '') + '</div>'
            + '<div class="ni-sub">' + _esc(snippet) + '</div></div>';
    });
  }

  if (newPos.length) {
    html += '<div class="notif-sec-lbl">New POs</div>';
    newPos.forEach(function(item) {
      html += '<div class="notif-item info" onclick="closeNotifPanel();showPage(\'orders\')">'
            + '<div class="ni-ttl">' + _esc(item.po || '—') + '</div>'
            + '<div class="ni-sub">' + _esc(item.buyer || '') + ' · just arrived</div></div>';
    });
  }

  if (delReqs.length) {
    html += '<div class="notif-sec-lbl">Delivery Requests</div>';
    delReqs.forEach(function(item) {
      html += '<div class="notif-item warn" onclick="closeNotifPanel();showPage(\'orders\')">'
            + '<div class="ni-ttl">' + _esc(item.po || '—') + '</div>'
            + '<div class="ni-sub">Delivery requested</div></div>';
    });
  }

  if (critical.length || atRisk.length) {
    html += '<div class="notif-sec-lbl">Delivery Deadlines</div>';
    critical.forEach(function(o) {
      var sub = o.dl !== null ? (o.dl < 0 ? 'Overdue by ' + Math.abs(o.dl) + ' days' : o.dl + ' days left — CRITICAL') : 'Past deadline';
      html += '<div class="notif-item crit" onclick="closeNotifPanel();showPage(\'delays\')">'
            + '<div class="ni-ttl">' + _esc(o.po || '—') + '</div>'
            + '<div class="ni-sub">' + sub + (o.buyer ? ' · ' + _esc(o.buyer) : '') + '</div></div>';
    });
    atRisk.forEach(function(o) {
      html += '<div class="notif-item warn" onclick="closeNotifPanel();showPage(\'delays\')">'
            + '<div class="ni-ttl">' + _esc(o.po || '—') + '</div>'
            + '<div class="ni-sub">' + (o.dl !== null ? o.dl + ' days remaining' : '') + (o.buyer ? ' · ' + _esc(o.buyer) : '') + '</div></div>';
    });
  }

  if (suspEmails.length) {
    html += '<div class="notif-sec-lbl">Email Issues</div>';
    suspEmails.forEach(function(e) {
      html += '<div class="notif-item warn">'
            + '<div class="ni-ttl">' + _esc(e.subject || '(no subject)') + '</div>'
            + '<div class="ni-sub">' + _esc(e.sender || '') + ' · ' + _esc(e.result || 'unknown') + '</div></div>';
    });
  }

  if (!html) html = '<div class="notif-empty">No alerts — all orders on track.</div>';
  bodyEl.innerHTML = html;
}

var _notifPanelOpen = false;

function toggleNotifPanel() {
  _notifPanelOpen = !_notifPanelOpen;
  var panel = document.getElementById('notif-panel');
  if (panel) panel.classList.toggle('show', _notifPanelOpen);
  if (_notifPanelOpen) { _markMentionsRead(); checkOTDAlerts(); }
}

function closeNotifPanel() {
  _notifPanelOpen = false;
  var panel = document.getElementById('notif-panel');
  if (panel) panel.classList.remove('show');
}

// ── Service Worker + Background Notifications ─────────────────────────────────

var _sw = null;
var _swPingTimer = null;
var _swMsgHandler = null; // stored so we can removeEventListener on re-init

function _initSW() {
  if (!('serviceWorker' in navigator)) return;
  // Remove previous listener before adding a new one to avoid duplicates
  // when the user logs out and back in without a page reload
  if (_swMsgHandler) {
    navigator.serviceWorker.removeEventListener('message', _swMsgHandler);
  }
  _swMsgHandler = function(e) {
    if (!e.data) return;
    if (e.data.type === 'SPM_ALERT_SOUND') _playAlertSound(e.data.level);
  };
  navigator.serviceWorker.addEventListener('message', _swMsgHandler);

  navigator.serviceWorker.register('/sw.js').then(function(reg) {
    _sw = reg;
    // Send token once SW is ready
    navigator.serviceWorker.ready.then(function() {
      _swSendToken();
      // Try to register periodic background sync (Chrome/Edge only)
      if (reg.periodicSync) {
        reg.periodicSync.register('spm-alerts', {minInterval: 5 * 60 * 1000}).catch(function() {});
      }
    });
  }).catch(function() { /* SW not supported or blocked */ });

  // Ping SW every 60s to trigger a poll while tab is open
  if (_swPingTimer) clearInterval(_swPingTimer);
  _swPingTimer = setInterval(_swPing, 60 * 1000);
}

function _swSendToken() {
  if (!navigator.serviceWorker.controller) return;
  navigator.serviceWorker.controller.postMessage({type: 'SPM_TOKEN', token: _authHeader});
}

function _swPing() {
  if (!navigator.serviceWorker.controller) return;
  navigator.serviceWorker.controller.postMessage({type: 'SPM_POLL'});
}

var _audioCtx = null;

// Unlock AudioContext on first user gesture so poll-triggered sounds can play.
// Browsers suspend audio until a click/keydown happens; without this, beeps
// scheduled from a setInterval callback are silently dropped.
document.addEventListener('click', function _unlockAudio() {
  if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
  if (_audioCtx.state === 'suspended') _audioCtx.resume();
  document.removeEventListener('click', _unlockAudio);
}, { once: true });

function _playAlertSound(level) {
  try {
    if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    var ctx = _audioCtx;
    var isCrit = level === 'critical';
    function _schedule() {
      if (isCrit) {
        // Three descending square-wave beeps — urgent alarm pattern
        [0, 0.22, 0.44].forEach(function(t, i) {
          var osc  = ctx.createOscillator();
          var gain = ctx.createGain();
          osc.connect(gain);
          gain.connect(ctx.destination);
          osc.type = 'square';
          osc.frequency.value = 1000 - (i * 120); // 1000 → 880 → 760 Hz
          gain.gain.setValueAtTime(0.001, ctx.currentTime + t);
          gain.gain.exponentialRampToValueAtTime(0.5, ctx.currentTime + t + 0.01);
          gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + t + 0.18);
          osc.start(ctx.currentTime + t);
          osc.stop(ctx.currentTime + t + 0.2);
        });
      } else {
        // Two ascending triangle-wave chimes — notification ding
        [{t:0, f:700}, {t:0.28, f:1050}].forEach(function(b) {
          var osc  = ctx.createOscillator();
          var gain = ctx.createGain();
          osc.connect(gain);
          gain.connect(ctx.destination);
          osc.type = 'triangle';
          osc.frequency.value = b.f;
          gain.gain.setValueAtTime(0.001, ctx.currentTime + b.t);
          gain.gain.exponentialRampToValueAtTime(0.45, ctx.currentTime + b.t + 0.01);
          gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + b.t + 0.28);
          osc.start(ctx.currentTime + b.t);
          osc.stop(ctx.currentTime + b.t + 0.3);
        });
      }
    }
    if (ctx.state === 'suspended') {
      ctx.resume().then(_schedule).catch(function() {});
    } else {
      _schedule();
    }
  } catch(e) { /* audio not available */ }
}

document.addEventListener('click', function(e) {
  if (_notifPanelOpen && !e.target.closest('.notif-wrap')) closeNotifPanel();
  if (!e.target.closest('#orders-period-dd')) {
    var om = document.getElementById('orders-period-menu');
    if (om) om.classList.add('hidden');
  }
  if (!e.target.closest('#otd-period-dd')) {
    var tm = document.getElementById('otd-period-menu');
    if (tm) tm.classList.add('hidden');
  }
  if (!e.target.closest('#nlng-otd-period-dd')) {
    var ntm = document.getElementById('nlng-otd-period-menu');
    if (ntm) ntm.classList.add('hidden');
  }
  if (!e.target.closest('#nlng-period-dd')) {
    var nm = document.getElementById('nlng-period-menu');
    if (nm) nm.classList.add('hidden');
  }
  if (!e.target.closest('#orders-promised-dd')) {
    var opm = document.getElementById('orders-promised-menu');
    if (opm) opm.classList.add('hidden');
  }
  if (!e.target.closest('#nlng-promised-dd')) {
    var npm = document.getElementById('nlng-promised-menu');
    if (npm) npm.classList.add('hidden');
  }
  if (!e.target.closest('#client-sw-dd')) {
    var cm = document.getElementById('client-sw-menu');
    if (cm) cm.classList.add('hidden');
  }
});

// ═══════════════════════════════════════════════════════════════════════
// QUOTES MODULE
// ═══════════════════════════════════════════════════════════════════════

var _quotes        = [];
var _editingQid    = null;
// Status of the quote currently open in the form. Tracked so that saving
// before a PDF download preserves it — without this, auto-saving a quote
// that had been marked "sent" would silently knock it back to "draft".
var _editingStatus = 'draft';
var _deleteQid     = null;
var _quoteRows     = [];   // line items in current form
var _quotePage     = 1;
var _QT_PER_PAGE   = 100;
var _activeQuoteCompany = localStorage.getItem('activeQuoteCompany') || 'spm';

var _Q_CLIENT_DFLT = {
  chevron: { company: 'Chevron Nigeria Limited',                 dest: 'Your Chevron Warri Warehouse', country: 'United States / United Kingdom' },
  nlng:    { company: 'Nigeria LNG Limited',                     dest: 'NLNG Plant, Bonny Island',     country: 'United Kingdom' },
  seplat:  { company: 'Seplat Petroleum Development Company',    dest: '',                              country: '' },
  exxon:   { company: 'ExxonMobil Nigeria Limited',             dest: '',                              country: 'United States' },
  total:   { company: 'TotalEnergies EP Nigeria Limited',        dest: '',                              country: 'France / United Kingdom' },
  others:  { company: '',                                        dest: '',                              country: '' },
};

function loadQuotes() {
  authFetch('/api/quotations').then(function(r){ return r.json(); }).then(function(d){
    if (!Array.isArray(d)) { console.error('loadQuotes', d); return; }
    _quotes = d;
    _applyQuoteCompanyUI();
    _renderQuotesList();
  }).catch(function(e){ console.error('loadQuotes', e); });
}

function setQuoteCompany(co) {
  _activeQuoteCompany = co;
  localStorage.setItem('activeQuoteCompany', co);
  _applyQuoteCompanyUI();
  _quotePage = 1;
  _renderQuotesList();
}

function _applyQuoteCompanyUI() {
  var isDM   = _activeQuoteCompany === 'danmag';
  var title  = document.getElementById('qt-co-title');
  var sub    = document.getElementById('qt-co-sub');
  var btnSpm = document.getElementById('qt-co-spm');
  var btnDM  = document.getElementById('qt-co-danmag');
  if (title) title.textContent = 'Quotations';
  if (sub)   sub.textContent   = isDM ? 'Daniel Mag Obontuaren (Nig) Limited' : 'Special Piping Materials (Nig.) Ltd.';
  if (btnSpm) btnSpm.classList.toggle('qt-co-tab-active', !isDM);
  if (btnDM)  btnDM.classList.toggle('qt-co-tab-active',  isDM);
}

function filterQuotesList() {
  _quotePage = 1;
  _renderQuotesList();
}

function _renderQuotesList() {
  var el = document.getElementById('quotes-list-body');
  if (!el) return;
  var qInput = document.getElementById('qt-search');
  var term = qInput ? qInput.value.trim().toLowerCase() : '';
  // Filter by active company first (quotes without company field default to 'spm')
  var list = _quotes.filter(function(q) {
    return (q.company || 'spm') === _activeQuoteCompany;
  });
  if (term) {
    list = list.filter(function(q) {
      return (q.quote_number  || '').toLowerCase().indexOf(term) !== -1
          || (q.client        || '').toLowerCase().indexOf(term) !== -1
          || (q.reference_po  || '').toLowerCase().indexOf(term) !== -1
          || (q.subject       || '').toLowerCase().indexOf(term) !== -1
          || (q.status        || '').toLowerCase().indexOf(term) !== -1;
    });
  }
  if (_quotePage > Math.max(1, Math.ceil(list.length / _QT_PER_PAGE))) _quotePage = 1;
  var start    = (_quotePage - 1) * _QT_PER_PAGE;
  var pageList = list.slice(start, start + _QT_PER_PAGE);

  if (!list.length) {
    el.innerHTML = '<tr><td colspan="8" style="text-align:center;padding:2.5rem;color:var(--t3)">'
      + (term ? 'No quotes match your search.' : 'No quotations yet — click New Quote to create one.')
      + '</td></tr>';
    renderPgBar('qt-pagination', 1, 0, _QT_PER_PAGE, 'qt-pg-prev', 'qt-pg-next', function(){}, function(){});
    return;
  }
  el.innerHTML = pageList.map(function(q) {
    var scls = { draft:'sp-n', sent:'sp-ok', accepted:'sp-ok', rejected:'sp-r' }[q.status] || 'sp-n';
    var total = (q.total != null) ? '$' + Number(q.total).toLocaleString('en-US', {minimumFractionDigits:2,maximumFractionDigits:2}) : '—';
    return '<tr>'
      + '<td class="mono" style="font-size:12px">' + htmlEscape(q.quote_number || '—') + '</td>'
      + '<td>' + htmlEscape((q.client || '').toUpperCase()) + '</td>'
      + '<td class="mono">' + htmlEscape(q.reference_po || '—') + '</td>'
      + '<td style="max-width:220px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">' + htmlEscape(q.subject || '—') + '</td>'
      + '<td style="text-align:right;font-variant-numeric:tabular-nums">' + total + '</td>'
      + '<td>' + (q.quote_date ? fmtTs(q.quote_date) : '—') + '</td>'
      + '<td><span class="sp ' + scls + '">' + htmlEscape(q.status || 'draft') + '</span></td>'
      + '<td style="white-space:nowrap;text-align:right">'
      +   '<button class="act-btn" onclick="openQuoteForm(\'' + q.id + '\')">Edit</button> '
      +   '<button class="act-btn" onclick="downloadQuotePdf(\'' + q.id + '\',\'' + htmlEscape(q.subject || q.quote_number || q.id) + '\')">PDF</button> '
      +   '<button class="act-btn act-del" onclick="confirmDeleteQuote(\'' + q.id + '\')">Del</button>'
      + '</td>'
      + '</tr>';
  }).join('');
  renderPgBar(
    'qt-pagination', _quotePage, list.length, _QT_PER_PAGE,
    'qt-pg-prev', 'qt-pg-next',
    function() { if (_quotePage > 1) { _quotePage--; _renderQuotesList(); } },
    function() { var p = Math.max(1, Math.ceil(list.length / _QT_PER_PAGE)); if (_quotePage < p) { _quotePage++; _renderQuotesList(); } }
  );
}

function openQuoteForm(quoteId) {
  _editingQid = quoteId;
  var title = document.getElementById('quotes-form-title');

  // Reset all fields to defaults
  _setQf('qf-client',            '');
  _setQf('qf-quote-number',      '');
  _setQf('qf-reference-po',      '');
  _setQf('qf-quote-date',        new Date().toISOString().slice(0,10));
  _setQf('qf-validity-days',     '30');
  _setQf('qf-prepared-by',       (_currentUser && _currentUser.name) || '');
  _setQf('qf-recipient-name',    '');
  _setQf('qf-recipient-dept',    '');
  _setQf('qf-recipient-company', '');
  _setQf('qf-recipient-email',   '');
  _setQf('qf-recipient-tel',     '');
  _setQf('qf-subject',           '');
  _setQf('qf-lead-time',         '');
  _setQf('qf-delivery-dest',     '');
  _setQf('qf-country-import',    '');
  _setQf('qf-shipping-mode',     'Sea');
  _setQf('qf-manufacturer',      '');
  _setQf('qf-weight',            '');
  _setQf('qf-currency',          'USD');
  _setQf('qf-discount-pct',      '0');
  _setQf('qf-markup-pct',        '0');
  var _eyeReset = document.getElementById('qf-markup-eye');
  if (_eyeReset) { _eyeReset.setAttribute('data-visible','1'); _eyeReset.title = 'Markup visible in PDF — click to hide'; }
  _setQf('qf-shipping-charges',  '0');
  _setQf('qf-notes',             'Looking forward to your business.');
  _setQf('qf-client-name',       '');
  var _othersRow = document.getElementById('qf-others-name-row');
  if (_othersRow) _othersRow.style.display = 'none';
  _quoteRows = [];
  _setPdfBtnVisible(false);
  clearImportBanner();

  if (quoteId) {
    if (title) title.textContent = 'Edit Quote';
    authFetch('/api/quotations/' + quoteId).then(function(r){ return r.json(); }).then(function(q){
      if (q.error) { alert('Could not load quote: ' + q.error); return; }
      _setQf('qf-company',           q.company || 'spm');
      _editingStatus = q.status || 'draft';
      // Restore each T&C eye; anything absent (older quote) defaults to shown.
      _TC_FIELDS.forEach(function(key){
        var f = 'show_' + key.replace(/-/g, '_');
        _setTcVisible(key, q[f] === undefined || q[f] === null ? true : !!q[f]);
      });
      _setQf('qf-client',            q.client || '');
      var othersRow = document.getElementById('qf-others-name-row');
      if (othersRow) othersRow.style.display = (q.client === 'others') ? '' : 'none';
      if (q.client === 'others') _setQf('qf-client-name', q.recipient_company || '');
      _setQf('qf-quote-number',      q.quote_number || '');
      _setQf('qf-reference-po',      q.reference_po || '');
      _setQf('qf-quote-date',        (q.quote_date || '').slice(0,10));
      _setQf('qf-validity-days',     String(q.validity_days || 30));
      _setQf('qf-prepared-by',       q.prepared_by || '');
      _setQf('qf-recipient-name',    q.recipient_name || '');
      _setQf('qf-recipient-dept',    q.recipient_dept || '');
      _setQf('qf-recipient-company', q.recipient_company || '');
      _setQf('qf-recipient-email',   q.recipient_email || '');
      _setQf('qf-recipient-tel',     q.recipient_tel || '');
      _setQf('qf-subject',           q.subject || '');
      _setQf('qf-lead-time',         q.lead_time || '');
      _setQf('qf-delivery-dest',     q.delivery_dest || '');
      _setQf('qf-country-import',    q.country_import || '');
      _setQf('qf-shipping-mode',     q.shipping_mode || 'Sea');
      _setQf('qf-manufacturer',      q.manufacturer || '');
      _setQf('qf-weight',            q.weight || '');
      _setQf('qf-currency',          q.currency || 'USD');
      _setQf('qf-discount-pct',      String(q.discount_pct || 0));
      _setQf('qf-markup-pct',        String(q.markup_pct || 0));
      var _eyeLoad = document.getElementById('qf-markup-eye');
      if (_eyeLoad) {
        var _mv = (q.markup_visible === false) ? '0' : '1';
        _eyeLoad.setAttribute('data-visible', _mv);
        _eyeLoad.title = _mv === '1' ? 'Markup visible in PDF — click to hide' : 'Markup hidden from PDF — click to show';
      }
      _setQf('qf-shipping-charges',  String(q.shipping_charges || 0));
      _setQf('qf-notes',             q.notes || '');
      _quoteRows = (q.line_items || []).map(function(it){
        return { item_no: it.item_no, product_no: it.product_no || '', description: it.description || '', uom: it.uom || 'EA',
                 quantity: it.quantity || 1, unit_price: it.unit_price || 0,
                 tax_rate: it.tax_rate || 0, line_total: it.line_total || 0 };
      });
      _renderQuoteItems();
      onCurrencyChange();
      _setPdfBtnVisible(true);
    });
  } else {
    if (title) title.textContent = 'New Quote';
    _setQf('qf-company', _activeQuoteCompany);
    _editingStatus = 'draft';
    // A new quote starts with every T&C field shown.
    _TC_FIELDS.forEach(function(key){ _setTcVisible(key, true); });
    _quoteRows = [{ item_no:1, product_no:'', description:'', uom:'EA', quantity:1, unit_price:0, tax_rate:0, line_total:0 }];
    _renderQuoteItems();
    onCurrencyChange();
  }

  document.getElementById('quotes-list-view').style.display = 'none';
  document.getElementById('quotes-form-view').style.display = '';
}

function closeQuoteForm() {
  _editingQid = null;
  document.getElementById('quotes-list-view').style.display = '';
  document.getElementById('quotes-form-view').style.display = 'none';
}

function _setQf(id, val) {
  var el = document.getElementById(id);
  if (el) el.value = val;
}

function _setPdfBtnVisible(show) {
  ['qf-pdf-btn','qf-pdf-btn2'].forEach(function(id){
    var el = document.getElementById(id);
    if (el) el.style.display = show ? '' : 'none';
  });
}

function onQuoteClientChange() {
  var c = document.getElementById('qf-client');
  if (!c) return;
  var isOthers = c.value === 'others';
  var othersRow = document.getElementById('qf-others-name-row');
  if (othersRow) othersRow.style.display = isOthers ? '' : 'none';
  if (!isOthers) {
    var nameEl = document.getElementById('qf-client-name');
    if (nameEl) nameEl.value = '';
  }
  var dflt = _Q_CLIENT_DFLT[c.value];
  if (dflt) {
    var comp = document.getElementById('qf-recipient-company');
    if (comp && !comp.value) comp.value = dflt.company;
    var dest = document.getElementById('qf-delivery-dest');
    if (dest && !dest.value) dest.value = dflt.dest;
    var ctry = document.getElementById('qf-country-import');
    if (ctry && !ctry.value) ctry.value = dflt.country;
  }
  _syncNlngCols();
  _renderQuoteItems();
}

function onOtherClientNameInput() {
  var nameEl = document.getElementById('qf-client-name');
  var compEl = document.getElementById('qf-recipient-company');
  if (nameEl && compEl) compEl.value = nameEl.value;
}

function _syncNlngCols() {
  var table   = document.getElementById('quotes-items-table');
  if (!table) return;
  var isNlng  = (document.getElementById('qf-client') || {}).value === 'nlng';
  table.classList.toggle('is-nlng', isNlng);
  var colDesc = document.getElementById('qf-col-desc');
  var colPno  = document.getElementById('qf-col-pno');
  if (colDesc) colDesc.style.width = isNlng ? '22%' : '37%';
  if (colPno)  colPno.style.width  = isNlng ? '15%' : '0';
}

function handleEnquiryFile(input) {
  var file = input.files[0];
  if (!file) return;
  var btn = document.getElementById('qf-import-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Importing…'; }
  var reader = new FileReader();
  reader.onload = function(e) {
    var b64 = e.target.result.split(',')[1];
    authFetch('/api/quotations/parse-enquiry', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ filename: file.name, data: b64 }),
    })
    .then(function(r) { return r.json(); })
    .then(function(d) {
      if (btn) { btn.disabled = false; btn.textContent = '↑ Import from Enquiry File'; }
      input.value = '';
      if (d.error) { alert('Import failed: ' + d.error); return; }
      // Prefill header fields
      if (d.client)            _setQf('qf-client',            d.client);
      if (d.client_name)       _setQf('qf-client-name',       d.client_name);
      if (d.reference_po)      _setQf('qf-reference-po',      d.reference_po);
      if (d.subject)           _setQf('qf-subject',           d.subject);
      if (d.delivery_dest)     _setQf('qf-delivery-dest',     d.delivery_dest);
      if (d.bill_to_company)   _setQf('qf-recipient-company', d.bill_to_company);
      if (d.department)        _setQf('qf-recipient-dept',    d.department);
      if (d.currency)          { _setQf('qf-currency', d.currency); onCurrencyChange(); }
      // Extra fields returned by SPM quote PDF parser
      if (d.quote_date)        _setQf('qf-quote-date',        d.quote_date);
      if (d.recipient_name)    _setQf('qf-recipient-name',    d.recipient_name);
      if (d.recipient_dept)    _setQf('qf-recipient-dept',    d.recipient_dept);
      if (d.recipient_email)   _setQf('qf-recipient-email',   d.recipient_email);
      if (d.recipient_tel)     _setQf('qf-recipient-tel',     d.recipient_tel);
      if (d.manufacturer)      _setQf('qf-manufacturer',      d.manufacturer);
      if (d.lead_time)         _setQf('qf-lead-time',         d.lead_time);
      if (d.validity_days)     _setQf('qf-validity-days',     d.validity_days);
      if (d.prepared_by)       _setQf('qf-prepared-by',       d.prepared_by);
      // Prefill line items
      if (Array.isArray(d.line_items) && d.line_items.length) {
        _quoteRows = d.line_items.map(function(it, i) {
          var up = parseFloat(it.unit_price) || 0;
          var lt = parseFloat(it.line_total) || 0;
          var tr = parseFloat(it.tax_rate)   || 0;
          return { item_no: i + 1, product_no: it.product_no || '', description: it.description || '', uom: it.uom || 'EA',
                   quantity: it.quantity || 1, unit_price: up, tax_rate: tr, line_total: lt };
        });
        _renderQuoteItems();
        recalcTotals();
      }
      // Show success banner
      var label = (d.count || 0) + ' line item' + (d.count === 1 ? '' : 's') + ' imported';
      if (d.original_quote_number) label += '  ·  ' + d.original_quote_number;
      else if (d.reference_po) label += '  ·  ' + d.reference_po;
      if (d.delivery_dest) label += '  ·  ' + d.delivery_dest;
      var ok  = document.getElementById('qf-import-ok');
      var msg = document.getElementById('qf-import-msg');
      if (msg) msg.textContent = label;
      if (ok)  ok.style.display = 'flex';
    })
    .catch(function(err) {
      if (btn) { btn.disabled = false; btn.textContent = '↑ Import from Enquiry File'; }
      input.value = '';
      alert('Network error: ' + err.message);
    });
  };
  reader.readAsDataURL(file);
}

function clearImportBanner() {
  var ok  = document.getElementById('qf-import-ok');
  var err = document.getElementById('qf-import-err');
  if (ok)  ok.style.display  = 'none';
  if (err) err.style.display = 'none';
}

function _showQfBanner(msg, isError) {
  clearImportBanner();
  if (isError) {
    var el  = document.getElementById('qf-import-err');
    var txt = document.getElementById('qf-import-err-msg');
    if (txt) txt.textContent = msg;
    if (el)  el.style.display = 'flex';
  } else {
    var el2  = document.getElementById('qf-import-ok');
    var txt2 = document.getElementById('qf-import-msg');
    if (txt2) txt2.textContent = msg;
    if (el2)  el2.style.display = 'flex';
  }
}

function exportLineItemsXlsx() {
  if (!_quoteRows || !_quoteRows.length) {
    alert('No line items to export.');
    return;
  }
  // If quote is already saved, let the server generate a proper XLSX (survives Excel save/reload)
  if (_editingQid) {
    var ref = (document.getElementById('qf-reference-po') || {}).value || 'quote';
    var xlsFname = 'line_items_' + ref.replace(/[^a-z0-9_\-]/gi, '_') + '.xlsx';
    authFetch('/api/quotations/' + _editingQid + '/export_xlsx')
      .then(function(res) {
        if (!res.ok) { _showQfBanner('Export failed: ' + res.status, true); return null; }
        return res.blob();
      })
      .then(function(blob) {
        if (!blob) return;
        var url = URL.createObjectURL(blob);
        var a = document.createElement('a');
        a.href = url; a.download = xlsFname;
        document.body.appendChild(a); a.click();
        setTimeout(function() { URL.revokeObjectURL(url); if (a.parentNode) a.parentNode.removeChild(a); }, 1000);
      });
    return;
  }
  var sym    = _currSym();
  var isNlng = (document.getElementById('qf-client') || {}).value === 'nlng';
  var headers = isNlng
    ? ['#', 'Product No.', 'Description', 'UOM', 'Qty', 'Unit Price (' + sym + ')', 'Tax %', 'Total (' + sym + ')']
    : ['#', 'Description', 'UOM', 'Qty', 'Unit Price (' + sym + ')', 'Tax %', 'Total (' + sym + ')'];
  var rows = _quoteRows.map(function(r, i) {
    var base = [
      i + 1,
      r.description || '',
      r.uom         || '',
      r.quantity     || 0,
      r.unit_price   || 0,
      r.tax_rate     || 0,
      r.line_total   || 0
    ];
    if (isNlng) base.splice(1, 0, r.product_no || '');
    return base;
  });
  // Build HTML table — Excel opens this format (.xls) natively
  var esc = function(v) { return String(v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); };
  var html = '<html xmlns:o="urn:schemas-microsoft-com:office:office" '
           + 'xmlns:x="urn:schemas-microsoft-com:office:excel" '
           + 'xmlns="http://www.w3.org/TR/REC-html40">'
           + '<head><meta charset="UTF-8"/></head><body><table>';
  html += '<tr>' + headers.map(function(h){ return '<th>' + esc(h) + '</th>'; }).join('') + '</tr>';
  rows.forEach(function(row) {
    html += '<tr>' + row.map(function(c){ return '<td>' + esc(c) + '</td>'; }).join('') + '</tr>';
  });
  html += '</table></body></html>';

  var ref   = (document.getElementById('qf-reference-po') || {}).value || 'quote';
  var fname = 'line_items_' + ref.replace(/[^a-z0-9_\-]/gi, '_') + '.xls';
  var blob  = new Blob([html], { type: 'application/vnd.ms-excel;charset=utf-8' });
  var url   = URL.createObjectURL(blob);
  var a     = document.createElement('a');
  a.href = url; a.download = fname; a.click();
  URL.revokeObjectURL(url);
}

function importLineItemPrices(input) {
  var file = input.files[0];
  if (!file) return;
  var quoteId = _editingQid;
  if (!quoteId) {
    _showQfBanner('Save the quote first before importing prices.', true);
    input.value = '';
    return;
  }
  var btn = document.getElementById('qf-price-import-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Importing…'; }
  var reader = new FileReader();
  reader.onload = function(e) {
    authFetch('/api/quotations/' + quoteId + '/import_prices', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ file_data: e.target.result })
    })
    // A failed import used to slip past the d.error check (FastAPI sends
    // d.detail), fall through, and assign an empty list — clearing every line
    // from the form and then announcing "0 line items updated" in green.
    .then(function(r) {
      return r.json().catch(function(){ return {}; }).then(function(body) {
        if (!r.ok) throw new Error(body.detail || body.error || ('Server error ' + r.status));
        return body;
      });
    })
    .then(function(d) {
      if (btn) { btn.disabled = false; btn.textContent = '↑ Import Prices'; }
      input.value = '';
      if (d.error) { _showQfBanner('Import error: ' + d.error, true); return; }
      // Never let a successful-looking response empty the form either. If the
      // server came back with no rows, the import found nothing to price and
      // what is on screen is still the good copy.
      if (!Array.isArray(d.line_items) || !d.line_items.length) {
        _showQfBanner('No prices were imported — your line items are unchanged. '
                      + 'Check the file matches this quote.', true);
        return;
      }
      _quoteRows = (d.line_items || []).map(function(it) {
        return {
          item_no: it.item_no, product_no: it.product_no || '',
          description: it.description || '', uom: it.uom || 'EA',
          quantity: it.quantity || 0, unit_price: it.unit_price || 0,
          tax_rate: it.tax_rate || 0, line_total: it.line_total || 0
        };
      });
      _renderQuoteItems();
      recalcTotals();
      _showQfBanner(_quoteRows.length + ' line item' + (_quoteRows.length === 1 ? '' : 's') + ' updated with imported prices.', false);
    })
    .catch(function(err) {
      if (btn) { btn.disabled = false; btn.textContent = '↑ Import Prices'; }
      input.value = '';
      _showQfBanner('Import failed — your line items are unchanged. ' + err.message, true);
    });
  };
  reader.readAsDataURL(file);
}

function _renderQuoteItems() {
  var body = document.getElementById('quotes-items-body');
  if (!body) return;
  _syncNlngCols();
  body.innerHTML = _quoteRows.map(function(row, idx){
    var tv = parseFloat(row.tax_rate) || 0;
    return '<tr data-idx="' + idx + '">'
      + '<td class="mono" style="color:var(--t3);text-align:center">' + (idx+1) + '</td>'
      + '<td><div class="qf-inp qf-td-inp qf-desc-ce" contenteditable="true" data-idx="' + idx + '" data-ph="Item description…" oninput="_ceDescInput(this,' + idx + ')"></div></td>'
      + '<td class="nlng-col"><input class="qf-inp qf-td-inp" type="text" placeholder="Product No." value="' + htmlEscape(row.product_no||'') + '" oninput="updateQuoteRow(' + idx + ',\'product_no\',this.value)"/></td>'
      + '<td><select class="qf-inp qf-td-inp" onchange="updateQuoteRow(' + idx + ',\'uom\',this.value)">'
      + ['EA','PC','KIT','ROLL','BOX','MTR','GL','SET','PACK'].map(function(u){ return '<option' + (((row.uom||'EA')===u)?' selected':'') + '>' + u + '</option>'; }).join('')
      + '</select></td>'
      + '<td><input class="qf-inp qf-td-inp" type="number" min="0" step="0.01" value="' + (row.quantity||1) + '" style="text-align:right" oninput="updateQuoteRow(' + idx + ',\'quantity\',parseFloat(this.value)||0)"/></td>'
      + '<td><input class="qf-inp qf-td-inp" type="number" min="0" step="0.01" value="' + (row.unit_price||'') + '" placeholder="0.00" style="text-align:right" oninput="updateQuoteRow(' + idx + ',\'unit_price\',parseFloat(this.value)||0)"/></td>'
      + '<td><select class="qf-inp qf-td-inp" onchange="updateQuoteRow(' + idx + ',\'tax_rate\',parseFloat(this.value))">'
      +   '<option value="0"' + (tv === 0 ? ' selected' : '') + '>No Tax</option>'
      +   '<option value="0.075"' + (tv === 0.075 ? ' selected' : '') + '>VAT (7.5%)</option>'
      + '</select></td>'
      + '<td style="text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap" id="qf-row-tot-' + idx + '">' + _fmtQ(row.line_total||0) + '</td>'
      + '<td style="text-align:center"><button class="qf-del-row" onclick="removeQuoteRow(' + idx + ')" title="Remove">×</button></td>'
      + '</tr>';
  }).join('');
  // Populate contenteditable description divs (innerHTML, not string concat, to preserve HTML markup)
  body.querySelectorAll('.qf-desc-ce').forEach(function(div) {
    var i = parseInt(div.getAttribute('data-idx'));
    div.innerHTML = (_quoteRows[i] && _quoteRows[i].description) || '';
  });
}

function updateQuoteRow(idx, field, val) {
  if (!_quoteRows[idx]) return;
  _quoteRows[idx][field] = val;
  // description field is handled directly via _ceDescInput — nothing extra needed here
  if (field === 'quantity' || field === 'unit_price' || field === 'tax_rate') {
    var qty = parseFloat(_quoteRows[idx].quantity) || 0;
    var p   = parseFloat(_quoteRows[idx].unit_price) || 0;
    var t   = parseFloat(_quoteRows[idx].tax_rate) || 0;
    _quoteRows[idx].line_total = Math.round(qty * p * (1 + t) * 100) / 100;
    var tel = document.getElementById('qf-row-tot-' + idx);
    if (tel) tel.textContent = _fmtQ(_quoteRows[idx].line_total);
    recalcTotals();
  }
}

function addQuoteRow() {
  _quoteRows.push({ item_no: _quoteRows.length + 1, product_no:'', description:'', uom:'EA', quantity:1, unit_price:0, tax_rate:0, line_total:0 });
  _renderQuoteItems();
  recalcTotals();
  var body = document.getElementById('quotes-items-body');
  if (body) { var last = body.lastElementChild; if (last) last.scrollIntoView({behavior:'smooth', block:'nearest'}); }
}

function removeQuoteRow(idx) {
  _quoteRows.splice(idx, 1);
  _quoteRows.forEach(function(r, i){ r.item_no = i+1; });
  _renderQuoteItems();
  recalcTotals();
}

function recalcTotals() {
  var sub    = _quoteRows.reduce(function(s,r){ return s + (parseFloat(r.line_total)||0); }, 0);
  var disc   = parseFloat((document.getElementById('qf-discount-pct') || {}).value) || 0;
  var da     = sub * disc / 100;
  var markup = parseFloat((document.getElementById('qf-markup-pct') || {}).value) || 0;
  var ma     = sub * markup / 100;
  var ship   = parseFloat((document.getElementById('qf-shipping-charges') || {}).value) || 0;
  var tot    = sub - da + ma + ship;
  var set    = function(id, v){ var e = document.getElementById(id); if(e) e.textContent = v; };
  set('qf-sub-total',    _fmtQ(sub));
  set('qf-discount-amt', '— ' + _fmtQ(da));
  set('qf-markup-amt',   '+ ' + _fmtQ(ma));
  set('qf-shipping-val', _fmtQ(ship));
  set('qf-total',        _fmtQ(tot));
}

function toggleMarkupVisibility() {
  var btn = document.getElementById('qf-markup-eye');
  if (!btn) return;
  var nowVisible = btn.getAttribute('data-visible') === '1';
  nowVisible = !nowVisible;
  btn.setAttribute('data-visible', nowVisible ? '1' : '0');
  btn.title = nowVisible ? 'Markup visible in PDF — click to hide' : 'Markup hidden from PDF — click to show';
}

// ── Terms & Conditions per-field visibility ───────────────────────────────────
// Each T&C field carries its own eye, same pattern as the markup toggle above.
// The key is the field's id suffix ('lead-time'), which maps to the eye button
// id ('qf-eye-lead-time') and, with dashes swapped for underscores, to the
// API/database field ('show_lead_time').
var _TC_FIELDS = ['lead-time', 'delivery-dest', 'country-import',
                  'shipping-mode', 'manufacturer', 'weight', 'currency'];

function toggleTcVisibility(key) {
  var btn = document.getElementById('qf-eye-' + key);
  if (!btn) return;
  var nowVisible = btn.getAttribute('data-visible') !== '1';
  btn.setAttribute('data-visible', nowVisible ? '1' : '0');
  btn.title = nowVisible ? 'Shown in PDF — click to hide'
                         : 'Hidden from PDF — click to show';
}

function _tcVisible(key) {
  var btn = document.getElementById('qf-eye-' + key);
  return !btn || btn.getAttribute('data-visible') !== '0';
}

function _setTcVisible(key, visible) {
  var btn = document.getElementById('qf-eye-' + key);
  if (!btn) return;
  btn.setAttribute('data-visible', visible ? '1' : '0');
  btn.title = visible ? 'Shown in PDF — click to hide'
                      : 'Hidden from PDF — click to show';
}

// ── Rich-text description helpers ─────────────────────────────────────────
// The PDF understands exactly four things: bold, italic, underline and colour,
// plus line breaks. The description box accepted anything, so a paste from Word
// or Outlook arrived carrying font names, point sizes and nested tables, and
// each new shape broke the PDF in a new way - a font stack crashed it, size="3"
// printed at 3pt, and a blank line typed in bold stripped a whole row back to
// plain text. Cleaning here, on the way IN, means the PDF only ever sees markup
// it already handles. Nothing is lost that the PDF could have rendered anyway.
var _activeCe = null;

// Which list of rows a description cell is editing. The quote form's cells
// carry no marker and fall through to _quoteRows, so nothing about the
// quotations changed when the purchase orders started using this editor.
function _ceRows(el) {
  var which = el && el.getAttribute && el.getAttribute('data-rows');
  return which === 'po' ? _poRows : _quoteRows;
}

var _CE_BLOCK = {DIV: 1, P: 1, LI: 1, TR: 1, BLOCKQUOTE: 1,
                 H1: 1, H2: 1, H3: 1, H4: 1, H5: 1, H6: 1};

function _ceEsc(s) {
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function _ceColour(el) {
  // A colour ReportLab can parse, or null. Black is treated as no colour so a
  // pasted document does not wrap every line in a redundant <font>.
  var raw = (el.getAttribute && el.getAttribute('color')) || (el.style && el.style.color) || '';
  raw = String(raw).trim();
  if (!raw) return null;
  var rgb = raw.match(/^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)/i);
  if (rgb) {
    var hex = [1, 2, 3].map(function(i) {
      return ('0' + (parseInt(rgb[i], 10) & 255).toString(16)).slice(-2);
    }).join('');
    return hex === '000000' ? null : '#' + hex;
  }
  if (/^#([0-9a-f]{3}|[0-9a-f]{6})$/i.test(raw)) {
    return /^#0{3,6}$/i.test(raw) ? null : raw.toLowerCase();
  }
  return null;    // 'windowtext', 'currentColor', named colours we cannot trust
}

function _ceDecode(s) {
  // Entities have to be decoded before re-escaping, or a saved "&amp;" turns
  // into "&amp;amp;" on the next save, and again on the one after that.
  var d = document.createElement('div');
  d.innerHTML = String(s);
  return d.textContent || '';
}

function _ceClean(html) {
  if (html == null || html === '') return '';
  var src = String(html);
  if (src.indexOf('<') < 0) return _ceEsc(_ceDecode(src));   // plain text, e.g. an import
  var doc = new DOMParser().parseFromString('<div id="r">' + src + '</div>', 'text/html');
  var root = doc.getElementById('r');
  if (!root) return _ceEsc(src.replace(/<[^>]*>/g, ''));
  var out = [];

  (function walk(node) {
    var kids = node.childNodes;
    for (var i = 0; i < kids.length; i++) {
      var n = kids[i];
      if (n.nodeType === 3) { out.push(_ceEsc(n.nodeValue)); continue; }
      if (n.nodeType !== 1) continue;
      var tag = n.tagName.toUpperCase();
      if (tag === 'BR') { out.push('<br>'); continue; }
      if (tag === 'SCRIPT' || tag === 'STYLE') continue;

      var st = n.style || {};
      var weight = String(st.fontWeight || '');
      var wraps = [];
      if (tag === 'B' || tag === 'STRONG' || weight === 'bold' || parseInt(weight, 10) >= 600) {
        wraps.push(['<b>', '</b>']);
      }
      if (tag === 'I' || tag === 'EM' || String(st.fontStyle || '') === 'italic') {
        wraps.push(['<i>', '</i>']);
      }
      if (tag === 'U' || /underline/i.test(String(st.textDecoration || st.textDecorationLine || ''))) {
        wraps.push(['<u>', '</u>']);
      }
      var col = _ceColour(n);
      if (col) wraps.push(['<font color="' + col + '">', '</font>']);

      wraps.forEach(function(w) { out.push(w[0]); });
      walk(n);
      for (var k = wraps.length - 1; k >= 0; k--) out.push(wraps[k][1]);
      // A block ends a line. Without this "<p>one</p><p>two</p>" becomes "onetwo".
      if (_CE_BLOCK[tag] && i < kids.length - 1) out.push('<br>');
      // Table cells sit on one line but still need separating, or a pasted
      // row reads "2IN300#".
      else if ((tag === 'TD' || tag === 'TH') && i < kids.length - 1) out.push(' ');
    }
  })(root);

  var s = out.join('');
  // A bold or coloured wrapper holding nothing but line breaks is what pressing
  // Enter with bold on leaves behind. Keep the breaks, drop the empty wrapper.
  for (var pass = 0; pass < 3; pass++) {
    s = s.replace(/<(b|i|u)>((?:\s|<br>)*)<\/\1>/g, '$2')
         .replace(/<font [^>]*>((?:\s|<br>)*)<\/font>/g, '$1');
  }
  return s
    .replace(/(?:\s*<br>\s*){3,}/g, '<br><br>')
    .replace(/^(?:\s*<br>)+|(?:<br>\s*)+$/g, '')
    .trim();
}

// Paste is delegated from the document because the rows are re-rendered often.
document.addEventListener('paste', function(e) {
  var el = e.target && e.target.closest && e.target.closest('.qf-desc-ce');
  if (!el || !e.clipboardData) return;
  var html = e.clipboardData.getData('text/html');
  var clean = html ? _ceClean(html)
                   : _ceEsc(e.clipboardData.getData('text/plain') || '').replace(/\r?\n/g, '<br>');
  e.preventDefault();
  var ok = false;
  try { ok = document.execCommand('insertHTML', false, clean); } catch (err) { ok = false; }
  if (!ok) {
    // Very old browsers: drop to plain text rather than lose the paste.
    try { document.execCommand('insertText', false, e.clipboardData.getData('text/plain') || ''); }
    catch (err2) { return; }
  }
  var idx  = parseInt(el.getAttribute('data-idx'));
  var rows = _ceRows(el);
  if (!isNaN(idx) && rows[idx] !== undefined) rows[idx].description = el.innerHTML;
});

function _ceDescInput(div, idx) {
  var rows = _ceRows(div);
  if (rows[idx] !== undefined) rows[idx].description = div.innerHTML;
  _activeCe = div;
}

function _ceSyncActive() {
  if (!_activeCe) return;
  var idx  = parseInt(_activeCe.getAttribute('data-idx'));
  var rows = _ceRows(_activeCe);
  if (!isNaN(idx) && rows[idx] !== undefined) rows[idx].description = _activeCe.innerHTML;
}

function _fmtExec(cmd) {
  document.execCommand(cmd);
  _ceSyncActive();
}

function _fmtColor(hex) {
  document.execCommand('foreColor', false, hex);
  _ceSyncActive();
}

(function() {
  document.addEventListener('selectionchange', function() {
    var bar = document.getElementById('qf-fmt-bar');
    if (!bar) return;
    var sel = window.getSelection();
    if (!sel || sel.isCollapsed || !sel.toString().trim()) {
      bar.style.display = 'none';
      return;
    }
    var node = sel.anchorNode;
    while (node && node.nodeType !== 1) node = node.parentNode;
    var ce = node && node.closest && node.closest('.qf-desc-ce');
    if (!ce) { bar.style.display = 'none'; return; }
    _activeCe = ce;
    var range = sel.getRangeAt(0);
    var rect  = range.getBoundingClientRect();
    bar.style.display = 'flex';
    var top = rect.top - bar.offsetHeight - 8;
    if (top < 8) top = rect.bottom + 6;
    bar.style.top  = top + 'px';
    bar.style.left = Math.min(rect.left, window.innerWidth - bar.offsetWidth - 8) + 'px';
  });
})();

function _currSym() {
  var s = document.getElementById('qf-currency');
  if (!s) return '$';
  return s.value === 'GBP' ? '£' : s.value === 'NGN' ? '₦' : '$';
}

function onCurrencyChange() {
  var sym = _currSym();
  var h1 = document.getElementById('qf-hdr-unit-price');
  if (h1) h1.textContent = 'Unit Price (' + sym + ')';
  var h2 = document.getElementById('qf-hdr-total');
  if (h2) h2.textContent = 'Total (' + sym + ')';
  var gl = document.getElementById('qf-grand-lbl');
  if (gl) gl.textContent = 'TOTAL (' + sym + ')';
  recalcTotals();
}

function _fmtQ(n) {
  return _currSym() + (parseFloat(n)||0).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
}

function _collectQData(status) {
  var sub    = _quoteRows.reduce(function(s,r){ return s + (parseFloat(r.line_total)||0); }, 0);
  var disc   = parseFloat((document.getElementById('qf-discount-pct') || {}).value) || 0;
  var markup = parseFloat((document.getElementById('qf-markup-pct') || {}).value) || 0;
  var ship   = parseFloat((document.getElementById('qf-shipping-charges') || {}).value) || 0;
  var eyeBtn = document.getElementById('qf-markup-eye');
  var markupVisible = !eyeBtn || eyeBtn.getAttribute('data-visible') !== '0';
  var tot  = Math.round((sub - sub*disc/100 + sub*markup/100 + ship) * 100) / 100;
  var gv   = function(id){ var e=document.getElementById(id); return e ? e.value.trim() : ''; };
  var data = {
    company:           gv('qf-company') || 'spm',
    client:            gv('qf-client'),
    status:            status || 'draft',
    reference_po:      gv('qf-reference-po'),
    prepared_by:       gv('qf-prepared-by'),
    quote_date:        gv('qf-quote-date'),
    validity_days:     parseInt(gv('qf-validity-days')) || 30,
    recipient_name:    gv('qf-recipient-name'),
    recipient_dept:    gv('qf-recipient-dept'),
    recipient_company: gv('qf-recipient-company'),
    recipient_email:   gv('qf-recipient-email'),
    recipient_tel:     gv('qf-recipient-tel'),
    subject:           gv('qf-subject'),
    lead_time:         gv('qf-lead-time'),
    delivery_dest:     gv('qf-delivery-dest'),
    country_import:    gv('qf-country-import'),
    shipping_mode:     gv('qf-shipping-mode'),
    manufacturer:      gv('qf-manufacturer'),
    weight:            gv('qf-weight'),
    currency:          gv('qf-currency') || 'USD',
    discount_pct:      disc,
    markup_pct:        markup,
    markup_visible:    markupVisible,
    shipping_charges:  ship,
    sub_total:         Math.round(sub * 100) / 100,
    total:             tot,
    notes:             gv('qf-notes'),
    line_items: _quoteRows.map(function(r,i){
      // Cleaned once more on the way out: typing can leave markup the paste
      // handler never saw, such as the "<b><br></b>" a blank line typed in bold
      // produces, which is what stripped 13 of 22 rows on SPM-00001-TEN.
      return { item_no: i+1, product_no: r.product_no||'', description: _ceClean(r.description||''), uom: r.uom||'EA',
               quantity: parseFloat(r.quantity)||0, unit_price: parseFloat(r.unit_price)||0,
               tax_rate: parseFloat(r.tax_rate)||0, line_total: parseFloat(r.line_total)||0 };
    }),
  };
  // Per-field T&C eyes → show_lead_time, show_delivery_dest, …
  _TC_FIELDS.forEach(function(key){
    data['show_' + key.replace(/-/g, '_')] = _tcVisible(key);
  });
  return data;
}

function saveQuote(status) {
  if (!document.getElementById('qf-client').value) { alert('Please select a client before saving.'); return; }
  var data   = _collectQData(status);
  var isNew  = !_editingQid;
  var url    = isNew ? '/api/quotations' : '/api/quotations/' + _editingQid;
  var method = isNew ? 'POST' : 'PUT';
  var btn    = document.getElementById('qf-save-btn');
  var btn2   = document.getElementById('qf-save-btn2');
  [btn, btn2].forEach(function(b){ if(b){ b.disabled=true; b.textContent='Saving…'; } });
  authFetch(url, { method:method, headers:{'Content-Type':'application/json'}, body:JSON.stringify(data) })
    // A failed save comes back as an HTTP error with FastAPI's {detail:…}, not
    // {error:…}. Reading only .json() and checking resp.error missed that
    // entirely, so a server error drew "Saved ✓" over a quote that had not
    // been saved. Check the status first and carry the detail into the throw.
    .then(function(r){
      return r.json().catch(function(){ return {}; }).then(function(body){
        if (!r.ok) throw new Error(body.detail || body.error || ('Server error ' + r.status));
        return body;
      });
    })
    .then(function(resp){
      if (resp.error) { alert('Save failed: ' + resp.error); [btn,btn2].forEach(function(b){if(b){b.disabled=false;b.textContent='Save Draft';}}); return; }
      if (isNew && resp.id) {
        _editingQid = resp.id;
        var qnEl = document.getElementById('qf-quote-number');
        if (qnEl && resp.quote_number) qnEl.value = resp.quote_number;
        _setPdfBtnVisible(true);
      }
      loadQuotes();
      [btn,btn2].forEach(function(b){
        if (!b) return;
        b.textContent = 'Saved ✓';
        setTimeout(function(){ b.textContent='Save Draft'; b.disabled=false; }, 2000);
      });
    })
    .catch(function(e){
      // Never clear the form on a failed save - what is on screen may be the
      // only surviving copy of the line items.
      alert('Save failed — your quote has NOT been saved.\n\n' + e.message
            + '\n\nEverything is still on screen. Press Save again.');
      [btn,btn2].forEach(function(b){ if(b){ b.disabled=false; b.textContent='Save Draft'; } });
    });
}

function downloadCurrentPdf() {
  if (!_editingQid) return;
  var label = (document.getElementById('qf-subject') || {}).value
           || (document.getElementById('qf-quote-number') || {}).value
           || _editingQid;

  // The PDF is rendered from the SAVED record, not from the form on screen.
  // Downloading without saving first silently produced a PDF that ignored any
  // unsaved edit — most visibly the eye toggles, which look like they should
  // take effect immediately. Save first so the file matches what's on screen.
  // _editingStatus is passed through so a "sent" quote isn't reset to draft.
  var btns = ['qf-pdf-btn','qf-pdf-btn2']
    .map(function(id){ return document.getElementById(id); })
    .filter(Boolean);
  var original = btns.length ? btns[0].textContent : '⬇ Download PDF';
  btns.forEach(function(b){ b.disabled = true; b.textContent = 'Saving…'; });

  authFetch('/api/quotations/' + _editingQid, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(_collectQData(_editingStatus)),
  })
    // The PDF is built server-side from the saved copy, so if this save fails
    // the document would be generated from the OLD figures while the screen
    // shows the new ones — a wrong-priced quote that looks entirely normal on
    // its way to a client. Checking r.ok is what stops that.
    .then(function(r){
      return r.json().catch(function(){ return {}; }).then(function(body) {
        if (!r.ok) throw new Error(body.detail || body.error || ('Server error ' + r.status));
        return body;
      });
    })
    .then(function(resp){
      if (resp && resp.error) throw new Error(resp.error);
      btns.forEach(function(b){ b.textContent = 'Generating…'; });
      downloadQuotePdf(_editingQid, label);
      loadQuotes();
    })
    .catch(function(e){
      alert('NOT saved, so no PDF was generated — it would have been built from '
            + 'the previous version.\n\n' + e.message
            + '\n\nYour changes are still on screen. Press Save, then try again.');
    })
    .finally(function(){
      btns.forEach(function(b){ b.disabled = false; b.textContent = original; });
    });
}

function downloadQuotePdf(quoteId, quoteNumber) {
  authFetch('/api/quotations/' + quoteId + '/pdf')
    .then(function(res){
      if (!res.ok) { res.json().then(function(d){ alert('PDF error: ' + (d.error||res.status)); }).catch(function(){ alert('PDF generation failed'); }); return null; }
      return res.blob();
    })
    .then(function(blob){
      if (!blob) return;
      var url2 = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url2;
      var safeName = (quoteNumber||quoteId).replace(/[^A-Za-z0-9\s._-]/g,'').trim().replace(/\s+/g,'_');
      a.download = (safeName || quoteId) + '.pdf';
      document.body.appendChild(a);
      a.click();
      setTimeout(function(){ URL.revokeObjectURL(url2); if(a.parentNode) a.parentNode.removeChild(a); }, 1000);
    })
    .catch(function(e){ alert('Download failed: ' + e.message); });
}

function confirmDeleteQuote(quoteId) {
  _deleteQid = quoteId;
  var q = _quotes.find(function(x){ return x.id === quoteId; });
  var numEl = document.getElementById('dq-quote-num');
  if (numEl) numEl.textContent = (q && q.quote_number) ? q.quote_number : 'this quote';
  var errEl = document.getElementById('dq-error');
  if (errEl) errEl.style.display = 'none';
  var m = document.getElementById('delete-quote-modal');
  if (m) m.classList.remove('hidden');
}

function closeDeleteQuoteModal() {
  _deleteQid = null;
  var btn = document.getElementById('dq-confirm-btn');
  if (btn) btn.disabled = false;
  var m = document.getElementById('delete-quote-modal');
  if (m) m.classList.add('hidden');
}

function doDeleteQuote() {
  if (!_deleteQid) return;
  var qid = _deleteQid;
  var btn = document.getElementById('dq-confirm-btn');
  if (btn) { btn.disabled = true; btn.textContent = 'Deleting…'; }
  authFetch('/api/quotations/' + qid, { method:'DELETE' })
    .then(function(r){ return r.json(); })
    .then(function(d){
      if (btn) { btn.disabled = false; btn.textContent = 'Delete'; }
      if (d.error) {
        var errEl = document.getElementById('dq-error');
        if (errEl) { errEl.textContent = 'Delete failed: ' + d.error; errEl.style.display = ''; }
        return;
      }
      closeDeleteQuoteModal();
      loadQuotes();
    })
    .catch(function(e){
      if (btn) { btn.disabled = false; btn.textContent = 'Delete'; }
      var errEl = document.getElementById('dq-error');
      if (errEl) { errEl.textContent = 'Network error: ' + e.message; errEl.style.display = ''; }
    });
}


// ══ Suppliers — Flexitallic PO/SO history ═══════════════════════════════════
// Reconstructed from Flexitallic's own acknowledgement emails, 2024 to date.
// Read-only: this is a point-in-time record for the investment review, not
// live pipeline data, so nothing here writes back.

var FX_ROWS = [];
var FX_CLIENT_PO_VALUES = {};   // client PO number -> what the client paid SPM
// {last_checked, newest_so_date}. last_checked is the listener's heartbeat, so
// it stops moving if the listener stops - the page says so instead of quietly
// presenting a stale snapshot as current figures.
var FX_STATUS = null;
var FX_STALE_MS = 60 * 60 * 1000;   // listener checks every 2 min; an hour is clearly stuck

function _fxLoadStatus() {
  return authFetch('/api/flexitallic_status')
    .then(function(r) { return r.ok ? r.json() : null; })
    .then(function(s) { FX_STATUS = s; })
    .catch(function() { FX_STATUS = null; });
}

function _fxAgo(iso) {
  var ms = Date.now() - new Date(iso).getTime();
  if (isNaN(ms)) return '';
  var m = Math.round(ms / 60000);
  if (m < 1) return 'just now';
  if (m < 60) return m + ' min ago';
  var h = Math.round(m / 60);
  if (h < 48) return h + ' h ago';
  return Math.round(h / 24) + ' days ago';
}

function _fxStatusHtml() {
  if (!FX_STATUS) return '';
  var out = '';
  if (FX_STATUS.last_checked) {
    var stale = Date.now() - new Date(FX_STATUS.last_checked).getTime() > FX_STALE_MS;
    out += ' · <span style="color:' + (stale ? 'var(--warn)' : 'var(--t3)') + '"'
      + ' title="' + _esc(new Date(FX_STATUS.last_checked).toLocaleString('en-GB')) + '">'
      + (stale ? 'not updated since ' : 'checked for new sales orders ')
      + _esc(_fxAgo(FX_STATUS.last_checked)) + '</span>';
  }
  if (FX_STATUS.newest_so_date) {
    out += ' · <span style="color:var(--t3)">newest SO ' + _esc(_fxDate(FX_STATUS.newest_so_date)) + '</span>';
  }
  return out;
}
var FX_PO_ROWS = {};            // client PO number -> the SO numbers carrying it

// Everything on this page is reported in USD. Client PO values arrive already
// converted at the rate on each PO's own date, and sterling sales orders carry
// a server-side usd_value converted at their SO date. The live rate below (the
// same source as the dashboard) is only the fallback for a row that has no
// dated value. Summing the raw figures instead once turned three naira POs
// into an extra $13.7M.
// Last-resort values, used only if open.er-api.com cannot be reached. Refreshed
// 12 Sep 2026 (live was 1 USD = 1,327 NGN / 0.7395 GBP); the old 1600/0.79 pair
// was 20% and 7% out respectively, which quietly skewed totals on any day the
// rate fetch failed.
var FX_RATES = {USD: 1, NGN: 1327, GBP: 0.74};

function _fxToUsd(amount, currency) {
  var v = Number(amount) || 0;
  var rate = FX_RATES[currency || 'USD'];
  return rate ? v / rate : v;
}

async function loadFxRates() {
  try {
    var res = await fetch('https://open.er-api.com/v6/latest/USD');
    var data = await res.json();
    if (data && data.rates) {
      if (data.rates.NGN) FX_RATES.NGN = data.rates.NGN;
      if (data.rates.GBP) FX_RATES.GBP = data.rates.GBP;
    }
  } catch (e) { console.warn('Flexitallic page: exchange rate fetch failed, using fallback'); }
}

function _fxClientPos(r) {
  var v = r.customer_po_numbers;
  if (!v) return [];
  if (typeof v === 'string') {
    try { v = JSON.parse(v); } catch (e) { return [String(v)]; }
  }
  return Array.isArray(v) ? v.map(String) : [String(v)];
}
var _fxYear = 'all';
var _fxCust = 'all';
var _fxPage = 1;
var FX_PER_PAGE = 50;
var _fxBasis = 'so';   // 'so' = what Flexitallic charged, 'po' = what SPM ordered

function setFxBasis(b) {
  _fxBasis = b;
  var so = document.getElementById('fx-basis-so');
  var po = document.getElementById('fx-basis-po');
  if (so) so.classList.toggle('qt-co-tab-active', b === 'so');
  if (po) po.classList.toggle('qt-co-tab-active', b === 'po');
  renderFlexitallic();
}

// A sales order value belongs to that one sales order. A PURCHASE order value
// belongs to the PO, and repeats on every sales order Flexitallic split it
// into - so summing it row by row would count those POs two or three times.
// Every total below goes through here.
function _fxSum(rows) {
  if (_fxBasis === 'so') {
    return rows.reduce(function(t, r) { return t + r._val; }, 0);
  }
  // Client PO values belong to the CLIENT's PO number, and the same client PO
  // can appear on more than one row, so each is counted once by number rather
  // than once per row.
  var seen = {}, total = 0;
  rows.forEach(function(r) {
    _fxClientPos(r).forEach(function(n) {
      if (seen[n]) return;
      var v = FX_CLIENT_PO_VALUES[n];
      if (v == null) return;
      seen[n] = 1;
      total += v;
    });
  });
  return total;
}

function _fxBasisLabel() {
  return _fxBasis === 'po' ? 'client purchase order value' : 'sales order value';
}

// Fixed hue order, assigned by customer identity and never cycled — a filter
// that changes which customers are present must not repaint the survivors.
// The five customers the business reports on. Everything else - China,
// Starfinix, Vagan, Ella, Indorama, Waltex, website orders, orders naming two
// customers, and the handful naming none - is grouped as "Other". Those are
// real orders and their money is still counted; they are just not separately
// interesting at summary level, and several of them are single small orders
// that render as invisible slivers on a donut.
var FX_NAMED_CUSTOMERS = ['CHEVRON', 'NLNG', 'MOBIL', 'SEPLAT', 'AVEON'];

function _fxGroup(customer) {
  return FX_NAMED_CUSTOMERS.indexOf(customer) >= 0 ? customer : 'Other';
}

var FX_CUST_COLORS = {
  CHEVRON: '--accent', NLNG: '#144FA0', MOBIL: '--warn',
  SEPLAT: '--ok', AVEON: '#6B4FA0', Other: '--t3'
};

function _fxColor(key) {
  var c = FX_CUST_COLORS[key] || '--t3';
  return c.charAt(0) === '-' ? _rptCssVar(c) : c;
}
function _fxMoney(n, cur) {
  var sym = cur === 'GBP' ? '£' : '$';
  return sym + (Number(n) || 0).toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 2});
}
function _fxShort(n) {
  n = Number(n) || 0;
  if (n >= 1e6) return '$' + (n / 1e6).toFixed(2) + 'M';
  if (n >= 1e3) return '$' + Math.round(n / 1e3) + 'K';
  return '$' + n.toFixed(0);
}

function loadFlexitallicHistory() {
  // The rows are cached for the session, but freshness is re-read on every
  // visit - it is one tiny request and the whole point is that it is current.
  if (FX_ROWS.length) { _fxLoadStatus().then(renderFlexitallic); return; }
  _fxLoadStatus();
  Promise.all([
    loadFxRates().then(function() {
      return authFetch('/api/flexitallic_history').then(function(res) { return res.json(); });
    }),
    authFetch('/api/client_po_values').then(function(res) { return res.json(); })
      .catch(function() { return []; })
  ])
    .then(function(both) {
      var rows = both[0];
      (both[1] || []).forEach(function(p) {
        if (p && p.po_number != null && p.po_value != null) {
          FX_CLIENT_PO_VALUES[String(p.po_number)] = _fxToUsd(p.po_value, p.currency);
        }
      });
      FX_ROWS = (rows || []).map(function(r) {
        r._year = String(r.so_date || '').slice(0, 4);
        r._cust_raw = r.customer || '(unknown)';
        r._cust = _fxGroup(r._cust_raw);
        r._po = r.spm_po_ref || r.spm_po_reference || '—';
        // Converted at the SO date on the server when non-USD; live rate otherwise.
        r._val = r.usd_value != null ? Number(r.usd_value) : _fxToUsd(r.order_value, r.currency);
        r._po_val = r.po_value == null ? null : _fxToUsd(r.po_value, r.po_currency || r.currency);
        // What the END CLIENT paid SPM. Values are keyed by the client's own
        // PO number and a row can carry several of them, so they are summed
        // here. Null when none of this row's client POs has been valued yet -
        // deliberately not zero, which would read as a free order.
        var cps = _fxClientPos(r), got = false, tot = 0;
        cps.forEach(function(n) {
          var v = FX_CLIENT_PO_VALUES[n];
          if (v != null) { tot += v; got = true; }
        });   // already converted to USD when the lookup was built
        r._client_val = got ? tot : null;
        r._client_po_count = cps.length;
        return r;
      });
      // The same client PO can sit on several sales orders — Chevron re-raises
      // one PO across multiple SOs. Each row shows that PO's full value, which
      // is right for reading one row but means adding the column up overstates
      // the total. The summary card already counts each PO once; this records
      // which POs repeat so the table can say so and the footer can show the
      // honest deduped total.
      FX_PO_ROWS = {};
      FX_ROWS.forEach(function(r) {
        _fxClientPos(r).forEach(function(n) {
          (FX_PO_ROWS[n] = FX_PO_ROWS[n] || []).push(r.so_number || '?');
        });
      });
      FX_ROWS.forEach(function(r) {
        r._shared_pos = _fxClientPos(r).filter(function(n) {
          return (FX_PO_ROWS[n] || []).length > 1;
        });
      });
      renderFlexitallic();
    })
    .catch(function() {
      var el = document.getElementById('fx-tbody');
      if (el) el.innerHTML = '<tr><td colspan="8" style="padding:2rem;text-align:center;color:var(--t3)">Could not load Flexitallic history.</td></tr>';
    });
}

function _fxFiltered() {
  return FX_ROWS.filter(function(r) {
    return (_fxYear === 'all' || r._year === _fxYear)
        && (_fxCust === 'all' || r._cust === _fxCust);
  });
}

function setFxYear(y) { _fxYear = y; _fxPage = 1; renderFlexitallic(); }

// Table sort for the two value columns. Clicking cycles highest first ->
// lowest first -> back to the default date order. Rows without a value always
// sit at the bottom, in both directions, so "no value yet" is never mistaken
// for the smallest order.
var _fxSortKey = null;
var _fxSortDir = 'desc';

function setFxSort(key) {
  if (_fxSortKey !== key) { _fxSortKey = key; _fxSortDir = 'desc'; }
  else if (_fxSortDir === 'desc') { _fxSortDir = 'asc'; }
  else { _fxSortKey = null; _fxSortDir = 'desc'; }
  _fxPage = 1;
  renderFlexitallic();
}

function _fxSorted(rows) {
  if (!_fxSortKey) return rows;
  var k = _fxSortKey, sign = _fxSortDir === 'desc' ? -1 : 1;
  return rows.slice().sort(function(a, b) {
    var av = a[k], bv = b[k];
    if (av == null && bv == null) return 0;
    if (av == null) return 1;
    if (bv == null) return -1;
    return (av - bv) * sign;
  });
}
function setFxCust(c) { _fxCust = c; _fxPage = 1; renderFlexitallic(); }
function fxPage(d) {
  var max = Math.max(1, Math.ceil(_fxFiltered().length / FX_PER_PAGE));
  _fxPage = Math.min(max, Math.max(1, _fxPage + d));
  renderFlexitallic();
}

function renderFlexitallic() {
  var rows = _fxFiltered();
  _rptEnsureTip();   // charts below use the shared tooltip

  // ── Summary cards ────────────────────────────────────────────────────────
  // USD and GBP are kept apart: 11 orders were priced in sterling and adding
  // them to the dollar figure would quietly invent money.
  var items = 0;
  var poSet = {}, soSet = {}, poPriced = {};
  rows.forEach(function(r) {
    items += (r.line_item_count || 0);
    poSet[r._po] = 1; soSet[r.so_number] = 1;
    if (r._po_val != null) poPriced[r._po] = 1;
  });
  var nPo = Object.keys(poSet).length, nSo = Object.keys(soSet).length;
  var nPriced = Object.keys(poPriced).length;

  // Both figures are always on screen. They are genuinely different numbers -
  // of 164 orders where both are known, one matched - so showing only the
  // active basis would invite the other to be assumed equal to it.
  var soTotal = rows.reduce(function(t, r) { return t + r._val; }, 0);
  // Client PO value: what Chevron, NLNG and Mobil paid SPM. Counted once per
  // client PO number, since one SPM order can bundle several of them.
  var seenC = {}, clientTotal = 0, clientPos = 0, clientValued = 0;
  rows.forEach(function(r) {
    _fxClientPos(r).forEach(function(n) {
      if (seenC[n]) return;
      seenC[n] = 1; clientPos += 1;
      var v = FX_CLIENT_PO_VALUES[n];
      if (v != null) { clientTotal += v; clientValued += 1; }
    });
  });

  var clientNote = clientPos === 0 ? 'no client POs referenced'
    : clientValued < clientPos
      ? (clientPos - clientValued) + ' of ' + clientPos + ' client POs not valued yet'
      : 'what the client paid SPM';
  document.getElementById('fx-kpis').innerHTML =
      _fxKpi('Client PO value', _fxShort(clientTotal), clientNote, _fxBasis === 'po')
    + _fxKpi('SO value', _fxShort(soTotal), 'what Flexitallic charged', _fxBasis === 'so')
    + _fxKpi('Client POs', String(clientPos),
             clientValued < clientPos
               ? clientValued + ' of them carry a value'
               : 'all valued')
    + _fxKpi('Sales orders', String(nSo), 'acknowledged by Flexitallic')
    + _fxKpi('Gasket lines', items.toLocaleString(), 'items across all orders');

  var sub = document.getElementById('fx-sub');
  if (sub) {
    sub.innerHTML = 'Purchase orders and sales orders, 2024 to date'
      + (_fxYear !== 'all' || _fxCust !== 'all' ? ' — filtered' : '')
      + _fxStatusHtml();
  }

  var ty = document.getElementById('fx-t-year');
  var tc = document.getElementById('fx-t-cust');
  if (ty) ty.textContent = (_fxBasis === 'po' ? 'Client PO' : 'Sales order') + ' value by year';
  if (tc) tc.textContent = (_fxBasis === 'po' ? 'Client PO' : 'Sales order') + ' value by end customer';

  _fxRenderChips();
  // The year chart exists to compare years, so it deliberately ignores the
  // year filter - otherwise picking 2024 leaves a single bar comparing
  // nothing. The customer filter still applies, and the chosen year is
  // highlighted instead.
  _fxRenderYearBars(FX_ROWS.filter(function(r) {
    return _fxCust === 'all' || r._cust === _fxCust;
  }));
  _fxRenderCustomerDonut(rows);
  _fxRenderMix(rows);
  _fxRenderTable(rows);
}

function _fxKpi(lbl, val, note, active) {
  return '<div class="kpi' + (active ? ' kpi-on' : '') + '"><div class="kpi-lbl">' + _esc(lbl) + '</div>'
       + '<div class="kpi-val">' + _esc(val) + '</div>'
       + (note ? '<div class="kpi-note">' + _esc(note) + '</div>' : '') + '</div>';
}

function _fxRenderChips() {
  var years = {};
  FX_ROWS.forEach(function(r) { years[r._year] = 1; });
  var ys = Object.keys(years).sort();
  var h = '<div class="chip' + (_fxYear === 'all' ? ' on' : '') + '" onclick="setFxYear(\'all\')">All years</div>';
  ys.forEach(function(y) {
    h += '<div class="chip' + (_fxYear === y ? ' on' : '') + '" onclick="setFxYear(\'' + y + '\')">' + y + '</div>';
  });
  document.getElementById('fx-chips-year').innerHTML = h;

  // Counted within the active YEAR filter, so the number on a chip matches
  // what you get when you click it.
  var scope = FX_ROWS.filter(function(r) { return _fxYear === 'all' || r._year === _fxYear; });
  var cs = {};
  scope.forEach(function(r) { cs[r._cust] = (cs[r._cust] || 0) + 1; });
  var keys = Object.keys(cs).sort(function(a, b) { return cs[b] - cs[a]; });
  var h2 = '<div class="chip' + (_fxCust === 'all' ? ' on' : '') + '" onclick="setFxCust(\'all\')">All customers</div>';
  keys.forEach(function(k) {
    h2 += '<div class="chip' + (_fxCust === k ? ' on' : '') + '" onclick="setFxCust(\'' + k.replace(/'/g, "\'") + '\')">'
        + _esc(k) + ' <span style="opacity:.6">' + cs[k] + '</span></div>';
  });
  document.getElementById('fx-chips-cust').innerHTML = h2;
}

// ── Order value by year (USD only — mixing currencies would be a lie) ───────
function _fxRenderYearBars(rows) {
  var cont = document.getElementById('fx-bars-year');
  if (!cont) return;
  cont.innerHTML = '';

  var byYear = {};
  var groups = {};
  rows.forEach(function(r) { (groups[r._year] = groups[r._year] || []).push(r); });
  Object.keys(groups).forEach(function(y) { byYear[y] = _fxSum(groups[y]); });
  var years = Object.keys(byYear).sort();
  if (!years.length) { cont.innerHTML = '<div class="act-empty">No orders in this selection.</div>'; return; }

  var VW = 380, VH = 158, ml = 40, mr = 8, mt = 10, mb = 26;
  var pw = VW - ml - mr, ph = VH - mt - mb;
  var maxV = Math.max.apply(null, years.map(function(y) { return byYear[y]; }));
  var svg = _rptSvgEl('svg', {viewBox: '0 0 ' + VW + ' ' + VH, width: '100%', height: VH});

  // recessive gridlines, so the bars carry the reading
  [0, 0.5, 1].forEach(function(f) {
    var y = mt + ph - f * ph;
    var g = _rptSvgEl('line', {x1: ml, x2: VW - mr, y1: y, y2: y, 'stroke-width': 1});
    g.style.stroke = _rptCssVar('--border');
    svg.appendChild(g);
    var t = _rptSvgTxt(_fxShort(maxV * f), {x: ml - 6, y: y + 3, 'text-anchor': 'end'});
    t.style.fontSize = '9px'; t.style.fill = _rptCssVar('--t3');
    svg.appendChild(t);
  });

  var bw = Math.min(64, pw / years.length - 18);
  years.forEach(function(y, i) {
    var v = byYear[y];
    var h = maxV ? (v / maxV) * ph : 0;
    var x = ml + (pw / years.length) * i + (pw / years.length - bw) / 2;
    var bar = _rptSvgEl('rect', {x: x, y: mt + ph - h, width: bw, height: Math.max(h, 1), rx: 4});
    bar.style.fill = _rptCssVar('--accent');
    bar.style.opacity = (_fxYear === 'all' || _fxYear === y) ? '1' : '0.28';
    bar.style.cursor = 'pointer';
    bar.addEventListener('click', function() { setFxYear(_fxYear === y ? 'all' : y); });
    bar.addEventListener('mousemove', function(e) { _rptShowTip(e, y, _fxMoney(v, 'USD') + ' · ' + _fxBasisLabel()); });
    bar.addEventListener('mouseleave', _rptHideTip);
    svg.appendChild(bar);

    var lbl = _rptSvgTxt(y, {x: x + bw / 2, y: VH - 8, 'text-anchor': 'middle'});
    lbl.style.fontSize = '10px'; lbl.style.fill = _rptCssVar('--t2');
    svg.appendChild(lbl);

    var val = _rptSvgTxt(_fxShort(v), {x: x + bw / 2, y: mt + ph - h - 4, 'text-anchor': 'middle'});
    val.style.fontSize = '10px'; val.style.fontWeight = '600'; val.style.fill = _rptCssVar('--t1');
    svg.appendChild(val);
  });
  cont.appendChild(svg);

  var note = document.getElementById('fx-bars-note');
  if (note) {
    var thisYear = String(new Date().getFullYear());
    var basis = (_fxBasis === 'po'
      ? 'What the end client paid SPM, counted once per client PO.'
      : 'Sales order totals as acknowledged by Flexitallic.')
      + ' All figures in USD; naira and sterling converted at the live rate.';
    note.textContent = basis + (years.indexOf(thisYear) >= 0
      ? ' ' + thisYear + ' is a part year, so its bar is not comparable with the full years beside it.'
      : ' USD orders only.');
  }
}

// ── Value by end customer ──────────────────────────────────────────────────
function _fxRenderCustomerDonut(rows) {
  var cont = document.getElementById('fx-donut-cust');
  var leg = document.getElementById('fx-leg-cust');
  if (!cont || !leg) return;
  cont.innerHTML = ''; leg.innerHTML = '';

  var by = {};
  var grp = {};
  rows.forEach(function(r) { (grp[r._cust] = grp[r._cust] || []).push(r); });
  Object.keys(grp).forEach(function(k) { by[k] = _fxSum(grp[k]); });
  var keys = Object.keys(by).filter(function(k) { return by[k] > 0; })
                            .sort(function(a, b) { return by[b] - by[a]; });
  var total = keys.reduce(function(s, k) { return s + by[k]; }, 0);
  if (!total) { cont.innerHTML = '<div class="act-empty">No orders in this selection.</div>'; return; }

  var W = 190, cx = 95, cy = 95, R = 70, sw = 17;
  var circ = 2 * Math.PI * R;
  var svg = _rptSvgEl('svg', {viewBox: '0 0 ' + W + ' ' + W, width: W, height: W});
  svg.style.minWidth = W + 'px';
  var bg = _rptSvgEl('circle', {cx: cx, cy: cy, r: R, fill: 'none', 'stroke-width': sw});
  bg.style.stroke = _rptCssVar('--s2');
  svg.appendChild(bg);

  var acc = 0;
  keys.forEach(function(k) {
    var v = by[k];
    var arc = (v / total) * circ;
    var gap = keys.length > 1 ? 2 : 0;   // 2px of surface between segments
    var c = _rptSvgEl('circle', {
      cx: cx, cy: cy, r: R, fill: 'none',
      'stroke-dasharray': Math.max(arc - gap, 0.5) + ' ' + (circ - arc + gap),
      'stroke-dashoffset': circ / 4 - acc,
      'stroke-width': sw, 'stroke-linecap': 'butt'
    });
    c.style.stroke = _fxColor(k);
    c.style.cursor = 'pointer';
    (function(kk, vv, pct) {
      c.addEventListener('mousemove', function(e) { _rptShowTip(e, kk, _fxMoney(vv, 'USD') + ' · ' + pct + '% · ' + _fxBasisLabel()); });
      c.addEventListener('mouseleave', _rptHideTip);
    })(k, v, Math.round(v / total * 100));
    svg.appendChild(c);
    acc += arc;
  });
  cont.appendChild(svg);

  // legend always present, so identity is never colour-alone
  keys.forEach(function(k) {
    var row = document.createElement('div');
    row.className = 'rpt-leg-row';
    row.innerHTML = '<span class="rpt-leg-l"><span class="rpt-leg-dot" style="background:'
      + _fxColor(k) + '"></span>' + _esc(k) + '</span>'
      + '<span class="rpt-leg-v">' + _fxShort(by[k]) + '</span>';
    leg.appendChild(row);
  });
}

// ── Gasket mix by Flexitallic item-code prefix ─────────────────────────────
// Deliberately labelled by raw code. The product names these prefixes map to
// were never confirmed with Flexitallic, and this page is for Flexitallic.
function _fxRenderMix(rows) {
  var cont = document.getElementById('fx-bars-mix');
  if (!cont) return;
  cont.innerHTML = '';

  var by = {}, qty = {};
  var seenPo = {};
  rows.forEach(function(r) {
    // On the PO basis the gasket lines come off the purchase order, and a PO
    // split into several sales orders must contribute its lines only once.
    var src = r.line_items || [];
    if (_fxBasis === 'po') {
      if (seenPo[r._po]) return;
      seenPo[r._po] = 1;
      src = r.po_line_items || r.line_items || [];
    }
    src.forEach(function(it) {
      var code = String(it.item_number || it.customer_po || '').toUpperCase();
      var m = code.match(/^([A-Z]+)/);
      var k = m ? m[1] : '—';
      by[k] = (by[k] || 0) + (Number(it.extended_price != null ? it.extended_price : it.total) || 0);
      qty[k] = (qty[k] || 0) + (Number(it.qty) || 0);
    });
  });
  var keys = Object.keys(by).sort(function(a, b) { return by[b] - by[a]; }).slice(0, 12);
  if (!keys.length) { cont.innerHTML = '<div class="act-empty">No line items in this selection.</div>'; return; }
  var max = by[keys[0]];

  keys.forEach(function(k) {
    var row = document.createElement('div');
    row.className = 'fx-bar-row';
    var pct = max ? (by[k] / max) * 100 : 0;
    row.innerHTML = '<div class="fx-bar-code">' + _esc(k) + '</div>'
      + '<div class="fx-bar-track"><div class="fx-bar-fill" style="width:' + pct.toFixed(1)
      + '%;background:' + _rptCssVar('--accent') + '"></div></div>'
      + '<div class="fx-bar-val">' + _fxShort(by[k]) + '</div>';
    row.title = k + ' — ' + _fxMoney(by[k], 'USD') + ' across ' + Math.round(qty[k]).toLocaleString() + ' pcs';
    cont.appendChild(row);
  });
}

// ── PO / SO table ──────────────────────────────────────────────────────────
var FX_COLS = [
  {k: '_po',      hdr: 'SPM PO'},
  {k: '_cust',    hdr: 'Customer'},
  {k: 'custpo',   hdr: 'Customer PO'},
  {k: 'so_number',hdr: 'Flexitallic SO'},
  {k: 'so_date',  hdr: 'SO date'},
  {k: '_client_val', hdr: 'Client PO value', num: true, sort: true},
  {k: '_val',     hdr: 'SO value', num: true, sort: true},
  {k: 'line_item_count', hdr: 'Items', num: true},
  {k: 'docs',     hdr: 'Documents'}
];

function _fxCustPo(r) {
  var v = r.customer_po_numbers;
  if (!v) return '';
  if (typeof v === 'string') { try { v = JSON.parse(v); } catch (e) { return v; } }
  return Array.isArray(v) ? v.join(', ') : String(v);
}

function _fxDate(iso) {
  if (!iso) return '';
  var d = new Date(iso);
  if (isNaN(d)) return String(iso).slice(0, 10);
  return d.toLocaleDateString('en-GB', {day: '2-digit', month: 'short', year: '2-digit'});
}

function _fxDocLinks(r) {
  var out = [];
  if (r.po_pdf_url) out.push('<a href="' + _esc(r.po_pdf_url) + '" target="_blank" rel="noopener" class="fx-doc">PO</a>');
  if (r.so_pdf_url) out.push('<a href="' + _esc(r.so_pdf_url) + '" target="_blank" rel="noopener" class="fx-doc">SO</a>');
  return out.length ? out.join(' ') : '<span style="color:var(--t3)">—</span>';
}

function _fxValTitle(r) {
  var bits = [];
  if (r._client_po_count > 1) bits.push('Covers ' + r._client_po_count + ' client POs, summed');
  (r._shared_pos || []).forEach(function(n) {
    var others = (FX_PO_ROWS[n] || []).filter(function(s) { return s !== r.so_number; });
    bits.push('PO ' + n + ' also appears on ' + others.join(', ')
              + ' — counted once in the summary, so do not add this column up');
  });
  return bits.join('. ');
}

function _fxRenderTable(rows) {
  var head = document.getElementById('fx-thead');
  var body = document.getElementById('fx-tbody');
  var foot = document.getElementById('fx-foot');
  if (!head || !body) return;

  head.innerHTML = FX_COLS.map(function(c) {
    if (!c.sort) {
      return '<th' + (c.num ? ' style="text-align:right"' : '') + '>' + _esc(c.hdr) + '</th>';
    }
    var on = _fxSortKey === c.k;
    var arrow = on ? (_fxSortDir === 'desc' ? ' ▼' : ' ▲') : ' ⇅';
    var next = !on ? 'highest first' : _fxSortDir === 'desc' ? 'lowest first' : 'date order';
    return '<th style="text-align:right;cursor:pointer;user-select:none;white-space:nowrap'
      + (on ? ';color:var(--accent)' : '') + '"'
      + ' role="button" tabindex="0"'
      + ' aria-sort="' + (on ? (_fxSortDir === 'desc' ? 'descending' : 'ascending') : 'none') + '"'
      + ' title="Sort by ' + _esc(c.hdr) + ' - ' + next + '"'
      + ' onclick="setFxSort(\'' + c.k + '\')"'
      + ' onkeydown="if(event.key===\'Enter\'||event.key===\' \'){event.preventDefault();setFxSort(\'' + c.k + '\')}">'
      + _esc(c.hdr) + '<span style="opacity:' + (on ? '1' : '.45') + '">' + arrow + '</span></th>';
  }).join('');

  rows = _fxSorted(rows);

  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="' + FX_COLS.length
      + '" style="padding:2rem;text-align:center;color:var(--t3)">No orders match this filter.</td></tr>';
    if (foot) foot.innerHTML = '';
    return;
  }

  var pages = Math.max(1, Math.ceil(rows.length / FX_PER_PAGE));
  if (_fxPage > pages) _fxPage = pages;
  var slice = rows.slice((_fxPage - 1) * FX_PER_PAGE, _fxPage * FX_PER_PAGE);

  body.innerHTML = slice.map(function(r) {
    return '<tr>'
      + '<td style="font-weight:600" title="' + _esc(r.spm_po_reference || '') + '">'
        + _esc(r._po) + '</td>'
      + '<td><span class="stage-pill" style="background:var(--s2);color:var(--t2)"'
        + (r._cust_raw !== r._cust ? ' title="Grouped as ' + _esc(r._cust) + ' in the summary"' : '')
        + '>'
        + '<span class="d" style="background:' + _fxColor(r._cust) + '"></span>'
        + _esc(r._cust_raw || r._cust) + '</span></td>'
      + '<td style="font-family:var(--mono);font-size:11px">' + _esc(_fxCustPo(r)) + '</td>'
      + '<td style="font-family:var(--mono);font-size:11px">' + _esc(r.so_number || '') + '</td>'
      + '<td class="td-dt">' + _esc(_fxDate(r.so_date)) + '</td>'
      + '<td style="text-align:right;font-variant-numeric:tabular-nums'
        + (_fxBasis === 'po' ? ';font-weight:600' : ';color:var(--t2)') + '"'
        + ' title="' + _esc(_fxValTitle(r)) + '">'
        + (r._client_val == null ? '<span style="color:var(--t3)">—</span>'
                                 : _esc(_fxMoney(r._client_val, 'USD')))
        // A dagger marks a value that also appears on another row, so it is
        // obvious the column cannot simply be added up.
        + ((r._shared_pos && r._shared_pos.length)
            ? ' <span style="color:var(--t3);cursor:help">†</span>' : '')
        + '</td>'
      + '<td style="text-align:right;font-variant-numeric:tabular-nums'
        + (_fxBasis === 'so' ? ';font-weight:600' : ';color:var(--t2)') + '">'
        + _esc(_fxMoney(r._val, 'USD')) + '</td>'
      + '<td style="text-align:right">' + (r.line_item_count || 0) + '</td>'
      + '<td style="white-space:nowrap">' + _fxDocLinks(r) + '</td>'
      + '</tr>';
  }).join('');

  if (foot) {
    foot.innerHTML =
      '<div class="pg-l">'
      + '<button class="pg-btn" onclick="fxPage(-1)"' + (_fxPage <= 1 ? ' disabled' : '') + '>← Prev</button>'
      + '<span style="font-size:12px;color:var(--t2)">Page <b>' + _fxPage + '</b> of ' + pages + '</span>'
      + '<button class="pg-btn" onclick="fxPage(1)"' + (_fxPage >= pages ? ' disabled' : '') + '>Next →</button>'
      + '</div>'
      // Deduped totals for whatever is currently filtered. Without this the
      // only way to total the client PO column is to add it up by hand, which
      // over-counts every PO that spans more than one sales order.
      + '<div class="pg-r" style="gap:1rem;align-items:baseline">'
      + '<span style="font-size:12px;color:var(--t3)">' + rows.length + ' sales orders</span>'
      + '<span style="font-size:12px;color:var(--t2)">Client PO <b style="font-variant-numeric:tabular-nums">'
        + _esc(_fxMoney(_fxDedupedClientTotal(rows), 'USD')) + '</b>'
        + '<span style="color:var(--t3)"> (each PO once)</span></span>'
      + '<span style="font-size:12px;color:var(--t2)">SO <b style="font-variant-numeric:tabular-nums">'
        + _esc(_fxMoney(rows.reduce(function(t, r){ return t + r._val; }, 0), 'USD')) + '</b></span>'
      + '</div>';
  }
}

function _fxDedupedClientTotal(rows) {
  var seen = {}, total = 0;
  rows.forEach(function(r) {
    _fxClientPos(r).forEach(function(n) {
      if (seen[n]) return;
      var v = FX_CLIENT_PO_VALUES[n];
      if (v == null) return;
      seen[n] = 1;
      total += v;
    });
  });
  return total;
}

function exportFlexitallicCsv() {
  // Same order as on screen, so a sorted view exports sorted.
  var rows = _fxSorted(_fxFiltered());
  if (!rows.length) return;
  // Two client PO columns on purpose. The first is what this sales order
  // relates to and repeats when a PO spans several SOs, so it reads correctly
  // per row but must not be summed. The second carries each PO's value once,
  // on its first appearance, so SUM() over that column is the real total.
  var hdr = ['SPM PO', 'Full reference', 'Customer', 'Customer group', 'Customer PO',
             'Flexitallic SO', 'SO date',
             'Client PO value (USD) - do not sum, repeats across rows',
             'Client PO value (USD) - counted once, safe to sum',
             'SPM PO value (USD)', 'SO value (USD)',
             'Original currency', 'Line items', 'PO PDF', 'SO PDF'];
  var lines = [hdr.map(_csvValue).join(',')];
  var csvSeen = {};
  rows.forEach(function(r) {
    var once = 0, gotOnce = false;
    _fxClientPos(r).forEach(function(n) {
      if (csvSeen[n]) return;
      var v = FX_CLIENT_PO_VALUES[n];
      if (v == null) return;
      csvSeen[n] = 1; once += v; gotOnce = true;
    });
    lines.push([r._po, r.spm_po_reference || '', r._cust_raw || r._cust, r._cust,
                _fxCustPo(r), r.so_number || '', String(r.so_date || '').slice(0, 10),
                r._client_val == null ? '' : r._client_val,
                gotOnce ? once : '',
                r._po_val == null ? '' : r._po_val, r._val, r.currency,
                r.line_item_count || 0, r.po_pdf_url || '', r.so_pdf_url || '']
               .map(_csvValue).join(','));
  });
  var blob = new Blob(['\ufeff' + lines.join('\r\n')], {type: 'text/csv;charset=utf-8;'});
  var a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'flexitallic_po_history.csv';
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  URL.revokeObjectURL(a.href);
}


// ══════════════════════════════════════════════════════════════════════════════
// PURCHASE ORDERS — the POs Special Piping raises on its own suppliers.
//
// Same shape as the quotes module above: a list, a form, a PDF. What is
// different is that nothing here is imported or parsed — the material comes
// from a different supplier almost every time, so every field is typed. The one
// piece of automation is the PO number, which is assembled from the client PO,
// the sequence and the vendor's initials exactly as the office writes it by
// hand: SPM- 0061484185-2-MD.
// ══════════════════════════════════════════════════════════════════════════════

var _spmPos       = [];
var _poPage       = 1;
var _PO_PER_PAGE  = 100;   // the same page size as orders, quotes and delays
var _editingPoId  = null;
var _poRows       = [];

var _PO_CURRENCY = { USD: '$', GBP: '£', EUR: '€', NGN: '₦' };

// Vendors SPM buys from regularly. The list on screen is these plus every
// vendor already used on a saved order, so it grows on its own and the details
// are picked rather than retyped — which is how the blueprint file ended up
// carrying a Flexitallic email on a MetalsDepot order.
var _PO_VENDOR_SEEDS = [
  { company:'Flexitallic',      address:'', email:'', name:'', role:'Sales' },
  { company:'MetalsDepot',      address:'4200 Revilo Rd, Winchester, KY 40391 USA', email:'', name:'', role:'Sales' },
  { company:'Rollstud Limited', address:'', email:'', name:'', role:'Sales' },
];

// ── Notices ──────────────────────────────────────────────────────────────
// Everything that can go wrong is said on the page. A browser alert covers
// the form, never names the box that is wrong, and vanishes as soon as it is
// dismissed — which is no help at all when the message is "your order was not
// saved and what is on screen is the only copy".
function clearPoNotice() {
  ['po-ok', 'po-err'].forEach(function(id){
    var el = document.getElementById(id);
    if (el) el.style.display = 'none';
  });
  document.querySelectorAll('.po-invalid').forEach(function(el){
    el.classList.remove('po-invalid');
  });
}

function _poNotice(kind, message) {
  clearPoNotice();
  var box = document.getElementById(kind === 'ok' ? 'po-ok' : 'po-err');
  var msg = document.getElementById(kind === 'ok' ? 'po-ok-msg' : 'po-err-msg');
  if (msg) msg.textContent = message;
  if (box) {
    box.style.display = 'flex';
    box.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  }
  if (kind === 'ok') setTimeout(function(){
    var el = document.getElementById('po-ok');
    if (el && el.style.display !== 'none') el.style.display = 'none';
  }, 4000);
}

// Marks the offending box and puts the cursor in it, so "which one?" never
// has to be worked out from the message alone.
function _poInvalid(fieldId, message) {
  _poNotice('err', message);
  var el = document.getElementById(fieldId);
  if (el) {
    el.classList.add('po-invalid');
    try { el.focus(); } catch (e) {}
  }
  return false;
}

// Everything that has to be true before an order can be saved, in the order
// someone reads the form. Returns true when the order is good to go.
function _poValidate() {
  if (!_gvPo('po-client')) {
    return _poInvalid('po-client', 'Choose the end client — the PO number is built from it, and a number is never reissued.');
  }
  if (_gvPo('po-client') === 'others' && !_gvPo('po-client-name')) {
    return _poInvalid('po-client-name', 'Type the customer\'s company name — its initials become the code in the PO number.');
  }
  if (!_gvPo('po-vendor-company')) {
    return _poInvalid('po-vendor-company', 'Enter the supplier company — the PO number ends with it.');
  }
  var priced = _poRows.filter(function(r){
    return (r.description || '').replace(/<[^>]*>/g, '').trim() || (parseFloat(r.line_total) || 0);
  });
  if (!priced.length) {
    _poNotice('err', 'Add at least one line item before saving — an order with no lines has nothing to send.');
    return false;
  }
  return true;
}

function _poSym() {
  var cur = (document.getElementById('po-currency') || {}).value || 'USD';
  return _PO_CURRENCY[cur] || '$';
}

function _fmtPo(n, sym) {
  return (sym || _poSym()) + (Number(n) || 0).toLocaleString('en-US', {minimumFractionDigits:2, maximumFractionDigits:2});
}

function _gvPo(id) {
  var el = document.getElementById(id);
  return el ? String(el.value || '').trim() : '';
}

function _setPo(id, val) {
  var el = document.getElementById(id);
  if (el) el.value = val;
}

// ── List ──────────────────────────────────────────────────────────────────────
function loadPurchaseOrders() {
  authFetch('/api/spm_pos')
    .then(function(r){
      return r.json().catch(function(){ return {}; }).then(function(body){
        if (!r.ok) throw new Error(body.detail || body.error || ('Server error ' + r.status));
        if (!Array.isArray(body)) throw new Error('Unexpected reply from the server.');
        return body;
      });
    })
    .then(function(rows){
      _spmPos = rows;
      _renderPoList();
    })
    .catch(function(e){
      console.error('loadPurchaseOrders', e);
      _poListMessage('Could not load purchase orders — ' + e.message
                     + ' If this page is new, the spm_pos tables may not exist yet.');
    });
}

// One place to put a message where the rows would be, so a failed load never
// leaves "Loading…" sitting there looking like it is still working.
function _poListMessage(text) {
  var el = document.getElementById('po-list-body');
  if (el) {
    el.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:2.5rem;color:var(--crit)">'
      + htmlEscape(text) + '</td></tr>';
  }
}

function filterPoList() {
  _poPage = 1;
  _renderPoList();
}

function _renderPoList() {
  var el = document.getElementById('po-list-body');
  if (!el) return;
  var input = document.getElementById('po-search');
  var term  = input ? input.value.trim().toLowerCase() : '';
  var list  = _spmPos;
  if (term) {
    list = list.filter(function(p) {
      return (p.po_number      || '').toLowerCase().indexOf(term) !== -1
          || (p.vendor_company || '').toLowerCase().indexOf(term) !== -1
          || (p.vendor_name    || '').toLowerCase().indexOf(term) !== -1
          || (p.client_po      || '').toLowerCase().indexOf(term) !== -1
          || (p.client         || '').toLowerCase().indexOf(term) !== -1
          || (p.status         || '').toLowerCase().indexOf(term) !== -1;
    });
  }
  if (_poPage > Math.max(1, Math.ceil(list.length / _PO_PER_PAGE))) _poPage = 1;
  var start    = (_poPage - 1) * _PO_PER_PAGE;
  var pageList = list.slice(start, start + _PO_PER_PAGE);

  if (!list.length) {
    el.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:2.5rem;color:var(--t3)">'
      + (term ? 'No purchase orders match your search.' : 'No purchase orders yet — click New PO to raise one.')
      + '</td></tr>';
    renderPgBar('po-pagination', 1, 0, _PO_PER_PAGE, 'po-pg-prev', 'po-pg-next', function(){}, function(){});
    return;
  }

  el.innerHTML = pageList.map(function(p) {
    var sym   = _PO_CURRENCY[(p.currency || 'USD').toUpperCase()] || '$';
    var total = (p.total != null) ? _fmtPo(p.total, sym) : '—';
    return '<tr>'
      + '<td class="mono" style="font-size:12px">' + htmlEscape(p.po_number || '—') + '</td>'
      + '<td>' + htmlEscape(p.vendor_company || p.vendor_name || '—') + '</td>'
      + '<td>' + htmlEscape((p.client || '').toUpperCase() || '—') + '</td>'
      + '<td class="mono">' + htmlEscape(p.client_po || '—') + '</td>'
      + '<td style="text-align:right;font-variant-numeric:tabular-nums">' + total + '</td>'
      + '<td>' + (p.po_date ? fmtTs(p.po_date) : '—') + '</td>'
      + '<td style="white-space:nowrap;text-align:right">'
      +   '<button class="act-btn" onclick="openPoForm(\'' + p.id + '\')">Edit</button> '
      +   '<button class="act-btn" onclick="downloadPoPdf(\'' + p.id + '\',\'' + htmlEscape(p.po_number || p.id) + '\')">PDF</button> '
      +   '<button class="act-btn act-del" onclick="confirmDeletePo(\'' + p.id + '\')">Del</button>'
      + '</td>'
      + '</tr>';
  }).join('');

  renderPgBar(
    'po-pagination', _poPage, list.length, _PO_PER_PAGE,
    'po-pg-prev', 'po-pg-next',
    function() { if (_poPage > 1) { _poPage--; _renderPoList(); } },
    function() { var pages = Math.max(1, Math.ceil(list.length / _PO_PER_PAGE)); if (_poPage < pages) { _poPage++; _renderPoList(); } }
  );
}

// ── Vendors ───────────────────────────────────────────────────────────────────
function _poVendors() {
  var out = {};
  _PO_VENDOR_SEEDS.forEach(function(v){
    if (v.company) out[v.company.toLowerCase()] = v;
  });
  // Saved orders come last so the most recent detail for a vendor wins over
  // the seed — an address that has been corrected once stays corrected.
  _spmPos.slice().reverse().forEach(function(p){
    if (!p.vendor_company) return;
    out[p.vendor_company.toLowerCase()] = {
      company:  p.vendor_company,
      address:  p.vendor_address  || '',
      email:    p.vendor_email    || '',
      name:     p.vendor_name     || '',
      role:     p.vendor_role     || '',
    };
  });
  return out;
}

function _fillPoVendorPicker(selected) {
  var sel = document.getElementById('po-vendor-pick');
  if (!sel) return;
  var vendors = _poVendors();
  var keys    = Object.keys(vendors).sort();
  sel.innerHTML = '<option value="">Select a vendor…</option>'
    + keys.map(function(k){
        return '<option value="' + htmlEscape(k) + '">' + htmlEscape(vendors[k].company) + '</option>';
      }).join('')
    + '<option value="__new">+ New supplier…</option>';
  sel.value = (selected || '').toLowerCase();
}

function applyPoVendor(key) {
  if (!key) return;
  if (key === '__new') {
    // A supplier SPM has not bought from before: clear the block rather than
    // leave the last vendor's details sitting under a new name, which is how
    // the blueprint file went out carrying the wrong contact email.
    ['po-vendor-company','po-vendor-name','po-vendor-address','po-vendor-email'].forEach(function(id){
      _setPo(id, '');
    });
    _setPo('po-vendor-role', 'Sales');
    var company = document.getElementById('po-vendor-company');
    if (company) { try { company.focus(); } catch (e) {} }
    renderPoNumber();
    return;
  }
  var v = _poVendors()[key];
  if (!v) return;
  _setPo('po-vendor-company',  v.company  || '');
  _setPo('po-vendor-address',  v.address  || '');
  _setPo('po-vendor-email',    v.email    || '');
  _setPo('po-vendor-name',     v.name     || '');
  _setPo('po-vendor-role',     v.role     || 'Sales');
  renderPoNumber();
}

// ── PO number ─────────────────────────────────────────────────────────────────
// The standard SPM number, built for you:
//
//     S.P.M.-C.N.L.-3101-0061451715-0061451710-0061448744-FLEXITALLIC
//
// Company · client code · running reference · every client PO on the order ·
// supplier. This mirrors _po_number() on the server so the field shows what
// will be saved, except for the reference itself: that is issued by the server
// at save time from the highest reference used anywhere, so the preview leaves
// it as #### rather than inventing one that may already be taken.
var _PO_CLIENT_CODES = {
  chevron: 'C.N.L.', nlng: 'NLNG', seplat: 'SEP',
  exxon: 'MPN', total: 'TEN', gca: 'GCA', others: 'OTH',
};

var _PO_CO_SUFFIX = /\b(LIMITED|LTD|INCORPORATED|INC|LLC|PLC|COMPANY|CO|CORP|CORPORATION|GMBH|BV|SA|NIG)\b\.?/gi;

var _poRef = null;      // the reference already issued to the order being edited

function _poSupplierSlug(company) {
  return String(company || '').replace(_PO_CO_SUFFIX, ' ').replace(/[^A-Za-z0-9]/g, '').toUpperCase();
}

// Chevron POs are ten digits beginning 006 and get written the short way on
// the table — '61484185' for a PO filed as '0061484185'. The number has to
// carry the form the client uses, so a bare 8- or 9-digit value is padded back
// to ten. Ten digits are already whole, and anything with a letter or a slash
// in it is somebody's own reference and is left alone. Mirrors _pad_client_po.
function _poPad(value) {
  var text = String(value == null ? '' : value).trim();
  if (!/^\d+$/.test(text) || text.length < 8 || text.length > 9) return text;
  while (text.length < 10) text = '0' + text;
  return text;
}

// Read off the P.O. No. column rather than a field of their own: one place to
// type them means the number can never disagree with the table under it.
function _poClientPoList() {
  var seen = {}, out = [];
  _poRows.forEach(function(row){
    var po = _poPad(row.client_po_no);
    if (po && !seen[po]) { seen[po] = 1; out.push(po); }
  });
  return out;
}

// "Others" is a customer who is not on the list and has no code of their own,
// so the name typed here supplies one — the same initials rule the quotations
// use, so both documents name the same customer the same way.
function onPoClientChange() {
  var isOthers = _gvPo('po-client') === 'others';
  var row = document.getElementById('po-client-name-row');
  if (row) row.style.display = isOthers ? '' : 'none';
  if (!isOthers) _setPo('po-client-name', '');
  clearPoNotice();
  renderPoNumber();
}

function _poInitials(name) {
  var words = String(name || '').replace(/[^A-Za-z\s]/g, '').split(/\s+/).filter(Boolean);
  if (!words.length) return 'OTH';
  return words.map(function(w){ return w.charAt(0).toUpperCase(); }).join('');
}

function _poCode() {
  var client = _gvPo('po-client');
  if (client === 'others') return _poInitials(_gvPo('po-client-name'));
  return _PO_CLIENT_CODES[client] || 'OTH';
}

function renderPoNumber(ref) {
  var hint = document.getElementById('po-client-code-hint');
  if (hint) hint.textContent = _poCode();
  var el = document.getElementById('po-number');
  if (!el) return;
  var parts = ['S.P.M.', _poCode(), ref || _poRef || '####']
    .concat(_poClientPoList());
  var slug = _poSupplierSlug(_gvPo('po-vendor-company'));
  if (slug) parts.push(slug);
  el.value = parts.join('-');
}

// ── Line items ────────────────────────────────────────────────────────────────
function _renderPoItems() {
  var body = document.getElementById('po-items-body');
  if (!body) return;
  body.innerHTML = _poRows.map(function(row, idx){
    return '<tr data-idx="' + idx + '">'
      + '<td class="mono" style="color:var(--t3);text-align:center">' + (idx+1) + '</td>'
      + '<td><input class="qf-inp qf-td-inp mono" type="text" placeholder="61484185" value="' + htmlEscape(row.client_po_no||'') + '" oninput="updatePoRow(' + idx + ',\'client_po_no\',this.value)"/></td>'
      + '<td><input class="qf-inp qf-td-inp mono" type="text" placeholder="1041744" value="' + htmlEscape(row.quote_ref||'') + '" oninput="updatePoRow(' + idx + ',\'quote_ref\',this.value)"/></td>'
      + '<td><div class="qf-inp qf-td-inp qf-desc-ce" contenteditable="true" data-rows="po" data-idx="' + idx + '" data-ph="Size, grade, spec…" oninput="_ceDescInput(this,' + idx + ')"></div></td>'
      + '<td><input class="qf-inp qf-td-inp" type="number" min="0" step="0.01" value="' + (row.quantity||'') + '" style="text-align:right" oninput="updatePoRow(' + idx + ',\'quantity\',parseFloat(this.value)||0)"/></td>'
      + '<td><input class="qf-inp qf-td-inp" type="number" min="0" step="0.01" value="' + (row.unit_price||'') + '" placeholder="0.00" style="text-align:right" oninput="updatePoRow(' + idx + ',\'unit_price\',parseFloat(this.value)||0)"/></td>'
      + '<td style="text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap" id="po-row-tot-' + idx + '">' + _fmtPo(row.line_total||0) + '</td>'
      + '<td style="text-align:center"><button class="qf-del-row" onclick="removePoRow(' + idx + ')" title="Remove">×</button></td>'
      + '</tr>';
  }).join('');
  // innerHTML, not string concatenation: the description carries markup and
  // would be escaped away if it were built into the row above.
  body.querySelectorAll('.qf-desc-ce').forEach(function(div){
    var i = parseInt(div.getAttribute('data-idx'));
    div.innerHTML = (_poRows[i] && _poRows[i].description) || '';
  });
}

function updatePoRow(idx, field, val) {
  if (!_poRows[idx]) return;
  _poRows[idx][field] = val;
  if (field === 'client_po_no') renderPoNumber();
  if (field === 'quantity' || field === 'unit_price') {
    var qty = parseFloat(_poRows[idx].quantity) || 0;
    var up  = parseFloat(_poRows[idx].unit_price) || 0;
    _poRows[idx].line_total = Math.round(qty * up * 100) / 100;
    var cell = document.getElementById('po-row-tot-' + idx);
    if (cell) cell.textContent = _fmtPo(_poRows[idx].line_total);
    recalcPoTotals();
  }
}

function addPoRow() {
  // A new line inherits the client PO from the one above it. Most orders sit
  // against a single client PO and retyping it on every line is where the
  // typos come from; the lines that genuinely differ are still editable.
  var last = _poRows.length ? _poRows[_poRows.length - 1] : null;
  _poRows.push({
    item_no: _poRows.length + 1,
    client_po_no: last ? (last.client_po_no || '') : '',
    quote_ref: '', description: '', quantity: 1, unit_price: 0, line_total: 0,
  });
  _renderPoItems();
  recalcPoTotals();
  renderPoNumber();
  var body = document.getElementById('po-items-body');
  if (body) { var el = body.lastElementChild; if (el) el.scrollIntoView({behavior:'smooth', block:'nearest'}); }
}

function removePoRow(idx) {
  _poRows.splice(idx, 1);
  _poRows.forEach(function(r, i){ r.item_no = i + 1; });
  _renderPoItems();
  recalcPoTotals();
  renderPoNumber();
}

function recalcPoTotals() {
  var sym = _poSym();
  var tot = _poRows.reduce(function(s, r){ return s + (parseFloat(r.line_total) || 0); }, 0);
  var set = function(id, v){ var e = document.getElementById(id); if (e) e.textContent = v; };
  set('po-total', _fmtPo(tot, sym));
  set('po-grand-lbl', 'TOTAL (' + sym + ')');
  set('po-line-count', String(_poRows.length));
  set('po-currency-lbl', (document.getElementById('po-currency') || {}).value || 'USD');
  var unit = document.getElementById('po-hdr-unit');
  var head = document.getElementById('po-hdr-total');
  if (unit) unit.textContent = 'Unit Price (' + sym + ')';
  if (head) head.textContent = 'Total (' + sym + ')';
  _poRows.forEach(function(r, i){
    var cell = document.getElementById('po-row-tot-' + i);
    if (cell) cell.textContent = _fmtPo(r.line_total || 0, sym);
  });
}

// ── Form ──────────────────────────────────────────────────────────────────────
function openPoForm(poId) {
  _editingPoId = poId;
  var title = document.getElementById('po-form-title');

  _poRef = null;
  clearPoNotice();
  _setPo('po-date',             new Date().toISOString().slice(0,10));
  _setPo('po-number',           '');
  _setPo('po-client',           '');
  _setPo('po-client-name',      '');
  var _nameRow = document.getElementById('po-client-name-row');
  if (_nameRow) _nameRow.style.display = 'none';
  _setPo('po-manager',          'FELIX AKARUE');
  _setPo('po-sales-person',     'IJALA STYLES ONOME');
  _setPo('po-vendor-name',      '');
  _setPo('po-vendor-role',      'Sales');
  _setPo('po-vendor-company',   '');
  _setPo('po-vendor-address',   '');
  _setPo('po-vendor-email',     '');
  _setPo('po-shipping-terms',   '');
  _setPo('po-shipping-method',  '');
  _setPo('po-delivery-date',    '');
  _setPo('po-deliver-to',       '');
  _setPo('po-currency',         'USD');
  _setPo('po-notes',            '');
  _poRows = [];
  _setPoPdfVisible(false);

  if (poId) {
    if (title) title.textContent = 'Edit Purchase Order';
    authFetch('/api/spm_pos/' + poId)
      .then(function(r){
        return r.json().catch(function(){ return {}; }).then(function(body){
          if (!r.ok) throw new Error(body.detail || body.error || ('Server error ' + r.status));
          return body;
        });
      })
      .then(function(p){
        if (p.error) { _poNotice('err', 'Could not load the order: ' + p.error); return; }
        _poRef = p.po_seq || null;
        _setPo('po-date',            (p.po_date || '').slice(0,10));
        _setPo('po-number',          p.po_number       || '');
        _setPo('po-client',          p.client          || '');
        _setPo('po-client-name',     p.client_name     || '');
        var nameRow = document.getElementById('po-client-name-row');
        if (nameRow) nameRow.style.display = (p.client === 'others') ? '' : 'none';
        _setPo('po-manager',         p.manager         || '');
        _setPo('po-sales-person',    p.sales_person    || '');
        _setPo('po-vendor-name',     p.vendor_name     || '');
        _setPo('po-vendor-role',     p.vendor_role     || '');
        _setPo('po-vendor-company',  p.vendor_company  || '');
        _setPo('po-vendor-address',  p.vendor_address  || '');
        _setPo('po-vendor-email',    p.vendor_email    || '');
        _setPo('po-shipping-terms',  p.shipping_terms  || '');
        _setPo('po-shipping-method', p.shipping_method || '');
        _setPo('po-delivery-date',   p.delivery_date   || '');
        _setPo('po-deliver-to',      p.deliver_to      || '');
        _setPo('po-currency',        (p.currency || 'USD').toUpperCase());
        _setPo('po-notes',           p.notes           || '');
        _fillPoVendorPicker(p.vendor_company || '');
        _poRows = (p.line_items || []).map(function(it){
          return { item_no: it.item_no, client_po_no: it.client_po_no || '', quote_ref: it.quote_ref || '',
                   description: it.description || '', quantity: it.quantity || 0,
                   unit_price: it.unit_price || 0, line_total: it.line_total || 0 };
        });
        _renderPoItems();
        recalcPoTotals();
        _setPoPdfVisible(true);
      })
      .catch(function(e){
        _poNotice('err', 'Could not load the order: ' + e.message + '. Go back and try again.');
      });
  } else {
    if (title) title.textContent = 'New Purchase Order';
    _fillPoVendorPicker('');
    _poRows = [{ item_no:1, client_po_no:'', quote_ref:'', description:'', quantity:1, unit_price:0, line_total:0 }];
    _renderPoItems();
    recalcPoTotals();
    renderPoNumber();
  }

  document.getElementById('po-list-view').style.display = 'none';
  document.getElementById('po-form-view').style.display = '';
}

function closePoForm() {
  _editingPoId = null;
  document.getElementById('po-list-view').style.display = '';
  document.getElementById('po-form-view').style.display = 'none';
}

function _setPoPdfVisible(show) {
  ['po-pdf-btn','po-pdf-btn2'].forEach(function(id){
    var el = document.getElementById(id);
    if (el) el.style.display = show ? '' : 'none';
  });
}

function _collectPoData() {
  var total = _poRows.reduce(function(s, r){ return s + (parseFloat(r.line_total) || 0); }, 0);
  return {
    po_date:         _gvPo('po-date'),
    client:          _gvPo('po-client'),
    client_name:     _gvPo('po-client-name'),
    manager:         _gvPo('po-manager'),
    sales_person:    _gvPo('po-sales-person'),
    vendor_name:     _gvPo('po-vendor-name'),
    vendor_role:     _gvPo('po-vendor-role'),
    vendor_company:  _gvPo('po-vendor-company'),
    vendor_address:  _gvPo('po-vendor-address'),
    vendor_email:    _gvPo('po-vendor-email'),
    deliver_to:      _gvPo('po-deliver-to'),
    shipping_terms:  _gvPo('po-shipping-terms'),
    shipping_method: _gvPo('po-shipping-method'),
    delivery_date:   _gvPo('po-delivery-date'),
    currency:        _gvPo('po-currency') || 'USD',
    notes:           _gvPo('po-notes'),
    total:           Math.round(total * 100) / 100,
    line_items: _poRows.map(function(r, i){
      return { item_no: i + 1, client_po_no: r.client_po_no || '', quote_ref: r.quote_ref || '',
               description: _ceClean(r.description || ''), quantity: parseFloat(r.quantity) || 0,
               unit_price: parseFloat(r.unit_price) || 0, line_total: parseFloat(r.line_total) || 0 };
    }),
  };
}

// Saving and downloading both go through here. Like the quotes, a failed save
// is read off the HTTP status and FastAPI's {detail:…} — never off resp.error,
// which a 500 does not carry — and the form is never cleared on failure,
// because what is on screen may be the only copy of the lines.
function _savePoRequest() {
  var isNew  = !_editingPoId;
  var url    = isNew ? '/api/spm_pos' : '/api/spm_pos/' + _editingPoId;
  var method = isNew ? 'POST' : 'PUT';
  return authFetch(url, {
    method: method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(_collectPoData()),
  }).then(function(r){
    return r.json().catch(function(){ return {}; }).then(function(body){
      if (!r.ok) throw new Error(body.detail || body.error || ('Server error ' + r.status));
      if (body.error) throw new Error(body.error);
      if (isNew && body.id) {
        _editingPoId = body.id;
        _setPoPdfVisible(true);
      }
      if (body.po_seq) _poRef = body.po_seq;
      if (body.po_number) _setPo('po-number', body.po_number);
      return body;
    });
  });
}

function savePo() {
  if (!_poValidate()) return;
  var btns = ['po-save-btn','po-save-btn2'].map(function(id){ return document.getElementById(id); }).filter(Boolean);
  btns.forEach(function(b){ b.disabled = true; b.textContent = 'Saving…'; });
  _savePoRequest()
    .then(function(body){
      loadPurchaseOrders();
      _poNotice('ok', 'Saved as ' + (body.po_number || _gvPo('po-number')) + '.');
      btns.forEach(function(b){
        b.textContent = 'Saved ✓';
        setTimeout(function(){ b.textContent = 'Save'; b.disabled = false; }, 2000);
      });
    })
    .catch(function(e){
      // The form is never cleared on a failure: what is on screen may be the
      // only copy of the line items.
      _poNotice('err', 'NOT saved — ' + e.message
                + ' Everything is still on screen; press Save again.');
      btns.forEach(function(b){ b.disabled = false; b.textContent = 'Save'; });
    });
}

function downloadCurrentPoPdf() {
  // The PDF is built server-side from the SAVED record, so it is saved first —
  // otherwise the file would be generated from the previous figures while the
  // screen shows the new ones.
  if (!_poValidate()) return;
  var btns = ['po-pdf-btn','po-pdf-btn2'].map(function(id){ return document.getElementById(id); }).filter(Boolean);
  var original = btns.length ? btns[0].textContent : '⬇ Download PDF';
  btns.forEach(function(b){ b.disabled = true; b.textContent = 'Saving…'; });
  _savePoRequest()
    .then(function(){
      btns.forEach(function(b){ b.textContent = 'Generating…'; });
      downloadPoPdf(_editingPoId, _gvPo('po-number') || _editingPoId);
      loadPurchaseOrders();
    })
    .catch(function(e){
      _poNotice('err', 'NOT saved, so no PDF was made — it would have been built from '
                + 'the previous version. ' + e.message
                + ' Your changes are still on screen; press Save, then try again.');
    })
    .finally(function(){
      btns.forEach(function(b){ b.disabled = false; b.textContent = original; });
    });
}

function downloadPoPdf(poId, label) {
  authFetch('/api/spm_pos/' + poId + '/pdf')
    .then(function(res){
      if (!res.ok) {
        res.json()
          .then(function(d){ _poNotice('err', 'PDF failed: ' + (d.detail || d.error || res.status)); })
          .catch(function(){ _poNotice('err', 'PDF generation failed.'); });
        return null;
      }
      return res.blob();
    })
    .then(function(blob){
      if (!blob) return;
      var url = URL.createObjectURL(blob);
      var a   = document.createElement('a');
      a.href  = url;
      var safe = String(label || poId).replace(/[^A-Za-z0-9\s._-]/g, '').trim().replace(/\s+/g, '_');
      a.download = (safe || poId) + '.pdf';
      document.body.appendChild(a);
      a.click();
      setTimeout(function(){ URL.revokeObjectURL(url); if (a.parentNode) a.parentNode.removeChild(a); }, 1000);
    })
    .catch(function(e){ _poNotice('err', 'PDF download failed: ' + e.message); });
}

var _deletePoId = null;

function confirmDeletePo(poId) {
  _deletePoId = poId;
  var po = _spmPos.filter(function(p){ return p.id === poId; })[0];
  var numEl = document.getElementById('dp-po-num');
  if (numEl) numEl.textContent = (po && po.po_number) || 'this purchase order';
  var errEl = document.getElementById('dp-error');
  if (errEl) errEl.style.display = 'none';
  var btn = document.getElementById('dp-confirm-btn');
  if (btn) { btn.disabled = false; btn.textContent = 'Delete'; }
  var modal = document.getElementById('delete-po-modal');
  if (modal) modal.classList.remove('hidden');
}

function closeDeletePoModal() {
  _deletePoId = null;
  var modal = document.getElementById('delete-po-modal');
  if (modal) modal.classList.add('hidden');
}

function doDeletePo() {
  if (!_deletePoId) return;
  var poId = _deletePoId;
  var btn  = document.getElementById('dp-confirm-btn');
  var err  = document.getElementById('dp-error');
  if (btn) { btn.disabled = true; btn.textContent = 'Deleting…'; }
  authFetch('/api/spm_pos/' + poId, { method:'DELETE' })
    .then(function(r){
      return r.json().catch(function(){ return {}; }).then(function(body){
        if (!r.ok) throw new Error(body.detail || body.error || ('Server error ' + r.status));
        return body;
      });
    })
    .then(function(){
      closeDeletePoModal();
      loadPurchaseOrders();
    })
    .catch(function(e){
      // Reported inside the modal, next to the button that was pressed.
      if (btn) { btn.disabled = false; btn.textContent = 'Delete'; }
      if (err) { err.textContent = 'Delete failed: ' + e.message; err.style.display = ''; }
    });
}
