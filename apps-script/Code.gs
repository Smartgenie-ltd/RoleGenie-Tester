// RoleGenie phone board: a Google Apps Script web app in YOUR Google account, over the Google Sheet it is attached to.
// RoleGenie (board_sheet.py) pushes your assessed jobs here and pulls your decisions back; the page records your taps.
// Every request must carry your board secret (Script Property BOARD_SECRET, created by `rolegenie.py board setup`).
// Deploy: Execute as "Me", Who has access "Anyone". See README "Phone board".
// Contract: contracts/rolegenie/board-sheet.ts (BRIEF-025, CR-rg-board-sheet). Helpers end in "_" so the page cannot
// call them; the page calls only pageDecide. Nothing here logs anything.
// Decision history (BRIEF-037, CR-rg-board-decision-history): every tap is first appended to the Events sheet (an
// append-only ledger), then the Board row's decision columns are updated as the latest-state view. A retried request id
// yields the original event. Rows that already held a decision are seeded ONCE as 'legacy-snapshot' events: earlier
// history cannot be reconstructed, and later hand edits to the Board's decision columns never enter the ledger.

var SHEET_NAME = 'Board';
var SECRET_PROPERTY = 'BOARD_SECRET';
var ASSESSMENT_COLUMNS = [
  'uid',
  'title',
  'company',
  'url',
  'score100',
  'unmetHard',
  'confirmedGaps',
  'missingEvidence',
  'uncertain',
  'assessedAt',
];
var EVENTS_SHEET_NAME = 'Events';
var EVENTS_SEEDED_PROPERTY = 'EVENTS_SEEDED';
var HISTORY_VERSION = 1;
var MAX_REQUEST_ID_LENGTH = 100;
var LOCK_MS = 10000;
var EVENT_COLUMNS = ['eventId', 'requestId', 'uid', 'decision', 'reason', 'decidedAt', 'source'];
var BUSY = { ok: false, error: 'the board is busy: try again' };
var DECISION_COLUMNS = ['decision', 'reason', 'decidedAt'];
// BRIEF-048 (CR-rg-daily-phone-board): draft/held status and a user-supplied document link. LAST, so sheets built before
// still read correctly (a missing cell is ''), and heal_/project_ offsets (ASSESSMENT_COLUMNS.length) stay valid.
var DOC_COLUMNS = ['cvStatus', 'docLink', 'docNote'];
var CV_STATUSES = ['', 'draft', 'held'];
var DOCS_VERSION = 1;
var COLUMNS = ASSESSMENT_COLUMNS.concat(DECISION_COLUMNS).concat(DOC_COLUMNS);
var DECISIONS = ['apply', 'consider', 'reject', 'applied-externally'];
var REASON_REQUIRED = ['apply', 'reject'];
var LABELS = {
  apply: 'Apply',
  consider: 'Consider',
  reject: 'Reject',
  'applied-externally': 'Already applied',
};
var REFUSED = { ok: false, error: 'refused' };

// --- web app entry points ---

function doGet(e) {
  var given = e && e.parameter ? e.parameter.s : '';
  if (!authorised_(given)) {
    return HtmlService.createHtmlOutput('<p>Not authorised.</p>').setTitle('RoleGenie board');
  }
  return HtmlService.createHtmlOutput(renderPage_(readJobs_(), given))
    .setTitle('RoleGenie board')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}

function doPost(e) {
  var req = null;
  try {
    req = JSON.parse(e.postData.contents);
  } catch (err) {
    req = null;
  }
  return ContentService.createTextOutput(JSON.stringify(handle_(req))).setMimeType(
    ContentService.MimeType.JSON,
  );
}

/** The page's tap (google.script.run). Same secret check and reason rule as the API. */
function pageDecide(json) {
  var req = null;
  try {
    req = JSON.parse(json);
  } catch (err) {
    req = null;
  }
  if (req && typeof req === 'object') req.action = 'decide';
  return handle_(req);
}

// --- the API ---

function handle_(req) {
  if (!req || typeof req !== 'object' || !authorised_(req.secret)) return REFUSED;
  if (req.action === 'push')
    return locked_(function () {
      return push_(req.rows);
    });
  if (req.action === 'pull') return locked_(pull_);
  if (req.action === 'decide')
    return locked_(function () {
      return decide_(req);
    });
  return { ok: false, error: 'unknown action' };
}

/** Serialises every mutation (and the pull's healing) with the script lock; the lock is always released. */
function locked_(fn) {
  var lock = LockService.getScriptLock();
  try {
    lock.waitLock(LOCK_MS);
  } catch (err) {
    return BUSY;
  }
  try {
    return fn();
  } catch (err) {
    return { ok: false, error: 'the board could not be updated: try again' };
  } finally {
    lock.releaseLock();
  }
}

function authorised_(given) {
  var secret = String(
    PropertiesService.getScriptProperties().getProperty(SECRET_PROPERTY) || '',
  ).trim();
  return secret !== '' && typeof given === 'string' && given === secret;
}

function push_(rows) {
  if (!Array.isArray(rows)) return { ok: false, error: 'push needs rows' };
  for (var i = 0; i < rows.length; i++) {
    if (!rows[i] || String(rows[i].uid == null ? '' : rows[i].uid).trim() === '') {
      return { ok: false, error: 'every row needs a uid' };
    }
  }
  var sh = sheet_();
  var values = sh.getDataRange().getValues();
  var at = {}; // uid -> index in values
  for (var r = 1; r < values.length; r++) at[String(values[r][0])] = r;
  var docAt = ASSESSMENT_COLUMNS.length + DECISION_COLUMNS.length;
  var docs = {}; // uid -> the document cells as they will stand (existing overlaid with pushed keys)
  var docTouched = {}; // uid -> true when a pushed row carried a document key
  var assess = values.slice(1).map(function (v) {
    return v.slice(0, ASSESSMENT_COLUMNS.length);
  });
  var added = [];
  var addedAt = {};
  var updated = 0;
  var bad = null;
  rows.forEach(function (row) {
    var cells = ASSESSMENT_COLUMNS.map(function (k) {
      if (k === 'score100') return Number(row.score100 || 0).toFixed(1);
      return row[k] == null ? '' : String(row[k]);
    });
    var uid = cells[0];
    var isOld = Object.prototype.hasOwnProperty.call(at, uid);
    if (!Object.prototype.hasOwnProperty.call(docs, uid)) {
      docs[uid] = DOC_COLUMNS.map(function (k, j) {
        var v = isOld ? values[at[uid]][docAt + j] : '';
        return v == null ? '' : String(v);
      });
    }
    DOC_COLUMNS.forEach(function (k, j) {
      if (Object.prototype.hasOwnProperty.call(row, k)) {
        docs[uid][j] = row[k] == null ? '' : String(row[k]).trim();
        docTouched[uid] = true;
      }
    });
    if (docTouched[uid] && !bad) {
      var problems = docProblems_({
        cvStatus: docs[uid][0],
        docLink: docs[uid][1],
        docNote: docs[uid][2],
      });
      if (problems.length) bad = uid + ': ' + problems.join('; ');
    }
    if (isOld) {
      assess[at[uid] - 1] = cells;
      updated++;
    } else if (Object.prototype.hasOwnProperty.call(addedAt, uid)) {
      added[addedAt[uid]] = cells;
    } else {
      addedAt[uid] = added.length;
      added.push(cells);
    }
  });
  // Every row is checked before anything is written: a refused document leaves the sheet as it was.
  if (bad) return { ok: false, error: bad };
  // Only the assessment columns of existing rows are rewritten: a recorded decision is never touched. The header is
  // rewritten so a sheet built before the document columns gains their titles.
  write_(sh, 1, 1, [COLUMNS]);
  if (assess.length) write_(sh, 2, 1, assess);
  Object.keys(docTouched).forEach(function (uid) {
    if (Object.prototype.hasOwnProperty.call(at, uid))
      write_(sh, at[uid] + 1, docAt + 1, [docs[uid]]);
  });
  if (added.length) {
    appendRows_(
      sh,
      added.map(function (cells) {
        return cells.concat(['', '', '']).concat(docs[cells[0]]);
      }),
    );
  }
  return { ok: true, added: added.length, updated: updated, docsVersion: DOCS_VERSION };
}

/** Mirror of docProblems in contracts/rolegenie/board-sheet.ts (shared cases). */
function docProblems_(d) {
  var status = String(d.cvStatus == null ? '' : d.cvStatus).trim();
  var link = String(d.docLink == null ? '' : d.docLink).trim();
  var note = String(d.docNote == null ? '' : d.docNote).trim();
  var problems = [];
  if (CV_STATUSES.indexOf(status) < 0) problems.push("unknown cvStatus '" + status + "'");
  if (link !== '' && !/^https:\/\/\S+$/i.test(link)) problems.push('docLink must be an https link');
  if (link !== '' && status === '') problems.push('docLink needs a cvStatus');
  if (status === 'held' && note === '') problems.push('held needs a docNote');
  return problems;
}

function pull_() {
  ensureLedger_();
  var events = readEvents_();
  heal_(events);
  var decisions = readJobs_()
    .filter(function (j) {
      return j.decision !== '';
    })
    .map(function (j) {
      return { uid: j.uid, decision: j.decision, reason: j.reason, decidedAt: j.decidedAt };
    });
  return { ok: true, decisions: decisions, historyVersion: HISTORY_VERSION, events: events };
}

function decide_(req) {
  var d = {
    uid: String(req.uid == null ? '' : req.uid),
    decision: String(req.decision == null ? '' : req.decision),
    reason: String(req.reason == null ? '' : req.reason).trim(),
  };
  var problems = decisionProblems_(d);
  if (problems.length) return { ok: false, error: problems.join('; ') };
  var rid = req.requestId;
  if (rid != null && (typeof rid !== 'string' || rid.length > MAX_REQUEST_ID_LENGTH)) {
    return { ok: false, error: 'requestId must be a short string' };
  }
  rid = rid == null ? '' : rid;
  // Read-only checks first: nothing is created or written until every one has passed.
  var events = readEvents_();
  var ev = null;
  if (rid !== '') {
    for (var i = 0; i < events.length; i++) if (events[i].requestId === rid) ev = events[i];
  }
  if (ev && (ev.uid !== d.uid || ev.decision !== d.decision || ev.reason.trim() !== d.reason)) {
    return { ok: false, error: 'request id reused for a different decision' };
  }
  var board = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
  var at = board ? rowOf_(board, d.uid) : 0;
  if (!at) return { ok: false, error: 'unknown job' };
  if (!ev) {
    // The event is appended BEFORE the visible row changes: if the second write fails, a retry (same request id)
    // or the next pull re-projects it from the ledger.
    var ledger = ensureLedger_();
    events = readEvents_();
    ev = {
      eventId: 'ev-' + Utilities.getUuid(),
      requestId: rid,
      uid: d.uid,
      decision: d.decision,
      reason: d.reason,
      decidedAt: new Date().toISOString(),
      source: 'tap',
    };
    appendRows_(ledger, [eventCells_(ev)]);
    project_(board, at, ev);
  } else {
    heal_(events);
  }
  return {
    ok: true,
    uid: ev.uid,
    decision: ev.decision,
    decidedAt: ev.decidedAt,
    eventId: ev.eventId,
  };
}

function rowOf_(sh, uid) {
  var values = sh.getDataRange().getValues();
  for (var r = 1; r < values.length; r++) if (String(values[r][0]) === uid) return r + 1;
  return 0;
}

/** Writes an event into the Board row's decision columns (the latest-state view). */
function project_(sh, rowNo, ev) {
  write_(sh, rowNo, ASSESSMENT_COLUMNS.length + 1, [[ev.decision, ev.reason, ev.decidedAt]]);
}

/** Re-projects a job whose last ledger event is not what its Board row shows (a failed second write). A row whose
 *  recorded time is later than the event (a hand edit) is left alone. */
function heal_(events) {
  var board = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
  if (!board || !events.length) return;
  var last = {};
  events.forEach(function (e) {
    last[e.uid] = e;
  });
  var values = board.getDataRange().getValues();
  for (var r = 1; r < values.length; r++) {
    var e = last[String(values[r][0])];
    if (!e) continue;
    var n = ASSESSMENT_COLUMNS.length;
    var cur = [
      String(values[r][n] || ''),
      String(values[r][n + 1] || ''),
      String(values[r][n + 2] || ''),
    ];
    if (
      e.decidedAt >= cur[2] &&
      (cur[0] !== e.decision || cur[1] !== e.reason || cur[2] !== e.decidedAt)
    ) {
      project_(board, r + 1, e);
    }
  }
}

// --- the ledger (Events sheet) ---

function eventCells_(e) {
  return EVENT_COLUMNS.map(function (k) {
    return e[k] == null ? '' : String(e[k]);
  });
}

function readEvents_() {
  var sh = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(EVENTS_SHEET_NAME);
  if (!sh) return [];
  return sh
    .getDataRange()
    .getValues()
    .slice(1)
    .filter(function (v) {
      return String(v[0] == null ? '' : v[0]).trim() !== '';
    })
    .map(function (v) {
      var e = {};
      EVENT_COLUMNS.forEach(function (k, i) {
        e[k] = v[i] == null ? '' : String(v[i]);
      });
      return e;
    });
}

/** Creates the Events sheet and, once, seeds one 'legacy-snapshot' event per Board row that already holds a valid,
 *  timed decision. Seeding is idempotent by event id and is recorded in a Script Property. */
function ensureLedger_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(EVENTS_SHEET_NAME);
  if (!sh) {
    sh = ss.insertSheet(EVENTS_SHEET_NAME);
    write_(sh, 1, 1, [EVENT_COLUMNS]);
  }
  var props = PropertiesService.getScriptProperties();
  if (props.getProperty(EVENTS_SEEDED_PROPERTY) === '1') return sh;
  var have = {};
  readEvents_().forEach(function (e) {
    have[e.eventId] = true;
  });
  var seeds = [];
  readJobs_().forEach(function (j) {
    var id = 'legacy:' + j.uid;
    if (have[id] || j.decidedAt.trim() === '' || decisionProblems_(j).length) return;
    seeds.push(
      eventCells_({
        eventId: id,
        requestId: '',
        uid: j.uid,
        decision: j.decision,
        reason: j.reason.trim(),
        decidedAt: j.decidedAt,
        source: 'legacy-snapshot',
      }),
    );
  });
  if (seeds.length) appendRows_(sh, seeds);
  props.setProperty(EVENTS_SEEDED_PROPERTY, '1');
  return sh;
}

/** Appends rows below the last one, growing the grid first (plain-text cells, as everywhere else). */
function appendRows_(sh, rows) {
  var first = sh.getLastRow() + 1;
  var need = first + rows.length - 1 - sh.getMaxRows();
  if (need > 0) sh.insertRowsAfter(sh.getMaxRows(), need);
  write_(sh, first, 1, rows);
}

/** Mirror of decisionProblems in contracts/rolegenie/board-sheet.ts. */
function decisionProblems_(d) {
  var problems = [];
  if (String(d.uid == null ? '' : d.uid).trim() === '') problems.push('decision needs a job uid');
  if (DECISIONS.indexOf(d.decision) < 0) problems.push("unknown decision '" + d.decision + "'");
  else if (
    REASON_REQUIRED.indexOf(d.decision) >= 0 &&
    String(d.reason == null ? '' : d.reason).trim() === ''
  ) {
    problems.push(d.decision + ' needs a reason');
  }
  return problems;
}

function columns_() {
  return COLUMNS;
}
function decisions_() {
  return DECISIONS;
}

// --- the sheet ---

function sheet_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sh = ss.getSheetByName(SHEET_NAME);
  if (!sh) {
    sh = ss.insertSheet(SHEET_NAME);
    write_(sh, 1, 1, [COLUMNS]);
  }
  return sh;
}

/** Plain-text format first, so Sheets keeps uids, times and "=..." ad text as text (no numbers, dates or formulas). */
function write_(sh, row, col, values) {
  sh.getRange(row, col, values.length, values[0].length).setNumberFormat('@').setValues(values);
}

function readJobs_() {
  var sh = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SHEET_NAME);
  if (!sh) return [];
  return sh
    .getDataRange()
    .getValues()
    .slice(1)
    .map(function (v) {
      var job = {};
      COLUMNS.forEach(function (k, i) {
        job[k] = v[i] == null ? '' : String(v[i]);
      });
      return job;
    });
}

// --- the page ---

function esc_(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function list_(heading, text, cls) {
  var items = String(text)
    .split('\n')
    .filter(function (x) {
      return x.trim() !== '';
    });
  return (
    '<div class="' +
    cls +
    '"><h3>' +
    heading +
    '</h3>' +
    (items.length
      ? '<ul>' +
        items
          .map(function (x) {
            return '<li>' + esc_(x) + '</li>';
          })
          .join('') +
        '</ul>'
      : '<p class="none">none</p>') +
    '</div>'
  );
}

/** The document status line. RoleGenie cannot check that a link opens for the user, so it only offers it. */
function docBlock_(j) {
  var status = String(j.cvStatus || '').trim();
  if (status === '') return '';
  var link = String(j.docLink || '').trim();
  var note = String(j.docNote || '').trim();
  if (status === 'held')
    return '<p class="doc held">Held: ' + esc_(note || 'no reason recorded') + '</p>';
  var out = '<p class="doc draft">Draft';
  if (link === '')
    out += ': ready, no private link supplied yet (the document stays on your machine)';
  else if (/^https:\/\/\S+$/i.test(link))
    out +=
      ' &middot; <a href="' + esc_(link) + '" target="_blank" rel="noopener">Open document</a>';
  else out += ': the supplied link is not a safe link and is not shown';
  return out + (note ? ' &mdash; ' + esc_(note) : '') + '</p>';
}

function card_(j) {
  var link = /^https?:\/\//i.test(j.url)
    ? ' <a href="' + esc_(j.url) + '" target="_blank" rel="noopener">ad</a>'
    : '';
  var decided = j.decision
    ? '<p class="decided">Decided: ' +
      esc_(LABELS[j.decision] || j.decision) +
      (j.reason ? ' &mdash; ' + esc_(j.reason) : '') +
      '</p>'
    : '';
  var doc = docBlock_(j);
  return (
    '<section class="job" data-uid="' +
    esc_(j.uid) +
    '">' +
    '<h2>' +
    esc_(j.title || '(untitled)') +
    '</h2>' +
    '<p class="meta">' +
    (j.company ? esc_(j.company) + ' &middot; ' : '') +
    'Score ' +
    esc_(j.score100) +
    ' / 100' +
    link +
    '</p>' +
    decided +
    doc +
    list_('Unmet hard requirements', j.unmetHard, 'unmet') +
    list_('Confirmed gaps', j.confirmedGaps, 'gaps') +
    list_('Missing evidence', j.missingEvidence, 'missing') +
    list_('Uncertain', j.uncertain, 'uncertain') +
    '<div class="buttons">' +
    DECISIONS.map(function (d) {
      return '<button type="button" data-d="' + d + '">' + LABELS[d] + '</button>';
    }).join('') +
    '</div>' +
    '<div class="decide" hidden><textarea rows="2"></textarea>' +
    '<button type="button" class="save">Save</button></div><p class="status"></p></section>'
  );
}

function renderPage_(jobs, secret) {
  jobs.sort(function (a, b) {
    if ((a.decision === '') !== (b.decision === '')) return a.decision === '' ? -1 : 1;
    return Number(b.score100) - Number(a.score100);
  });
  var body = jobs.length
    ? jobs.map(card_).join('')
    : '<p>No jobs yet: run <code>rolegenie.py board push</code>.</p>';
  return HtmlService.createHtmlOutputFromFile('Page')
    .getContent()
    .replace('<!--SECRET-->', function () {
      return esc_(secret);
    })
    .replace('<!--JOBS-->', function () {
      return body;
    });
}
