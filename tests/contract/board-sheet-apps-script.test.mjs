// The phone board web app (BRIEF-025, CR-rg-board-sheet): the REAL apps-script/Code.gs and Page.html, run under node
// over the in-memory Apps Script stand-in (tests/fixtures/board/gas-fake.mjs). AB2 secret, AB3 reason rule, AB4 no
// overwrite, AB6 page; plus parity of Code.gs with the TypeScript contract over the shared case file.
//   node --test tests/contract/board-sheet-apps-script.test.mjs

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import { loadBoard } from '../fixtures/board/gas-fake.mjs';
import {
  SHEET_COLUMNS,
  SHEET_DECISIONS,
  SECRET_PROPERTY,
  SHEET_NAME,
} from '../../contracts/rolegenie/board-sheet.ts';

const cases = JSON.parse(
  readFileSync(new URL('../fixtures/board/contract-cases.json', import.meta.url), 'utf8'),
);
const SECRET = 'test-secret-Ab3_xYz9';
const row = (over = {}) => ({
  uid: '0f3a9c1e5b7d2a46',
  title: 'Platform Specialist',
  company: 'Example Labs',
  url: '',
  score100: 61.25,
  unmetHard: 'r2 Five years of Kubernetes in production\nr4 Security clearance',
  confirmedGaps: 'r4 Security clearance',
  missingEvidence: 'r2 Five years of Kubernetes in production',
  uncertain: 'r7 Some travel',
  assessedAt: '2026-11-04T09:00:00',
  ...over,
});
const fresh = () => loadBoard({ props: { [SECRET_PROPERTY]: SECRET }, sheets: {} });
const seeded = () => {
  const b = fresh();
  assert.equal(b.post({ action: 'push', secret: SECRET, rows: [row()] }).ok, true);
  return b;
};
const cells = (b) => b.sheets[SHEET_NAME].rows;

test('Code.gs mirrors the contract: columns, decisions, decisionProblems cases', () => {
  const b = fresh();
  assert.deepEqual(b.call('columns_'), [...SHEET_COLUMNS]);
  assert.deepEqual(b.call('decisions_'), [...SHEET_DECISIONS]);
  for (const c of cases.decisionProblems)
    assert.deepEqual(b.call('decisionProblems_', c.d), c.problems, JSON.stringify(c.d));
});

test('AB2: push, pull and decide without the right secret are refused and return no data', () => {
  const bad = [undefined, '', 'wrong', SECRET + ' ', SECRET.toUpperCase()];
  for (const secret of bad) {
    const b = seeded();
    const before = JSON.stringify(b.state());
    for (const req of [
      { action: 'push', secret, rows: [row({ title: 'Injected' })] },
      { action: 'pull', secret },
      { action: 'decide', secret, uid: row().uid, decision: 'consider', reason: '' },
    ]) {
      const res = b.post(req);
      assert.deepEqual(res, { ok: false, error: 'refused' }, `${req.action} with ${secret}`);
      const page = b.pageDecide({ ...req, action: 'decide' });
      assert.deepEqual(page, { ok: false, error: 'refused' });
    }
    assert.equal(JSON.stringify(b.state()), before, 'a refused request changed the sheet');
    for (const s of [undefined, '', 'wrong']) {
      const out = b.get(s === undefined ? {} : { s });
      assert.doesNotMatch(out.content, /Platform Specialist|Example Labs|Kubernetes/);
      assert.match(out.content, /Not authorised/);
    }
  }
});

test('AB2: with no secret configured in Script Properties, everything is refused (even an empty secret)', () => {
  const b = loadBoard({ props: {}, sheets: {} });
  for (const secret of [undefined, '', 'null'])
    for (const action of ['push', 'pull', 'decide'])
      assert.deepEqual(b.post({ action, secret, rows: [row()] }), { ok: false, error: 'refused' });
  assert.match(b.get({ s: '' }).content, /Not authorised/);
  assert.equal(b.sheets[SHEET_NAME], undefined);
});

test('AB2: malformed body and unknown action are refused', () => {
  const b = seeded();
  assert.deepEqual(b.post('not json'), { ok: false, error: 'refused' });
  assert.equal(b.post({ action: 'drop', secret: SECRET }).ok, false);
});

test('AB3: Apply or Reject without a reason is refused; Consider and Already applied need none', () => {
  const b = seeded();
  for (const decision of ['apply', 'reject'])
    for (const reason of ['', '   ', undefined]) {
      const res = b.post({ action: 'decide', secret: SECRET, uid: row().uid, decision, reason });
      assert.deepEqual(res, { ok: false, error: `${decision} needs a reason` });
    }
  assert.equal(cells(b)[1][10], '', 'a refused tap was recorded');
  for (const decision of ['consider', 'applied-externally']) {
    const res = b.post({ action: 'decide', secret: SECRET, uid: row().uid, decision, reason: '' });
    assert.equal(res.ok, true, decision);
    assert.equal(res.decision, decision);
  }
  const ok = b.pageDecide({
    secret: SECRET,
    uid: row().uid,
    decision: 'reject',
    reason: 'five days on site',
  });
  assert.equal(ok.ok, true);
  assert.deepEqual(cells(b)[1].slice(10, 12), ['reject', 'five days on site']);
  assert.equal(
    b.post({ action: 'decide', secret: SECRET, uid: 'nope', decision: 'consider', reason: '' }).ok,
    false,
  );
});

test('AB4: pushing a job again updates its assessment columns and keeps the recorded decision and reason', () => {
  const b = seeded();
  const d = b.post({
    action: 'decide',
    secret: SECRET,
    uid: row().uid,
    decision: 'apply',
    reason: 'strong platform match',
  });
  assert.equal(d.ok, true);
  const res = b.post({
    action: 'push',
    secret: SECRET,
    rows: [row({ score100: 80, unmetHard: '' }), row({ uid: 'b1', title: 'Second role' })],
  });
  assert.deepEqual(res, { ok: true, added: 1, updated: 1, docsVersion: 1 });
  const rows = cells(b);
  assert.deepEqual(rows[0], [...SHEET_COLUMNS]);
  assert.equal(rows.length, 3);
  assert.equal(rows[1][4], '80.0');
  assert.equal(rows[1][5], '');
  assert.deepEqual(rows[1].slice(10, 13), ['apply', 'strong platform match', d.decidedAt]);
  assert.deepEqual(rows[2].slice(10, 13), ['', '', '']);
  const pulled = b.post({ action: 'pull', secret: SECRET });
  assert.deepEqual(pulled, {
    ok: true,
    decisions: [
      {
        uid: row().uid,
        decision: 'apply',
        reason: 'strong platform match',
        decidedAt: d.decidedAt,
      },
    ],
    historyVersion: 1,
    events: [
      {
        eventId: d.eventId,
        requestId: '',
        uid: row().uid,
        decision: 'apply',
        reason: 'strong platform match',
        decidedAt: d.decidedAt,
        source: 'tap',
      },
    ],
  });
});

test('every write sets plain-text format first, so Sheets keeps uids, times and =text as text', () => {
  const b = seeded();
  b.post({ action: 'decide', secret: SECRET, uid: row().uid, decision: 'consider', reason: '' });
  const f = b.sheets[SHEET_NAME].formats;
  assert.ok(f.length >= 3);
  assert.ok(f.every((x) => x.f === '@'));
});

test('push grows the grid past its initial rows', () => {
  const b = fresh();
  const rows = Array.from({ length: 12 }, (_, i) => row({ uid: `u${i}` }));
  assert.deepEqual(b.post({ action: 'push', secret: SECRET, rows }), {
    ok: true,
    added: 12,
    updated: 0,
    docsVersion: 1,
  });
  assert.equal(cells(b).length, 13);
});

test('AB6: the page lists unmet hard requirements before anything else in each job, escaped, 360 px, no external script', () => {
  const b = seeded();
  b.post({
    action: 'push',
    secret: SECRET,
    rows: [row({ uid: 'x2', title: '<script>alert(1)</script>', company: 'A & B', score100: 90 })],
  });
  const out = b.get({ s: SECRET });
  const page = out.content;
  assert.equal(out.meta.viewport, 'width=device-width, initial-scale=1');
  // escaping: the hostile title is inert text
  assert.doesNotMatch(page, /<script>alert\(1\)<\/script>/);
  assert.match(page, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(page, /A &amp; B/);
  // order within the first job card
  const card = page.slice(page.indexOf('data-uid="0f3a9c1e5b7d2a46"'));
  const at = (s) => {
    const i = card.indexOf(s);
    assert.ok(i >= 0, `missing ${s}`);
    return i;
  };
  const unmet = at('Unmet hard requirements');
  assert.ok(at('Platform Specialist') < unmet);
  assert.ok(unmet < at('Five years of Kubernetes'));
  assert.ok(at('Five years of Kubernetes') < at('Confirmed gaps'));
  assert.ok(at('Confirmed gaps') < at('Missing evidence'));
  assert.ok(at('Missing evidence') < at('Uncertain'));
  assert.ok(at('Uncertain') < at('data-d="apply"'));
  for (const d of SHEET_DECISIONS) assert.ok(card.includes(`data-d="${d}"`));
  assert.match(card, /Already applied/);
  // the secret reaches the page only as an HTML attribute, never inside a script
  const scripts = [...page.matchAll(/<script[^>]*>([\s\S]*?)<\/script>/g)].map((m) => m[1]);
  assert.ok(scripts.length >= 1);
  assert.ok(scripts.every((s) => !s.includes(SECRET)));
  assert.match(page, new RegExp(`data-s="${SECRET}"`));
  // no external script, stylesheet or font; no framework; nothing wider than 360 px
  assert.doesNotMatch(page, /<script[^>]+src=/i);
  assert.doesNotMatch(page, /<link[^>]+href=/i);
  assert.doesNotMatch(page, /https?:\/\//);
  for (const m of page.matchAll(/(?:min-)?width:\s*(\d+)px/g)) assert.ok(Number(m[1]) <= 360, m[0]);
});

test('AB6: undecided jobs first, then by score; a decided job shows its decision and reason', () => {
  const b = seeded();
  b.post({
    action: 'push',
    secret: SECRET,
    rows: [row({ uid: 'hi', title: 'High score', score100: 95 })],
  });
  b.post({
    action: 'decide',
    secret: SECRET,
    uid: 'hi',
    decision: 'reject',
    reason: 'relocation <now>',
  });
  const page = b.get({ s: SECRET }).content;
  assert.ok(page.indexOf('data-uid="0f3a9c1e5b7d2a46"') < page.indexOf('data-uid="hi"'));
  assert.match(page, /relocation &lt;now&gt;/);
});

// --- BRIEF-037 (CR-rg-board-decision-history): decision history ---
const decide = (b, over = {}) =>
  b.post({
    action: 'decide',
    secret: SECRET,
    uid: row().uid,
    decision: 'consider',
    reason: '',
    ...over,
  });
const pull = (b) => b.post({ action: 'pull', secret: SECRET });
const events = (b) => (b.sheets.Events ? b.sheets.Events.rows.slice(1) : []);
const EV = { uid: 2, decision: 3, reason: 4 }; // column indexes in the Events sheet

test('H1/H2: Apply then Reject before any pull keeps both events, reasons and times; latest is Reject; repeat pull adds none', () => {
  const b = seeded();
  assert.equal(decide(b, { decision: 'apply', reason: 'strong match', requestId: 'r1' }).ok, true);
  assert.equal(
    decide(b, { decision: 'reject', reason: 'on-site 5 days', requestId: 'r2' }).ok,
    true,
  );
  const first = pull(b);
  assert.equal(first.historyVersion, 1);
  assert.deepEqual(
    first.events.map((e) => [e.decision, e.reason, e.source]),
    [
      ['apply', 'strong match', 'tap'],
      ['reject', 'on-site 5 days', 'tap'],
    ],
  );
  assert.ok(first.events.every((e) => e.decidedAt && e.eventId));
  assert.equal(new Set(first.events.map((e) => e.eventId)).size, 2);
  assert.deepEqual(
    first.decisions.map((d) => [d.decision, d.reason]),
    [['reject', 'on-site 5 days']],
  );
  const second = pull(b);
  assert.deepEqual(second.events, first.events);
  assert.equal(events(b).length, 2);
});

test('H2: retrying one request id gives the original event; two separate identical taps stay distinct', () => {
  const b = seeded();
  const a = decide(b, { decision: 'apply', reason: 'x', requestId: 'r1' });
  const again = decide(b, { decision: 'apply', reason: ' x ', requestId: 'r1' });
  assert.equal(again.eventId, a.eventId);
  assert.equal(again.decidedAt, a.decidedAt);
  assert.equal(events(b).length, 1);
  decide(b, { decision: 'apply', reason: 'x', requestId: 'r2' });
  assert.equal(events(b).length, 2);
});

test('H2: a missing requestId (old page) records a normal event with no dedupe', () => {
  const b = seeded();
  decide(b, { decision: 'consider' });
  decide(b, { decision: 'consider' });
  assert.equal(events(b).length, 2);
});

test('H3: a reused request id with a different decision or reason is refused and changes nothing', () => {
  const b = seeded();
  decide(b, { decision: 'apply', reason: 'x', requestId: 'r1' });
  const before = JSON.stringify(b.state());
  for (const over of [
    { decision: 'reject', reason: 'x' },
    { decision: 'apply', reason: 'y' },
    { uid: 'other', decision: 'apply', reason: 'x' },
  ]) {
    const res = decide(b, { requestId: 'r1', ...over });
    assert.deepEqual(res, { ok: false, error: 'request id reused for a different decision' });
  }
  assert.equal(JSON.stringify(b.state()), before);
});

test('H3: a non-string or over-long requestId is refused before any write', () => {
  const b = seeded();
  const before = JSON.stringify(b.state());
  for (const requestId of [42, {}, 'x'.repeat(101)])
    assert.equal(decide(b, { requestId }).ok, false);
  assert.equal(JSON.stringify(b.state()), before);
});

test('H3: invalid uid, reason and secret write nothing; unknown uid on an empty board creates no sheet', () => {
  const e = fresh();
  assert.equal(decide(e, { uid: 'nope', requestId: 'r1' }).ok, false);
  assert.equal(e.sheets[SHEET_NAME], undefined);
  assert.equal(e.sheets.Events, undefined);
  const b = seeded();
  const before = JSON.stringify(b.state());
  assert.equal(decide(b, { decision: 'apply', reason: ' ', requestId: 'r1' }).ok, false);
  assert.equal(decide(b, { uid: '', requestId: 'r1' }).ok, false);
  assert.equal(decide(b, { uid: 'nope', requestId: 'r1' }).ok, false);
  assert.equal(decide(b, { secret: 'bad', requestId: 'r1' }).ok, false);
  assert.equal(pull({ ...b, post: (x) => b.post({ ...x, secret: 'bad' }) }).ok, false);
  assert.equal(JSON.stringify(b.state()), before);
});

test('H3: a re-push keeps the Board decisions and every event', () => {
  const b = seeded();
  decide(b, { decision: 'apply', reason: 'x', requestId: 'r1' });
  decide(b, { decision: 'reject', reason: 'y', requestId: 'r2' });
  const evBefore = JSON.stringify(events(b));
  b.post({ action: 'push', secret: SECRET, rows: [row({ score100: 10 })] });
  assert.equal(JSON.stringify(events(b)), evBefore);
  assert.deepEqual(cells(b)[1].slice(10, 12), ['reject', 'y']);
});

test('H4: failure between the event append and the projection; the same request id recovers one event and the right row', () => {
  const b = seeded();
  decide(b, { decision: 'apply', reason: 'first', requestId: 'r1' });
  let armed = true;
  b.hooks.beforeWrite = (sheet, r, c) => {
    if (armed && sheet === SHEET_NAME && c === 11) {
      armed = false;
      throw new Error('injected');
    }
  };
  const failed = decide(b, { decision: 'reject', reason: 'second', requestId: 'r2' });
  assert.equal(failed.ok, false);
  assert.equal(b.lockHeld(), false, 'the lock was not released');
  assert.equal(events(b).length, 2, 'the event is appended before the projection');
  assert.deepEqual(cells(b)[1].slice(10, 12), ['apply', 'first']);
  const ok = decide(b, { decision: 'reject', reason: 'second', requestId: 'r2' });
  assert.equal(ok.ok, true);
  assert.equal(events(b).length, 2);
  assert.deepEqual(cells(b)[1].slice(10, 12), ['reject', 'second']);
});

test('H4: with no retry at all, a pull heals the stale row and reports the right latest view', () => {
  const b = seeded();
  decide(b, { decision: 'apply', reason: 'first', requestId: 'r1' });
  let armed = true;
  b.hooks.beforeWrite = (sheet, r, c) => {
    if (armed && sheet === SHEET_NAME && c === 11) {
      armed = false;
      throw new Error('injected');
    }
  };
  decide(b, { decision: 'reject', reason: 'second', requestId: 'r2' });
  const res = pull(b);
  assert.deepEqual(
    res.decisions.map((d) => [d.decision, d.reason]),
    [['reject', 'second']],
  );
  assert.equal(res.events.length, 2);
  assert.deepEqual(cells(b)[1].slice(10, 12), ['reject', 'second']);
});

test('H4: a later hand edit of the Board row wins over an older event', () => {
  const b = seeded();
  decide(b, { decision: 'apply', reason: 'first', requestId: 'r1' });
  cells(b)[1][10] = 'consider';
  cells(b)[1][11] = 'typed by hand';
  cells(b)[1][12] = '2999-01-01T00:00:00.000Z';
  const res = pull(b);
  assert.deepEqual(res.decisions[0].reason, 'typed by hand');
  assert.equal(res.events.length, 1);
});

test('H4: a request that arrives while another holds the lock is told the board is busy and writes nothing', () => {
  const b = seeded();
  let nested = null;
  b.hooks.beforeWrite = (sheet) => {
    if (nested === null && sheet === 'Events') {
      b.hooks.beforeWrite = null;
      nested = decide(b, { decision: 'consider', requestId: 'r9' });
    }
  };
  const outer = decide(b, { decision: 'apply', reason: 'x', requestId: 'r1' });
  assert.deepEqual(nested, { ok: false, error: 'the board is busy: try again' });
  assert.equal(outer.ok, true);
  assert.equal(events(b).length, 1);
  assert.equal(b.lockHeld(), false);
});

test('H4: many events grow the Events sheet, and text starting with = stays text', () => {
  const b = seeded();
  for (let i = 0; i < 8; i++)
    assert.equal(
      decide(b, { decision: 'consider', reason: `=r${i}`, requestId: `q${i}` }).ok,
      true,
    );
  assert.equal(events(b).length, 8);
  assert.ok(b.sheets.Events.formats.every((x) => x.f === '@'));
  assert.equal(pull(b).events.length, 8);
});

test('H5: rows that already hold a decision are seeded once as legacy snapshots; later hand edits are not', () => {
  const b = seeded();
  cells(b)[1][10] = 'apply';
  cells(b)[1][11] = 'older tap';
  cells(b)[1][12] = '2026-11-01T10:00:00.000Z';
  b.post({ action: 'push', secret: SECRET, rows: [row({ uid: 'u2', title: 'Two' })] });
  cells(b)[2][10] = 'consider'; // no time: cannot be seeded
  const first = pull(b);
  assert.deepEqual(
    first.events.map((e) => [e.uid, e.source, e.decision, e.reason]),
    [[row().uid, 'legacy-snapshot', 'apply', 'older tap']],
  );
  assert.equal(first.events[0].decidedAt, '2026-11-01T10:00:00.000Z');
  cells(b)[2][10] = 'reject';
  cells(b)[2][11] = 'typed';
  cells(b)[2][12] = '2026-11-02T10:00:00.000Z';
  assert.equal(pull(b).events.length, 1, 'seeded again after the first time');
  decide(b, { uid: 'u2', decision: 'consider', requestId: 'r1' });
  assert.equal(pull(b).events.length, 2);
});

test('H6: the pull response of an unchanged board still carries decisions, now with historyVersion and events', () => {
  const b = seeded();
  assert.deepEqual(pull(b), { ok: true, decisions: [], historyVersion: 1, events: [] });
});

test('H1: the page sends a request id per tap, new for a changed pick or reason, kept for an unchanged retry', () => {
  const page = readFileSync(new URL('../../apps-script/Page.html', import.meta.url), 'utf8');
  assert.match(page, /requestId: card\.dataset\.rid/);
  assert.match(page, /var sig = d \+ '\|' \+ reason;/);
  assert.match(page, /if \(card\.dataset\.sig !== sig\)/); // new id only when the decision or reason changed
  assert.match(page, /delete card\.dataset\.rid/); // cleared once saved
});

// --- BRIEF-048 (CR-rg-daily-phone-board): draft/held status and a supplied document link ---
const DOC = { cvStatus: 'draft', docLink: 'https://drive.example/d/abc', docNote: '' };
const cellsOf = (b, uid = '0f3a9c1e5b7d2a46') => {
  const r = b.sheets.Board.rows.find((x) => x[0] === uid);
  return { doc: r.slice(13, 16), decision: r.slice(10, 13) };
};
const push = (b, rows) => b.post({ action: 'push', secret: SECRET, rows });

test('B048 doc columns: written on push, kept by a plain re-push, replaced by a new value, cleared by ""', () => {
  const b = fresh();
  const r1 = push(b, [row({ ...DOC })]);
  assert.equal(r1.ok, true);
  assert.equal(r1.docsVersion, 1);
  assert.deepEqual(cellsOf(b).doc, ['draft', 'https://drive.example/d/abc', '']);
  assert.deepEqual(b.sheets.Board.rows[0], [...SHEET_COLUMNS]);
  push(b, [row({ title: 'Renamed' })]); // no doc keys: keep
  assert.deepEqual(cellsOf(b).doc, ['draft', 'https://drive.example/d/abc', '']);
  push(b, [row({ docLink: 'https://drive.example/d/new' })]); // link-only refresh onto a draft row is valid
  assert.deepEqual(cellsOf(b).doc, ['draft', 'https://drive.example/d/new', '']);
  push(b, [row({ cvStatus: '', docLink: '', docNote: '' })]);
  assert.deepEqual(cellsOf(b).doc, ['', '', '']);
});

test('B048 doc validation runs on the merged result; a refused row writes nothing', () => {
  const b = fresh();
  push(b, [row({ cvStatus: 'held', docNote: 'access: not shared yet' })]);
  for (const bad of [
    { docNote: '' }, // held row with the note cleared
    { cvStatus: 'draft', docLink: 'http://drive.example/d' },
    { cvStatus: 'draft', docLink: 'javascript:alert(1)' },
    { cvStatus: 'final' },
  ]) {
    const before = JSON.stringify(b.state().sheets);
    const res = push(b, [row({ title: 'Changed', ...bad })]);
    assert.equal(res.ok, false, JSON.stringify(bad));
    assert.equal(JSON.stringify(b.state().sheets), before, 'nothing written');
  }
  const ok = push(b, [row({ uid: 'new1', docLink: 'https://x.example/d' })]); // new row: link with no status
  assert.equal(ok.ok, false);
});

test('B048 an old 13-column sheet gains the doc columns without touching decisions or the ledger', () => {
  const b = seeded();
  const sid = '0f3a9c1e5b7d2a46';
  assert.equal(
    b.pageDecide({
      secret: SECRET,
      uid: sid,
      decision: 'apply',
      reason: 'stretch',
      requestId: 'q1',
    }).ok,
    true,
  );
  const st = b.state();
  st.sheets.Board = st.sheets.Board.map((r) => r.slice(0, 13)); // as sheets built before BRIEF-048
  const old = loadBoard(st);
  const before = JSON.stringify(old.state().sheets.Events);
  assert.equal(old.post({ action: 'pull', secret: SECRET }).ok, true);
  const res = push(old, [row({ ...DOC })]);
  assert.equal(res.ok, true);
  const c = cellsOf(old);
  assert.deepEqual(c.doc, ['draft', 'https://drive.example/d/abc', '']);
  assert.equal(c.decision[0], 'apply');
  assert.equal(c.decision[1], 'stretch');
  assert.equal(JSON.stringify(old.state().sheets.Events), before);
  assert.deepEqual(old.sheets.Board.rows[0], [...SHEET_COLUMNS]);
  const pulled = old.post({ action: 'pull', secret: SECRET });
  assert.equal(pulled.decisions[0].decision, 'apply');
});

test('B048 a decision tap after doc columns exist still lands in the decision columns', () => {
  const b = fresh();
  push(b, [row({ ...DOC })]);
  const r = b.pageDecide({
    secret: SECRET,
    uid: '0f3a9c1e5b7d2a46',
    decision: 'consider',
    requestId: 'z',
  });
  assert.equal(r.ok, true);
  assert.equal(cellsOf(b).decision[0], 'consider');
  assert.deepEqual(cellsOf(b).doc, ['draft', 'https://drive.example/d/abc', '']);
});

test('B048 card: Draft badge, Open document link, explicit missing-link and Held text; unsafe links never an anchor', () => {
  const html = (b) => b.get({ s: SECRET }).content;
  let b = fresh();
  push(b, [row({ cvStatus: 'draft', docLink: 'https://drive.example/d/abc' })]);
  let h = html(b);
  assert.match(h, /Draft/);
  assert.match(h, /<a href="https:\/\/drive\.example\/d\/abc"[^>]*>Open document<\/a>/);
  b = fresh();
  push(b, [row({ cvStatus: 'draft' })]);
  assert.match(html(b), /no private link supplied yet/);
  b = fresh();
  push(b, [row({ cvStatus: 'held', docNote: 'access: <b>not</b> shared' })]);
  h = html(b);
  assert.match(h, /Held: access: &lt;b&gt;not&lt;\/b&gt; shared/);
  // a hand-edited sheet cell holding an unsafe link is shown as text, not as an anchor
  b = fresh();
  push(b, [row({ cvStatus: 'draft', docLink: 'https://ok.example/d' })]);
  const st = b.state();
  st.sheets.Board[1][14] = 'javascript:alert(1)';
  h = html(loadBoard(st));
  assert.doesNotMatch(h, /href="javascript/i);
  assert.match(h, /not a safe link/);
  b = fresh();
  push(b, [row()]);
  assert.doesNotMatch(html(b), /Open document|Draft|Held:/);
});
