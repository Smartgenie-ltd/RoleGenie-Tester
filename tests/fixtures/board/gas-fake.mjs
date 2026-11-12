// A minimal in-memory stand-in for the Apps Script built-ins apps-script/Code.gs uses (SpreadsheetApp,
// PropertiesService, HtmlService, ContentService), so the REAL Code.gs runs under node (BRIEF-025, CR-rg-board-sheet).
// Only the calls Code.gs makes are modelled. Every cell is stored as written; `formats` records setNumberFormat calls.
// Not modelled (the AB8 real-deploy boundary): Sheets value coercion, google.script.run transport, the 302 redirect.

import vm from 'node:vm';
import { readFileSync } from 'node:fs';

const APPS_SCRIPT = new URL('../../../apps-script/', import.meta.url);

class FakeSheet {
  constructor(name, rows = [], hooks = {}) {
    this.hooks = hooks;
    this.name = name;
    this.rows = rows; // 2D array of cells, row 1 = rows[0]
    this.maxRows = Math.max(rows.length, 5);
    this.formats = [];
  }
  getLastRow() {
    return this.rows.length;
  }
  getMaxRows() {
    return this.maxRows;
  }
  insertRowsAfter(after, n) {
    if (after !== this.maxRows) throw new Error('fake: insertRowsAfter only at the end');
    this.maxRows += n;
  }
  getDataRange() {
    const rows = this.rows;
    return { getValues: () => rows.map((r) => r.slice()) };
  }
  getRange(row, col, nRows, nCols) {
    const sheet = this;
    if (row + nRows - 1 > sheet.maxRows) throw new Error('fake: range is beyond the sheet grid');
    const range = {
      setNumberFormat(f) {
        sheet.formats.push({ row, col, nRows, nCols, f });
        return range;
      },
      setValues(values) {
        if (sheet.hooks.beforeWrite) sheet.hooks.beforeWrite(sheet.name, row, col);
        if (values.length !== nRows || values.some((v) => v.length !== nCols))
          throw new Error('fake: setValues shape does not match the range');
        for (let i = 0; i < nRows; i++) {
          const r = row - 1 + i;
          while (sheet.rows.length <= r) sheet.rows.push([]);
          for (let j = 0; j < nCols; j++) sheet.rows[r][col - 1 + j] = values[i][j];
        }
        // pad short rows so getValues stays rectangular, as Sheets does
        const width = Math.max(...sheet.rows.map((x) => x.length));
        for (const x of sheet.rows) while (x.length < width) x.push('');
        return range;
      },
    };
    return range;
  }
}

/** Load Code.gs into a fresh context. `state` = { props: {...}, sheets: { name: rows } } (JSON-serialisable). */
export function loadBoard(state = { props: {}, sheets: {} }) {
  const sheets = {};
  // hooks.beforeWrite(sheetName,row,col) runs before every setValues: tests inject failures or a nested request.
  const hooks = {};
  let lockHeld = false;
  let uuid = state.uuid || 0;
  for (const [n, rows] of Object.entries(state.sheets || {}))
    sheets[n] = new FakeSheet(n, rows, hooks);
  const props = { ...(state.props || {}) };
  const html = (content) => {
    const out = {
      content,
      title: '',
      meta: {},
      getContent: () => out.content,
      setTitle(t) {
        out.title = t;
        return out;
      },
      addMetaTag(k, v) {
        out.meta[k] = v;
        return out;
      },
    };
    return out;
  };
  const ctx = vm.createContext({
    SpreadsheetApp: {
      getActiveSpreadsheet: () => ({
        getSheetByName: (n) => sheets[n] || null,
        insertSheet: (n) => (sheets[n] = new FakeSheet(n, [], hooks)),
      }),
    },
    PropertiesService: {
      getScriptProperties: () => ({
        getProperty: (k) => (k in props ? props[k] : null),
        setProperty: (k, v) => ((props[k] = String(v)), undefined),
      }),
    },
    // Script lock: waitLock throws when another request holds it (the real call throws on timeout).
    LockService: {
      getScriptLock: () => ({
        waitLock() {
          if (lockHeld) throw new Error('fake: could not obtain lock');
          lockHeld = true;
        },
        releaseLock() {
          lockHeld = false;
        },
      }),
    },
    Utilities: { getUuid: () => `uuid-${++uuid}` },
    HtmlService: {
      createHtmlOutput: (s) => html(String(s)),
      createHtmlOutputFromFile: (name) =>
        html(readFileSync(new URL(`${name}.html`, APPS_SCRIPT), 'utf8')),
    },
    ContentService: {
      MimeType: { JSON: 'application/json' },
      createTextOutput: (s) => {
        const out = { content: s, mime: '', setMimeType: (m) => ((out.mime = m), out) };
        return out;
      },
    },
  });
  vm.runInContext(readFileSync(new URL('Code.gs', APPS_SCRIPT), 'utf8'), ctx, {
    filename: 'Code.gs',
  });
  // Results cross back from the vm realm as plain JSON so deepStrictEqual compares values, not realms.
  const plain = (v) => (v === undefined ? v : JSON.parse(JSON.stringify(v)));
  return {
    ctx,
    sheets,
    props,
    hooks,
    lockHeld: () => lockHeld,
    post: (body) =>
      JSON.parse(
        ctx.doPost({
          postData: { contents: typeof body === 'string' ? body : JSON.stringify(body) },
        }).content,
      ),
    get: (parameter = {}) => ctx.doGet({ parameter }),
    pageDecide: (body) => plain(ctx.pageDecide(JSON.stringify(body))),
    call: (fn, ...args) => plain(ctx[fn](...args)),
    state: () => ({
      props,
      uuid,
      sheets: Object.fromEntries(Object.entries(sheets).map(([n, s]) => [n, s.rows])),
    }),
  };
}
