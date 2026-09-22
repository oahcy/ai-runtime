"""Browser state subscription API. Transport implementation is private to Runtime."""
CLIENT_JS = r"""
(() => {
  const subscribers = new Set(), statusListeners = new Set();
  let socket = null, timer = null, latest = null, status = 'idle', delay = 500, blocked = false;
  const notify = (fn, ...args) => { try { fn(...args); } catch (e) { console.error('Preview subscriber failed', e); } };
  const setStatus = value => { status = value; for (const fn of statusListeners) notify(fn, value); };
  const active = () => subscribers.size + statusListeners.size > 0;
  function stop() {
    clearTimeout(timer); timer = null;
    const old = socket; socket = null;
    if (old) old.close();
    setStatus('idle');
  }
  function connect() {
    if (socket || timer || blocked || !active()) return;
    const token = location.hash.slice(1);
    if (!token) { blocked = true; setStatus('unauthorized'); return; }
    setStatus('connecting');
    const ws = new WebSocket((location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + (location.pathname || '/').replace(/\/?$/, '/') + 'ws');
    socket = ws;
    let revision = -1;
    ws.onopen = () => { if (socket === ws) ws.send(JSON.stringify({token})); };
    ws.onmessage = event => {
      if (socket !== ws) return;
      let data;
      try { data = JSON.parse(event.data); } catch { return; }
      if (!data || !Number.isInteger(data.revision) || data.revision < 0 ||
          !data.state || typeof data.state !== 'object' || Array.isArray(data.state) ||
          !Array.isArray(data.history) || data.revision <= revision) return;
      revision = data.revision;
      latest = data; delay = 500; setStatus('connected');
      for (const fn of subscribers) notify(fn, data.state, {revision:data.revision, history:data.history});
    };
    ws.onclose = event => {
      if (socket !== ws) return;
      socket = null;
      if (event.code === 1008) { blocked = true; setStatus('unauthorized'); return; }
      setStatus('disconnected');
      if (active()) {
        timer = setTimeout(() => { timer = null; connect(); }, delay);
        delay = Math.min(delay * 2, 5000);
      }
    };
    ws.onerror = () => ws.close();
  }
  function register(set, fn) {
    if (typeof fn !== 'function') throw new TypeError('Expected a callback');
    set.add(fn);
    if (set === statusListeners) notify(fn, status);
    else if (latest) notify(fn, latest.state, {revision:latest.revision, history:latest.history});
    connect();
    return () => { set.delete(fn); if (!active()) stop(); };
  }
  globalThis.RuntimePreview = Object.freeze({
    subscribe: fn => register(subscribers, fn),
    onStatus: fn => register(statusListeners, fn)
  });
  addEventListener('pagehide', stop);
  addEventListener('pageshow', connect);
  addEventListener('hashchange', () => { blocked = false; latest = null; stop(); connect(); });
})();
"""
