// ── Reports page (extracted from script.js) ───────────────────────────────────

function switchReport(client) {
  document.getElementById('rpt-chevron').style.display = client === 'chevron' ? '' : 'none';
  document.getElementById('rpt-nlng').style.display    = client === 'nlng'    ? '' : 'none';
  document.getElementById('rpt-btn-chevron').classList.toggle('on', client === 'chevron');
  document.getElementById('rpt-btn-nlng').classList.toggle('on',    client === 'nlng');
  updateReports();
}

// ── State ─────────────────────────────────────────────────────────────────────
var _rptChBarMode = 'received';
var _rptNlBarMode = 'received';
var _rptChBarYear = new Date().getFullYear();
var _rptNlBarYear = new Date().getFullYear();

// ── CSS var helper ────────────────────────────────────────────────────────────
function _rptCssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// ── Bar toggle controls ───────────────────────────────────────────────────────
function _rptBarToggle(client, mode) {
  if (client === 'ch') _rptChBarMode = mode; else _rptNlBarMode = mode;
  _rptRedrawBars(client);
}

function _rptBarYearChange(client, year) {
  if (client === 'ch') _rptChBarYear = +year; else _rptNlBarYear = +year;
  _rptRedrawBars(client);
}

function _rptRedrawBars(client) {
  var isCh       = client === 'ch';
  var orders     = isCh ? ORDERS : NLNG_ORDERS;
  var mode       = isCh ? _rptChBarMode : _rptNlBarMode;
  var year       = isCh ? _rptChBarYear : _rptNlBarYear;
  var color      = isCh ? _rptCssVar('--accent') : _rptCssVar('--ch1');
  var field      = mode === 'received' ? 'notification_received_at' : 'required_delivery_date';
  var tipLbl     = mode === 'received' ? 'POs received' : 'POs due';
  var months     = _rptMonthBuckets(orders, field, mode === 'due' ? year : null);
  var delMonths  = mode === 'received' ? _rptMonthBuckets(orders, 'delivered_at') : null;
  _rptDrawBars('rpt-bars-' + client, months, color, tipLbl, delMonths);

  ['recv', 'due'].forEach(function(m) {
    var isOn = (m === 'recv') === (mode === 'received');
    var btn  = document.getElementById('rpt-' + client + '-btn-' + m);
    if (!btn) return;
    btn.classList.toggle('rpt-btn-on', isOn);
    btn.style.background  = isOn ? color : '';
    btn.style.borderColor = isOn ? color : '';
    btn.style.color       = isOn ? '#fff' : '';
  });

  var sel = document.getElementById('rpt-' + client + '-yr-sel');
  if (!sel) return;
  sel.style.display = mode === 'due' ? '' : 'none';
  if (mode === 'due') {
    var yearsSet = {};
    yearsSet[new Date().getFullYear()] = true;
    orders.forEach(function(o) {
      var raw = o['required_delivery_date']; if (!raw) return;
      var d = new Date(raw); if (isNaN(d)) return;
      yearsSet[d.getFullYear()] = true;
    });
    var years = Object.keys(yearsSet).map(Number).sort();
    sel.innerHTML = years.map(function(y) {
      return '<option value="' + y + '"' + (y === year ? ' selected' : '') + '>' + y + '</option>';
    }).join('');
  }
}

// ── Month bucketing ───────────────────────────────────────────────────────────
function _rptMonthBuckets(orders, dateField, year) {
  var now = new Date();
  var months = [];
  if (year) {
    for (var i = 0; i < 12; i++) {
      months.push({ y: year, m: i, lbl: new Date(year, i, 1).toLocaleString('en', { month: 'short' }), v: 0 });
    }
  } else {
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

// ── OTD score (by line items) ─────────────────────────────────────────────────
function _rptOtd(delivered, liField) {
  var liOtd = 0, liLate = 0, totalDays = 0, daysN = 0, lateDays = 0, lateN = 0, minDays = Infinity;
  delivered.forEach(function(o) {
    var cls   = otdClass(o);
    var n     = (o[liField || 'order_line_items'] || []).length || 1;
    var del   = o.delivered_at             ? new Date(o.delivered_at)             : null;
    var start = o.notification_received_at ? new Date(o.notification_received_at) : null;
    if (del && start && !isNaN(del) && !isNaN(start)) {
      var days = Math.round((del - start) / 86400000);
      if (days > 0) { totalDays += days; daysN++; if (days < minDays) minDays = days; }
    }
    if (cls === 'del-otd') {
      liOtd += n;
    } else if (cls === 'del-late') {
      liLate += n;
      var rdd = o.required_delivery_date ? new Date(o.required_delivery_date) : null;
      if (del && rdd && !isNaN(del) && !isNaN(rdd)) { lateDays += Math.round((del - rdd) / 86400000); lateN++; }
    }
  });
  var liTotal = liOtd + liLate;
  return {
    onTime: liOtd, late: liLate, total: liTotal,
    score:       liTotal > 0 ? Math.round(liOtd / liTotal * 100) : null,
    avgDays:     daysN   > 0 ? Math.round(totalDays / daysN)     : null,
    avgLateDays: lateN   > 0 ? Math.round(lateDays / lateN)      : null,
    minDays:     minDays < Infinity ? minDays : null
  };
}

// ── OTD by month (trend) ──────────────────────────────────────────────────────
function _rptOtdByMonth(orders, liField, mode) {
  var now = new Date();
  var months = [];
  for (var i = 11; i >= 0; i--) {
    var d = new Date(now.getFullYear(), now.getMonth() - i, 1);
    months.push({ y: d.getFullYear(), m: d.getMonth(), lbl: d.toLocaleString('en', { month: 'short' }), liOtd: 0, liLate: 0 });
  }
  orders.forEach(function(o) {
    if (!o.delivered_at) return;
    var cls = otdClass(o);
    if (cls !== 'del-otd' && cls !== 'del-late') return;
    var groupDate;
    if (mode === 'due') {
      var dueDate = getOtdDate(o);
      if (!dueDate) return;
      groupDate = new Date(dueDate);
    } else {
      groupDate = new Date(o.delivered_at);
    }
    if (isNaN(groupDate)) return;
    var n = (o[liField] || []).length || 1;
    for (var j = 0; j < months.length; j++) {
      if (groupDate.getFullYear() === months[j].y && groupDate.getMonth() === months[j].m) {
        if (cls === 'del-otd') months[j].liOtd += n; else months[j].liLate += n;
        break;
      }
    }
  });
  return months.map(function(m) {
    var total = m.liOtd + m.liLate;
    return { lbl: m.lbl, score: total > 0 ? Math.round(m.liOtd / total * 100) : null, total: total };
  });
}

// ── Product category matcher ──────────────────────────────────────────────────
// ORDER MATTERS: first match wins. Specific subtypes before generic catch-alls.
var _RPT_CATS = [
  { name: 'Spiral Wound',       keys: ['SPIRAL', 'SPW', 'SPWD', 'SW GASKET'] },
  { name: 'Ring Joint',         keys: ['RING JOINT', 'RING-JOINT', 'RTJ', 'APIR'] },
  { name: 'Kammprofile',        keys: ['KAMMPROFILE', 'KAMM', 'GROOVED', 'CORRUGATED'] },
  { name: 'Heat Exchanger',     keys: ['HEAT EXCHANGER', 'H/E SEAL', 'HX SEAL'] },
  { name: 'Lens Ring',          keys: ['LENS RING', 'LENS GASKET'] },
  { name: 'Soft Iron',          keys: ['SOFT IRON'] },
  // General gasket/seal before Structural — prevents sheet-gaskets being caught by SHEET
  { name: 'Gasket / Seal',      keys: ['GASKET', 'GSKT', 'SEAL'] },
  { name: 'Stud Bolts',         keys: ['STUDBOLT', 'STUD BOLT', 'SSTUDBOLTS', 'BOLT:STUD'] },
  { name: 'Skillet',            keys: ['SKILLET'] },
  // Valves: expanded to include gate/globe/check/ball valves, nozzles and repair kits
  { name: 'Valves',             keys: ['REPAIRKIT', 'REPAIR KIT', 'VALVE REPAIR',
                                        'GATE VALVE', 'GATEVALVE', 'GLOBE VALVE', 'GLOBEVALVE',
                                        'CHECK VALVE', 'CHECKVALVE', 'BALL VALVE', 'BALLVALVE',
                                        'BUTTERFLY', 'NOZZLE:', 'VALVE'] },
  { name: 'Filter',             keys: ['FILTER'] },
  // Instruments before Pipe Fitting — SWITCH/PANEL take priority over FITTING in conduit items
  { name: 'Instruments',        keys: ['TRANSMITTER', 'REGULATOR', 'IGNITOR', 'PANEL',
                                        'SWITCH', 'SENSOR', 'GAUGE', 'LIGHT:', 'ACTUATOR'] },
  // Pipe fitting: specific prefixes with colon first, then generic PIPE/FITTING
  { name: 'Pipe Fitting',       keys: ['RISER:', 'BELLOWS:', 'ELBOW:', 'TEE:', 'NIPPLE:',
                                        'SPACER', 'FLANGE:', 'PIPE', 'PIPING', 'FITTING'] },
  { name: 'Hardware',           keys: ['RIVET', 'SCREW', 'LINER', 'GLAND', 'THREADTAPE',
                                        'THREAD TAPE', 'NUT:', 'WASHER', 'FASTENER',
                                        'CLAMP', 'BRACKET', 'ASSEMBLY'] },
  { name: 'Rotating Equipment', keys: ['MOTOR', 'GEARBOX', 'COUPLING', 'SPRING', 'BUSHING',
                                        'REDUCER', 'BEARING', 'BELT', 'COMPRESSOR', 'PUMP',
                                        'TURBINE'] },
  { name: 'Structural',         keys: ['BEAM', 'TUBE', 'SHEET', 'COIL', 'RAFTER', 'COLUMN'] },
  { name: 'Consumables',        keys: ['ELECTRODE', 'FLUID', 'CAN:', 'PALLET', 'REFRACTORY',
                                        'INSULATION', 'LUBRICANT', 'PAINT', 'CHEMICAL'] },
  { name: 'Services',           keys: ['TRAINING', '%HCD', 'SERVICE CHARGE'] },
];

function _rptCatItem(desc) {
  if (!desc) return 'Other';
  var u = desc.toUpperCase();
  for (var i = 0; i < _RPT_CATS.length; i++) {
    var c = _RPT_CATS[i];
    for (var j = 0; j < c.keys.length; j++) {
      if (u.indexOf(c.keys[j]) !== -1) return c.name;
    }
  }
  return 'Other';
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
var _rptTip = null, _rptTipLbl, _rptTipVal, _rptTipItems;
function _rptEnsureTip() {
  if (_rptTip) return;
  _rptTip = document.createElement('div'); _rptTip.id = 'rpt-tip';
  _rptTip.innerHTML = '<div id="rpt-tip-lbl"></div><div id="rpt-tip-val"></div><div id="rpt-tip-items"></div>';
  document.body.appendChild(_rptTip);
  _rptTipLbl   = document.getElementById('rpt-tip-lbl');
  _rptTipVal   = document.getElementById('rpt-tip-val');
  _rptTipItems = document.getElementById('rpt-tip-items');
}
// items: optional array of description strings to list below the value
function _rptShowTip(e, lbl, val, items) {
  _rptTipLbl.textContent = lbl; _rptTipVal.textContent = val;
  if (_rptTipItems) {
    if (items && items.length) {
      _rptTipItems.style.display = 'block';
      _rptTipItems.innerHTML = '';
      items.slice(0, 4).forEach(function(s) {
        var sp = document.createElement('span');
        sp.textContent = '· ' + (s.length > 44 ? s.slice(0, 43) + '…' : s);
        _rptTipItems.appendChild(sp);
      });
    } else {
      _rptTipItems.style.display = 'none';
    }
  }
  var tipW = _rptTip.offsetWidth || 200, tipH = _rptTip.offsetHeight || 60;
  var left = Math.min(e.clientX + 12, window.innerWidth  - tipW - 8);
  var top  = Math.max(e.clientY - 28, 8);
  if (top + tipH > window.innerHeight - 8) top = e.clientY - tipH - 8;
  _rptTip.style.left = left + 'px'; _rptTip.style.top = top + 'px';
  _rptTip.classList.add('on');
}
function _rptHideTip() { _rptTip.classList.remove('on'); }

// ── Pipeline stage definitions ────────────────────────────────────────────────
var RPT_STAGES = [
  { label: 'In Transit',  ck: '--ch1', statuses: new Set(['dispatched', 'delivery_requested']) },
  { label: 'At Supplier', ck: '--ch2', statuses: new Set(['po_sent', 'awaiting_supplier_so', 'supplier_acknowledged']) },
  { label: 'Dispatching', ck: '--ch3', statuses: new Set(['dispatch_packed_awaiting_instruction', 'dispatch_instruction_sent', 'so_sent_to_warehouse', 'ready_for_dispatch']) },
  { label: 'Pending',     ck: '--ch4', statuses: null },
  { label: 'Delivered',   ck: '--ch6', statuses: CLOSED_STATUSES },
];

function _rptBucketCh(o) {
  var s = o.overall_status || '';
  for (var i = 0; i < RPT_STAGES.length; i++) {
    if (RPT_STAGES[i].statuses && RPT_STAGES[i].statuses.has(s)) return i;
  }
  return 3;
}

function _rptBucketNl(o) {
  if (o.overall_status === 'delivered' || !!o.delivered_at) return 4;
  var s = o.overall_status || '';
  for (var i = 0; i < 3; i++) {
    if (RPT_STAGES[i].statuses && RPT_STAGES[i].statuses.has(s)) return i;
  }
  return 3;
}

// Build a donut arc path. Handles the full-circle degenerate case (start === end).
function _rptDonutPath(cx, cy, R, ri, startAng, angle) {
  if (angle >= 2 * Math.PI - 0.001) {
    // Full circle: two semicircular arcs for outer ring, two for inner hole
    var top  = [cx + R  * Math.cos(startAng), cy + R  * Math.sin(startAng)];
    var bot  = [cx + R  * Math.cos(startAng + Math.PI), cy + R  * Math.sin(startAng + Math.PI)];
    var itop = [cx + ri * Math.cos(startAng), cy + ri * Math.sin(startAng)];
    var ibot = [cx + ri * Math.cos(startAng + Math.PI), cy + ri * Math.sin(startAng + Math.PI)];
    return 'M '  + top[0].toFixed(2)  + ' ' + top[1].toFixed(2) +
           ' A ' + R  + ' ' + R  + ' 0 1 1 ' + bot[0].toFixed(2)  + ' ' + bot[1].toFixed(2) +
           ' A ' + R  + ' ' + R  + ' 0 1 1 ' + top[0].toFixed(2)  + ' ' + top[1].toFixed(2) +
           ' M ' + itop[0].toFixed(2) + ' ' + itop[1].toFixed(2) +
           ' A ' + ri + ' ' + ri + ' 0 1 0 ' + ibot[0].toFixed(2) + ' ' + ibot[1].toFixed(2) +
           ' A ' + ri + ' ' + ri + ' 0 1 0 ' + itop[0].toFixed(2) + ' ' + itop[1].toFixed(2) + ' Z';
  }
  var endAng = startAng + angle, large = angle > Math.PI ? 1 : 0;
  var x1  = cx + R  * Math.cos(startAng), y1  = cy + R  * Math.sin(startAng);
  var x2  = cx + R  * Math.cos(endAng),   y2  = cy + R  * Math.sin(endAng);
  var xi1 = cx + ri * Math.cos(endAng),   yi1 = cy + ri * Math.sin(endAng);
  var xi2 = cx + ri * Math.cos(startAng), yi2 = cy + ri * Math.sin(startAng);
  return 'M '  + x1.toFixed(2)  + ' ' + y1.toFixed(2) +
         ' A ' + R  + ' ' + R  + ' 0 ' + large + ' 1 ' + x2.toFixed(2)  + ' ' + y2.toFixed(2) +
         ' L ' + xi1.toFixed(2) + ' ' + yi1.toFixed(2) +
         ' A ' + ri + ' ' + ri + ' 0 ' + large + ' 0 ' + xi2.toFixed(2) + ' ' + yi2.toFixed(2) + ' Z';
}

// ── Donut chart ───────────────────────────────────────────────────────────────
function _rptDrawDonut(containerId, legendId, values) {
  var cont = document.getElementById(containerId);
  var leg  = document.getElementById(legendId);
  if (!cont) return;
  cont.innerHTML = '';
  cont.style.flex = '1';
  if (leg) { leg.innerHTML = ''; leg.style.display = 'none'; }

  var total = values.reduce(function(s, v) { return s + v; }, 0);
  if (!total) return;

  var segs = RPT_STAGES.map(function(st, i) {
    return { label: st.label, ck: st.ck, v: values[i] || 0 };
  }).filter(function(s) { return s.v > 0; });

  var VH = Math.max(200, segs.length * 19 + 24);
  var VW = 490, cx = 100, cy = VH / 2, R = 80, ri = 50;

  var svg = _rptSvgEl('svg', { viewBox: '0 0 ' + VW + ' ' + VH, width: '100%', preserveAspectRatio: 'xMidYMid meet' });

  var startAng = -Math.PI / 2;
  segs.forEach(function(seg) {
    var angle = (seg.v / total) * 2 * Math.PI;
    if (angle < 0.001) { startAng += angle; return; }
    var clr = _rptCssVar(seg.ck);
    var path = _rptSvgEl('path', { d: _rptDonutPath(cx, cy, R, ri, startAng, angle) });
    path.style.fill = clr; path.style.cursor = 'pointer';
    path.setAttribute('stroke', _rptCssVar('--bg') || '#000');
    path.setAttribute('stroke-width', '1.5');
    (function(sg) {
      var pct = ((sg.v / total) * 100).toFixed(1);
      path.addEventListener('mousemove', function(e) { _rptShowTip(e, sg.label, sg.v + ' orders · ' + pct + '%'); });
      path.addEventListener('mouseleave', _rptHideTip);
    })(seg);
    svg.appendChild(path);
    startAng += angle;
  });

  svg.appendChild(_rptSvgTxt(total, { x: cx, y: cy - 5, 'text-anchor': 'middle', 'dominant-baseline': 'auto', 'font-size': '20', 'font-weight': '800' }, 'rch-dv'));
  svg.appendChild(_rptSvgTxt('TOTAL POs', { x: cx, y: cy + 12, 'text-anchor': 'middle', 'dominant-baseline': 'auto' }, 'rch-dl'));

  var legX = 205, legY0 = (VH - segs.length * 19) / 2;
  segs.forEach(function(seg, i) {
    var y   = legY0 + i * 19;
    var clr = _rptCssVar(seg.ck);
    var pct = ((seg.v / total) * 100).toFixed(1) + '%';

    var sq = _rptSvgEl('rect', { x: legX, y: y + 1, width: 10, height: 10, rx: 2 });
    sq.style.fill = clr;
    svg.appendChild(sq);
    svg.appendChild(_rptSvgTxt(seg.label, { x: legX + 14, y: y + 9, 'dominant-baseline': 'auto' }, 'rch-ax'));
    svg.appendChild(_rptSvgTxt(seg.v, { x: VW - 44, y: y + 9, 'text-anchor': 'end', 'dominant-baseline': 'auto', 'font-variant-numeric': 'tabular-nums' }, 'rch-ax'));
    svg.appendChild(_rptSvgTxt(pct, { x: VW - 2, y: y + 9, 'text-anchor': 'end', 'dominant-baseline': 'auto', 'font-variant-numeric': 'tabular-nums' }, 'rch-ax'));
  });

  cont.appendChild(svg);
}

// ── Monthly bar chart (+ optional delivered line overlay) ─────────────────────
function _rptDrawBars(containerId, months, fillColor, tipLabel, delMonths) {
  tipLabel = tipLabel || 'POs received';
  var cont = document.getElementById(containerId);
  if (!cont) return;
  cont.innerHTML = '';

  var VW = 380, VH = 158, ml = 28, mr = 6, mt = 10, mb = 24;
  var pw = VW - ml - mr, ph = VH - mt - mb;
  var maxV = Math.max.apply(null, months.map(function(m) { return m.v; }));
  if (delMonths) maxV = Math.max(maxV, Math.max.apply(null, delMonths.map(function(m) { return m.v; })));
  if (!maxV) maxV = 1;
  var rawStep = maxV / 5;
  var mag = Math.pow(10, Math.floor(Math.log10(rawStep || 1)));
  var step = [1, 2, 5, 10].reduce(function(chosen, n) { return n * mag >= rawStep && n * mag < chosen ? n * mag : chosen; }, Infinity);
  step = Math.max(Math.round(step), 1);
  var topTick = Math.ceil(maxV / step) * step || step;
  var svg = _rptSvgEl('svg', { viewBox: '0 0 ' + VW + ' ' + VH, width: '100%', preserveAspectRatio: 'xMidYMid meet' });

  for (var t = 0; t <= topTick; t += step) {
    var y = mt + ph - (t / topTick) * ph;
    var gl = _rptSvgEl('line', { x1: ml, x2: ml + pw, y1: y, y2: y, 'stroke-width': '1' });
    gl.style.stroke = t === 0 ? _rptCssVar('--t3') : _rptCssVar('--s2');
    svg.appendChild(gl);
    if (t > 0) {
      svg.appendChild(_rptSvgTxt(t, { x: ml - 4, y: y, 'text-anchor': 'end', 'dominant-baseline': 'central', 'font-variant-numeric': 'tabular-nums' }, 'rch-ax'));
    }
  }

  var slotW = pw / months.length;
  var barW  = Math.min(22, slotW * 0.55);
  months.forEach(function(m, i) {
    var x    = ml + i * slotW + (slotW - barW) / 2;
    var barH = Math.max((m.v / topTick) * ph, 2);
    var yBar = mt + ph - barH;
    var rect = _rptSvgEl('rect', { x: x, y: yBar, width: barW, height: barH, rx: 3 });
    rect.style.fill = fillColor; rect.style.opacity = '0.85'; rect.style.cursor = 'pointer';
    (function(mo) {
      rect.addEventListener('mousemove', function(e) { _rptShowTip(e, mo.lbl, mo.v + ' ' + tipLabel); });
      rect.addEventListener('mouseleave', _rptHideTip);
    })(m);
    svg.appendChild(rect);
    var skipLbl = months.length > 12 && i % 2 !== 0;
    if (!skipLbl) svg.appendChild(_rptSvgTxt(m.lbl, { x: x + barW / 2, y: mt + ph + 16, 'text-anchor': 'middle' }, 'rch-ax'));
    if (m.v === maxV || i === months.length - 1) {
      svg.appendChild(_rptSvgTxt(m.v, { x: x + barW / 2, y: yBar - 5, 'text-anchor': 'middle', 'font-weight': '700' }, 'rch-tip'));
    }
  });

  var ringColor = _rptCssVar('--s1');

  if (delMonths) {
    // Green delivered line overlay
    var delColor = _rptCssVar('--ok') || '#0ca30c';
    var dlPts = delMonths.map(function(m, i) {
      return (ml + i * slotW + slotW / 2) + ',' + (mt + ph - (m.v / topTick) * ph);
    }).join(' ');
    var dlLine = _rptSvgEl('polyline', { points: dlPts, fill: 'none', stroke: delColor, 'stroke-width': '2', 'stroke-linecap': 'round', 'stroke-linejoin': 'round' });
    dlLine.style.pointerEvents = 'none';
    svg.appendChild(dlLine);
    delMonths.forEach(function(m, i) {
      var cx = ml + i * slotW + slotW / 2;
      var cy = mt + ph - (m.v / topTick) * ph;
      var dot = _rptSvgEl('circle', { cx: cx, cy: cy, r: '3.5' });
      dot.style.fill = delColor; dot.style.stroke = ringColor; dot.style.strokeWidth = '1.5';
      (function(mo) {
        dot.addEventListener('mousemove', function(e) { _rptShowTip(e, mo.lbl, mo.v + ' delivered'); });
        dot.addEventListener('mouseleave', _rptHideTip);
      })(m);
      svg.appendChild(dot);
    });
    // Legend
    var leg = document.createElement('div'); leg.className = 'rpt-bars-legend';
    leg.innerHTML = '<span style="color:var(--t3)"><span class="rpt-bars-swatch" style="background:' + fillColor + '"></span>Received</span>'
      + '<span style="color:var(--ok)"><span class="rpt-bars-swatch" style="background:' + delColor + '"></span>Delivered</span>';
    cont.appendChild(svg);
    cont.appendChild(leg);
  } else {
    // Self-trend line (no comparison series)
    var trPts = months.map(function(m, i) {
      return (ml + i * slotW + slotW / 2) + ',' + (mt + ph - (m.v / topTick) * ph);
    }).join(' ');
    var trLine = _rptSvgEl('polyline', { points: trPts, fill: 'none', stroke: fillColor, 'stroke-width': '1.5', 'stroke-linecap': 'round', 'stroke-linejoin': 'round', opacity: '0.55' });
    trLine.style.pointerEvents = 'none';
    svg.appendChild(trLine);
    months.forEach(function(m, i) {
      var cx = ml + i * slotW + slotW / 2;
      var cy = mt + ph - (m.v / topTick) * ph;
      var dot = _rptSvgEl('circle', { cx: cx, cy: cy, r: '3.5' });
      dot.style.fill = fillColor; dot.style.stroke = ringColor; dot.style.strokeWidth = '1.5';
      (function(mo) {
        dot.addEventListener('mousemove', function(e) { _rptShowTip(e, mo.lbl, mo.v + ' ' + tipLabel); });
        dot.addEventListener('mouseleave', _rptHideTip);
      })(m);
      svg.appendChild(dot);
    });
    cont.appendChild(svg);
  }
}

// ── Delivery performance bars ─────────────────────────────────────────────────
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
          '<span class="rpt-perf-count">' + otd.onTime + ' line items &middot; ' + onPct + '%</span>',
        '</div>',
        '<div class="rpt-track"><div class="rpt-fill" style="width:' + onPct + '%;background:var(--ok)"></div></div>',
      '</div>',
      '<div>',
        '<div class="rpt-perf-hd">',
          '<span class="rpt-perf-status"><span class="rpt-perf-icon" style="background:var(--crit)"></span>Late</span>',
          '<span class="rpt-perf-count">' + otd.late + ' line items &middot; ' + latePct + '%</span>',
        '</div>',
        '<div class="rpt-track"><div class="rpt-fill" style="width:' + latePct + '%;background:var(--crit)"></div></div>',
      '</div>',
    '</div>',
    '<div class="rpt-metrics">',
      (otd.avgDays    != null ? '<div class="rpt-metric"><span class="rpt-metric-lbl">Avg days PO → delivery</span><span class="rpt-metric-val">' + otd.avgDays + ' days</span></div>' : ''),
      (otd.avgLateDays!= null ? '<div class="rpt-metric"><span class="rpt-metric-lbl">Avg overdue (late)</span><span class="rpt-metric-val" style="color:var(--warn)">' + otd.avgLateDays + ' days</span></div>' : ''),
      (otd.minDays    != null ? '<div class="rpt-metric"><span class="rpt-metric-lbl">Fastest delivery</span><span class="rpt-metric-val" style="color:var(--ok)">' + otd.minDays + ' days</span></div>' : ''),
      (mt === 0 ? '<div class="rpt-metric"><span class="rpt-metric-lbl" style="color:var(--t3)">No closed orders with delivery dates on record yet</span></div>' : ''),
    '</div>'
  ].join('');
}

// ── shared category totaller — returns { name, v, samples[] } ─────────────────
function _rptCatTotals(orders, liField, valFn) {
  var totals = {}, samples = {};
  orders.forEach(function(o) {
    (o[liField] || []).forEach(function(li) {
      var cat = _rptCatItem(li.description);
      totals[cat] = (totals[cat] || 0) + valFn(li);
      if (li.description) {
        if (!samples[cat]) samples[cat] = [];
        var already = false;
        for (var si = 0; si < samples[cat].length; si++) {
          if (samples[cat][si] === li.description) { already = true; break; }
        }
        if (!already && samples[cat].length < 5) samples[cat].push(li.description);
      }
    });
  });
  return Object.keys(totals)
    .map(function(k) { return { name: k, v: totals[k], samples: samples[k] || [] }; })
    .sort(function(a, b) { return b.v - a.v; });
}

// ── NGN → USD conversion rate (approximate 2026) ─────────────────────────────
var _NGN_USD_RATE = 1580;

// ── shared vertical column chart (showLine = optional line overlay on bar tops) ─
function _rptDrawColChart(containerId, cats, color, fmtTip, fmtAxis, showLine) {
  var cont = document.getElementById(containerId);
  if (!cont) return;
  cont.innerHTML = '';

  if (!cats.length) {
    cont.innerHTML = '<div style="padding:.5rem 0;color:var(--t3);font-size:12px">No data available</div>';
    return;
  }

  var VW = 660, VH = 190, ml = 44, mr = 6, mt = 10, mb = 62;
  var pw = VW - ml - mr, ph = VH - mt - mb;
  var maxV = cats[0].v;
  var mag  = Math.pow(10, Math.floor(Math.log10(maxV || 1)));
  var topTick = Math.ceil(maxV / mag) * mag || 1;
  var slotW = pw / cats.length;
  var barW  = Math.max(Math.min(slotW * 0.62, 40), 6);

  var svg = _rptSvgEl('svg', { viewBox: '0 0 ' + VW + ' ' + VH, width: '100%', preserveAspectRatio: 'xMidYMid meet' });

  [0, 0.25, 0.5, 0.75, 1].forEach(function(frac) {
    var y = mt + ph - frac * ph;
    var gl = _rptSvgEl('line', { x1: ml, x2: ml + pw, y1: y, y2: y, 'stroke-width': '1' });
    gl.style.stroke = frac === 0 ? _rptCssVar('--t3') : _rptCssVar('--s2');
    svg.appendChild(gl);
    if (frac > 0) {
      svg.appendChild(_rptSvgTxt(fmtAxis(topTick * frac), {
        x: ml - 4, y: y, 'text-anchor': 'end', 'dominant-baseline': 'central', 'font-variant-numeric': 'tabular-nums'
      }, 'rch-ax'));
    }
  });

  var linePts = [];

  cats.forEach(function(c, i) {
    var cx   = ml + i * slotW + slotW / 2;
    var barH = Math.max((c.v / topTick) * ph, 2);
    var yBar = mt + ph - barH;
    var bx   = cx - barW / 2;

    if (showLine) linePts.push([cx, yBar]);

    var rect = _rptSvgEl('rect', { x: bx, y: yBar, width: barW, height: barH, rx: 3 });
    rect.style.fill = color; rect.style.opacity = '0.85'; rect.style.cursor = 'pointer';
    (function(c2) {
      rect.addEventListener('mousemove', function(e) { _rptShowTip(e, c2.name, fmtTip(c2.v), c2.samples); });
      rect.addEventListener('mouseleave', _rptHideTip);
    })(c);
    svg.appendChild(rect);

    if (i < 4) {
      svg.appendChild(_rptSvgTxt(fmtAxis(c.v), { x: cx, y: yBar - 4, 'text-anchor': 'middle', 'font-weight': '700' }, 'rch-tip'));
    }

    var short = c.name.length > 13 ? c.name.slice(0, 12) + '…' : c.name;
    var lx = cx, ly = mt + ph + 5;
    var lbl = _rptSvgTxt(short, { x: lx, y: ly, 'text-anchor': 'end', 'dominant-baseline': 'auto' }, 'rch-ax');
    lbl.setAttribute('transform', 'rotate(-42 ' + lx + ' ' + ly + ')');
    svg.appendChild(lbl);
  });

  // Line overlay drawn after bars so it sits on top
  if (showLine && linePts.length > 1) {
    var pl = _rptSvgEl('polyline', { points: linePts.map(function(p) { return p[0] + ',' + p[1]; }).join(' '), fill: 'none', 'stroke-width': '2.5' });
    pl.style.stroke = color;
    pl.style.pointerEvents = 'none';
    svg.appendChild(pl);
    linePts.forEach(function(p) {
      var dot = _rptSvgEl('circle', { cx: p[0], cy: p[1], r: '4' });
      dot.style.fill = color;
      dot.style.pointerEvents = 'none';
      dot.setAttribute('stroke', _rptCssVar('--bg') || '#000');
      dot.setAttribute('stroke-width', '2');
      svg.appendChild(dot);
    });
  }

  cont.appendChild(svg);
}

// ── standalone line plot (for value-by-category charts) ───────────────────────
function _rptDrawLinePlot(containerId, cats, color, fmtTip, fmtAxis) {
  var cont = document.getElementById(containerId);
  if (!cont) return;
  cont.innerHTML = '';

  if (!cats.length) {
    cont.innerHTML = '<div style="padding:.5rem 0;color:var(--t3);font-size:12px">No data available</div>';
    return;
  }

  var VW = 660, VH = 190, ml = 44, mr = 6, mt = 10, mb = 62;
  var pw = VW - ml - mr, ph = VH - mt - mb;
  var maxV = cats[0].v;
  var mag  = Math.pow(10, Math.floor(Math.log10(maxV || 1)));
  var topTick = Math.ceil(maxV / mag) * mag || 1;
  var slotW = pw / cats.length;

  var svg = _rptSvgEl('svg', { viewBox: '0 0 ' + VW + ' ' + VH, width: '100%', preserveAspectRatio: 'xMidYMid meet' });

  [0, 0.25, 0.5, 0.75, 1].forEach(function(frac) {
    var y = mt + ph - frac * ph;
    var gl = _rptSvgEl('line', { x1: ml, x2: ml + pw, y1: y, y2: y, 'stroke-width': '1' });
    gl.style.stroke = frac === 0 ? _rptCssVar('--t3') : _rptCssVar('--s2');
    svg.appendChild(gl);
    if (frac > 0) {
      svg.appendChild(_rptSvgTxt(fmtAxis(topTick * frac), {
        x: ml - 4, y: y, 'text-anchor': 'end', 'dominant-baseline': 'central', 'font-variant-numeric': 'tabular-nums'
      }, 'rch-ax'));
    }
  });

  var pts = cats.map(function(c, i) {
    return { x: ml + i * slotW + slotW / 2, y: mt + ph - (c.v / topTick) * ph, c: c };
  });

  // Area fill under line
  var areaD = 'M ' + pts[0].x + ' ' + (mt + ph);
  pts.forEach(function(p) { areaD += ' L ' + p.x + ' ' + p.y; });
  areaD += ' L ' + pts[pts.length - 1].x + ' ' + (mt + ph) + ' Z';
  var area = _rptSvgEl('path', { d: areaD });
  area.style.fill = color;
  area.style.opacity = '0.12';
  svg.appendChild(area);

  // Polyline
  var pl = _rptSvgEl('polyline', {
    points: pts.map(function(p) { return p.x + ',' + p.y; }).join(' '),
    fill: 'none', 'stroke-width': '2.5'
  });
  pl.style.stroke = color;
  svg.appendChild(pl);

  pts.forEach(function(p, i) {
    // Transparent hit zone for tooltip
    (function(p2) {
      var hit = _rptSvgEl('circle', { cx: p2.x, cy: p2.y, r: '12', fill: 'transparent', cursor: 'pointer' });
      hit.addEventListener('mousemove', function(e) { _rptShowTip(e, p2.c.name, fmtTip(p2.c.v), p2.c.samples); });
      hit.addEventListener('mouseleave', _rptHideTip);
      svg.appendChild(hit);
    })(p);

    // Dot
    var dot = _rptSvgEl('circle', { cx: p.x, cy: p.y, r: '4' });
    dot.style.fill = color;
    dot.setAttribute('stroke', _rptCssVar('--bg') || '#000');
    dot.setAttribute('stroke-width', '2');
    svg.appendChild(dot);

    // Value label on first 4
    if (i < 4) {
      svg.appendChild(_rptSvgTxt(fmtAxis(p.c.v), { x: p.x, y: p.y - 8, 'text-anchor': 'middle', 'font-weight': '700' }, 'rch-tip'));
    }

    // X-axis label
    var short = p.c.name.length > 13 ? p.c.name.slice(0, 12) + '…' : p.c.name;
    var lx = p.x, ly = mt + ph + 5;
    var lbl = _rptSvgTxt(short, { x: lx, y: ly, 'text-anchor': 'end', 'dominant-baseline': 'auto' }, 'rch-ax');
    lbl.setAttribute('transform', 'rotate(-42 ' + lx + ' ' + ly + ')');
    svg.appendChild(lbl);
  });

  cont.appendChild(svg);
}

// ── get the best available date string from an order ──────────────────────────
function _rptOrderDate(o) {
  return o.notification_received_at || o.po_date || o.created_at || '';
}

// ── Product breakdown by quantity (bar + line overlay) ────────────────────────
function _rptDrawLineItems(containerId, orders, liField, color) {
  var cats = _rptCatTotals(orders, liField, function(li) { var q = parseFloat(li.quantity); return isNaN(q) ? 1 : q; });
  var fmtQ = function(v) { return v >= 1000 ? (v / 1000).toFixed(1) + 'k' : Math.round(v) + ''; };
  _rptDrawColChart(containerId, cats, color, function(v) { return Math.round(v) + ' units'; }, fmtQ, true);
}

// ── Categorical palette for donut chart ──────────────────────────────────────
var _RPT_PIE_COLORS = [
  '#3B82F6','#F59E0B','#10B981','#8B5CF6','#EF4444',
  '#06B6D4','#F97316','#84CC16','#EC4899','#6366F1',
  '#14B8A6','#A16207'
];

// ── Donut chart for product value by category ─────────────────────────────────
function _rptDrawCatDonut(containerId, cats, fmtVal) {
  var cont = document.getElementById(containerId);
  if (!cont) return;
  cont.innerHTML = '';

  if (!cats.length) {
    cont.innerHTML = '<div style="padding:.5rem 0;color:var(--t3);font-size:12px">No cost data available</div>';
    return;
  }

  var MAX_SEGS = 10;
  var segs = cats.slice(0, MAX_SEGS);
  var otherV = 0, otherNames = [];
  for (var oi = MAX_SEGS; oi < cats.length; oi++) { otherV += cats[oi].v; otherNames.push(cats[oi].name); }
  if (otherV > 0) segs = segs.concat([{ name: 'Other', v: otherV, samples: otherNames }]);

  var total = segs.reduce(function(s, c) { return s + c.v; }, 0);
  if (!total) return;

  var VH = Math.max(200, segs.length * 19 + 24);
  var VW = 490, cx = 100, cy = VH / 2, R = 80, ri = 50;

  var svg = _rptSvgEl('svg', { viewBox: '0 0 ' + VW + ' ' + VH, width: '100%', preserveAspectRatio: 'xMidYMid meet' });

  var startAng = -Math.PI / 2;
  segs.forEach(function(seg, i) {
    var angle = (seg.v / total) * 2 * Math.PI;
    if (angle < 0.001) { startAng += angle; return; }
    var clr = _RPT_PIE_COLORS[i % _RPT_PIE_COLORS.length];
    var path = _rptSvgEl('path', { d: _rptDonutPath(cx, cy, R, ri, startAng, angle) });
    path.style.fill = clr;
    path.style.cursor = 'pointer';
    path.setAttribute('stroke', _rptCssVar('--bg') || '#000');
    path.setAttribute('stroke-width', '1.5');
    (function(sg) {
      var pct = ((sg.v / total) * 100).toFixed(1);
      path.addEventListener('mousemove', function(e) { _rptShowTip(e, sg.name, fmtVal(sg.v) + '  ·  ' + pct + '%', sg.samples); });
      path.addEventListener('mouseleave', _rptHideTip);
    })(seg);
    svg.appendChild(path);
    startAng += angle;
  });

  // Center label: total
  var centerTop = _rptSvgTxt(fmtVal(total), { x: cx, y: cy - 5, 'text-anchor': 'middle', 'dominant-baseline': 'auto', 'font-weight': '700', 'font-size': '12' }, 'rch-tip');
  svg.appendChild(centerTop);
  var centerBot = _rptSvgTxt('total', { x: cx, y: cy + 10, 'text-anchor': 'middle', 'dominant-baseline': 'auto', 'font-size': '9' }, 'rch-ax');
  svg.appendChild(centerBot);

  // Legend
  var legX = 205, legY0 = (VH - segs.length * 19) / 2;
  segs.forEach(function(seg, i) {
    var y = legY0 + i * 19;
    var clr = _RPT_PIE_COLORS[i % _RPT_PIE_COLORS.length];
    var pct = ((seg.v / total) * 100).toFixed(1) + '%';

    var sq = _rptSvgEl('rect', { x: legX, y: y + 1, width: 10, height: 10, rx: 2 });
    sq.style.fill = clr;
    svg.appendChild(sq);

    svg.appendChild(_rptSvgTxt(seg.name, { x: legX + 14, y: y + 9, 'dominant-baseline': 'auto' }, 'rch-ax'));
    svg.appendChild(_rptSvgTxt(fmtVal(seg.v), { x: VW - 44, y: y + 9, 'text-anchor': 'end', 'dominant-baseline': 'auto', 'font-variant-numeric': 'tabular-nums' }, 'rch-ax'));
    svg.appendChild(_rptSvgTxt(pct, { x: VW - 2, y: y + 9, 'text-anchor': 'end', 'dominant-baseline': 'auto', 'font-variant-numeric': 'tabular-nums' }, 'rch-ax'));
  });

  cont.appendChild(svg);
}

// ── Product value → donut chart, per-order currency conversion ────────────────
function _rptDrawCatValue(containerId, orders, liField, color) {
  var totals = {}, sampleMap = {};
  var grandTotal = 0;

  function _addSample(cat, desc) {
    if (!desc) return;
    if (!sampleMap[cat]) sampleMap[cat] = [];
    for (var si = 0; si < sampleMap[cat].length; si++) {
      if (sampleMap[cat][si] === desc) return;
    }
    if (sampleMap[cat].length < 5) sampleMap[cat].push(desc);
  }

  orders.forEach(function(o) {
    var curr = (o.po_currency || o.currency || 'USD').toUpperCase();
    var rate = curr === 'NGN' ? 1 / _NGN_USD_RATE : 1;
    var items = o[liField] || [];
    if (!items.length) return;

    var hasLiPrices = items.some(function(li) { return parseFloat(li.net_amount || 0) > 0; });

    if (hasLiPrices) {
      // NLNG: line items carry actual amounts in the order's native currency
      items.forEach(function(li) {
        var v = parseFloat(li.net_amount || 0) * rate;
        if (v <= 0) return;
        var cat = _rptCatItem(li.description);
        totals[cat] = (totals[cat] || 0) + v;
        grandTotal += v;
        _addSample(cat, li.description);
      });
    } else {
      // Chevron: no line-item prices — distribute order amount equally across items
      var amount = parseFloat(o.po_amount || o.net_value || 0) * rate;
      if (!amount) return;
      grandTotal += amount;
      var perItem = amount / items.length;
      items.forEach(function(li) {
        var cat = _rptCatItem(li.description);
        totals[cat] = (totals[cat] || 0) + perItem;
        _addSample(cat, li.description);
      });
    }
  });

  if (!grandTotal) {
    var cont = document.getElementById(containerId);
    if (cont) cont.innerHTML = '<div style="padding:.5rem 0;color:var(--t3);font-size:12px">No cost data available</div>';
    return;
  }

  var cats = Object.keys(totals)
    .filter(function(k) { return totals[k] > 0; })
    .map(function(k) { return { name: k, v: totals[k], samples: sampleMap[k] || [] }; })
    .sort(function(a, b) { return b.v - a.v; });

  var fmtV = function(v) {
    return v >= 1000000 ? '$' + (v / 1000000).toFixed(1) + 'M'
         : v >= 1000    ? '$' + (v / 1000).toFixed(0) + 'k'
         : '$' + Math.round(v);
  };
  _rptDrawCatDonut(containerId, cats, fmtV);
}

// ── Year / month filter helpers ───────────────────────────────────────────────
var _RPT_MONTH_NAMES = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];

function _rptPopulateCatFilters(prefix, orders) {
  var years = {};
  orders.forEach(function(o) {
    var d = _rptOrderDate(o);
    if (!d) return;
    var yr = new Date(d).getFullYear();
    if (!isNaN(yr)) years[yr] = true;
  });

  var yrSel = document.getElementById('rpt-yr-' + prefix);
  var moSel = document.getElementById('rpt-mo-' + prefix);
  if (!yrSel || !moSel) return;

  var prevYr = yrSel.value;
  var prevMo = moSel.value;

  yrSel.innerHTML = '<option value="">All years</option>';
  Object.keys(years).sort().forEach(function(y) {
    var opt = document.createElement('option');
    opt.value = y; opt.textContent = y;
    yrSel.appendChild(opt);
  });

  moSel.innerHTML = '<option value="">All months</option>';
  _RPT_MONTH_NAMES.forEach(function(m, i) {
    var opt = document.createElement('option');
    opt.value = i + 1; opt.textContent = m;
    moSel.appendChild(opt);
  });

  yrSel.value = prevYr;
  moSel.value = prevMo;
}

function _rptFilterByDate(orders, yr, mo) {
  return orders.filter(function(o) {
    var d = _rptOrderDate(o);
    if (!d) return !yr && !mo;
    var dt = new Date(d);
    if (yr && dt.getFullYear() !== parseInt(yr, 10)) return false;
    if (mo && dt.getMonth() + 1 !== parseInt(mo, 10)) return false;
    return true;
  });
}

function _rptApplyCatFilter(prefix, allOrders, liField, color) {
  var yr = (document.getElementById('rpt-yr-' + prefix) || {}).value || '';
  var mo = (document.getElementById('rpt-mo-' + prefix) || {}).value || '';
  var filtered = _rptFilterByDate(allOrders, yr, mo);
  _rptDrawLineItems('rpt-litems-' + prefix, filtered, liField, color);
  _rptDrawCatValue('rpt-catval-' + prefix, filtered, liField, color);
}

function _rptWireCatFilters(prefix, allOrders, liField, color) {
  _rptPopulateCatFilters(prefix, allOrders);
  ['rpt-yr-' + prefix, 'rpt-mo-' + prefix].forEach(function(id) {
    var old = document.getElementById(id);
    if (!old) return;
    var savedVal = old.value;
    var fresh = old.cloneNode(true);
    fresh.value = savedVal; // cloneNode may not preserve live JS value state; restore explicitly
    old.parentNode.replaceChild(fresh, old);
    fresh.addEventListener('change', function() {
      _rptApplyCatFilter(prefix, allOrders, liField, color);
    });
  });
}

// ── Pipeline cycle times ──────────────────────────────────────────────────────
function _rptCycleTimes(orders) {
  var stages = [
    { label: 'PO → SO ack',          t1: 'notification_received_at',   t2: 'so_received_at',                total: 0, n: 0 },
    { label: 'SO → Dispatch instr',   t1: 'so_received_at',             t2: 'dispatch_instructions_sent_at', total: 0, n: 0 },
    { label: 'Dispatch → Delivery',   t1: 'dispatched_at',              t2: 'delivered_at',                  total: 0, n: 0 },
  ];
  orders.forEach(function(o) {
    stages.forEach(function(s) {
      var t1 = o[s.t1] ? new Date(o[s.t1]) : null;
      var t2 = o[s.t2] ? new Date(o[s.t2]) : null;
      if (!t1 || !t2 || isNaN(t1) || isNaN(t2)) return;
      var days = Math.round((t2 - t1) / 86400000);
      if (days > 0 && days < 365) { s.total += days; s.n++; }
    });
  });
  return stages.map(function(s) {
    return { label: s.label, avg: s.n > 0 ? Math.round(s.total / s.n) : null, n: s.n };
  });
}

function _rptDrawCycleTimes(containerId, orders) {
  var cont = document.getElementById(containerId);
  if (!cont) return;
  cont.innerHTML = '';

  var stages = _rptCycleTimes(orders);
  var maxDays = Math.max.apply(null, stages.map(function(s) { return s.avg || 0; }));
  if (!maxDays) {
    cont.innerHTML = '<div style="padding:.5rem 0;color:var(--t3);font-size:12px">Cycle time data not available yet</div>';
    return;
  }

  var rowH = 34, namePx = 130, countPx = 36, padR = 6;
  var VW = 340, VH = rowH * stages.length + 8, barArea = VW - namePx - countPx - padR;
  var svg = _rptSvgEl('svg', { viewBox: '0 0 ' + VW + ' ' + VH, width: '100%' });

  stages.forEach(function(s, i) {
    var y = i * rowH + 8;
    var track = _rptSvgEl('rect', { x: namePx, y: y + 12, width: barArea, height: 8, rx: 4 });
    track.style.fill = _rptCssVar('--s2');
    svg.appendChild(track);
    if (s.avg != null) {
      var barW = Math.max((s.avg / maxDays) * barArea, 4);
      var barColor = s.avg <= 7 ? _rptCssVar('--ok') : s.avg <= 21 ? _rptCssVar('--warn') : _rptCssVar('--crit');
      var fill = _rptSvgEl('rect', { x: namePx, y: y + 12, width: barW, height: 8, rx: 4 });
      fill.style.fill = barColor; fill.style.cursor = 'pointer';
      (function(st) {
        fill.addEventListener('mousemove', function(e) { _rptShowTip(e, st.label, st.avg + ' days avg · ' + st.n + ' orders'); });
        fill.addEventListener('mouseleave', _rptHideTip);
      })(s);
      svg.appendChild(fill);
      svg.appendChild(_rptSvgTxt(s.avg + 'd', { x: namePx + barArea + 5, y: y + 20, 'dominant-baseline': 'central', 'font-weight': '700', 'font-variant-numeric': 'tabular-nums' }, 'rch-tip'));
    } else {
      svg.appendChild(_rptSvgTxt('—', { x: namePx + barArea + 5, y: y + 20, 'dominant-baseline': 'central' }, 'rch-ax'));
    }
    var short = s.label;
    svg.appendChild(_rptSvgTxt(short, { x: namePx - 5, y: y + 20, 'text-anchor': 'end', 'dominant-baseline': 'central' }, 'rch-ax'));
  });
  cont.appendChild(svg);
}

// ── OTD trend line chart ──────────────────────────────────────────────────────
function _rptDrawOtdTrend(containerId, orders, liField, color) {
  var cont = document.getElementById(containerId);
  if (!cont) return;
  var prevH = cont.offsetHeight;
  if (prevH) cont.style.minHeight = prevH + 'px';
  cont.innerHTML = '';

  var modeId = containerId.replace('rpt-otdtrend-', 'rpt-otdmode-');
  var modeSel = document.getElementById(modeId);
  var mode = modeSel ? modeSel.value : 'delivery';
  if (modeSel) {
    var freshSel = modeSel.cloneNode(true);
    freshSel.value = mode; // cloneNode copies attributes not JS property state; restore explicitly
    modeSel.parentNode.replaceChild(freshSel, modeSel);
    freshSel.addEventListener('change', function() {
      _rptDrawOtdTrend(containerId, orders, liField, color);
    });
  }

  var months = _rptOtdByMonth(orders, liField, mode);
  var hasData = months.some(function(m) { return m.score !== null; });
  if (!hasData) {
    cont.innerHTML = '<div style="padding:.5rem 0;color:var(--t3);font-size:12px">No delivery data yet — OTD trend will appear once orders are delivered</div>';
    cont.style.minHeight = '';
    return;
  }

  var VW = 700, VH = 150, ml = 30, mr = 16, mt = 12, mb = 24;
  var pw = VW - ml - mr, ph = VH - mt - mb;
  var svg = _rptSvgEl('svg', { viewBox: '0 0 ' + VW + ' ' + VH, width: '100%', preserveAspectRatio: 'xMidYMid meet' });

  [0, 25, 50, 75, 100].forEach(function(pct) {
    var y = mt + ph - (pct / 100) * ph;
    var gl = _rptSvgEl('line', { x1: ml, x2: ml + pw, y1: y, y2: y, 'stroke-width': '1' });
    gl.style.stroke = pct === 0 ? _rptCssVar('--t3') : _rptCssVar('--s2');
    svg.appendChild(gl);
    if (pct > 0) {
      svg.appendChild(_rptSvgTxt(pct + '%', { x: ml - 4, y: y, 'text-anchor': 'end', 'dominant-baseline': 'central' }, 'rch-ax'));
    }
  });

  // 90% target dashed reference line
  var targetY = mt + ph - 0.9 * ph;
  var tLine = _rptSvgEl('line', { x1: ml, x2: ml + pw, y1: targetY, y2: targetY, 'stroke-width': '1', 'stroke-dasharray': '4,3' });
  tLine.style.stroke = _rptCssVar('--ok'); tLine.style.opacity = '0.45';
  svg.appendChild(tLine);
  svg.appendChild(_rptSvgTxt('90%', { x: ml + pw + 2, y: targetY, 'dominant-baseline': 'central' }, 'rch-ax'));

  var slotW = pw / months.length;
  months.forEach(function(m, i) {
    var skipLbl = months.length > 9 && i % 2 !== 0;
    if (!skipLbl) svg.appendChild(_rptSvgTxt(m.lbl, { x: ml + i * slotW + slotW / 2, y: mt + ph + 16, 'text-anchor': 'middle' }, 'rch-ax'));
  });

  var pts = months.map(function(m, i) {
    if (m.score === null) return null;
    // clamp y so a 0% dot doesn't sit on the axis line and become invisible
    var y = Math.min(mt + ph - (m.score / 100) * ph, mt + ph - 4);
    return { x: ml + i * slotW + slotW / 2, y: y, m: m };
  });

  for (var i = 1; i < pts.length; i++) {
    if (!pts[i] || !pts[i - 1]) continue;
    var seg = _rptSvgEl('line', { x1: pts[i-1].x, y1: pts[i-1].y, x2: pts[i].x, y2: pts[i].y, 'stroke-width': '2', 'stroke-linecap': 'round' });
    seg.style.stroke = color;
    svg.appendChild(seg);
  }

  var ringColor = _rptCssVar('--s1');
  pts.forEach(function(pt) {
    if (!pt) return;
    var dotColor = pt.m.score >= 90 ? _rptCssVar('--ok') : pt.m.score >= 70 ? _rptCssVar('--warn') : _rptCssVar('--crit');
    var dot = _rptSvgEl('circle', { cx: pt.x, cy: pt.y, r: '4' });
    dot.style.fill = dotColor; dot.style.stroke = ringColor; dot.style.strokeWidth = '1.5'; dot.style.cursor = 'pointer';
    (function(p) {
      dot.addEventListener('mousemove', function(e) { _rptShowTip(e, p.m.lbl, p.m.score + '% OTD · ' + p.m.total + ' line items'); });
      dot.addEventListener('mouseleave', _rptHideTip);
    })(pt);
    svg.appendChild(dot);
  });

  cont.style.minHeight = '';
  cont.appendChild(svg);
}

// ── Main updateReports ────────────────────────────────────────────────────────
function updateReports() {
  _rptEnsureTip();

  // ── Chevron ──
  var chClosed  = ORDERS.filter(function(o) { return CLOSED_STATUSES.has(o.overall_status); });
  var chActive  = ORDERS.filter(function(o) { return !CLOSED_STATUSES.has(o.overall_status); });
  var chOverdue = chActive.filter(function(o) {
    return o.required_delivery_date && new Date(o.required_delivery_date) < new Date();
  }).length;
  var otdCh = _rptOtd(chClosed, 'order_line_items');

  _set('rpt-ch-total',     ORDERS.length);
  _set('rpt-ch-active',    chActive.length);
  _set('rpt-ch-delivered', chClosed.length);

  var chOtdEl = document.getElementById('rpt-ch-otd');
  if (chOtdEl) {
    chOtdEl.textContent = otdCh.score != null ? otdCh.score + '%' : '—';
    chOtdEl.style.color = otdCh.score == null ? '' : otdCh.score >= 90 ? 'var(--ok)' : otdCh.score >= 75 ? 'var(--warn)' : 'var(--crit)';
  }
  _set('rpt-ch-otd-note', otdCh.total > 0 ? otdCh.onTime + ' / ' + otdCh.total + ' line items' : 'On-time delivery rate');

  var chOvEl = document.getElementById('rpt-ch-overdue');
  if (chOvEl) { chOvEl.textContent = chOverdue || '0'; chOvEl.style.color = chOverdue > 0 ? 'var(--crit)' : ''; }

  var chBuckets = [0, 0, 0, 0, 0];
  ORDERS.forEach(function(o) { chBuckets[_rptBucketCh(o)]++; });
  _rptDrawDonut('rpt-donut-ch', 'rpt-leg-ch', chBuckets);

  _rptRedrawBars('ch');
  _rptDrawPerf('rpt-perf-ch', otdCh);
  _rptWireCatFilters('ch', ORDERS, 'order_line_items', _rptCssVar('--accent'));
  _rptApplyCatFilter('ch', ORDERS, 'order_line_items', _rptCssVar('--accent'));
  _rptDrawOtdTrend('rpt-otdtrend-ch', ORDERS, 'order_line_items', _rptCssVar('--accent'));

  // ── NLNG ──
  var nlClosed  = NLNG_ORDERS.filter(function(o) { return o.overall_status === 'delivered' || !!o.delivered_at; });
  var nlActive  = NLNG_ORDERS.filter(function(o) { return o.overall_status !== 'delivered' && !o.delivered_at; });
  var nlOverdue = nlActive.filter(function(o) {
    return o.required_delivery_date && new Date(o.required_delivery_date) < new Date();
  }).length;
  var otdNl = _rptOtd(nlClosed, 'nlng_order_line_items');

  _set('rpt-nl-total',     NLNG_ORDERS.length);
  _set('rpt-nl-active',    nlActive.length);
  _set('rpt-nl-delivered', nlClosed.length);

  var nlOtdEl = document.getElementById('rpt-nl-otd');
  if (nlOtdEl) {
    nlOtdEl.textContent = otdNl.score != null ? otdNl.score + '%' : '—';
    nlOtdEl.style.color = otdNl.score == null ? '' : otdNl.score >= 90 ? 'var(--ok)' : otdNl.score >= 75 ? 'var(--warn)' : 'var(--crit)';
  }
  _set('rpt-nl-otd-note', otdNl.total > 0 ? otdNl.onTime + ' / ' + otdNl.total + ' line items' : 'On-time delivery rate');

  var nlOvEl = document.getElementById('rpt-nl-overdue');
  if (nlOvEl) { nlOvEl.textContent = nlOverdue || '0'; nlOvEl.style.color = nlOverdue > 0 ? 'var(--crit)' : ''; }

  var nlBuckets = [0, 0, 0, 0, 0];
  NLNG_ORDERS.forEach(function(o) { nlBuckets[_rptBucketNl(o)]++; });
  _rptDrawDonut('rpt-donut-nl', 'rpt-leg-nl', nlBuckets);

  _rptRedrawBars('nl');
  _rptDrawPerf('rpt-perf-nl', otdNl);
  _rptWireCatFilters('nl', NLNG_ORDERS, 'nlng_order_line_items', _rptCssVar('--ch1'));
  _rptApplyCatFilter('nl', NLNG_ORDERS, 'nlng_order_line_items', _rptCssVar('--ch1'));
  _rptDrawOtdTrend('rpt-otdtrend-nl', NLNG_ORDERS, 'nlng_order_line_items', _rptCssVar('--ch1'));
}
