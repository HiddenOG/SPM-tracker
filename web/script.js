// ── Auth ──────────────────────────────────────────────────────────────────────
var _authHeader  = localStorage.getItem('spm_auth') || '';
var _currentUser = null;
try { _currentUser = JSON.parse(localStorage.getItem('spm_user') || 'null'); } catch(e) {}

function authFetch(url, opts) {
  opts = opts || {};
  opts.headers = Object.assign({}, opts.headers, _authHeader ? {Authorization: _authHeader} : {});
  return fetch(url, opts);
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
  closed:                               {lbl:'Closed',                   cls:'sp-ok'}
};

var CLOSED_STATUSES = new Set(['delivered','waybill_received','invoiced','paid','closed']);
var DISPATCH_STAGES = ['dispatch_packed_awaiting_instruction','dispatch_instruction_sent','so_sent_to_warehouse','ready_for_dispatch','dispatched'];

// Pipeline order (must match STATUS_RANK in sync.py)
var PIPELINE_ORDER = [
  'new','pending_acknowledgment','acknowledged',
  'awaiting_warehouse_stock_check','stock_check_needs_review','stock_check_complete',
  'pricing','po_sent','awaiting_supplier_so','supplier_acknowledged',
  'dispatch_packed_awaiting_instruction','dispatch_instruction_sent',
  'so_sent_to_warehouse','ready_for_dispatch','dispatched','delivery_requested',
  'delivered','waybill_received','invoiced','paid','closed'
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
  {key:'delivered',                            label:'Delivered'}
];

// ── Client switcher ───────────────────────────────────────────────────────────
var _activeClient = 'chevron'; // 'chevron' | 'nlng' | 'seplat'

// ── NLNG data + state ─────────────────────────────────────────────────────────
var NLNG_ORDERS     = [];
var _nlngActiveFilter = 'all';
var _nlngSubFilter    = 'all';
var _nlngPeriodFilter = 'all';
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

// ── Per-stage filter function ─────────────────────────────────────────────────
function applyStageFilter(o, stageKey, sub) {
  var now = new Date();
  var s = sub || 'all';

  if (stageKey === 'all') return true;

  if (stageKey === 'stock_check') {
    if (!o.sent_to_warehouse_at) return false;
    return !o.stock_check_completed_at;
  }

  if (stageKey === 'not_in_stock') {
    // Strictly none in stock — partial stock is separate context shown in status column
    return isNotInStock(o);
  }

  if (stageKey === 'sales_order') {
    if (!o.spm_po_sent_at) return false;
    if (s === 'awaiting')  return !o.so_received_at;
    if (s === 'received')  return !!o.so_received_at;
    return true;
  }

  if (stageKey === 'shipping') {
    if (!o.ready_for_dispatch_at) return false;
    if (s === 'to_co')       return !o.dispatched_at;
    if (s === 'confirmed')   return !!o.dispatched_at;
    if (s === 'delivery_req')return !!o.delivery_requested_at;
    return true;
  }

  if (stageKey === 'live')    return !CLOSED_STATUSES.has(o.overall_status) && !o.delivered_at;
  if (stageKey === 'closed')  return CLOSED_STATUSES.has(o.overall_status) || !!o.delivered_at;

  if (stageKey === 'delivered') {
    return !!o.delivered_at;
  }

  return true;
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
    checkOTDAlerts();
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
  _pollTimer = setInterval(loadOrders, POLL_MS);
}

function toggleFunnelLabel(el) { el.classList.toggle('tapped'); }

// ── Role-based access ─────────────────────────────────────────────────────────
var ROLE_PAGES = {
  admin:       ['dashboard','orders','suppliers','delays','reports','settings','team','messages'],
  procurement: ['dashboard','orders','suppliers','delays','reports','messages'],
  expeditor:   ['dashboard','orders','delays','messages'],
  warehouse:   ['dashboard','orders','messages'],
  accounts:    ['dashboard','reports','messages'],
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
    var color    = _avColor(u.email);
    var isMe     = _currentUser && _currentUser.email === u.email;
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
  try {
    // Send one message per selected role
    await Promise.all(roles.map(function(role) {
      return authFetch('/api/messages', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({
          to_role:      role,
          subject:      subject,
          body:         body,
          message_type: _composeOrderId ? 'availability_check' : 'general',
          order_id:     _composeOrderId,
          order_client: _composeOrderClient,
          po_pdf_url:   _composePdfUrl || undefined,
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
    btn.disabled = false; btn.textContent = 'Send';
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

function openAddUserModal() {
  ['au-name','au-email','au-pw'].forEach(function(id){ document.getElementById(id).value = ''; });
  document.getElementById('au-role').value = '';
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
  var email = document.getElementById('au-email').value.trim();
  var role  = document.getElementById('au-role').value;
  var pw    = document.getElementById('au-pw').value;
  var errEl = document.getElementById('au-error');
  var btn   = document.getElementById('au-submit-btn');
  errEl.style.display = 'none';
  if (!email || !role || !pw) {
    errEl.textContent = 'Email, role, and password are required.';
    errEl.style.display = 'block';
    return;
  }
  btn.disabled = true; btn.textContent = 'Creating…';
  try {
    var res = await authFetch('/api/users', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({full_name: name, email: email, role: role, password: pw}),
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
  return _ngnRate || 1600; // fallback to approximate rate if API fails
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
  updateReports();
}

// ── Reports page ─────────────────────────────────────────────────────────────
function switchReport(client) {
  document.getElementById('rpt-chevron').style.display = client === 'chevron' ? '' : 'none';
  document.getElementById('rpt-nlng').style.display    = client === 'nlng'    ? '' : 'none';
  document.getElementById('rpt-btn-chevron').classList.toggle('on', client === 'chevron');
  document.getElementById('rpt-btn-nlng').classList.toggle('on',    client === 'nlng');
  updateReports();
}

function updateReports() {
  var chActive    = ORDERS.filter(function(o){ return !CLOSED_STATUSES.has(o.overall_status); });
  var chDelivered = ORDERS.filter(function(o){ return CLOSED_STATUSES.has(o.overall_status); });
  var chSpm       = ORDERS.filter(function(o){ return o.spm_po_sent_at; });
  _set('rpt-ch-total',     ORDERS.length);
  _set('rpt-ch-active',    chActive.length);
  _set('rpt-ch-delivered', chDelivered.length);
  _set('rpt-ch-spm',       chSpm.length);

  var nlngActive    = NLNG_ORDERS.filter(function(o){ return o.overall_status !== 'delivered' && !o.delivered_at; });
  var nlngDelivered = NLNG_ORDERS.filter(function(o){ return o.overall_status === 'delivered' || !!o.delivered_at; });
  var nlngSpm       = NLNG_ORDERS.filter(function(o){ return o.spm_po_sent_at; });
  _set('rpt-nl-total',     NLNG_ORDERS.length);
  _set('rpt-nl-active',    nlngActive.length);
  _set('rpt-nl-delivered', nlngDelivered.length);
  _set('rpt-nl-spm',       nlngSpm.length);
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
  buildOrdersPeriodChips(); // rebuild period row whenever status chips rebuild
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
var _activePeriodFilter = 'all'; // 'all' | 'rcv_24h' | 'rcv_week' | 'rcv_2wk' | 'rcv_month' | 'rcv_YYYY-MM'
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

function buildOrdersPeriodChips() {
  var menu = document.getElementById('orders-period-menu');
  var btn  = document.getElementById('orders-period-btn');
  if (!menu) return;
  menu.innerHTML = _periodItems().map(function(c) {
    return '<div class="period-dd-item' + (_activePeriodFilter === c.key ? ' on' : '')
      + '" onclick="setOrdersPeriod(\'' + c.key + '\')">' + c.label + '</div>';
  }).join('');
  if (btn) btn.textContent = _periodLabel(_activePeriodFilter) + ' ▾';
}

function toggleOrdersPeriodDd() {
  var menu = document.getElementById('orders-period-menu');
  if (menu) menu.classList.toggle('hidden');
}

function setOrdersPeriod(f) {
  _activePeriodFilter = f;
  var menu = document.getElementById('orders-period-menu');
  if (menu) menu.classList.add('hidden');
  buildOrdersPeriodChips();
  filterOrders();
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
  buildNlngPeriodChips();
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

function buildNlngPeriodChips() {
  var menu = document.getElementById('nlng-period-menu');
  var btn  = document.getElementById('nlng-period-btn');
  if (!menu) return;
  menu.innerHTML = _periodItems(NLNG_ORDERS).map(function(c) {
    return '<div class="period-dd-item' + (_nlngPeriodFilter === c.key ? ' on' : '')
      + '" onclick="setNlngPeriod(\'' + c.key + '\')">' + c.label + '</div>';
  }).join('');
  if (btn) btn.textContent = _periodLabel(_nlngPeriodFilter, NLNG_ORDERS) + ' ▾';
}

function toggleNlngPeriodDd() {
  var menu = document.getElementById('nlng-period-menu');
  if (menu) menu.classList.toggle('hidden');
}

function setNlngPeriod(f) {
  _nlngPeriodFilter = f;
  var menu = document.getElementById('nlng-period-menu');
  if (menu) menu.classList.add('hidden');
  buildNlngPeriodChips();
  filterNlng();
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
    var mf = (_nlngActiveFilter === 'all') ? true : applyNlngStageFilter(o, _nlngActiveFilter);
    return mq && mf && applyPeriodFilter(o, _nlngPeriodFilter);
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
      + '<button class="nc-notes-btn" id="nctrig-' + _noid + '" title="Team notes" onclick="ncOpenNotes(\'' + _noid + '\',\'nlng\')">'
      + '<svg class="nc-chv" width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>'
      + '<span class="nc-badge nc-badge-zero" id="ncbadge-' + _noid + '">0</span>'
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
        var isDone = o.overall_status === 'delivered' || !!o.delivered_at;
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
    html += '<tr class="nc-thread-row nc-closed" id="ncthread-' + _noid + '" data-type="nlng" data-loaded="0">'
      + '<td colspan="15"><div class="nc-thread-panel">'
      + '<span class="nc-note-label">Team Notes — ' + _esc(o.po_number || '') + '</span>'
      + '<div class="nc-limit-warn" id="ncwarn-' + _noid + '">Max 20 notes reached — delete one to add a new note</div>'
      + '<div class="nc-thread-list" id="nclist-' + _noid + '"><div class="nc-empty-cmnt">Loading…</div></div>'
      + '<div class="nc-thread-divider"></div>'
      + '<div class="nc-compose-row">'
      + '<div class="nc-av nc-av-blue">' + _ncInitials() + '</div>'
      + '<div class="nc-compose-right">'
      + '<textarea class="nc-compose-input" id="ncta-' + _noid + '" placeholder="Add a note…" rows="1"'
      + ' oninput="ncAutoResize(this);ncUpdatePost(\'' + _noid + '\')"'
      + ' onkeydown="if(event.key===\'Enter\'&&!event.shiftKey){event.preventDefault();ncPost(\'' + _noid + '\',\'nlng\')}"></textarea>'
      + '<div class="nc-compose-actions">'
      + '<button class="nc-post-btn" id="ncpost-' + _noid + '" disabled onclick="ncPost(\'' + _noid + '\',\'nlng\')">Post</button>'
      + '</div></div></div>'
      + '</div></td></tr>';
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
    return mq && mf && applyPeriodFilter(o, _activePeriodFilter);
  });

  if (_sortCol) {
    rows = rows.slice().sort(function(a, b) {
      var av = a[_sortCol]; var bv = b[_sortCol];
      if (av == null) av = ''; if (bv == null) bv = '';
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
      + '<button class="nc-notes-btn" id="nctrig-' + _oid + '" title="Team notes" onclick="ncOpenNotes(\'' + _oid + '\',\'chevron\')">'
      + '<svg class="nc-chv" width="9" height="9" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="6 9 12 15 18 9"/></svg>'
      + '<span class="nc-badge nc-badge-zero" id="ncbadge-' + _oid + '">0</span>'
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
        var isDone = o.overall_status === 'delivered' || !!o.delivered_at;
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
    html += '<tr class="nc-thread-row nc-closed" id="ncthread-' + _oid + '" data-type="chevron" data-loaded="0">'
      + '<td colspan="15"><div class="nc-thread-panel">'
      + '<span class="nc-note-label">Team Notes — ' + _esc(po) + '</span>'
      + '<div class="nc-limit-warn" id="ncwarn-' + _oid + '">Max 20 notes reached — delete one to add a new note</div>'
      + '<div class="nc-thread-list" id="nclist-' + _oid + '"><div class="nc-empty-cmnt">Loading…</div></div>'
      + '<div class="nc-thread-divider"></div>'
      + '<div class="nc-compose-row">'
      + '<div class="nc-av nc-av-blue">' + _ncInitials() + '</div>'
      + '<div class="nc-compose-right">'
      + '<textarea class="nc-compose-input" id="ncta-' + _oid + '" placeholder="Add a note…" rows="1"'
      + ' oninput="ncAutoResize(this);ncUpdatePost(\'' + _oid + '\')"'
      + ' onkeydown="if(event.key===\'Enter\'&&!event.shiftKey){event.preventDefault();ncPost(\'' + _oid + '\',\'chevron\')}"></textarea>'
      + '<div class="nc-compose-actions">'
      + '<button class="nc-post-btn" id="ncpost-' + _oid + '" disabled onclick="ncPost(\'' + _oid + '\',\'chevron\')">Post</button>'
      + '</div></div></div>'
      + '</div></td></tr>';
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
        v = o[k] != null ? String(o[k]) : '';
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
        v = (o.overall_status === 'delivered' || !!o.delivered_at) ? 'Closed' : 'Live';
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
        v = o[col.key] != null ? String(o[col.key]) : '';
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
function openStoryDrawer(orderId, orderType) {
  var type = orderType || 'chevron';
  var order = (type === 'nlng' ? NLNG_ORDERS : ORDERS).find(function(o){ return o.id === orderId; });
  var poNum = (order && (order.buyer_po_number || order.po_number)) ? (order.buyer_po_number || order.po_number) : orderId;
  document.getElementById('story-po-num').textContent = poNum;
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
  document.getElementById('story-scroll').innerHTML = '<div class="se-loading">Scanning Gmail for emails…</div>';
  document.getElementById('story-ai-card').style.display = 'none';
  document.getElementById('ai-msgs').innerHTML = '';
  document.getElementById('ai-input').value = '';
  document.getElementById('ai-input').disabled = true;
  document.getElementById('ai-send').disabled  = true;
  document.getElementById('story-overlay').classList.add('show');
  document.getElementById('story-drawer').classList.add('open');
  document.body.style.overflow = 'hidden';
  loadStoryEmails(orderId, type);
}

function closeStoryDrawer() {
  document.getElementById('story-overlay').classList.remove('show');
  document.getElementById('story-drawer').classList.remove('open');
  document.body.style.overflow = '';
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
  return _ncActiveOid !== null || !!document.querySelector('.nc-thread-row:not(.nc-closed)');
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
    + '<button class="nc-act-item" onclick="ncOpenNotes(\'' + oid + '\',\'' + type + '\')">'
    + _ncNoteSvg + '<span class="nc-act-item-label">Team Notes</span>'
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
            try { full += (JSON.parse(d).c || ''); } catch(e) {}
            if (el) el.textContent = full;
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
      var isDone = order.overall_status === 'delivered' || !!order.delivered_at;
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
  if (sw) sw.style.display = (name === 'dashboard') ? 'none' : '';
  if (name === 'orders') { if (_activeClient === 'nlng') filterNlng(); else filterOrders(); }
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
  document.getElementById('screen-login').style.display = 'none';
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
  loadNlngOrders();   // pre-load NLNG so dashboard combined stats are accurate from the start
  loadUnreadCount();
  startPolling();
  _initSW();
}

document.getElementById('btn-in').addEventListener('click', async function() {
  var email    = document.getElementById('em').value.trim();
  var password = document.getElementById('pw').value;
  var errEl    = document.querySelector('.login-foot');
  var btn      = this;
  btn.innerHTML = 'Signing in&hellip;';
  btn.disabled  = true;
  try {
    var res  = await fetch('/api/auth/login', {
      method:  'POST',
      headers: {'Content-Type': 'application/json'},
      body:    JSON.stringify({email: email, password: password}),
    });
    var data = await res.json();
    if (!res.ok) {
      errEl.textContent  = data.error || 'Invalid email or password.';
      errEl.style.color  = 'var(--crit)';
      btn.innerHTML      = 'Sign in <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>';
      btn.disabled       = false;
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
    btn.innerHTML     = 'Sign in <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.25" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M5 12h14M13 6l6 6-6 6"/></svg>';
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
  _authHeader  = '';
  _currentUser = null;
  location.reload();
}

// Auto-launch if a valid token is already stored
if (_authHeader && _currentUser) {
  _launchApp();
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

// Earliest promised_date across line items (from the Chevron PO PDF).
function getPoPromisedDate(o) {
  var lids = (o.order_line_items || []).filter(function(li) { return li.promised_date; });
  if (!lids.length) return null;
  return lids.reduce(function(min, li) { return li.promised_date < min ? li.promised_date : min; }, lids[0].promised_date);
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

function checkOTDAlerts() {
  if (!ORDERS || !ORDERS.length) return;
  var critical = [], atRisk = [];
  ORDERS.forEach(function(o) {
    var cls = otdClass(o);
    if (cls === 'critical' || cls === 'overdue') critical.push(o);
    else if (cls === 'at-risk') atRisk.push(o);
  });

  var total = critical.length + atRisk.length;
  var pip   = document.getElementById('notif-pip');
  if (pip) pip.style.display = total > 0 ? 'block' : 'none';

  if (critical.length > 0 && 'Notification' in window) {
    if (Notification.permission === 'default') {
      Notification.requestPermission(); // ask once; will fire next poll cycle if granted
    } else if (Notification.permission === 'granted') {
      // Store one key per PO so resolving one order doesn't re-fire notifications for others
      var newCrit = critical.filter(function(o) {
        return !localStorage.getItem('otd_notif_' + o.buyer_po_number);
      });
      if (newCrit.length > 0) {
        try {
          new Notification('SPMprocure360 — Action Required', {
            body: newCrit.length + ' order' + (newCrit.length > 1 ? 's' : '') + ' overdue or critical — delivery deadline at risk.',
            tag: 'otd-critical'
          });
        } catch(e) { /* mobile requires ServiceWorker for push — skip silently */ }
        newCrit.forEach(function(o) {
          localStorage.setItem('otd_notif_' + o.buyer_po_number, '1');
        });
      }
    }
  }

  var bodyEl = document.getElementById('notif-body');
  if (!bodyEl) return;
  var html = '';
  if (critical.length === 0 && atRisk.length === 0) {
    html = '<div class="notif-empty">No alerts — all orders on track.</div>';
  } else {
    critical.forEach(function(o) {
      var dl  = otdDaysLeft(o);
      var sub = dl !== null ? (dl < 0 ? 'Overdue by ' + Math.abs(dl) + ' days' : dl + ' days left — CRITICAL') : 'Past deadline';
      html += '<div class="notif-item crit" onclick="closeNotifPanel();showPage(\'delays\')">'
            + '<div class="ni-ttl">' + (o.buyer_po_number || '—') + '</div>'
            + '<div class="ni-sub">' + sub + (o.buyer_name ? ' · ' + o.buyer_name : '') + '</div></div>';
    });
    atRisk.forEach(function(o) {
      var dl  = otdDaysLeft(o);
      html += '<div class="notif-item warn" onclick="closeNotifPanel();showPage(\'delays\')">'
            + '<div class="ni-ttl">' + (o.buyer_po_number || '—') + '</div>'
            + '<div class="ni-sub">' + (dl !== null ? dl + ' days remaining' : '') + (o.buyer_name ? ' · ' + o.buyer_name : '') + '</div></div>';
    });
  }
  bodyEl.innerHTML = html;
}

var _notifPanelOpen = false;

function toggleNotifPanel() {
  _notifPanelOpen = !_notifPanelOpen;
  var panel = document.getElementById('notif-panel');
  if (panel) panel.classList.toggle('show', _notifPanelOpen);
}

function closeNotifPanel() {
  _notifPanelOpen = false;
  var panel = document.getElementById('notif-panel');
  if (panel) panel.classList.remove('show');
}

// ── Service Worker + Background Notifications ─────────────────────────────────

var _sw = null;
var _swPingTimer = null;

function _initSW() {
  if (!('serviceWorker' in navigator)) return;
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
    // Listen for sound messages from SW
    navigator.serviceWorker.addEventListener('message', function(e) {
      if (!e.data) return;
      if (e.data.type === 'SPM_ALERT_SOUND') _playAlertSound(e.data.level);
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
function _playAlertSound(level) {
  try {
    if (!_audioCtx) _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    var ctx = _audioCtx;
    var isCrit = level === 'critical';
    // Two short beeps for info, three urgent ones for critical
    var beats = isCrit ? [0, 0.18, 0.36] : [0, 0.22];
    beats.forEach(function(t) {
      var osc  = ctx.createOscillator();
      var gain = ctx.createGain();
      osc.connect(gain);
      gain.connect(ctx.destination);
      osc.frequency.value = isCrit ? 880 : 660;
      osc.type = 'sine';
      gain.gain.setValueAtTime(0.001, ctx.currentTime + t);
      gain.gain.exponentialRampToValueAtTime(isCrit ? 0.6 : 0.4, ctx.currentTime + t + 0.02);
      gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + t + 0.14);
      osc.start(ctx.currentTime + t);
      osc.stop(ctx.currentTime + t + 0.15);
    });
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
  if (!e.target.closest('#client-sw-dd')) {
    var cm = document.getElementById('client-sw-menu');
    if (cm) cm.classList.add('hidden');
  }
});

