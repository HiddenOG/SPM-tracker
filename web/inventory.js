/*
  inventory.js — Stock and Products.

  Self-contained: this file fetches inventory.html at start-up, injects the two
  pages and their modals into the page container, and owns everything they do.
  Nothing in script.js knows about stock beyond the two sidebar rows, so the
  whole feature is three files — inventory.html, inventory.css, inventory.js —
  that can be read, changed and uploaded on their own.

  What it borrows from script.js: authFetch, htmlEscape, renderPgBar, showPage,
  fmtTs. Those are the app's shared plumbing and are not worth a second copy.

  Why stock is a manual register rather than a calculated balance: nothing in
  the order pipeline records quantities. Of 154 warehouse replies on file, none
  carry a count — they say "Completely delivered" or "PO not in stock" in
  prose. So the number starts from what someone physically counts, and every
  change is logged so it can still be explained a month later.
*/
'use strict';

(function () {

  var _stock        = [];
  var _products     = [];
  var _stockPage    = 1;
  var _productsPage = 1;
  var PER_PAGE      = 100;   // the same page size as orders, quotes and delays
  var _editingId    = null;     // stock item being edited, null when creating
  var _editingQty   = 0;        // its quantity when the form opened
  var _openSnapshot = '';       // the form as it was opened, to spot unsaved work
  var _deletingId   = null;
  var _loaded       = false;    // has the fragment been injected yet

  // ── helpers ──────────────────────────────────────────────────────────────

  function esc(s) {
    return (typeof htmlEscape === 'function')
      ? htmlEscape(s)
      : String(s == null ? '' : s).replace(/[&<>"]/g, function (c) {
          return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c];
        });
  }

  function num(v) {
    var n = parseFloat(v);
    return isNaN(n) ? 0 : n;
  }

  function money(n) {
    return '$' + num(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  // Counts are stored to three decimals but are whole numbers nearly always.
  // Printing "12.000" everywhere makes a stock list much harder to scan.
  function qty(n) {
    var v = num(n);
    return v === Math.round(v) ? String(Math.round(v))
                               : v.toLocaleString('en-US', { maximumFractionDigits: 3 });
  }

  function day(iso) {
    if (!iso) return '—';
    if (typeof fmtTs === 'function') { try { return fmtTs(iso); } catch (e) {} }
    return String(iso).slice(0, 10);
  }

  // Everything the person can type, as one string. Comparing it against what
  // the form held when it opened is how an accidental close is caught.
  var FORM_FIELDS = ['sf-description', 'sf-quantity', 'sf-uom',
                     'sf-unit-cost', 'sf-notes', 'sf-move-note'];

  function snapshot() {
    return FORM_FIELDS.map(function (id) { return val(id); }).join('\u0000');
  }

  function isDirty() {
    var modal = get('stock-form-modal');
    if (!modal || modal.classList.contains('hidden')) return false;
    return snapshot() !== _openSnapshot;
  }

  // The server refuses it either way; hiding the button means nobody at a
  // shelf presses something that was only ever going to be turned down.
  function canDelete() {
    var role = (typeof _currentUser !== 'undefined' && _currentUser && _currentUser.role) || '';
    return role === 'admin' || role === 'procurement';
  }

  function get(id) { return document.getElementById(id); }
  function val(id) { var el = get(id); return el ? String(el.value || '').trim() : ''; }
  function set(id, v) { var el = get(id); if (el) el.value = v; }

  // ── the fragment ─────────────────────────────────────────────────────────

  function mount() {
    if (_loaded) return Promise.resolve();
    return fetch('inventory.html', { credentials: 'same-origin' })
      .then(function (r) {
        if (!r.ok) throw new Error('inventory.html ' + r.status);
        return r.text();
      })
      .then(function (html) {
        if (_loaded) return;
        var holder = document.createElement('div');
        holder.innerHTML = html;

        // Pages go inside whatever holds the other pages; modals go to the end
        // of the body, where every other overlay in the app lives — a modal
        // nested inside a hidden .page would never be visible.
        var first = document.querySelector('section.page');
        var container = first ? first.parentNode : document.body;
        Array.prototype.slice.call(holder.querySelectorAll('section.page')).forEach(function (page) {
          container.appendChild(page);
        });
        Array.prototype.slice.call(holder.querySelectorAll('.cell-overlay')).forEach(function (modal) {
          document.body.appendChild(modal);
          modal.addEventListener('click', function (e) { if (e.target === modal) closeAll(); });
        });
        _loaded = true;
      });
  }

  function closeAll() {
    closeStockForm();
    closeDeleteStockModal();
  }

  // A warehouse count is typed once, standing at the shelf. Clicking beside the
  // box or brushing Escape used to bin it silently, and the only copy of that
  // number was on the screen that just closed.
  function confirmDiscard() {
    if (!isDirty()) return true;
    return window.confirm(
      'You have typed something that is not saved yet.\n\n'
      + 'Press Cancel to go back and save it, or OK to throw it away.');
  }

  // ── sidebar group ────────────────────────────────────────────────────────

  function toggleNavGroup(name) {
    var head = get('nav-' + name + '-group');
    var body = get('nav-sub-' + name);
    if (!head || !body) return;
    var open = !body.classList.contains('open');
    body.classList.toggle('open', open);
    head.classList.toggle('open', open);
    try { localStorage.setItem('navGroup_' + name, open ? '1' : '0'); } catch (e) {}
  }

  // ── Stock: list ──────────────────────────────────────────────────────────

  function loadStock() {
    return mount()
      .then(function () { return authFetch('/api/stock'); })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          if (!r.ok) throw new Error(body.detail || body.error || ('Server error ' + r.status));
          if (!Array.isArray(body)) throw new Error('Unexpected reply from the server.');
          return body;
        });
      })
      .then(function (rows) { _stock = rows; renderStock(); })
      .catch(function (e) {
        console.error('loadStock', e);
        var el = get('stock-list-body');
        if (el) {
          el.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:2.5rem;color:var(--crit)">'
            + esc('Could not load stock — ' + e.message
                  + ' If this page is new, run migrations/create_stock.sql in Supabase.')
            + '</td></tr>';
        }
      });
  }

  function filterStockList() { _stockPage = 1; renderStock(); }

  function renderStock() {
    var body = get('stock-list-body');
    if (!body) return;
    var search = get('stock-search');
    var term = search ? search.value.trim().toLowerCase() : '';
    var list = term ? _stock.filter(function (x) {
      return (x.description || '').toLowerCase().indexOf(term) !== -1
          || (x.notes || '').toLowerCase().indexOf(term) !== -1
          || (x.stock_id || '').toLowerCase().indexOf(term) !== -1;
    }) : _stock;

    renderStockKpis(list);

    if (!list.length) {
      body.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:2.5rem;color:var(--t3)">'
        + (term ? 'Nothing matches your search.'
                : 'Nothing counted yet — press New Item to record what is on the shelf.')
        + '</td></tr>';
      if (typeof renderPgBar === 'function') {
        renderPgBar('stock-pagination', 1, 0, PER_PAGE, 'stk-prev', 'stk-next', function () {}, function () {});
      }
      return;
    }

    var pages = Math.max(1, Math.ceil(list.length / PER_PAGE));
    if (_stockPage > pages) _stockPage = 1;
    var start = (_stockPage - 1) * PER_PAGE;

    body.innerHTML = list.slice(start, start + PER_PAGE).map(function (x) {
      var value = (x.unit_cost != null && x.unit_cost !== '')
        ? money(num(x.quantity) * num(x.unit_cost)) : '—';
      var low = num(x.quantity) <= 0;
      return '<tr>'
        + '<td class="mono" style="font-size:12px;white-space:nowrap">' + esc(x.stock_id || '—') + '</td>'
        + '<td style="max-width:400px">' + esc(x.description || '—') + '</td>'
        + '<td style="text-align:right;font-variant-numeric:tabular-nums'
        +   (low ? ';color:var(--crit);font-weight:600' : '') + '">' + qty(x.quantity) + '</td>'
        + '<td>' + esc(x.uom || 'EA') + '</td>'
        + '<td style="text-align:right;font-variant-numeric:tabular-nums">' + value + '</td>'
        + '<td>' + day(x.last_counted_at) + '</td>'
        + '<td style="white-space:nowrap;text-align:right">'
        +   '<button class="act-btn" onclick="openStockForm(\'' + x.id + '\')">Edit</button> '
        +   (canDelete()
              ? '<button class="act-btn act-del" onclick="confirmDeleteStock(\'' + x.id + '\')">Del</button>'
              : '')
        + '</td>'
        + '</tr>';
    }).join('');

    if (typeof renderPgBar === 'function') {
      renderPgBar('stock-pagination', _stockPage, list.length, PER_PAGE, 'stk-prev', 'stk-next',
        function () { if (_stockPage > 1) { _stockPage--; renderStock(); } },
        function () { if (_stockPage < pages) { _stockPage++; renderStock(); } });
    }
  }

  // The class names are the app's own — kpi-grid lays them out four across,
  // and kpi-lbl/kpi-val/kpi-note style them exactly like the dashboard's.
  // An earlier version invented its own names, so the cards had no grid to sit
  // in and stacked down the page instead of spreading across it.
  function kpi(label, value, sub) {
    return '<div class="kpi"><div class="kpi-lbl">' + esc(label) + '</div>'
      + '<div class="kpi-val mono">' + value + '</div>'
      + (sub ? '<div class="kpi-note">' + esc(sub) + '</div>' : '') + '</div>';
  }

  function renderStockKpis(list) {
    var box = get('stock-kpis');
    if (!box) return;
    var units  = list.reduce(function (s, x) { return s + num(x.quantity); }, 0);
    var valued = list.filter(function (x) { return x.unit_cost != null && x.unit_cost !== ''; });
    var worth  = valued.reduce(function (s, x) { return s + num(x.quantity) * num(x.unit_cost); }, 0);
    var zero   = list.filter(function (x) { return num(x.quantity) <= 0; }).length;
    box.innerHTML =
        kpi('Items', String(list.length))
      + kpi('Total units', qty(units))
      + kpi('Value', money(worth), valued.length + ' of ' + list.length + ' priced')
      + kpi('At zero', String(zero));
  }

  // ── Products ─────────────────────────────────────────────────────────────

  function loadProducts() {
    return mount()
      .then(function () { return authFetch('/api/products'); })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          if (!r.ok) throw new Error(body.detail || body.error || ('Server error ' + r.status));
          if (!Array.isArray(body)) throw new Error('Unexpected reply from the server.');
          return body;
        });
      })
      .then(function (rows) { _products = rows; renderProducts(); })
      .catch(function (e) {
        console.error('loadProducts', e);
        var el = get('products-list-body');
        if (el) {
          el.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:2.5rem;color:var(--crit)">'
            + esc('Could not load products — ' + e.message) + '</td></tr>';
        }
      });
  }

  function filterProductsList() { _productsPage = 1; renderProducts(); }

  function renderProducts() {
    var body = get('products-list-body');
    if (!body) return;
    var search = get('products-search');
    var term = search ? search.value.trim().toLowerCase() : '';
    var list = term
      ? _products.filter(function (p) { return (p.part_code || '').toLowerCase().indexOf(term) !== -1; })
      : _products;

    var box = get('products-kpis');
    if (box) {
      var lines = list.reduce(function (s, p) { return s + (p.times_ordered || 0); }, 0);
      var units = list.reduce(function (s, p) { return s + num(p.total_qty); }, 0);
      var worth = list.reduce(function (s, p) { return s + num(p.total_qty) * num(p.last_price); }, 0);
      box.innerHTML = kpi('Parts', String(list.length))
        + kpi('Order lines', String(lines))
        + kpi('Units despatched', qty(units))
        + kpi('At last price', money(worth), 'what that volume is worth today');
    }

    if (!list.length) {
      body.innerHTML = '<tr><td colspan="7" style="text-align:center;padding:2.5rem;color:var(--t3)">'
        + (term ? 'No part matches your search.' : 'No parts yet — they appear as Flexitallic sales orders arrive.')
        + '</td></tr>';
      if (typeof renderPgBar === 'function') {
        renderPgBar('products-pagination', 1, 0, PER_PAGE, 'prd-prev', 'prd-next', function () {}, function () {});
      }
      return;
    }

    var pages = Math.max(1, Math.ceil(list.length / PER_PAGE));
    if (_productsPage > pages) _productsPage = 1;
    var start = (_productsPage - 1) * PER_PAGE;

    body.innerHTML = list.slice(start, start + PER_PAGE).map(function (p) {
      return '<tr>'
        + '<td class="mono" style="font-size:12px">' + esc(p.part_code) + '</td>'
        + '<td style="text-align:right;font-variant-numeric:tabular-nums">' + (p.times_ordered || 0) + '</td>'
        + '<td style="text-align:right;font-variant-numeric:tabular-nums">' + qty(p.total_qty) + '</td>'
        + '<td>' + esc(p.uom || 'EA') + '</td>'
        + '<td style="text-align:right;font-variant-numeric:tabular-nums">'
        +   (p.last_price != null ? money(p.last_price) : '—') + '</td>'
        + '<td>' + esc(p.last_ordered || '—') + '</td>'
        + '<td class="mono" style="font-size:12px">' + esc(p.last_so || '—') + '</td>'
        + '</tr>';
    }).join('');

    if (typeof renderPgBar === 'function') {
      renderPgBar('products-pagination', _productsPage, list.length, PER_PAGE, 'prd-prev', 'prd-next',
        function () { if (_productsPage > 1) { _productsPage--; renderProducts(); } },
        function () { if (_productsPage < pages) { _productsPage++; renderProducts(); } });
    }
  }

  // ── the form ─────────────────────────────────────────────────────────────

  // The number the next item will be given, worked out from the list already
  // on screen. The server issues the real one on save and its answer wins —
  // this is a preview, so that describing something shows you its label
  // straight away rather than after a round trip.
  function nextStockId() {
    var highest = 0;
    _stock.forEach(function (x) {
      var n = parseInt(x.stock_seq, 10);
      if (!isNaN(n) && n > highest) highest = n;
    });
    return 'STK-' + String(highest + 1).padStart(4, '0');
  }

  // An ID belongs to a thing, and until it is described there is no thing.
  function onStockDescription() {
    var box = get('sf-stock-id');
    if (!box || _editingId) return;              // a saved item keeps its own
    box.value = val('sf-description') ? nextStockId() : '';
  }

  function formError(message) {
    var box = get('stock-form-err');
    if (!box) return;
    if (!message) { box.style.display = 'none'; return; }
    box.textContent = message;
    box.style.display = 'block';
  }

  function openStockForm(itemId) {
    mount().then(function () {
      _editingId = itemId || null;
      _editingQty = 0;
      formError('');
      set('sf-stock-id', '');
      set('sf-description', ''); set('sf-quantity', '0'); set('sf-uom', 'EA');
      set('sf-unit-cost', ''); set('sf-notes', ''); set('sf-move-note', '');
      var noteRow = get('sf-note-row'); if (noteRow) noteRow.style.display = 'none';
      var histWrap = get('sf-history-wrap'); if (histWrap) histWrap.style.display = 'none';
      var title = get('stock-form-title');
      if (title) title.textContent = itemId ? 'Edit stock item' : 'New stock item';

      var modal = get('stock-form-modal');
      if (modal) modal.classList.remove('hidden');

      if (!itemId) {
        _openSnapshot = snapshot();
        var d = get('sf-description'); if (d) { try { d.focus(); } catch (e) {} }
        return;
      }

      authFetch('/api/stock/' + itemId)
        .then(function (r) {
          return r.json().catch(function () { return {}; }).then(function (body) {
            if (!r.ok) throw new Error(body.detail || body.error || ('Server error ' + r.status));
            return body;
          });
        })
        .then(function (item) {
          _editingQty = num(item.quantity);
          set('sf-stock-id',    item.stock_id || '');
          set('sf-description', item.description || '');
          set('sf-quantity',    qty(item.quantity));
          set('sf-uom',         item.uom || 'EA');
          set('sf-unit-cost',   item.unit_cost != null ? item.unit_cost : '');
          set('sf-notes',       item.notes || '');
          renderHistory(item.movements || []);
          _openSnapshot = snapshot();
        })
        .catch(function (e) { formError('Could not load this item: ' + e.message); });
    }).catch(function (e) { console.error('openStockForm', e); });
  }

  function renderHistory(moves) {
    var wrap = get('sf-history-wrap');
    var box  = get('sf-history');
    if (!wrap || !box) return;
    if (!moves.length) { wrap.style.display = 'none'; return; }
    wrap.style.display = '';
    box.innerHTML = moves.map(function (m) {
      var delta = num(m.change);
      return '<div class="sf-move">'
        + '<span class="sf-move-delta ' + (delta < 0 ? 'sf-move-down' : 'sf-move-up') + '">'
        +   (delta > 0 ? '+' : '') + qty(delta) + '</span>'
        + '<span class="sf-move-note">' + esc(m.note || 'adjusted')
        +   (m.created_by ? ' · ' + esc(m.created_by) : '') + '</span>'
        + '<span class="sf-move-when">' + day(m.created_at) + '</span>'
        + '</div>';
    }).join('');
  }

  function closeStockForm(force) {
    if (!force && !confirmDiscard()) return;
    var modal = get('stock-form-modal');
    if (modal) modal.classList.add('hidden');
    _editingId = null;
    _openSnapshot = '';
  }

  function saveStockItem() {
    if (!val('sf-description')) {
      formError('A description is required — it is what the count is against.');
      var d = get('sf-description'); if (d) { try { d.focus(); } catch (e) {} }
      return;
    }
    if (val('sf-quantity') === '') {
      formError('Enter the quantity you counted. If there are none left, type 0.');
      var q = get('sf-quantity'); if (q) { try { q.focus(); } catch (e) {} }
      return;
    }
    formError('');

    var payload = {
      description: val('sf-description'),
      quantity:    val('sf-quantity'),        // sent raw: the server decides
      // What the count read when this form was opened. The server refuses the
      // save if the stored figure has moved since, rather than quietly wiping
      // somebody else's count.
      seen_quantity: _editingId ? _editingQty : null,
      uom:         val('sf-uom') || 'EA',
      unit_cost:   val('sf-unit-cost') === '' ? null : num(val('sf-unit-cost')),
      notes:       val('sf-notes'),
      note:        val('sf-move-note'),
    };

    var btn = get('sf-save-btn');
    if (btn) { btn.disabled = true; btn.textContent = 'Saving…'; }

    var url    = _editingId ? '/api/stock/' + _editingId : '/api/stock';
    var method = _editingId ? 'PUT' : 'POST';

    authFetch(url, {
      method: method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          if (!r.ok) {
            var err = new Error(body.detail || body.error || ('Server error ' + r.status));
            err.status = r.status;
            throw err;
          }
          return body;
        });
      })
      .then(function (body) {
        closeStockForm(true);
        loadStock();
        // The count is saved either way; the history entry is what may be
        // missing, and the person who took the count should hear about it.
        if (body && body.logged === false) {
          window.alert('Saved — but the change could not be added to the item\'s '
                     + 'history. The count itself is recorded.');
        }
      })
      .catch(function (e) {
        // Never close on a failure — what is typed may be the only record of
        // a count somebody just walked the warehouse to take.
        if (e.status === 409) {
          // Pressing Save again would clash again; the fix is to look at the
          // other person's count first.
          formError(e.message);
        } else if (e.status === 400) {
          formError(e.message);            // already says exactly what to fix
        } else {
          formError('NOT saved — ' + e.message
                    + ' Your entry is still here; press Save again.');
        }
      })
      .finally(function () {
        if (btn) { btn.disabled = false; btn.textContent = 'Save'; }
      });
  }

  // Asking why the count changed, but only when it actually changed.
  function watchQuantity() {
    var input = get('sf-quantity');
    var row   = get('sf-note-row');
    if (!input || !row) return;
    var changed = _editingId && Math.abs(num(input.value) - _editingQty) > 1e-9;
    row.style.display = changed ? '' : 'none';
  }

  // ── delete ───────────────────────────────────────────────────────────────

  function confirmDeleteStock(itemId) {
    _deletingId = itemId;
    var item = _stock.filter(function (x) { return x.id === itemId; })[0];
    var name = get('ds-name');
    if (name) name.textContent = (item && item.description) || 'this item';
    var err = get('ds-error'); if (err) err.style.display = 'none';
    var btn = get('ds-confirm-btn'); if (btn) { btn.disabled = false; btn.textContent = 'Delete'; }
    var modal = get('delete-stock-modal'); if (modal) modal.classList.remove('hidden');
  }

  function closeDeleteStockModal() {
    _deletingId = null;
    var modal = get('delete-stock-modal');
    if (modal) modal.classList.add('hidden');
  }

  function doDeleteStockItem() {
    if (!_deletingId) return;
    var btn = get('ds-confirm-btn');
    var err = get('ds-error');
    if (btn) { btn.disabled = true; btn.textContent = 'Deleting…'; }
    authFetch('/api/stock/' + _deletingId, { method: 'DELETE' })
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          if (!r.ok) throw new Error(body.detail || body.error || ('Server error ' + r.status));
          return body;
        });
      })
      .then(function () { closeDeleteStockModal(); loadStock(); })
      .catch(function (e) {
        if (btn) { btn.disabled = false; btn.textContent = 'Delete'; }
        if (err) { err.textContent = 'Delete failed: ' + e.message; err.style.display = ''; }
      });
  }

  // ── wiring ───────────────────────────────────────────────────────────────

  // The inline handlers in inventory.html and the sidebar rows in index.html
  // call these by name, so they have to sit on window. Everything else above
  // stays private to this file.
  window.toggleNavGroup        = toggleNavGroup;
  window.loadStock             = loadStock;
  window.filterStockList       = filterStockList;
  window.openStockForm         = openStockForm;
  window.onStockDescription    = onStockDescription;
  window.closeStockForm        = closeStockForm;
  window.saveStockItem         = saveStockItem;
  window.confirmDeleteStock    = confirmDeleteStock;
  window.closeDeleteStockModal = closeDeleteStockModal;
  window.doDeleteStockItem     = doDeleteStockItem;
  window.loadProducts          = loadProducts;
  window.filterProductsList    = filterProductsList;

  function ready(fn) {
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', fn);
    else fn();
  }

  ready(function () {
    // showPage lives in script.js and knows nothing about stock. Rather than
    // edit it, wrap it: the original still runs, and these two pages get their
    // data loaded on the way past.
    var original = window.showPage;
    if (typeof original === 'function') {
      window.showPage = function (name) {
        original.apply(this, arguments);
        if (name === 'stock')    loadStock();
        if (name === 'products') loadProducts();
      };
    }

    // The sidebar group remembers whether it was left open.
    try {
      if (localStorage.getItem('navGroup_inventory') === '1') toggleNavGroup('inventory');
    } catch (e) {}

    // Typing a new count reveals the "reason" box.
    document.addEventListener('input', function (e) {
      if (e.target && e.target.id === 'sf-quantity') watchQuantity();
    });

    // Escape closes whichever of these is open, like the app's other modals.
    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape') return;
      var form = get('stock-form-modal');
      var del  = get('delete-stock-modal');
      if (del && !del.classList.contains('hidden')) closeDeleteStockModal();
      else if (form && !form.classList.contains('hidden')) closeStockForm();
      // closeStockForm asks first when there is unsaved work in the box.
    });

    // The last line of defence: reloading or closing the tab with a count
    // half-typed gets the browser's own "leave site?" prompt.
    window.addEventListener('beforeunload', function (e) {
      if (!isDirty()) return;
      e.preventDefault();
      e.returnValue = '';
    });

    // Pull the fragment in straight away so the pages exist before they are
    // first opened; a failure here is not fatal, loadStock will try again.
    mount().catch(function (e) { console.error('inventory: fragment not loaded', e); });
  });

})();
