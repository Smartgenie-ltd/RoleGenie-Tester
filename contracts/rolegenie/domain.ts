// RoleGenie domain rules — AUTHORITATIVE TypeScript specification (arch.md §4, §6, §9,
// §11, §12). CR-rg-arch-v1.
//
// RoleGenie's runtime is Python. This file states the rules the Python must obey as
// small pure functions; tests/contract/rolegenie.test.mjs pins them, and each product
// slice's Python tests are its acceptance evidence against them.
//
// Erasable-TypeScript only (Node 24 type-stripping): no enum, namespace, decorators,
// dependencies or Node type references.

// ---------------------------------------------------------------------------
// Invariants (arch.md §12)
// ---------------------------------------------------------------------------

export const INVARIANTS: Readonly<Record<string, string>> = Object.freeze({
  'INV-RG-01':
    'A posting is never marked seen unless it was assessed; a run that cannot assess writes nothing.',
  'INV-RG-02':
    'State and decision history are append-only; a re-run or re-import never duplicates or rewrites an entry.',
  'INV-RG-03':
    'No application is submitted without an owner Apply decision, until that checkpoint is dropped under §11.',
  'INV-RG-04':
    'Per-company limits and hard exclusions are checked immediately before every submission.',
  'INV-RG-05': 'Every CV claim traces to the master CV; nothing is invented.',
  'INV-RG-06':
    'Form answers come only from the answer bank; an unanswerable question holds the application.',
  'INV-RG-07':
    'Every submission ends verified, failed or held, and the owner is told of every failure or hold.',
  'INV-RG-08':
    'Personal data is never committed, never sent to the board, never placed in a model prompt that does not need it.',
  'INV-RG-09': 'No paid API keys; models run via subscription CLIs or locally, per config.',
  'INV-RG-10':
    'Checkpoint drops follow §11 exactly; a dropped checkpoint can always be reinstated.',
});

// ---------------------------------------------------------------------------
// Assessment bands (arch.md §4)
// ---------------------------------------------------------------------------

export type Band = 'apply' | 'consider' | 'archive';

export function bandOf(score: number): Band {
  if (!Number.isInteger(score) || score < 1 || score > 10) {
    throw new Error(`RG-BAND: score must be an integer 1-10, got ${score}`);
  }
  return score >= 7 ? 'apply' : score >= 4 ? 'consider' : 'archive';
}

// ---------------------------------------------------------------------------
// Owner decisions (arch.md §1, §4)
// ---------------------------------------------------------------------------

export type DecisionKind = 'apply' | 'consider' | 'reject';

// Board decisions include 'applied-externally' (Already applied): a distinct call that clears the
// role from the queue and is learned as a positive example (asCall maps it to 'apply'); and
// 'archive' (CR-rg-archive): remove a role from review WITHOUT learning anything from it —
// asCall maps it to null, so it is never a learning example, unlike reject.
export type BoardDecision = DecisionKind | 'applied-externally' | 'archive';
export const BOARD_DECISIONS: readonly BoardDecision[] = [
  'apply',
  'consider',
  'reject',
  'applied-externally',
  'archive',
];

export type Decision = {
  uid: string;
  decision: BoardDecision;
  reason: string;
  at: string;
  // CR-rg-jd-decompose: the requirements the owner ticked as having driven this
  // call, snapshotted at decision time (reject = blockers, apply = attractors).
  drivers?: DriverSnapshot[];
};

/** A reject must carry a short reason (A27) OR at least one ticked driver
 *  (CR-rg-jd-decompose); apply and consider may leave both empty. */
export function validateDecision(d: Decision): string[] {
  const errors: string[] = [];
  if (d.uid.trim() === '') errors.push('decision needs a posting uid');
  if (!BOARD_DECISIONS.includes(d.decision)) errors.push(`unknown decision '${d.decision}'`);
  if (d.decision === 'reject' && d.reason.trim() === '' && !(d.drivers && d.drivers.length > 0))
    errors.push('a reject needs a reason or at least one driver');
  if (Number.isNaN(Date.parse(d.at))) errors.push('decision time is not a date');
  return errors;
}

// ---------------------------------------------------------------------------
// JD decomposition (owner, 2026-10-04/05; CR-rg-jd-decompose)
// ---------------------------------------------------------------------------

// A single discrete ask extracted from an advert. `source` is a short verbatim
// excerpt the ask was drawn from, so every requirement is traceable and nothing is
// invented (INV-RG-05). `advertImportance` is read from the advert alone; `match`
// is the owner's standing against it, judged from the master.
export const REQUIREMENT_KINDS = [
  'tech',
  'competency',
  'soft',
  'location',
  'onsite-pattern',
  'clearance',
  'right-to-work',
  'qualification',
  'domain',
] as const;
export type RequirementKind = (typeof REQUIREMENT_KINDS)[number];

export const ADVERT_IMPORTANCE = ['required', 'preferred', 'contextual'] as const;
export type AdvertImportance = (typeof ADVERT_IMPORTANCE)[number];

export const MATCH_VALUES = ['meet', 'partial', 'gap', 'unknown'] as const;
export type MatchValue = (typeof MATCH_VALUES)[number];

// Jev (System One) product constants (CR-rg-jev-judge-move). The product scores requirements through
// `JEV_PRODUCT_MODULE`, which holds these and never imports from eval/ (the owner's labels stay out of a tester's clone).
// tests/contract/jev-core.test.mjs parses that module and compares.
export const JEV_MODEL = 'jev-1.13.0'; // pinned: never the moving jev-latest alias
export const JEV_ESCAPE_OPTION = 'unknown'; // the always-present escape option of the match Choice
export const JEV_REQUIREMENT_TEXT_LIMIT = 600; // advert-derived requirement text is cut to this many characters
export const JEV_INSTRUCTION_MARKERS = [
  'ignore previous',
  'ignore the above',
  'disregard',
  'system prompt',
  'you are now',
  'new instructions',
] as const; // requirement text containing any of these is omitted, not carried
export const JEV_PRODUCT_MODULE = 'jev_core.py';

export const OWNER_PRIORITY = ['high', 'med', 'low'] as const;
export type OwnerPriority = (typeof OWNER_PRIORITY)[number];

// Data-sanity ceiling on the STORED requirements per posting. Also Jev's
// questions-per-call limit, so a later per-requirement scoring pass stays one call
// per posting. The stored decomposition keeps every valid requirement up to this
// ceiling (importance-ranked retention only when a posting exceeds it); the review
// checklist is bounded separately, at display time (see CHECKLIST_LIMIT).
export const MAX_REQUIREMENTS = 50;

// At most this many requirements shown in the owner's review checklist, so the
// review never recreates the cognitive-load problem this bound exists to solve.
// Applied at display time (board.py) over the priority-ordered list — never to the
// stored data the scorer sees.
export const CHECKLIST_LIMIT = 15;

// --- Production fit score (CR-rg-score-rollup; BRIEF-012 measured, BRIEF-013 switched) ---
// The score is Jev's per-requirement match (per the /jev skill) rolled up in code:
//   rollup100 = 100 * Σ(importanceWeight · matchWeight) / Σ(importanceWeight)
// Weights are FIXED and DECLARED here (never fitted to the owner's decisions — the
// two-planes rule: fit is a calculation, the apply decision is human perception).
//   match: meet=full credit; partial=half (related/lesser evidence); gap=none (stated
//   absence); unknown=none (silence is not evidence — a conservative lower bound).
export const MATCH_WEIGHTS: Record<MatchValue, number> = {
  meet: 1.0,
  partial: 0.5,
  gap: 0.0,
  unknown: 0.0,
};
//   importance: weight ∝ how essential the advert makes it (the certified, section-
//   function importance); contextual duties weigh least but not zero.
export const IMPORTANCE_WEIGHTS: Record<AdvertImportance, number> = {
  required: 1.0,
  preferred: 0.5,
  contextual: 0.25,
};
// The roll-up is 0–100; the board score keeps its 0–10 meaning (bands, triage, tests)
// via score10 = round(rollup100 / 10). KNOWN CONSEQUENCE (reported, not hidden):
// certified scores compress high, so the under-6 triage threshold is now
// mis-calibrated and awaits the owner's re-tune on live data — NOT changed here.
export const SCORE_SCALE = 100;

// A hydrated posting whose decomposition yields fewer than this many requirements is
// NOT scored — it is marked thin and held pending fuller hydration (mirrors the
// hydrate gate). The five thin Palantir fixtures are the known cases.
export const THIN_MIN_REQUIREMENTS = 4;

// --- Outcome loop (CR-rg-outcome-loop; BRIEF-014) ---
// Every apply (board, CLI, or external) becomes an application record; its outcome runs a
// small lifecycle so the funnel — surfaced → applied → rejected/lapsed/interview → landed —
// is measurable (the north star: interviews per application). Outcomes are ground truth for
// that funnel, NEVER scorer-tuning targets (two-planes).

export const OUTCOME_STATES = [
  'open',
  'rejected',
  'interview',
  'offer',
  'landed',
  'lapsed',
] as const;
export type OutcomeState = (typeof OUTCOME_STATES)[number];

export const APPLY_CHANNELS = ['site', 'manual', 'external', 'referral', 'other'] as const;
export type ApplyChannel = (typeof APPLY_CHANNELS)[number];

// Legal outcome transitions. `open` awaits feedback; `lapsed` is ghosting (a distinct signal
// from a `rejected`) and reopens if late feedback arrives; `rejected` and `landed` are
// terminal. A post-lapse rejection is lapsed→rejected, keeping both timestamps in history.
export const OUTCOME_TRANSITIONS: Record<OutcomeState, OutcomeState[]> = {
  open: ['rejected', 'interview', 'lapsed'],
  interview: ['offer', 'rejected', 'lapsed'],
  offer: ['landed', 'rejected'],
  lapsed: ['rejected', 'interview'],
  rejected: [],
  landed: [],
};

// The owner's no-feedback window: an `open` application older than this many days lapses.
// Overridable via config `outcomes.lapse_days`; this is the default.
export const LAPSE_DAYS = 28;

export type OutcomeEvent = {
  state: OutcomeState;
  at: string; // ISO
  note: string;
  evidence: string; // how it was decided: 'owner', 'lapse-pass', or a matcher proposal summary
};

export type ApplicationRecord = {
  appliedAt: string; // ISO; when the apply was recorded
  channel: ApplyChannel;
  cvRef: string | null; // which tailored CV (folder or master sha); null for a manual apply
  referral: boolean; // a recorded high-value lever (default false)
  outcome: OutcomeState; // current state
  outcomeAt: string; // ISO; when the current state was entered
  note: string; // short free text on the latest transition
  history: OutcomeEvent[]; // append-only: every state entry with its evidence
};

/** A transition is legal when the target is listed for the current state. */
export function outcomeCanTransition(from: OutcomeState, to: OutcomeState): boolean {
  return (OUTCOME_TRANSITIONS[from] ?? []).includes(to);
}

export type Requirement = {
  id: string; // assigned locally by position (r1, r2, …); never minted by the model
  text: string;
  source: string; // verbatim advert excerpt backing the requirement (INV-RG-05)
  kind: RequirementKind;
  advertImportance: AdvertImportance;
  match: MatchValue;
};

// Stored on a decision for each ticked requirement: a snapshot taken at decision
// time, so a later re-score cannot change what a past decision meant (INV-RG-02).
export type DriverSnapshot = {
  id: string;
  text: string;
  kind: RequirementKind;
  advertImportance: AdvertImportance;
  match: MatchValue;
};

/**
 * Reasons one requirement object from the scorer cannot be used; empty means it
 * passes. The model supplies text, source, kind, advertImportance and match; it
 * must not supply an id (assigned locally), and its values must be in range.
 */
export function requirementProblems(raw: unknown): string[] {
  if (raw === null || typeof raw !== 'object') return ['requirement is not an object'];
  const o = raw as Record<string, unknown>;
  const problems: string[] = [];
  if (!(typeof o['text'] === 'string' && o['text'].trim())) problems.push('requirement needs text');
  if (!(typeof o['source'] === 'string' && o['source'].trim()))
    problems.push('requirement needs a source excerpt');
  if (!REQUIREMENT_KINDS.includes(o['kind'] as RequirementKind))
    problems.push(`unknown kind '${String(o['kind'])}'`);
  if (!ADVERT_IMPORTANCE.includes(o['advertImportance'] as AdvertImportance))
    problems.push(`unknown advertImportance '${String(o['advertImportance'])}'`);
  if (!MATCH_VALUES.includes(o['match'] as MatchValue))
    problems.push(`unknown match '${String(o['match'])}'`);
  return problems;
}

// Ordering rank: higher sorts first. Advert importance dominates, the owner's
// priority breaks ties within a band. Ordering only — it changes no score.
const IMPORTANCE_RANK: Record<AdvertImportance, number> = {
  required: 2,
  preferred: 1,
  contextual: 0,
};
const PRIORITY_RANK: Record<OwnerPriority, number> = { high: 2, med: 1, low: 0 };

export function reviewPriority(
  advertImportance: AdvertImportance,
  ownerPriority: OwnerPriority,
): number {
  return IMPORTANCE_RANK[advertImportance] * 3 + PRIORITY_RANK[ownerPriority];
}

/** Requirements ordered for the review pane and the driver checklist, highest
 *  first. Stable: equal ranks keep their original order. */
export function orderByPriority(
  reqs: readonly Requirement[],
  ownerPriorityOf: (r: Requirement) => OwnerPriority,
): Requirement[] {
  return reqs
    .map((r, i) => ({ r, i, rank: reviewPriority(r.advertImportance, ownerPriorityOf(r)) }))
    .sort((a, b) => b.rank - a.rank || a.i - b.i)
    .map((x) => x.r);
}

/**
 * One learning-example line for a decision's drivers: the sign comes from the
 * decision kind (reject = blocked by, apply/consider = drawn by), and each driver
 * carries its advert importance and match so the cause is unambiguous. Empty string
 * when there are no drivers.
 */
export function driverLine(decisionKind: DecisionKind, drivers: readonly DriverSnapshot[]): string {
  if (!drivers.length) return '';
  const verb = decisionKind === 'reject' ? 'blocked by' : 'drawn by';
  // Driver text is advert-derived data: quote it so it reads as a datum, not an
  // instruction, in the learning prompt (review #7). The Python mirror also flattens
  // it to a single bounded line.
  const parts = drivers.map((d) => `"${d.text}" (${d.advertImportance}, ${d.match})`);
  return `${verb}: ${parts.join('; ')}`;
}

// ---------------------------------------------------------------------------
// Applications (arch.md §9, INV-RG-06, INV-RG-07)
// ---------------------------------------------------------------------------

export type ApplicationStatus = 'queued' | 'submitted' | 'verified' | 'failed' | 'held';

/** The states an attempt may end in. `submitted` is not an end: it must be verified. */
export const TERMINAL_STATUSES: readonly ApplicationStatus[] = ['verified', 'failed', 'held'];

/** Statuses the owner must be told about (INV-RG-07). */
export function needsOwnerNotice(status: ApplicationStatus): boolean {
  return status === 'failed' || status === 'held';
}

// ---------------------------------------------------------------------------
// Submission guard (INV-RG-03, INV-RG-04; arch.md §6 limits)
// ---------------------------------------------------------------------------

export const COMPANY_LIMIT = { applications: 2, days: 30 } as const;
export const AUTONOMOUS_MIN_SCORE = 7;

export type SubmitCheck = {
  ownerApproved: boolean; // an owner Apply decision exists for this posting
  submitCheckpointDropped: boolean; // §11: the submit checkpoint has been earned away
  score: number;
  excluded: boolean; // a hard exclusion (A17) matched
  companySubmissionsAt: readonly string[]; // ISO times of earlier submissions to this company
  companyPublishedLimit: { applications: number; days: number } | null; // the company's own rule wins
  now: string; // ISO time of the check
};

/** Reasons a submission must not go ahead; empty means it may. */
export function submitBlockers(c: SubmitCheck): string[] {
  const reasons: string[] = [];
  if (!c.ownerApproved) {
    if (!c.submitCheckpointDropped) reasons.push('no owner Apply decision (INV-RG-03)');
    else if (c.score < AUTONOMOUS_MIN_SCORE)
      reasons.push(`score ${c.score} is below ${AUTONOMOUS_MIN_SCORE} for an autonomous submit`);
  }
  if (c.excluded) reasons.push('a hard exclusion matches (INV-RG-04)');
  const limit = c.companyPublishedLimit ?? COMPANY_LIMIT;
  const since = Date.parse(c.now) - limit.days * 86_400_000;
  const recent = c.companySubmissionsAt.filter((t) => Date.parse(t) > since).length;
  if (recent >= limit.applications) {
    reasons.push(
      `company limit reached: ${recent} of ${limit.applications} in ${limit.days} days (INV-RG-04)`,
    );
  }
  return reasons;
}

// ---------------------------------------------------------------------------
// Checkpoint trust (arch.md §11, A22, INV-RG-10)
// ---------------------------------------------------------------------------

export type Checkpoint = 'pick' | 'cv' | 'submit';
export const DROP_AFTER = 20;

export type CheckpointTrust = {
  checkpoint: Checkpoint;
  streak: number; // consecutive correct predictions
  state: 'active' | 'dropped' | 'reinstated';
};

/** Record one prediction outcome. A miss resets the streak; 20 in a row drops it. */
export function recordPrediction(t: CheckpointTrust, correct: boolean): CheckpointTrust {
  if (t.state === 'dropped') return t; // no owner call to compare against once dropped
  const streak = correct ? t.streak + 1 : 0;
  return { ...t, streak, state: streak >= DROP_AFTER ? 'dropped' : t.state };
}

/** The owner can always put a checkpoint back; its streak starts again from zero. */
export function reinstate(t: CheckpointTrust): CheckpointTrust {
  return { ...t, streak: 0, state: 'reinstated' };
}

// ---------------------------------------------------------------------------
// Local board (arch.md §10 D1, RG-S02; CR-rg-s02)
// ---------------------------------------------------------------------------

// The board listens only on localhost and the PC's Tailscale address (D1). A request
// whose Host header names anything else is refused, so a web page elsewhere cannot
// reach it by DNS rebinding.
export function isAllowedHost(host: string, allowed: readonly string[]): boolean {
  const name = host.trim().toLowerCase().replace(/:\d+$/, '');
  return allowed.some((a) => a.toLowerCase() === name);
}

// A state-changing call must carry this header. Browsers cannot add a custom header to
// a cross-site request without a CORS preflight, which the board never grants.
export const BOARD_WRITE_HEADER = 'X-RoleGenie';

// Every state write (nightly run, board, `mark`) happens under one lock and re-reads
// state first, so no writer overwrites another's entries (INV-RG-02).
export const STATE_WRITE_RULE = 'lock, re-read, apply, write atomically' as const;

// ---------------------------------------------------------------------------
// Tailored CV (arch.md §7, RG-S03; INV-RG-05, INV-RG-08; CR-rg-s03)
// ---------------------------------------------------------------------------

// A role heading exactly as the master CV holds it.
export type RoleHeading = { employer: string; title: string; dates: string };

// What the model returns. Contact details are NOT part of it: they are added locally
// from the master, so the model never needs them (INV-RG-08).
export type TailoredCv = {
  headline: string; // may mirror the target role (A12)
  profile: string[];
  coreExpertise: string[];
  experience: (RoleHeading & { bullets: string[] })[];
  selectedWork: { name: string; text: string }[];
  certifications: string[];
  education: string[];
};

/** Numbers in a text, normalised (thousands commas and trailing dots dropped). */
export function numbersIn(text: string): string[] {
  return (text.match(/\d[\d,.]*/g) ?? []).map((n) => n.replace(/,/g, '').replace(/\.+$/, ''));
}

/** Wording that admits or hedges a gap (CR-rg-tailor-v2). */
export const GAP_WORDING =
  /\bnot yet (?:part of|used|worked|had|hands-on|certified|experienced)\b|\bhands-on record\b|\b(?:limited|minimal|little|some|basic|initial|no) (?:direct |prior |previous |formal |hands-on |production |commercial |working )?(?:experience|exposure|knowledge)\b(?!-)|\b(?:limited|minimal|some|basic|initial) exposure to\b|\bfamiliar(?:ity)? with\b|\bcurrently learning\b|\btransferable\b|\(training\)|\bnot a certification\b|\bnot (?:currently |yet )?(?:sc|dv|security)[ -]?cleared\b|\buncleared\b|\bdo(?:es)? not hold (?:an? |any )?(?:sc|dv|security)? ?clearance\b|\b(?:have|has) not used\b|\bnever used\b|\bnot (?:yet )?certified\b|\bwithout (?:any |direct |prior |formal |hands-on )?(?:experience|exposure)\b|\black(?:s|ing)? (?:of )?(?:direct |prior |hands-on )?(?:experience|exposure|knowledge)\b|(?:^|\n)\s*new to\b|\b(?:am|relatively|still|being|fairly|completely) new to\b|\b(?:keen|eager|willing) to learn\b/i;

/**
 * Reasons a tailored CV cannot be used; empty means it passes. Every role heading must
 * be one the master holds, in the master's order; every number must appear in the
 * master; no contact detail may come from the model; no gap may be admitted.
 */
export function cvProblems(
  cv: TailoredCv,
  masterText: string,
  masterRoles: readonly RoleHeading[],
  knowledgeOnly: readonly string[] = [],
): string[] {
  const problems: string[] = [];
  const key = (r: RoleHeading): string => `${r.employer} | ${r.title} | ${r.dates}`;
  const order = masterRoles.map(key);
  let last = -1;
  for (const r of cv.experience) {
    const at = order.indexOf(key(r));
    if (at < 0) problems.push(`role heading not in the master: ${key(r)}`);
    else if (at < last) problems.push(`role out of the master's order: ${key(r)}`);
    else last = at;
  }
  // CR-rg-tailor-v2: every master role is kept, so stated years of experience add up.
  const kept = new Set(cv.experience.map(key));
  for (const k of order) if (!kept.has(k)) problems.push(`role missing from the CV: ${k}`);
  const masterNumbers = new Set(numbersIn(masterText));
  const written = [
    cv.headline,
    ...cv.profile,
    ...cv.coreExpertise,
    ...cv.experience.flatMap((r) => r.bullets),
    ...cv.selectedWork.map((w) => `${w.name} ${w.text}`),
  ].join('\n');
  const text = [written, ...cv.certifications, ...cv.education].join('\n');
  for (const n of new Set(numbersIn(text))) {
    if (!masterNumbers.has(n)) problems.push(`number not in the master: ${n}`);
  }
  if (/@|\+44|\b07\d{3}\s?\d{6}\b/.test(text))
    problems.push('contact details must not come from the model');
  // A CV is written in the implied first person: never about "the candidate".
  if (/\bthe candidate\b/i.test(text))
    problems.push('write in the implied first person, not "the candidate"');
  // CR-rg-tailor-v2: the CV never states or hedges a gap; gaps belong in the report.
  // Certifications and education are copied verbatim from the master, so only written text is checked.
  if (GAP_WORDING.test(written))
    problems.push('state no gap or hedge in the CV; gaps go only in the report');
  // CR-rg-knowledge-only: a capability the owner knows but has not delivered with recently is listed
  // plainly as a capability, never as delivered work. Delivered-work sections may not name it at all;
  // the headline, profile and core expertise may not pair it with a delivery verb.
  const delivered = norm(
    [
      ...cv.experience.flatMap((r) => r.bullets),
      ...cv.selectedWork.map((w) => `${w.name} ${w.text}`),
    ].join('\n'),
  );
  const claims = [cv.headline, ...cv.profile, ...cv.coreExpertise]
    .flatMap((l) => l.split(/[.;:]/))
    .map(norm);
  for (const k of knowledgeOnly) {
    const nk = norm(k);
    if (
      nk &&
      (delivered.includes(nk) || claims.some((c) => c.includes(nk) && DELIVERY_VERB.test(c)))
    )
      problems.push(`${k} is knowledge only: list it as a capability, not as delivered work`);
  }
  return problems;
}

// ---------------------------------------------------------------------------
// Board review view (RG-S04; CR-rg-s04)
// ---------------------------------------------------------------------------

export const SERVABLE_KINDS: readonly string[] = ['pdf', 'docx'];

/**
 * The board serves a CV file only when state records that exact path for the posting
 * and it sits inside the jobs folder. A request never names a path itself.
 */
export function servablePath(recorded: string | null | undefined, jobsDir: string): string | null {
  if (!recorded) return null;
  const norm = (p: string): string => p.replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
  const r = norm(recorded);
  const root = norm(jobsDir);
  if (r.split('/').includes('..')) return null;
  return r.startsWith(`${root}/`) ? recorded : null;
}

// ---------------------------------------------------------------------------
// Daily brief (A23, A24, RG-S06; CR-rg-s06)
// ---------------------------------------------------------------------------

// Order of the brief: anything needing the owner first (A24), then what's new, then
// what still waits for a decision.
export const BRIEF_SECTIONS = [
  'Needs you',
  'New since the last brief',
  'Waiting for your decision',
] as const;

// `to` is every recipient of the call (to, cc and bcc together); `draft` is true when the
// call sent a stored draft instead of a new message.
export type SendCall = { tool: string; to: readonly string[]; draft?: boolean };

// Loading a deferred tool's schema is not an action; it is ignored by the check.
export const TOOL_LOADER = 'ToolSearch';

/**
 * The brief is emailed by a model run allowed only the Gmail send tool. Afterwards the
 * run's tool calls are checked: exactly one send, to the owner alone. Anything else is
 * reported as a failure.
 */
export function briefSendProblems(calls: readonly SendCall[], owner: string): string[] {
  const acting = calls.filter((c) => c.tool !== TOOL_LOADER);
  const sends = acting.filter((c) => c.tool.endsWith('send_message'));
  const problems: string[] = [];
  if (acting.length !== sends.length) problems.push('a tool other than send_message was called');
  if (sends.some((c) => c.draft)) problems.push('a stored draft was sent');
  if (sends.length !== 1) problems.push(`expected one send, saw ${sends.length}`);
  for (const s of sends) {
    const to = s.to.map((a) => a.trim().toLowerCase());
    if (to.length !== 1 || to[0] !== owner.toLowerCase())
      problems.push(`sent to ${s.to.join(', ') || 'nobody'}`);
  }
  return problems;
}

// ---------------------------------------------------------------------------
// Matching and exclusions (arch.md §6; A17, A25, A26; RG-S07; CR-rg-s07)
// ---------------------------------------------------------------------------

export type Zone = 'uk' | 'europe';

// What the scorer reads from the advert. null means the advert doesn't say.
export type PostingFacts = {
  employmentType: 'permanent' | 'contract' | null;
  remote: boolean | null;
  languageRequired: string | null; // a language other than English the role needs
  quotaSales: boolean | null;
  // Contract details (CR-rg-contracts). null when the advert does not say.
  ir35: 'inside' | 'outside' | null;
  dayRate: string | null; // as the advert states it, e.g. "£550-£600 per day"
  length: string | null; // e.g. "6 months"
};

// Where a posting sits on the board (owner, 2026-10-02, HITL #23). Inside-IR35 contracts
// are personal roles like permanent ones; outside-IR35 contracts go through the user's own limited company;
// nothing is excluded for IR35.
export type Category = 'permanent' | 'contract-inside' | 'contract-outside' | 'contract-unstated';

/** A posting is a contract when the advert says so, or when it says nothing and the source
 *  listed it as a contract (Adzuna's contract filter). */
export function categoryOf(f: PostingFacts, listedAsContract: boolean): Category {
  const contract =
    f.employmentType === 'contract' || (f.employmentType === null && listedAsContract);
  if (!contract) return 'permanent';
  return f.ir35 === 'outside'
    ? 'contract-outside'
    : f.ir35 === 'inside'
      ? 'contract-inside'
      : 'contract-unstated';
}

/** Why a scored posting is excluded, or null when it stays in. */
export function exclusionReason(zone: Zone, f: PostingFacts): string | null {
  if (zone === 'europe' && !(f.employmentType === 'contract' && f.remote === true)) {
    return 'outside the UK and not a remote contract role';
  }
  if (f.languageRequired && f.languageRequired.trim().toLowerCase() !== 'english') {
    return `requires ${f.languageRequired.trim()}`;
  }
  if (f.quotaSales === true) return 'quota-carrying sales role';
  return null;
}

// Seniority words that exclude a title before scoring (A17). "Associate" is excluded
// only as a junior grade, not in senior titles such as "Associate Director".
export const JUNIOR_TITLE =
  /\b(junior|jr\.?|rotational|rotation programme|rotation program)\b|\bassociate\b(?!\s+(director|partner|principal|vice president|vp)\b)/i;

// ---------------------------------------------------------------------------
// Re-score on master change (CR-rg-rescore)
// ---------------------------------------------------------------------------

// The 1-10 fit score is computed once, against the master CV in force when the
// posting was first scored, so each score is stamped with the master's
// fingerprint. A score is stale when it carries no fingerprint (scored before
// stamping) or one that differs from the current master; a stale, still-undecided
// posting is re-scored against the current master.
export function masterStale(
  scoredMaster: string | null | undefined,
  currentMaster: string,
): boolean {
  return !scoredMaster || scoredMaster !== currentMaster;
}

// The owner reviews only score >= REVIEW_MIN_SCORE; anything below is excluded
// from the review queue. Exclusion is NOT a rejection and never becomes a
// learning example — it only keeps the board manageable. A re-score re-applies
// this threshold: a posting that rises to it is released into review, one that
// drops below it is excluded with this reason. A hard exclusion (exclusionReason)
// always takes precedence over the threshold.
export const REVIEW_MIN_SCORE = 6;
export function belowThresholdReason(score: number, min: number = REVIEW_MIN_SCORE): string | null {
  if (!Number.isInteger(score) || score < 1 || score > 10) {
    throw new Error(`RG-RESCORE: score must be an integer 1-10, got ${score}`);
  }
  return score < min ? `below review threshold (score < ${min})` : null;
}

// ---------------------------------------------------------------------------
// Master score card (CR-rg-master-card)
// ---------------------------------------------------------------------------

// A compact, versioned distillation of the master CV, used as the candidate
// profile for scoring in place of the full CV blob. Generated once per master
// version (keyed by the master fingerprint; see masterStale) and faithful to the
// master — every line traces to the master, nothing is invented (INV-RG-05).
export const MASTER_CARD_FIELDS = [
  'positioning',
  'seniority',
  'competencies',
  'domains',
  'deliveryRecord',
  'certifications',
  'education',
  'knownGaps',
  'constraints',
] as const;

// Validates a generated card before it is cached or used: the two headline fields are
// non-empty strings; the evidence lists (competencies, domains, deliveryRecord) and knownGaps
// are non-empty string lists; certifications, education and constraints are string lists that
// may be empty. knownGaps is required non-empty (CR-rg-card-completeness): the master states
// explicit experience gaps, and a card that drops them makes the scorer match against a profile
// that appears to have no weaknesses, false-positiving on mandatory requirements.
export function cardProblems(raw: unknown): string[] {
  if (raw === null || typeof raw !== 'object') return ['card is not an object'];
  const o = raw as Record<string, unknown>;
  const problems: string[] = [];
  for (const f of ['positioning', 'seniority']) {
    if (!(typeof o[f] === 'string' && (o[f] as string).trim()))
      problems.push(`${f} must be a non-empty string`);
  }
  for (const f of ['competencies', 'domains', 'deliveryRecord', 'knownGaps']) {
    const v = o[f];
    if (!Array.isArray(v) || v.length === 0) problems.push(`${f} must be a non-empty list`);
    else if (!v.every((x) => typeof x === 'string' && x.trim()))
      problems.push(`${f} must be non-empty strings`);
  }
  // Credential and constraint lists may be empty (a master may hold none) but must be string lists.
  for (const f of ['certifications', 'education', 'constraints']) {
    const v = o[f];
    if (!Array.isArray(v) || !v.every((x) => typeof x === 'string'))
      problems.push(`${f} must be a list of strings`);
  }
  return problems;
}

// ---------------------------------------------------------------------------
// Hydration gate (owner, 2026-10-07; CR-rg-hydrate-gate)
// ---------------------------------------------------------------------------

// A JD must carry the full advert before it is scored. Some sources (the Adzuna
// API, an unresolved detail URL) yield only an excerpt; a score computed from an
// excerpt is wrong, and a wrong score is worse than no score. isHydrated is the
// deterministic gate: a body at or above HYDRATED_MIN_CHARS is treated as the
// full advert and may be scored; a shorter one is held as pending hydration, left
// unscored (score null), and never given a provisional number. The threshold
// separates real adverts (observed 1874+ chars) from excerpts (Adzuna runs ~500):
// on the board at adoption it held 108 of 111 Adzuna excerpts and 0 of 195 full
// adverts. Length only, by the agreed teaser heuristic.
export const HYDRATED_MIN_CHARS = 900;
export function isHydrated(body: string | null | undefined): boolean {
  return typeof body === 'string' && body.trim().length >= HYDRATED_MIN_CHARS;
}

// Correctness check for a FETCHED advert (CR-rg-hydrate-fetch): the output validator /
// handover regulation of the hydration stage. A fetch (Playwright, or an interactive
// browser fallback) hands over its result ONLY when this returns no problems. It is
// deterministic and model-free. It catches what a length gate cannot: an HTTP error or
// expired advert (a 404 page still renders ~20k chars of related-jobs chrome), and an
// anti-bot challenge / error page dressed as content. "hydrated" must mean correctly
// captured, not merely long enough.
export const HYDRATION_REJECT_PHRASES: readonly string[] = [
  'no longer available',
  'this job may no longer',
  'this vacancy has expired',
  'page not found',
  'just a moment',
  'checking your browser',
  'enable javascript and cookies',
  'attention required',
  'verify you are human',
  'access denied',
];
export function hydrationProblems(
  body: string | null | undefined,
  httpStatus?: number | null,
): string[] {
  const problems: string[] = [];
  if (typeof httpStatus === 'number' && httpStatus >= 400) {
    problems.push(`advert unavailable (HTTP ${httpStatus})`);
  }
  if (!isHydrated(body)) {
    problems.push('too short to be a full advert');
  }
  const low = (body ?? '').toLowerCase();
  for (const phrase of HYDRATION_REJECT_PHRASES) {
    if (low.includes(phrase)) problems.push(`not an advert: "${phrase}"`);
  }
  return problems;
}

// Where a fetched page's advert is read from (CR-rg-ad-page-chrome), in order. Adzuna's advert container
// (`section.adp-body`) is used unconditionally when present, as before, so the nightly scan is unchanged. Otherwise
// `main`, `[role=main]`, `article` are tried in order, and one is used only when BOTH the page body and that
// container pass `hydrationProblems` (text only): an expiry or anti-bot phrase outside the container still reaches
// the validator. When none is used, the page body is read, as before, so no real ad line is dropped by a guess (R1).
// Mirrored by `ADVERT_CONTAINERS` in hydrate_fetch.py.
export const ADVERT_CONTAINERS: readonly string[] = [
  'section.adp-body',
  'main',
  '[role=main]',
  'article',
];

// ---------------------------------------------------------------------------
// Application CV file (owner, 2026-09-27; CR-rg-s03)
// ---------------------------------------------------------------------------

// Every application uploads the tailored CV as a PDF (never the .docx), A4, carrying
// the least metadata possible: no document-information dictionary, no XMP stream.
export const APPLICATION_CV_FORMAT = 'pdf' as const;
export const PDF_METADATA_ALLOWED: readonly string[] = [];

// ---------------------------------------------------------------------------
// Learning from decisions (arch.md §11; RG-S08; CR-rg-s08)
// ---------------------------------------------------------------------------

// Earlier `mark` vocabulary maps onto the board's three calls.
export const LEGACY_DECISION: Readonly<Record<string, DecisionKind>> = Object.freeze({
  applied: 'apply',
  shortlisted: 'consider',
  ignored: 'reject',
});

export function asCall(decision: string): DecisionKind | null {
  if (decision === 'apply' || decision === 'consider' || decision === 'reject') return decision;
  // Applying externally is a positive signal: learn it like an apply (CR-rg-applied-externally).
  if (decision === 'applied-externally') return 'apply';
  return LEGACY_DECISION[decision] ?? null;
}

// A posting's owner call for learning and measurement is the FIRST call made on it:
// later changes of mind are kept in history but the first reaction is what a
// prediction made beforehand is compared with.
export function firstCall(history: readonly { decision: string }[]): DecisionKind | null {
  for (const h of history) {
    const c = asCall(h.decision);
    if (c) return c;
  }
  return null;
}

// The owner has FINALIZED a posting when they have made any learning call on it OR archived
// it (CR-rg-archive). Finalized postings leave the review queue and are not re-scored or
// re-decomposed, with ONE exception: the heal-on-hydrate transition (staleSinceProcessed,
// CR-rg-heal-on-hydrate) — a role decided while its advert was a teaser and since hydrated is
// re-decomposed and re-scored on the full advert, its decision history untouched. Archive is
// finalized but NOT a call, so it never becomes a learning example.
export function isFinalized(history: readonly { decision: string }[]): boolean {
  return history.some((h) => asCall(h.decision) !== null || h.decision === 'archive');
}

// The heal-on-hydrate rule (CR-rg-heal-on-hydrate; mirror of learning.stale_since_processed).
// A finalized posting is normally frozen, but one processed (scored / decomposed) while its
// advert was a teaser carries a recorded processing fingerprint — the advert it was last
// processed against (scoredAdvert, or the decomposition's advertSha). It is reopened to
// self-heal ONLY when that fingerprint is present AND differs from the current advert (the
// teaser → hydrated transition). A missing fingerprint is NOT stale — a legacy finalized role
// is never reopened — and a master change alone (same advert) never reopens a finalized
// decision.
export function staleSinceProcessed(
  processedAdvert: string | null | undefined,
  currentAdvert: string,
): boolean {
  return !!processedAdvert && processedAdvert !== currentAdvert;
}

/** A prediction counts only if it was recorded before the owner's first call. */
export function predictionOutcome(
  prediction: { decision: DecisionKind; at: string } | null,
  history: readonly { decision: string; at: string }[],
): 'correct' | 'wrong' | 'unmeasured' {
  const first = history.find((h) => asCall(h.decision) !== null);
  if (!prediction || !first) return 'unmeasured';
  if (Date.parse(prediction.at) >= Date.parse(first.at)) return 'unmeasured';
  return asCall(first.decision) === prediction.decision ? 'correct' : 'wrong';
}

// At most this many past calls go into the scoring prompt as examples, newest first.
export const MAX_EXAMPLES = 40;

// ---------------------------------------------------------------------------
// Trust replay (arch.md §11; A22; INV-RG-10; RG-S09; CR-rg-s09)
// ---------------------------------------------------------------------------

export type TrustEvent =
  | { kind: 'outcome'; at: string; correct: boolean }
  | { kind: 'reinstate'; at: string };

/**
 * A checkpoint's trust is never stored as a mutable number: it is replayed from its
 * measured outcomes and the owner's reinstatements, in time order.
 */
export function replayTrust(
  checkpoint: Checkpoint,
  events: readonly TrustEvent[],
): CheckpointTrust {
  let t: CheckpointTrust = { checkpoint, streak: 0, state: 'active' };
  const ordered = [...events].sort((a, b) => Date.parse(a.at) - Date.parse(b.at));
  for (const e of ordered)
    t = e.kind === 'reinstate' ? reinstate(t) : recordPrediction(t, e.correct);
  return t;
}

// ---------------------------------------------------------------------------
// Applying (arch.md §9; A24, A27; INV-RG-03, -04, -06, -07; RG-S05; CR-rg-s05)
// ---------------------------------------------------------------------------

// What one applier run reports. `need` says exactly what the owner must do for a held
// or failed attempt; `confirmation` is the site's confirmation text for a submission.
export type ApplierResult = {
  status: 'submitted' | 'held' | 'failed';
  need: string;
  confirmation: string;
};

/** Reasons an applier's report cannot be trusted as-is (then it is recorded as failed). */
export function applierResultProblems(r: unknown): string[] {
  if (r === null || typeof r !== 'object') return ['result is not an object'];
  const o = r as Record<string, unknown>;
  const problems: string[] = [];
  if (!['submitted', 'held', 'failed'].includes(o['status'] as string))
    problems.push('unknown status');
  if (o['status'] !== 'submitted' && !(typeof o['need'] === 'string' && o['need'].trim()))
    problems.push('a held or failed attempt must say what is needed');
  if (
    o['status'] === 'submitted' &&
    !(typeof o['confirmation'] === 'string' && o['confirmation'].trim())
  )
    problems.push('a submission must carry the confirmation it saw');
  return problems;
}

// A posting is queued for the applier when the owner's LATEST call is apply, its CV
// passed the master check, has a PDF and was drafted from the CURRENT master CV
// (CR-rg-profile-sync), and no attempt is submitted or verified yet.
export function isQueued(p: {
  latestCall: DecisionKind | null;
  cvStatus: string | null;
  hasPdf: boolean;
  masterFresh: boolean;
  attempts: readonly { status: ApplicationStatus }[];
}): boolean {
  if (p.latestCall !== 'apply' || p.cvStatus !== 'drafted' || !p.hasPdf || !p.masterFresh)
    return false;
  return !p.attempts.some((a) => a.status === 'submitted' || a.status === 'verified');
}

// ---------------------------------------------------------------------------
// Interview ceiling and the tailoring gate (owner, 2026-10-02, HITL #24; CR-rg-cv-screen;
// tailoring gate superseded 2026-10-07 by CR-rg-cv-on-apply)
// ---------------------------------------------------------------------------

// The ceiling is a cold recruiter's view, judged from the master CV and the advert with no
// CV written: the chance (0-100) that the best honest CV gets an interview. A tailored CV
// can only rephrase the master, so it cannot beat the ceiling. The ceiling still ranks and
// informs review, but it no longer gates tailoring: under CR-rg-cv-on-apply a CV is drafted
// only when the owner applies (see wantsCv below), not proactively by ceiling.
export const MIN_CEILING_TO_TAILOR = 50;

// Tailored CV file names (CR-rg-tailor-cv-name, BRIEF-032 Amendment 1). The docx, PDF and
// alignment report share one stem: <name>_<title>_<board>. The name is the user's own, as their
// master CV gives it (read, never invented: R1; never the owner's: R4): its FIRST line, cut at
// the first `|`, stripped. When that is empty, or still holds `@` or a digit (contact data or a
// phone number never goes into a file name), the prefix is the neutral CV_FILE_PREFIX_DEFAULT.
// Mirrored in tailor.py (cv_file_stem). CR-rg-cv-name-accents: the name keeps its own letters
// (Unicode letters, marks and digits, NFC), so José stays José; any decimal digit in any script
// still gives CV. Title and board keep the ASCII rule (cvFileSafe).
export const CV_FILE_PREFIX_DEFAULT = 'CV';
export const CV_FILE_STEM_PARTS = ['name', 'title', 'board'] as const;
export const CV_FILE_STEM_LIMITS = { name: 40, title: 50, board: 30 } as const;

/** The file-safe rule (tailor.py `_safe` without its `x` fallback): runs of anything but
 *  ASCII letters and digits become `_`, trimmed of `_`, cut to `limit`. May be empty. */
export function cvFileSafe(text: string, limit: number): string {
  return text
    .replace(/[^A-Za-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, limit);
}

// The name part's UTF-8 byte cap: with title, board, two `_` and the longest suffix
// (`_Alignment_Report.md`, 20 bytes) a file name stays within 222 bytes, under Linux's 255-byte
// and Windows's 255-unit limits. An ASCII name (at most 40 bytes) never reaches it.
export const CV_FILE_NAME_MAX_BYTES = 120;
// Windows reserved device names (compared ASCII case-insensitively). The rule guards the whole
// stem only (co-arch ruling, BRIEF-073 attempt 2): a name that is a reserved word is kept as
// written ("Con" gives Con_...), since the joined stem always carries `_<title>_<board>`.
export const CV_FILE_RESERVED_NAMES = [
  'CON',
  'PRN',
  'AUX',
  'NUL',
  'COM1',
  'COM2',
  'COM3',
  'COM4',
  'COM5',
  'COM6',
  'COM7',
  'COM8',
  'COM9',
  'COM¹',
  'COM²',
  'COM³',
  'LPT1',
  'LPT2',
  'LPT3',
  'LPT4',
  'LPT5',
  'LPT6',
  'LPT7',
  'LPT8',
  'LPT9',
  'LPT¹',
  'LPT²',
  'LPT³',
] as const;

/** The name part's file-safe rule: NFC; runs of anything but Unicode letters, marks and digits
 *  (so every path separator, Windows-forbidden character, control character, space and dot)
 *  become `_`, trimmed of `_`; cut to `limit` code points, then to CV_FILE_NAME_MAX_BYTES of
 *  UTF-8 over whole code points (no re-trim after the cut, as before). May be empty. */
export function cvFileNameSafe(text: string, limit: number): string {
  const kept = text
    .normalize('NFC')
    .replace(/[^\p{L}\p{M}\p{N}]+/gu, '_')
    .replace(/^_+|_+$/g, '');
  const points = Array.from(kept).slice(0, limit);
  // UTF-8 length of one code point (no TextEncoder in the contract's TypeScript lib).
  const bytes = (c: string): number => {
    const cp = c.codePointAt(0) ?? 0;
    return cp < 0x80 ? 1 : cp < 0x800 ? 2 : cp < 0x10000 ? 3 : 4;
  };
  let size = points.reduce((n, c) => n + bytes(c), 0);
  while (size > CV_FILE_NAME_MAX_BYTES) size -= bytes(points.pop() ?? '');
  return points.join('');
}

/** The name prefix of a tailored CV's file names, from the master CV's name. */
export function cvFilePrefix(masterName: string): string {
  const first = (masterName.split(/\r?\n/)[0] ?? '').split('|')[0]?.trim() ?? '';
  if (/[@]|\p{Nd}/u.test(first)) return CV_FILE_PREFIX_DEFAULT;
  return cvFileNameSafe(first, CV_FILE_STEM_LIMITS.name) || CV_FILE_PREFIX_DEFAULT;
}

/** True when a whole stem is a Windows reserved device name (ASCII case-insensitive). */
export function cvFileReserved(stem: string): boolean {
  const upper = stem.replace(/[a-z]/g, (c) => c.toUpperCase());
  return (CV_FILE_RESERVED_NAMES as readonly string[]).includes(upper);
}

/** The shared stem of a tailored CV's docx, PDF and alignment report. Title and board keep
 *  the `_safe` rule's `x` when empty (unchanged from before this record). */
export function cvFileStem(masterName: string, title: string, board: string): string {
  const parts = {
    name: cvFilePrefix(masterName),
    title: cvFileSafe(title, CV_FILE_STEM_LIMITS.title) || 'x',
    board: cvFileSafe(board, CV_FILE_STEM_LIMITS.board) || 'x',
  };
  const stem = CV_FILE_STEM_PARTS.map((p) => parts[p]).join('_');
  // Guard on the whole stem (co-arch ruling, attempt 2): the joined stem cannot be reserved
  // today, but if it ever were, the neutral prefix keeps the file creatable on Windows.
  return cvFileReserved(stem) ? `${CV_FILE_PREFIX_DEFAULT}_${stem}` : stem;
}

// Verbs that turn a capability into a delivery claim (CR-rg-knowledge-only).
export const DELIVERY_VERB =
  /\b(built|build|builds|delivered|deliver|delivers|shipped|ships|deployed|deploys|implemented|implements|developed|develops|created|launched|ran|runs|operated|configured|set up|stood up|productionised|productionized|migrated)\b/;

/** Lower case, hyphens and runs of spaces as single spaces, so "AI-Foundry" matches "ai foundry". */
export function norm(text: string): string {
  return text
    .toLowerCase()
    .replace(/[\s-]+/g, ' ')
    .trim();
}

/** A ceiling the scorer returned, or null when it is missing or not a whole number 0-100. */
export function ceilingOf(raw: unknown): number | null {
  return typeof raw === 'number' && Number.isInteger(raw) && raw >= 0 && raw <= 100 ? raw : null;
}

/** Whether a posting should have a tailored CV drafted (CR-rg-cv-on-apply): only when the
 *  owner's latest board decision is a genuine Apply (not applied-externally, consider or
 *  reject) AND no current CV exists — none drafted, or one drafted from an older master.
 *  No resumes until Apply; the Apply tap drafts the first CV and the owner reviews it before
 *  applying manually. */
export function wantsCv(
  latestDecision: string | null | undefined,
  cv: { status?: string; masterSha?: string } | null | undefined,
  currentMaster: string,
): boolean {
  if (latestDecision !== 'apply') return false;
  return cv?.status !== 'drafted' || cv?.masterSha !== currentMaster;
}

// ---------------------------------------------------------------------------
// ATS parseability check (owner, 2026-10-05; CR-rg-ats-check)
// ---------------------------------------------------------------------------

// A CV must parse cleanly before any human sees it. The ruleset itself lives as
// editable data in ats_rules.yaml and the file-reading checks live in the Python
// engine (ats_check.py); these are the shared, pure pieces the report is built
// on, mirrored in Python. A rule fails as a BLOCKER (known to drop or garble
// content in a parser) or a WARNING (parses, but suboptimal).
export const ATS_SEVERITIES = ['blocker', 'warning'] as const;
export type AtsSeverity = (typeof ATS_SEVERITIES)[number];

// One rule's outcome against one resume artifact.
export type AtsFinding = {
  ruleId: string;
  severity: AtsSeverity;
  passed: boolean;
  detail: string; // why it failed; empty when it passed
};

// Whole-resume verdict: a failed blocker blocks sending, a failed warning only
// warns, everything passing is clean.
export const ATS_VERDICTS = ['clean', 'warnings', 'blocked'] as const;
export type AtsVerdict = (typeof ATS_VERDICTS)[number];

/** The verdict for a set of findings. */
export function atsVerdict(findings: readonly AtsFinding[]): AtsVerdict {
  if (findings.some((f) => !f.passed && f.severity === 'blocker')) return 'blocked';
  if (findings.some((f) => !f.passed && f.severity === 'warning')) return 'warnings';
  return 'clean';
}

/** True when the resume may be sent: no open blocker. Warnings never block. */
export function atsSendable(findings: readonly AtsFinding[]): boolean {
  return atsVerdict(findings) !== 'blocked';
}

// Canonical section names an ATS reliably recognises. A heading maps to one of
// these directly or through the synonym map (ruleset data); an unmapped heading
// on a core section is a blocker — the section is ignored or mislabelled.
export const ATS_CANONICAL_SECTIONS = [
  'Summary',
  'Experience',
  'Education',
  'Skills',
  'Certifications',
  'Projects',
  'Languages',
] as const;
export type AtsSection = (typeof ATS_CANONICAL_SECTIONS)[number];

/**
 * The canonical section a heading maps to, or null when nothing recognises it.
 * Case-insensitive; trailing punctuation/whitespace is ignored. A heading equal
 * to a canonical name maps to itself; otherwise the lowercased form is looked up
 * in `synonyms` (data from the ruleset, e.g. {'profile':'Summary'}).
 */
export function canonicalSection(
  heading: string,
  synonyms: Readonly<Record<string, string>>,
): string | null {
  const key = heading
    .trim()
    .toLowerCase()
    .replace(/[\s:._–—-]+$/u, '');
  for (const c of ATS_CANONICAL_SECTIONS) if (c.toLowerCase() === key) return c;
  return Object.prototype.hasOwnProperty.call(synonyms, key) ? synonyms[key]! : null;
}

// One parseable date token: a month name (abbrev or full) + 4-digit year, a
// numeric MM/YYYY, or a bare 4-digit year.
const ATS_MONTH =
  'Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?';
const ATS_DATE_TOKEN = `(?:(?:${ATS_MONTH})\\s+\\d{4}|\\d{1,2}/\\d{4}|\\d{4})`;
const ATS_DATE_RANGE = new RegExp(
  `^${ATS_DATE_TOKEN}(?:\\s*[–-]\\s*(?:${ATS_DATE_TOKEN}|Present))?$`,
  'iu',
);

/**
 * Whether a role's date string is one accepted, parseable family: a single date
 * token or a range of two joined by a hyphen or en-dash, the end optionally the
 * word 'Present'. Rejects the word 'to', seasons, period separators and
 * 'Now'/'Currently', which parsers misread (atsverification; techinterview).
 */
export function dateRangeOk(dates: string): boolean {
  return ATS_DATE_RANGE.test(dates.trim());
}

// ---------------------------------------------------------------------------
// Certification verification links (owner, 2026-10-06; CR-rg-cert-links)
// ---------------------------------------------------------------------------

// A certification's official verification link. `match` is a set of lowercase
// tokens that must ALL appear in the cert text (case-insensitive) for `url` to
// apply — tolerant to small variations in how a cert is phrased. Only verified
// credentials belong here; an unverified badge is never linked.
export type CertLink = { match: readonly string[]; url: string };

/**
 * The verification URL for a certification line, or null when none matches. The
 * first link whose every token is present (case-insensitive) wins, so order more
 * specific links first. An empty `match` never matches.
 */
export function certLinkFor(cert: string, links: readonly CertLink[]): string | null {
  const hay = cert.toLowerCase();
  for (const l of links) {
    if (l.match.length && l.match.every((t) => hay.includes(t.toLowerCase()))) return l.url;
  }
  return null;
}
