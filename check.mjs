#!/usr/bin/env node
// RoleGenie tester check: shipped as check.mjs at the top of the tester folder (BRIEF-034, CR-rg-tester-export).
// Confirms every file is exactly as exported, then runs the product's selected tests. Needs no development tools.
//
//   node check.mjs [--python <path to the python with requirements-dev.txt installed>]
//
// Exit 0: passed. Exit 1: a file differs or a test failed. Exit 3: passed, but the Apps Script test was skipped
// because Node is older than 24 (the product itself never needs Node; only this one test does).
import { createHash } from 'node:crypto';
import { existsSync, readFileSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.dirname(fileURLToPath(import.meta.url));
const argv = process.argv.slice(2);
const pyArg = argv[0] === '--python' ? argv[1] : undefined;
const python =
  (pyArg && path.resolve(pyArg)) ??
  (process.platform === 'win32'
    ? path.join(root, '.venv', 'Scripts', 'python.exe')
    : path.join(root, '.venv', 'bin', 'python'));

function fail(msg) {
  console.error(`check: FAILED: ${msg}`);
  process.exit(1);
}

// 1. Every exported file is present and unchanged.
const manifest = JSON.parse(readFileSync(path.join(root, 'export-manifest.json'), 'utf8'));
for (const f of manifest.files) {
  const p = path.join(root, ...f.path.split('/'));
  if (!existsSync(p)) fail(`missing file ${f.path}`);
  if (createHash('sha256').update(readFileSync(p)).digest('hex') !== f.sha256)
    fail(`changed file ${f.path}`);
}
console.log(`check: ${manifest.files.length} files match the export manifest`);

// 2. The selected Python tests, keyless, from this folder, writing no bytecode or cache here.
if (!existsSync(python))
  fail(`no Python at ${python}: create .venv and install requirements-dev.txt, or pass --python`);
const sel = JSON.parse(readFileSync(path.join(root, 'export-tests.json'), 'utf8'));
const env = {
  ...process.env,
  PYTHONDONTWRITEBYTECODE: '1',
  TYPESAFE_API_KEY: '',
  PYTHONPATH: '',
};
const py = spawnSync(
  python,
  [
    '-m',
    'pytest',
    '-q',
    '-p',
    'no:cacheprovider',
    ...sel.deselect.flatMap((d) => ['--deselect', d.id]),
    ...sel.python,
  ],
  { cwd: root, env, stdio: 'inherit' },
);
if (py.status !== 0) fail(`Python tests (exit ${py.status ?? py.error?.message})`);
console.log(
  `check: Python tests passed (${sel.deselect.length} deselected, reasons in export-tests.json)`,
);

// 3. The phone board's Apps Script test. Node 24+ only: it loads a .ts file directly.
const major = Number(process.versions.node.split('.')[0]);
if (major < 24) {
  console.log(`check: SKIPPED: Apps Script test needs Node 24+ (found v${process.versions.node})`);
  console.log('check: passed-with-skip');
  process.exit(3);
}
const node = spawnSync(process.execPath, ['--test', ...sel.node], {
  cwd: root,
  env,
  stdio: 'inherit',
});
if (node.status !== 0) fail(`Apps Script test (exit ${node.status ?? node.error?.message})`);
console.log('check: passed');
