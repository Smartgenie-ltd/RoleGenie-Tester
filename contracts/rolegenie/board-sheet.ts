// Board slice contract (BRIEF-025, CR-rg-board-sheet; BRIEF-020 §1 D7a, AIM steps 4-5).
// The phone board is a Google Apps Script web app in the user's own Google account, over a Google Sheet in their Drive.
// RoleGenie pushes assessed jobs to it and pulls the user's decisions back; the page records taps. Every request carries
// the per-user secret. Mirrors: apps-script/Code.gs (the web app) and board_sheet.py (the CLI), tied to
// tests/fixtures/board/contract-cases.json by tests/contract/board-sheet*.test.mjs and tests/test_board_sheet.py.

import { BOARD_DECISIONS, type BoardDecision } from './domain.ts';

// --- The Sheet: one row per job ---

/** Written by push (upsert by uid). */
export const ASSESSMENT_COLUMNS = [
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
] as const;
/** Written only by decide (a tap). Push NEVER writes these, so a recorded decision survives any re-push. */
export const DECISION_COLUMNS = ['decision', 'reason', 'decidedAt'] as const;
/** BRIEF-048: draft/held status and a user-supplied private document link. Appended LAST so every existing sheet
 *  (decisions at columns 11-13) stays valid and heal/project offsets stay put. Written by push only when the pushed row
 *  carries the key (absent = keep; "" = clear). RoleGenie shows the link; it cannot check that it opens for the user. */
export const DOC_COLUMNS = ['cvStatus', 'docLink', 'docNote'] as const;
export const CV_STATUSES = ['', 'draft', 'held'] as const;
export const DOCS_VERSION = 1;
export const SHEET_COLUMNS = [...ASSESSMENT_COLUMNS, ...DECISION_COLUMNS, ...DOC_COLUMNS] as const;

/** Reasons a document block (the MERGED cells: existing overlaid with the pushed keys) is refused. Empty = valid. */
export function docProblems(d: { cvStatus: string; docLink: string; docNote: string }): string[] {
  const s = (v: unknown) => (typeof v === 'string' ? v.trim() : '');
  const status = s(d.cvStatus);
  const problems: string[] = [];
  if (!(CV_STATUSES as readonly string[]).includes(status))
    problems.push(`unknown cvStatus '${status}'`);
  if (s(d.docLink) !== '' && !/^https:\/\/\S+$/i.test(s(d.docLink)))
    problems.push('docLink must be an https link');
  if (s(d.docLink) !== '' && status === '') problems.push('docLink needs a cvStatus');
  if (status === 'held' && s(d.docNote) === '') problems.push('held needs a docNote');
  return problems;
}

// --- Identity between the board and local state (BRIEF-048, CR-rg-daily-phone-board) ---
// The board's uid is assessment.boardUid; local state is keyed by the posting uid. `links` is recorded at push from the
// assessments (never from titles) and must be one-to-one. Anything else is held, visibly, never guessed.
export type BoardLinks = {
  version: 1;
  links: Record<string, string>; // boardUid -> postingUid
  ambiguous: Record<string, string[]>; // boardUid -> the competing postingUids
};
export type Identity =
  | { kind: 'resolved'; postingUid: string }
  | { kind: 'held'; reason: 'unknown' | 'ambiguous' };

export function resolveBoardIdentity(
  links: BoardLinks | null,
  boardUid: string,
  knownPostingUids: readonly string[],
): Identity {
  if (!links) return { kind: 'held', reason: 'unknown' };
  if (Object.prototype.hasOwnProperty.call(links.ambiguous, boardUid))
    return { kind: 'held', reason: 'ambiguous' };
  if (!Object.prototype.hasOwnProperty.call(links.links, boardUid))
    return { kind: 'held', reason: 'unknown' };
  const postingUid = links.links[boardUid]!;
  const sharing = Object.entries(links.links).filter(([, p]) => p === postingUid).length;
  if (sharing > 1) return { kind: 'held', reason: 'ambiguous' };
  if (!knownPostingUids.includes(postingUid)) return { kind: 'held', reason: 'unknown' };
  return { kind: 'resolved', postingUid };
}

/** The state vocabulary (rolegenie.py DECISIONS) for a board decision; Already applied is the recorded mark `applied`. */
export function stateDecisionFor(d: SheetDecision): 'apply' | 'consider' | 'reject' | 'applied' {
  return d === 'applied-externally' ? 'applied' : d;
}

/** What `pull` hands the integrator (038 wires it to CV and preference consumers). */
export type ImportedDecision = {
  eventId: string;
  boardUid: string;
  postingUid: string;
  decision: ReturnType<typeof stateDecisionFor>;
  boardDecision: SheetDecision;
  reason: string;
  decidedAt: string;
  source: (typeof EVENT_SOURCES)[number];
};
export type HeldDecision = { eventId: string; boardUid: string; reason: string };
export type PullImport = {
  imported: ImportedDecision[];
  held: HeldDecision[];
  alreadyKnown: number;
};

/** One job as pushed. The list columns are text: one `id text` line per requirement. */
export type AssessmentRow = {
  uid: string; // assessment.boardUid (assessment.ts): the normalised link when there is one, else the ad's sha
  title: string;
  company: string;
  url: string;
  score100: number;
  unmetHard: string; // shown FIRST on the page (AIM step 4)
  confirmedGaps: string;
  missingEvidence: string;
  uncertain: string;
  assessedAt: string;
  cvStatus?: string; // BRIEF-048: only when supplied; "" clears
  docLink?: string;
  docNote?: string;
};

// --- Decisions ---

/** Apply / Consider / Reject / Already applied. 'archive' (owner-era, remove without learning) is not on this board. */
export type SheetDecision = Exclude<BoardDecision, 'archive'>;
export const SHEET_DECISIONS: readonly SheetDecision[] = BOARD_DECISIONS.filter(
  (d): d is SheetDecision => d !== 'archive',
);
/** A reason is required on Apply and on Reject (AIM step 5); Consider and Already applied take an optional one. */
export const REASON_REQUIRED: readonly SheetDecision[] = ['apply', 'reject'];

/** Reasons a tap must be refused. Empty means it is recorded. Apply only records the decision (D5a: no auto-apply). */
export function decisionProblems(d: { uid: string; decision: string; reason: string }): string[] {
  const problems: string[] = [];
  if (String(d.uid ?? '').trim() === '') problems.push('decision needs a job uid');
  if (!(SHEET_DECISIONS as readonly string[]).includes(d.decision))
    problems.push(`unknown decision '${d.decision}'`);
  else if (
    (REASON_REQUIRED as readonly string[]).includes(d.decision) &&
    String(d.reason ?? '').trim() === ''
  )
    problems.push(`${d.decision} needs a reason`);
  return problems;
}

// --- The web app API (doPost; the page's taps go through the same handler) ---

export type PushRequest = { action: 'push'; secret: string; rows: AssessmentRow[] };
export type PullRequest = { action: 'pull'; secret: string };
export type DecideRequest = {
  action: 'decide';
  secret: string;
  uid: string;
  decision: SheetDecision;
  reason: string;
  /** One per deliberate tap (BRIEF-037). A retry of the same tap reuses it; a missing one means "no dedupe". */
  requestId?: string;
};
export type BoardRequest = PushRequest | PullRequest | DecideRequest;

export type PulledDecision = {
  uid: string;
  decision: SheetDecision;
  reason: string;
  decidedAt: string;
};
export type PushResponse = {
  ok: true;
  added: number;
  updated: number;
  docsVersion: typeof DOCS_VERSION;
};

// --- Decision history (BRIEF-037, CR-rg-board-decision-history) ---
// The `Events` sheet is an append-only ledger: one event per tap, written BEFORE the Board row's decision columns (the
// latest-state projection) are updated. A retried request id yields the original event. Rows that already carried a
// decision when the ledger was first created are seeded once as `legacy-snapshot` events: history before that cannot
// be reconstructed, and hand edits to the Board's decision columns after seeding never enter the ledger.
export const EVENTS_SHEET_NAME = 'Events';
export const EVENTS_SEEDED_PROPERTY = 'EVENTS_SEEDED';
export const HISTORY_VERSION = 1;
export const EVENT_COLUMNS = [
  'eventId',
  'requestId',
  'uid',
  'decision',
  'reason',
  'decidedAt',
  'source',
] as const;
export const EVENT_SOURCES = ['tap', 'legacy-snapshot'] as const;
export const MAX_REQUEST_ID_LENGTH = 100;
export type DecisionEvent = {
  eventId: string;
  requestId: string;
  uid: string;
  decision: SheetDecision;
  reason: string;
  decidedAt: string;
  source: (typeof EVENT_SOURCES)[number];
};

/** Reasons a ledger event must not be kept: the decision rules plus identity, time and source. */
export function decisionEventProblems(e: Record<string, unknown>): string[] {
  const s = (v: unknown) => (typeof v === 'string' ? v : '');
  const problems = decisionProblems({
    uid: s(e['uid']),
    decision: s(e['decision']),
    reason: s(e['reason']),
  });
  if (s(e['eventId']).trim() === '') problems.push('event needs an eventId');
  if (s(e['decidedAt']).trim() === '') problems.push('event needs a decidedAt');
  if (!(EVENT_SOURCES as readonly string[]).includes(s(e['source'])))
    problems.push(`unknown event source '${s(e['source'])}'`);
  return problems;
}

/** `decisions` is the latest-state view (unchanged); `events` is the whole ledger. A server without them is old. */
export type PullResponse = {
  ok: true;
  decisions: PulledDecision[];
  historyVersion: typeof HISTORY_VERSION;
  events: DecisionEvent[];
};
export type DecideResponse = {
  ok: true;
  uid: string;
  decision: SheetDecision;
  decidedAt: string;
  eventId: string;
};
/** A refusal carries an error and NO row data. A wrong or missing secret is always `refused`. */
export type Refusal = { ok: false; error: string };
export const REFUSED = 'refused';

/** Where the secret lives: `~/.rolegenie/board.json` (RoleGenie) and Script Property `BOARD_SECRET` (the web app). */
export const SECRET_PROPERTY = 'BOARD_SECRET';
export const SHEET_NAME = 'Board';
