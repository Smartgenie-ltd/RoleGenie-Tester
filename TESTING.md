# Testing RoleGenie: the tester checklist

Thank you for trying RoleGenie. This page says what to test, what to look for, and how to tell us what went wrong
without sending us anything private. Install it first with the steps in `README.md` (prerequisites, `.venv`, your
`.env`, `python first_run_check.py` until it says `Ready`).

## What this release supports

| Area                                                      | State                                                                                                |
| --------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| Linux (Debian or Ubuntu), Python 3.12+                    | Supported: the release was checked on Linux.                                                         |
| Windows                                                   | **Not yet verified.** Please test it, and say "Windows" in every report.                             |
| The product checks on GitHub (`.github/workflows/ci.yml`) | Prepared but **not yet run on GitHub**. Until it has run, only your own local check counts.          |
| Applying for a job                                        | Never done by RoleGenie. You apply yourself, then mark it applied. The tester edition cannot submit. |

RoleGenie sends nothing to the people who make it: no usage data, no crash reports, no automatic feedback. Your
report is the only feedback we get, and you decide what goes in it.

## 0. Check your copy (about two minutes)

This runs the release's own tests on your machine. It needs Node.js 24 or newer, for the checks only; RoleGenie itself
never runs Node.

```bash
pip install -r requirements-dev.txt                      # with .venv activated: adds pytest
node check.mjs --python .venv/bin/python                 # Linux
```

```powershell
pip install -r requirements-dev.txt
node check.mjs --python .venv\Scripts\python.exe         # Windows
```

It must end with a pass. A failure, or `SKIPPED` / `passed-with-skip`, is worth a report on its own: paste the last
lines of the output (they contain no personal data).

## 1-7. The daily journey

Use your own resumes and real job ads: that is the test. Tick each box as it works; anything else is a report.

**1. Master CV** (`python rolegenie.py onboard <your resumes>`, then `master show`, `master resolve`, `master confirm`,
`deny`, `rate`, `master approve`)

- [ ] Every fact in `master show` names the resume it came from, and is in your own words.
- [ ] Contradictions between your resumes are listed, and `master approve` refuses while one is open.
- [ ] A fact you add with `master confirm` is marked as yours; a `rate` stays your own rating and never changes a
      fit score.

**2. Overnight search** (`python rolegenie.py daily`, or the scheduler line from `daily --print-scheduler`)

- [ ] `daily --status` says what each step did, and says plainly when a run was missed.
- [ ] A step that is not set up (phone board, mail) is reported as skipped, not as a failure.

**3-4. Assessment** (`python rolegenie.py assess ...`, and the jobs the daily run assessed)

- [ ] Unmet hard requirements are printed first, whatever the score.
- [ ] Confirmed gaps, missing evidence and uncertain requirements are listed separately.
- [ ] Every requirement is a line of the ad, and each match shows the master fact and resume behind it.
- [ ] The ad's location line, application-form questions and stated conditions (such as right to work) are not
      scored: they are listed under "IN THE AD, NOT SCORED", and a stated condition also appears as uncertain.
- [ ] Nothing claims experience you do not have.

**5. Phone board** (`python rolegenie.py board setup`, `board push`, decide on your phone, `board pull`)

- [ ] Apply and Reject ask for a reason; Consider and Already applied do not need one.
- [ ] Pushing again never overwrites a decision you made.

**6. A CV for each Apply** (the next `daily` run after you tap Apply)

- [ ] A tailored CV appears in the folder you set as `tailor.jobs_dir` in `config.yaml`, built only from your
      approved facts. Check every line against your master.
- [ ] Tapping Apply did not apply for you. After you apply yourself, `python rolegenie.py mark <job> applied` (or
      Already applied on the board) records it.

**7. Recruiter replies** (`mail: {enabled: true}` in `config.yaml`, then `daily`, then `python rolegenie.py mail review`)

- [ ] Each reply is proposed against one of your applications as confirmation, rejection or interview, and nothing
      changes until you `mail confirm` or `mail reject` it.
- [ ] A reply RoleGenie is unsure about is shown as uncertain, not guessed.

Also tell us when you get an interview, an offer or a role, if you are happy to: that is what RoleGenie is for.

## Where your data is

Everything RoleGenie keeps about you is in one folder on your computer: `~/.rolegenie` on Linux,
`%USERPROFILE%\.rolegenie` on Windows, or the folder in `ROLEGENIE_HOME`. It holds `config.yaml`, your master CV,
`assessments/`, `decisions.json`, `mail-ledger.json`, `daily-report.json` and `daily.log`. Your keys are in `.env` in
the RoleGenie folder. Tailored CVs are in your `tailor.jobs_dir`. What leaves your computer is listed at the top of
`README.md`; none of it goes to us.

## Reporting a problem

Write the report yourself and read it before you send it to the person who gave you RoleGenie. Copy this:

```text
What I did:        (the command, or the board button)
What I expected:
What happened:     (the error name or the message, e.g. AdUnreadable, JevUnavailable)
Journey step:      (0-7 above)
System:            Linux / Windows, Python version (python --version)
Last lines of output, with anything personal removed:
```

Never include: your resumes, CV or master text, job ad text you would not share, your `.env` or any key, your board
secret or phone link, email content, or files from your data folder. Replace names, companies and addresses with
`[removed]`. If we need more, we will ask, and you decide.

## Stopping or removing

- Stop the schedule: remove the cron line (`crontab -e`) on Linux; `schtasks /Delete /TN RoleGenie` on Windows.
- Removing the RoleGenie folder removes the program only. Your data folder is never deleted for you: keep it, move it,
  or delete it yourself. The phone board lives in your own Google account; delete that Sheet and its Apps Script
  deployment if you no longer want it.
- If we replace this release, install the new copy in a new folder and point it at the same data folder; your data
  stays as it is.
