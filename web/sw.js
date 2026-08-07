'use strict';
/* SPMprocure360 Service Worker
   Polls /api/alerts every 90s and fires OS notifications.
   Works while browser is open, even when the SPM tab is closed. */

var CACHE_NAME = 'spm-sw-v2';
var _token = null;
var _seen  = {};

// ── Cache helpers (persist across SW restarts) ────────────────────────────────
function persist(key, val) {
  return caches.open(CACHE_NAME).then(function(c) {
    return c.put('/__sw/' + key,
      new Response(JSON.stringify(val), {headers: {'Content-Type': 'application/json'}}));
  });
}
function recall(key) {
  return caches.open(CACHE_NAME)
    .then(function(c) { return c.match('/__sw/' + key); })
    .then(function(r) { return r ? r.json() : null; });
}

// ── Lifecycle ─────────────────────────────────────────────────────────────────
self.addEventListener('install', function(e) { self.skipWaiting(); });

self.addEventListener('activate', function(e) {
  e.waitUntil(
    clients.claim().then(function() {
      return Promise.all([
        recall('token').then(function(d) { if (d && d.v) _token = d.v; }),
        recall('seen').then(function(d)  { if (d) _seen = d; })
      ]);
    }).then(function() {
      // Kick off polling loop on activation
      schedulePoll();
    })
  );
});

// ── Message from main page ────────────────────────────────────────────────────
self.addEventListener('message', function(e) {
  if (!e.data) return;
  if (e.data.type === 'SPM_TOKEN') {
    _token = e.data.token;
    persist('token', {v: _token});
  }
  if (e.data.type === 'SPM_POLL') {
    poll();
  }
});

// ── Periodic background sync (Chrome/Edge) ────────────────────────────────────
self.addEventListener('periodicsync', function(e) {
  if (e.tag === 'spm-alerts') e.waitUntil(poll());
});

// ── Notification click → open/focus the app ───────────────────────────────────
self.addEventListener('notificationclick', function(e) {
  e.notification.close();
  e.waitUntil(
    clients.matchAll({type: 'window', includeUncontrolled: true}).then(function(list) {
      for (var i = 0; i < list.length; i++) {
        if ('focus' in list[i]) { list[i].focus(); return; }
      }
      return clients.openWindow('/');
    })
  );
});

// ── Polling loop ──────────────────────────────────────────────────────────────
var _pollTimer = null;

function schedulePoll() {
  if (_pollTimer) return; // already running
  poll();
  _pollTimer = setInterval(poll, 90 * 1000); // every 90 seconds
}

function poll() {
  var tok = _token;
  if (!tok) {
    return recall('token').then(function(d) {
      if (d && d.v) { _token = d.v; return _doPoll(); }
    });
  }
  return _doPoll();
}

function _doPoll() {
  if (!_token) return;
  return fetch('/api/alerts', {
    headers: {'Authorization': _token, 'Accept': 'application/json'}
  })
  .then(function(r) {
    if (r.status === 401) { _token = null; persist('token', {v: null}); return null; }
    return r.ok ? r.json() : null;
  })
  .then(function(data) {
    if (!data) return;
    return recall('seen').then(function(saved) {
      _seen = saved || {};
      var notifPromises = [];

      // ── Critical/Overdue OTD ───────────────────────────────────────────────
      (data.critical || []).forEach(function(o) {
        var k = 'crit-' + o.id;
        if (_seen[k]) return;
        _seen[k] = Date.now();
        var overdue = o.days_left < 0;
        notifPromises.push(self.registration.showNotification(
          (overdue ? '🚨 OVERDUE' : '⚠️ CRITICAL') + ' — ' + (o.po || 'PO'),
          {
            body: overdue
              ? 'Overdue by ' + Math.abs(o.days_left) + ' day' + (Math.abs(o.days_left) !== 1 ? 's' : '') + '. Immediate action required.'
              : o.days_left + ' day' + (o.days_left !== 1 ? 's' : '') + ' to deadline. Delivery at risk.',
            tag: k,
            requireInteraction: true,
            vibrate: [300, 100, 300, 100, 300],
            data: {url: '/'}
          }
        ));
        // Tell open clients to play sound
        clients.matchAll({type: 'window'}).then(function(list) {
          list.forEach(function(c) { c.postMessage({type: 'SPM_ALERT_SOUND', level: 'critical'}); });
        });
      });

      // ── New POs ────────────────────────────────────────────────────────────
      (data.new_pos || []).forEach(function(o) {
        var k = 'newpo-' + o.id;
        if (_seen[k]) return;
        _seen[k] = Date.now();
        notifPromises.push(self.registration.showNotification(
          '📦 New PO — ' + (o.po || 'Unknown'),
          {
            body: (o.buyer ? 'From ' + o.buyer + '. ' : '') + 'New purchase order requires acknowledgement.',
            tag: k,
            requireInteraction: false,
            data: {url: '/'}
          }
        ));
        clients.matchAll({type: 'window'}).then(function(list) {
          list.forEach(function(c) { c.postMessage({type: 'SPM_ALERT_SOUND', level: 'info'}); });
        });
      });

      // ── Delivery Requests ──────────────────────────────────────────────────
      (data.delivery_requests || []).forEach(function(o) {
        var k = 'delreq-' + o.id;
        if (_seen[k]) return;
        _seen[k] = Date.now();
        notifPromises.push(self.registration.showNotification(
          '🚚 Delivery Request — ' + (o.po || 'Unknown'),
          {
            body: 'Warehouse dispatch has been requested. Please confirm.',
            tag: k,
            requireInteraction: false,
            data: {url: '/'}
          }
        ));
        clients.matchAll({type: 'window'}).then(function(list) {
          list.forEach(function(c) { c.postMessage({type: 'SPM_ALERT_SOUND', level: 'info'}); });
        });
      });

      notifPromises.push(persist('seen', _seen));
      return Promise.all(notifPromises);
    });
  })
  .catch(function() { /* network error — retry next cycle */ });
}
