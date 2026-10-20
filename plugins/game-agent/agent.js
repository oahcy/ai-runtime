module.exports = function start(config) {
    'use strict';
    const g = globalThis;
    if (g.__AI_RUNTIME_AGENT__) throw Error('Agent already loaded; call __AI_RUNTIME_AGENT__.close() first');
    if (!g.wx || typeof g.wx.connectSocket !== 'function') throw Error('wx.connectSocket required');
    if (!config || typeof config.url !== 'string' || typeof config.token !== 'string') throw Error('url and token required');
    const task = g.wx.connectSocket({url: config.url});
    if (!task || typeof task.onMessage !== 'function') throw Error('Independent SocketTask required');
    let closed = false, ready = false;
    const originals = [], cache = new Map();
    function send(row) {
        if (closed) return;
        let data;
        try { data = JSON.stringify(row); } catch (_) { return; }
        if (data.length > 20000) data = JSON.stringify({type: 'result', id: row.id, error: 'Result exceeds transport budget'});
        try { task.send({data, fail: function () {}}); } catch (_) {}
    }
    const objects = new Map(), ids = new WeakMap(), roots = Object.create(null);
    let serial = 0;
    function pack(value) {
        if (value !== null && (typeof value === 'object' || typeof value === 'function')) {
            let id = ids.get(value);
            if (!id || !objects.has(id)) {
                if (objects.size >= 2048) throw Error('Object limit reached; release unused handles');
                id = 'o' + (++serial); objects.set(id, value); ids.set(value, id);
            }
            return {type: typeof value, $ref: id, array: Array.isArray(value)};
        }
        if (typeof value === 'undefined' || typeof value === 'bigint' || typeof value === 'symbol' ||
            (typeof value === 'number' && !Number.isFinite(value))) return {type: typeof value, text: String(value)};
        return {type: typeof value, value: typeof value === 'string' ? value.slice(0, 8000) : value};
    }
    function resolve(id) {
        if (typeof id !== 'string' || !objects.has(id)) throw Error('Unknown object handle');
        return objects.get(id);
    }
    function key(name) {
        if (typeof name !== 'string' || ['__proto__', 'prototype', 'constructor'].includes(name)) throw Error('Invalid property');
        return name;
    }
    function argument(v) {
        // Only explicit envelopes are accepted; plain JSON objects cannot impersonate handles.
        if (!v || typeof v !== 'object') throw Error('Use {value: JSON} or {$ref: id}');
        if (Object.keys(v).length === 1 && typeof v.$ref === 'string') return resolve(v.$ref);
        if (Object.keys(v).length === 1 && Object.prototype.hasOwnProperty.call(v, 'value')) return v.value;
        throw Error('Invalid argument envelope');
    }
    async function operate(c) {
        if (closed) throw Error('Agent closed');
        if (!c || typeof c !== 'object') throw Error('Command required');
        if (c.op === 'roots') return Object.keys(roots).map(name => ({name, object: pack(roots[name])}));
        if (c.op === 'release') {
            resolve(c.object); objects.delete(c.object); return {released: true};
        }
        const object = resolve(c.object);
        if (c.op === 'get') return pack(object[key(c.property)]);
        if (c.op === 'set') { object[key(c.property)] = argument(c.value); return {written: true}; }
        if (c.op === 'call') {
            const fn = object[key(c.method)];
            if (typeof fn !== 'function') throw Error('Property is not callable');
            if (!Array.isArray(c.args) || c.args.length > 64) throw Error('args must be an array of at most 64 arguments');
            return pack(await Reflect.apply(fn, object, c.args.map(argument)));
        }
        if (c.op === 'keys') {
            const offset = c.offset === undefined ? 0 : c.offset;
            if (!Number.isInteger(offset) || offset < 0) throw Error('Invalid offset');
            const names = Object.getOwnPropertyNames(object);
            return {keys: names.slice(offset, offset + 100), total: names.length, next: offset + 100 < names.length ? offset + 100 : null};
        }
        throw Error('Unsupported operation');
    }
    const api = {target_id: null, register: function (name, value) {
        if (typeof name !== 'string' || !name || name.length > 100) throw Error('Invalid root name');
        if (!Object.prototype.hasOwnProperty.call(roots, name) && Object.keys(roots).length >= 64) throw Error('Root limit reached');
        roots[name] = value;
    }, close: function () {
        if (closed) return;
        closed = true; ready = false; objects.clear(); cache.clear();
        for (const name of Object.keys(roots)) delete roots[name];
        for (const [name, original, wrapper] of originals) if (console[name] === wrapper) console[name] = original;
        if (g.__AI_RUNTIME_AGENT__ === api) delete g.__AI_RUNTIME_AGENT__;
        try { task.close({code: 1000, reason: 'Agent stopped'}); } catch (_) {}
    }};
    g.__AI_RUNTIME_AGENT__ = api;
    for (const name of Object.keys(config.roots || {})) api.register(name, config.roots[name]);
    task.onOpen(() => send({type: 'hello', token: config.token}));
    const reportError = console.error.bind(console);
    task.onClose(event => {
        if (!closed) reportError('[ai-runtime] Socket closed', event && event.code, event && event.reason);
        api.close();
    });
    task.onError(event => {
        reportError('[ai-runtime] Socket error', event && event.errMsg || event);
        api.close();
    });
    task.onMessage(function (event) {
        let row;
        try { row = JSON.parse(event.data); } catch (_) { return; }
        if (!row || typeof row !== 'object' || closed) return;
        if (row.type === 'welcome') {
            if (ready) return;
            ready = true; api.target_id = row.target_id;
            let logWindow = Date.now(), logCount = 0;
            for (const name of ['log', 'info', 'warn', 'error']) {
                const original = console[name];
                if (typeof original !== 'function') continue;
                const wrapper = function (...args) {
                    original.apply(console, args);
                    if (Date.now() - logWindow >= 1000) { logWindow = Date.now(); logCount = 0; }
                    if (++logCount > 20) return;
                    try { send({type: 'log', level: name, message: JSON.stringify(args.map(pack)).slice(0, 4000)}); } catch (_) {}
                };
                try { console[name] = wrapper; originals.push([name, original, wrapper]); } catch (_) {}
            }
            return;
        }
        if (!ready || row.type !== 'command' || typeof row.id !== 'string') return;
        if (cache.has(row.id)) { cache.get(row.id).then(send); return; }
        if (cache.size >= 256) { send({type: 'result', id: row.id, error: 'Command limit reached; reconnect Agent'}); return; }
        // Retain request IDs for this connection; never execute a duplicate mutation.
        const result = Promise.resolve().then(() => operate(row.command))
            .then(value => ({type: 'result', id: row.id, value: value}))
            .catch(error => ({type: 'result', id: row.id, error: String(error && error.stack || error).slice(0, 8000)}));
        cache.set(row.id, result); result.then(send);
    });
    return api;
};
