// Run with: node --test tests/loops_player.test.cjs
// Exercise async player lifecycle without fetching remote sites.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

function harness() {
  const pending = [];
  const events = {};
  const frames = [];
  const document = { addEventListener() {}, documentElement: { dataset: {} },
    adoptNode: (node) => node,
    createElement: () => {
      const frame = { setAttribute() {}, addEventListener() {}, contentWindow: { postMessage() {} } };
      frames.push(frame);
      return frame;
    } };
  const slot = { innerHTML: 'poster', replaceChildren(box) { this.box = box; } };
  const el = { dataset: { loop: '/video?url=test' }, isConnected: true, slot,
    hasAttribute: () => !!el.onScreen };
  const player = { dataset: { embed: 'https://vimeo.com/123' },
    closest: (sel) => sel === 'article.loop' ? el : null,
    matches: () => false, replaceChildren() {} };
  slot.player = player;
  const box = { id: 'video-file-test' };
  const context = vm.createContext({ document, window: {}, URL, console,
    addEventListener: (event, fn) => { events[event] = fn; },
    ResizeObserver: class { observe() {} unobserve() {} },
    matchMedia: () => ({ matches: false }),
    watchSaving() {}, stopVideos() {},
    fetchDoc: () => new Promise((resolve, reject) => pending.push({ resolve, reject })),
    $: (sel, root) => {
      if (sel.startsWith('.loop-stage')) return root.veiled ? {} : null;
      if (sel === '.loop-slot') return root.slot;
      if (sel === 'article.video-box') return root.box || null;
      return null;
    },
    $$: (sel, root) => {
      if (sel.startsWith('article.live-box')) {
        const target = root === document ? slot : root;
        return target.player && !target.player.dataset.started ? [target.player] : [];
      }
      if (sel === 'video') return root.videos || [];
      if (sel === 'article.loop iframe') return frames;
      return [];
    } });
  const source = fs.readFileSync(path.join(__dirname, '../threadbnc/static/app.js'), 'utf8');
  const part = source.slice(source.indexOf('  let loopNear = null;'), source.indexOf('  const askedHosts = new Set();'));
  vm.runInContext(part + '\nthis.api = { loadLoop, unloadLoop, playLoop, startPlayers };', context);
  return { ...context.api, el, slot, player, box, pending, frames, events };
}

test('a missing player response can be retried', async () => {
  const h = harness();
  const first = h.loadLoop(h.el);
  h.pending[0].resolve({});
  await first;
  assert.equal(h.el.dataset.loaded, undefined);
  const second = h.loadLoop(h.el);
  h.pending[1].resolve({ box: h.box });
  await second;
  assert.equal(h.slot.box, h.box);
});

test('a stale response cannot replace a newer load after scrolling away and back', async () => {
  const h = harness();
  const first = h.loadLoop(h.el);
  h.unloadLoop(h.el);
  const second = h.loadLoop(h.el);
  h.pending[0].resolve({ box: { id: 'stale' } });
  await first;
  assert.equal(h.slot.box, undefined);
  h.pending[1].resolve({ box: h.box });
  await second;
  assert.equal(h.slot.box, h.box);
});

test('an older rejected request cannot cancel a newer request', async () => {
  const h = harness();
  const first = h.loadLoop(h.el);
  h.unloadLoop(h.el);
  const second = h.loadLoop(h.el);
  h.pending[0].reject(new Error('network'));
  await first;
  assert.equal(h.el.dataset.loaded, 'loading');
  h.pending[1].resolve({ box: h.box });
  await second;
  assert.equal(h.el.dataset.loaded, '1');
});

test('sensitive posts do not fetch players before being revealed', async () => {
  const h = harness();
  h.el.veiled = true;
  await h.loadLoop(h.el, true);
  assert.equal(h.pending.length, 0);
});

test('nearby players stay unloaded until visible, and can restart when revisited', () => {
  const h = harness();
  h.startPlayers();
  assert.equal(h.frames.length, 0);
  h.el.onScreen = true;
  h.playLoop(h.el, true);
  assert.equal(h.frames.length, 1);
  // Offscreen generic embeds are removed by playLoop(false); revisiting starts them again.
  delete h.player.dataset.started;
  h.playLoop(h.el, true);
  assert.equal(h.frames.length, 2);
});

test('clicking a loaded player starts it without a second fetch', async () => {
  const h = harness();
  let plays = 0;
  const video = { dataset: {}, play: () => { plays++; return Promise.resolve(); }, pause() {} };
  h.slot.videos = [video];
  h.el.dataset.loaded = '1';
  await h.loadLoop(h.el, true);
  assert.equal(plays, 1);
  assert.equal(video.muted, true);
  assert.equal(h.pending.length, 0);
});
