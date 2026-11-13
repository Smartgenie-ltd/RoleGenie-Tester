<!-- Tester edition README (BRIEF-034, CR-rg-tester-export). Shipped as README.md in the exported folder. -->

# RoleGenie

RoleGenie helps you land the right next role. You give it your resumes and the job ads you are considering. It builds
one master CV from your own words, which you approve, then shows how well each ad fits that evidence: the hard
requirements you do not meet come first, then a score, the gaps and the missing evidence. You decide what to apply
for. RoleGenie never applies for you and never invents a claim. This tester edition cannot submit an application for
you, whatever any setting says: you submit it yourself, then mark the job applied.

It runs on your own computer (Windows or Linux), with your own Claude account, your own Jev key and, if you want the
phone board, your own Google account. Your data stays in one folder on your machine. What leaves it: your CV text and
the job ads go to Claude and Jev to build and assess your master CV; if you set up the phone board, your assessed jobs
go to a Google Sheet in your own account; the optional daily job search asks the public job boards and Adzuna for
postings, and those requests carry no personal data. Nothing is sent to the people who make RoleGenie.

## Install

Follow these steps in order. Each one has the exact command for Linux and for Windows (PowerShell). Where you see
`<clone path>`, use the folder you cloned into, for example `/home/you/rolegenie` or `C:\Users\you\rolegenie`.

### 1. Install the prerequisites (once)

You need Python 3.12 or newer, and Claude Code (the `claude` command, signed in to your own Claude account).

Linux (Debian or Ubuntu):

```bash
sudo apt update && sudo apt install -y python3 python3-venv python3-pip curl
curl -fsSL https://claude.ai/install.sh | bash
claude          # once: sign in to your Claude account, then type /exit
```

Windows (PowerShell):

```powershell
winget install --id Python.Python.3.12 -e
irm https://claude.ai/install.ps1 | iex
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned    # once: lets PowerShell run the .venv activation script
```

Close and reopen PowerShell so the new commands are found, then run `claude` once to sign in and type `/exit`.

### 2. Get RoleGenie and its Python packages

Linux:

```bash
cd <folder you received>
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
playwright install chromium        # the browser RoleGenie uses to read job adverts
```

Windows (PowerShell):

```powershell
cd <folder you received>
py -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
playwright install chromium        # the browser RoleGenie uses to read job adverts
```

Every later command runs in this folder with `.venv` activated (your prompt starts with `(.venv)`). In a new
terminal, `cd` into the folder and run the activate line again.

### 3. Add your keys

RoleGenie reads its keys from a file named `.env` in this folder. Copy the template and fill in your Jev key
(`TYPESAFE_API_KEY`, from your Jev account): it is the only required one. The two Adzuna keys are optional; they are
needed only if you want the daily job search to include Adzuna (a free developer account at developer.adzuna.com).

```bash
cp .env.example .env && nano .env                       # Linux
```

```powershell
Copy-Item .env.example .env; notepad .env               # Windows
```

Never share `.env`.

### 4. Check everything is ready

```bash
python first_run_check.py
```

Each line says `OK`, `MISSING` (followed by `->` and the one thing to do) or `OPTIONAL`. Fix each `MISSING` line and
run it again until it ends with `Ready: every required item is OK.` It never prints your keys, only `set` or
`missing`, and the only thing it creates is your empty data folder.

### 5. Your data folder

All your data (master CV, assessments, decisions, your settings) lives in one folder outside the clone:
`~/.rolegenie` on Linux, `%USERPROFILE%\.rolegenie` on Windows. To use another folder, set `ROLEGENIE_HOME`
before running RoleGenie (Linux: `export ROLEGENIE_HOME=/path/to/folder`; Windows:
`$env:ROLEGENIE_HOME="D:\path\to\folder"`).

The first run creates your settings file, `config.yaml` in that folder, from `config.example.yaml`. Edit that copy,
never the example. If you used RoleGenie before it kept data in its own folder, move it across once with
`python rolegenie.py migrate`.

## Choose your search

Answer a few questions (job titles, places, contract or permanent, employers to watch) instead of editing YAML:

```bash
python rolegenie.py search setup   # asks one question at a time; Enter keeps what is set now; saves only on y
python rolegenie.py search show    # what the daily run will look for, in plain words
```

Hand-editing `config.yaml` still works; `search setup` keeps a copy of the previous file as `config.yaml.bak`.

## First run: your master CV and your first assessment

Your resumes and one job ad give you a master CV you approve, then an assessment of that ad against it.

```bash
python rolegenie.py onboard cv-2023.docx cv-2025.pdf cv-short.txt   # .docx .pdf .txt .md, two or more versions
python rolegenie.py master show                      # every fact, the resume it came from, any contradictions
python rolegenie.py master resolve c1 --keep f9      # keep one side of a contradiction ...
python rolegenie.py master resolve c2 --ack          # ... or acknowledge it and keep both
python rolegenie.py master confirm "Fluent in Spanish"   # add a fact your resumes do not state (marked as yours)
python rolegenie.py deny "Terraform"                 # something you do NOT have: shown as a confirmed gap
python rolegenie.py rate "Kubernetes" 4              # your own 1-5 rating: stored apart, never changes the fit score
python rolegenie.py master approve                   # refused while a contradiction is open
python rolegenie.py assess --company "Acme Ltd" --url https://www.example.com/jobs/123   # read the ad from its link
python rolegenie.py assess --company "Acme Ltd" --url https://... job-ad.txt  # or from a saved file; the link is kept
python rolegenie.py assess job-ad.txt                # or, on Linux: python rolegenie.py assess - < pasted-ad.txt
python rolegenie.py assess --help                    # every option
```

How to read the result:

- Every master fact is copied word for word from one of your resumes and checked in code. A fact the model could
  not quote exactly is listed as rejected, not stored. Unreadable files are named, never skipped silently.
- The assessment prints the **unmet hard requirements first**, whatever the score. Then it prints the score out of
  100 with each requirement's contribution, the **confirmed gaps** (you denied it, or your resumes say you lack it),
  the **missing evidence** (your master says nothing either way) and the **uncertain** requirements (the ad is
  hedged or unclear). Every requirement is a verbatim line of the ad. The JSON is saved under `assessments/` in your
  data folder.
- Under each requirement you meet or partly meet, `evidence:` lines show the master fact(s) behind it (`f3 "..."`)
  and the resume file each one is quoted from, so you can check every match against your own words. The fact ids are
  checked in code: a match whose cited facts do not exist in your approved master is printed as
  `met - evidence not linked` and listed under **MATCHED BUT EVIDENCE NOT LINKED**; its score is not changed. The ad's
  title line (role, company, place) is never counted as a requirement. Nor are its location line, application-form
  questions or stated conditions (such as right to work): a resume cannot evidence them, so they are listed under
  **IN THE AD, NOT SCORED** for you to check yourself, and a stated condition is also shown as uncertain. An ad file that is not readable text gives
  `AdUnreadable` and nothing is written.
- With `--url` and no file, RoleGenie opens the link the way the nightly search does (a headless browser, so the
  Playwright install step is needed) and reads the ad from the page. If the page cannot be read cleanly (blocked,
  expired, a login or anti-bot page, too short to be a full ad) you get `AdUnreadable` and nothing is written: save
  the ad text to a file and pass it with `--url`. `--company` is recorded exactly as you type it; RoleGenie never
  guesses the company from the ad. Both go to your board.
- Any change to the master (a confirmation, a resolution, a new `onboard`) needs `master approve` again before
  `assess`. If Claude or Jev is unavailable you get a named error (`ModelUnavailable`, `JevUnavailable`) and nothing
  half-written.
- Windows is not yet verified for this journey. Linux is.

## Optional: phone board

Put your assessed jobs on a board on your phone and decide each one there. The board is a small Google Apps Script
web app in **your own Google account**, over a Google Sheet in your Drive: nothing goes to anyone else, and no Google
library or sign-in is needed on your computer. The steps are the same on Windows and Linux.

```bash
python rolegenie.py board setup          # creates your board secret and prints the deploy steps below, with the secret
```

Then, once, in your browser:

1. Create a new Google Sheet (`sheets.new`), then **Extensions > Apps Script**.
2. Replace the code in `Code.gs` with this repository's `apps-script/Code.gs`. Add an HTML file named `Page`
   (**File > + > HTML**) and paste `apps-script/Page.html` into it. Save.
3. **Project Settings** (the gear) > **Script Properties** > **Add script property**: property `BOARD_SECRET`, value
   the secret `board setup` printed (also stored in `board.json` in your data folder).
4. **Deploy > New deployment**, type **Web app**. Execute as: **Me**. Who has access: **Anyone**. Deploy, and allow
   the access Google asks for (the script reads and writes only this Sheet).
5. Copy the **Web app URL** (it ends in `/exec`).

```bash
python rolegenie.py board setup https://script.google.com/macros/s/.../exec   # prints your phone link: bookmark it
python rolegenie.py board push           # sends every assessment in your data folder to the board
# ... on your phone: Apply / Reject / Consider / Already applied ...
python rolegenie.py board pull           # writes your decisions to decisions.json in your data folder
```

- Each job shows its title, score and the **unmet hard requirements first**, then confirmed gaps, missing evidence
  and uncertain requirements. Tap a decision, add a reason (**required for Apply and Reject**, optional otherwise),
  then Save. Apply only records your decision: RoleGenie never applies for you.
- Pushing again updates the scores and lists but **never overwrites a decision** you have made. You can change a
  decision on the phone at any time; `board pull` reads the current ones.
- Every request needs your secret: without it the board shows nothing and changes nothing. The phone link carries
  the secret, so keep it private. `board setup URL` prints that link again whenever you run it; nothing else prints
  the secret. To change it, edit `board.json` and the `BOARD_SECRET` property together.
- Each job shows the company and a link to the ad when you gave them to `assess` (`--company`, `--url`); jobs
  assessed without them show blank cells.
- Known limits: the title is the ad's file name, or "(ad from link)" for an ad read from its link. Assessing the
  same link again updates its one row (the latest assessment wins); links count as the same only when they match
  after trimming spaces (Adzuna links by their job number), and a job first pushed without `--url` gets a second
  row if you later assess it with `--url`. Decisions are stored only; nothing learns from them yet.

## Optional: run the daily workflow on a schedule

Once you have an approved master (`onboard`, then `master approve`), one command runs the whole overnight journey:

```bash
python rolegenie.py daily            # run it now
python rolegenie.py daily --status   # what the last run did, and whether one was missed
python rolegenie.py daily --print-scheduler   # the scheduler line for THIS machine; nothing is installed
```

It runs, in order: import your phone decisions, search for new jobs and assess them on your approved master, retry any
assessment that failed, record each genuine Apply as an intent, draft a CV from your approved facts for it, queue
recruiter replies for your review (if `mail: {enabled: true}` is in `config.yaml`), and update the status on your phone
board. A step that is not set up (phone board, mail) is reported as skipped; one that fails does not stop the others.
A second run cannot start while one is active.

**Decisions sync overnight only.** A job you tap Apply on during the day gets its CV after the next nightly run.
Tapping Apply is an intent; only your own "Already applied" mark (or `rolegenie.py mark JOB applied`) counts as
applied. RoleGenie never submits an application in this workflow. Review replies with `rolegenie.py mail review`, then
`mail confirm PROPOSAL_ID` or `mail reject PROPOSAL_ID`.

Nothing runs while your computer is off or asleep; `daily --status` says so plainly when a run was missed.
`daily --print-scheduler` prints the exact line. The shapes are:

Linux (cron): on Linux it prints a ready block for this machine, between `# RoleGenie daily (begin)` and
`# RoleGenie daily (end)`. The block holds a `PATH=` line with the folders where `claude`, `node` and your `.venv`
Python are found now (cron's own PATH is too short to find them), a `ROLEGENIE_HOME=` line if you set one, and the
7:00 schedule line. If `claude` is not found, it says so and the block says the nightly run will fail until it is.
Nothing is installed until you paste the command. In your RoleGenie folder, in the same terminal you use for your own
runs (so it finds the same tools and data folder), paste this to add the block, or to replace an earlier one (your
other crontab lines are kept):

```bash
b="$(.venv/bin/python rolegenie.py daily --print-scheduler)" && { crontab -l 2>/dev/null | sed '/^# RoleGenie daily (begin)$/,/^# RoleGenie daily (end)$/d'; printf '%s\n' "$b"; } | crontab -
```

To remove it again (only the RoleGenie block goes):

```bash
crontab -l 2>/dev/null | sed '/^# RoleGenie daily (begin)$/,/^# RoleGenie daily (end)$/d' | crontab -
```

Check it with `crontab -l` (you should see the block once), then run `python rolegenie.py daily --status` the next
morning. The run writes its own log to `daily.log` in your data folder. Keep the RoleGenie block as the last lines
of your crontab: cron applies its `PATH=` and `ROLEGENIE_HOME=` lines to every line below them. If you edit the block
by hand, keep both marker lines: without the end line, the remove command would delete everything below the begin line.

Windows (Task Scheduler), one line in PowerShell; the log is written for you in your data folder:

```powershell
schtasks /Create /SC DAILY /ST 07:00 /TN RoleGenie /TR "\"<clone path>\.venv\Scripts\python.exe\" \"<clone path>\rolegenie.py\" daily"
```

The task runs only while you are signed in to Windows. Remove it with `schtasks /Delete /TN RoleGenie`. Plain
`python rolegenie.py` still runs the older scan-only flow.

## Optional: back up your data folder

If you use rclone (rclone.org) with a remote you have set up (`rclone config`), add this to `config.yaml` in your
data folder and RoleGenie copies the data folder to that remote after each run:

```yaml
sync:
  remote: myremote:rolegenie-backup
```

`python first_run_check.py` reports rclone as `OPTIONAL`: without it, everything else still works.

---

## Check your copy (optional)

This folder is a tester edition: it holds the product and its checks only, and `export-manifest.json` records the
exact files it was built from. Other files in the folder, such as a `.git` folder from your own copy, are ignored by
the check.

To confirm your copy is complete and working, install the test runner once and run the check:

```bash
pip install -r requirements-dev.txt
node check.mjs
```

It confirms every file matches the manifest, then runs the product's own tests with no keys and no network. Node 24
or newer is needed for this check's phone-board test only; RoleGenie itself never needs Node. With an older Node the
check says `SKIPPED` for that test and ends with `passed-with-skip` (exit code 3): that is not a pass.

## Testing this release

`TESTING.md` is the tester checklist: what this release supports and what is not yet verified (Windows), the daily
journey to try step by step, where your data is, and how to report a problem by hand without sending anything private.
RoleGenie never sends usage data or reports anywhere; your own report is the only feedback.
