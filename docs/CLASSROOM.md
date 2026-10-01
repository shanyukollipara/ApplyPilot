# Teaching ApplyPilot

Use this guide to walk a class through the local fork. Each student runs it on their own computer, with their own resume and their own accounts. Do not share one login, one resume, or one `~/.applypilot` folder across the class.

The upstream project is [Pickle-Pixel/ApplyPilot](https://github.com/Pickle-Pixel/ApplyPilot). This fork adds a Simplify Summer 2027 importer, headless Codex workers, and a classroom-safe transcript path.

## What students are learning

ApplyPilot is a local job-application agent. A run has three visible pieces:

1. **Discover.** `applypilot sync-simplify` pulls the public Simplify Summer 2027 internship list into a local SQLite database.
2. **Decide.** Graduate-only roles and iCIMS links are parked and are not submitted. Everything else stays in the queue.
3. **Apply.** `applypilot apply` opens Chrome, fills the employer form from the student's profile and resume, and records applied or failed.

The database, resume, and profile never leave the student's machine unless they commit them. Those files are gitignored on purpose.

## What each student needs

| Requirement | Why |
|---|---|
| Python 3.11+ | Runs the package |
| Node.js 18+ | Playwright browser tools |
| Google Chrome | The apply workers drive Chrome |
| Codex CLI, signed in | The form-filling model |
| Their own resume | `~/.applypilot/resume.pdf` and `resume.txt` |
| Their own `profile.json` | Name, email, phone, school, work authorization |
| Optional transcript | `~/.applypilot/transcript.pdf` |

API keys, if they use them, go in `~/.applypilot/.env`. Copy from `.env.example`. Never paste a key into chat, a slide, or git.

## Lesson 1 — Install and look around

```bash
git clone https://github.com/shanyukollipara/ApplyPilot.git
cd ApplyPilot
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
applypilot doctor
```

Have students open `src/applypilot/cli.py` and name the commands: `init`, `run`, `sync-simplify`, `apply`, `status`.

Talking point: the agent does not have a shared class account. `applypilot init` writes `~/.applypilot/` on that laptop only.

## Lesson 2 — Make the applicant them, not the sample

`src/applypilot/apply/codex_prompt.py` still contains one sample applicant's form answers: school, degree, GPA, graduation date, gender, race, pay, and preferred city. Those lines are instructions to the model. If a student runs apply without editing them, the form can be filled with the sample applicant's facts.

Before any real submission, each student replaces those answers with their own. The profile block above them (`APPLICANT PROFILE`) is already generated from `~/.applypilot/profile.json`. The hardcoded procedure steps must match that profile.

Also set:

- `~/.applypilot/resume.pdf` — the file that gets uploaded
- `~/.applypilot/resume.txt` — the text version the model reads
- `~/.applypilot/transcript.pdf` — only if they want transcript questions answered by upload

A missing transcript is allowed. The worker stops instead of uploading the resume in its place.

## Lesson 3 — Load jobs, do not submit yet

```bash
applypilot sync-simplify
applypilot status
```

`sync-simplify` prints `received`, `inserted`, `skipped`, and `excluded`.

- **inserted** — new rows in the local database
- **skipped** — already imported
- **excluded** — inactive, not Summer 2027, or graduate-only

Ask students to count pending rows and name one company from the queue. iCIMS URLs are stored as failed with `icims_unsupported` so workers do not open them.

## Lesson 4 — One dry run

```bash
applypilot apply --limit 1 --workers 1 --model gpt-5.6-luna
```

Add `--dry-run` to fill a form without the final submit. Use one worker the first time so the class can watch the browser. `--headless` is for later, when they already trust the setup.

After the run:

```bash
applypilot status
```

Have them explain the result in one sentence: applied, expired, login issue, captcha, or a manual question. A captcha stop is a successful safety stop. The worker is not supposed to solve the puzzle.

## Lesson 5 — A small batch

Only after a dry run looks right:

```bash
applypilot apply --continuous --workers 2 --headless --model gpt-5.6-luna
```

Two workers is enough for a laptop lab. More Chrome instances use a lot of memory. The queue stops when pending work is gone.

Students should check `applypilot status` again and sort failures into:

- **login_issue** — the site wanted an account the worker could not finish
- **manual_question** — a required answer was not in the profile (SSN, an NDA checkbox, a missing file)
- **captcha** — the page showed a challenge
- **expired** — the posting was closed

## Classroom rules

- Apply only to roles the student is actually eligible for.
- Do not apply to a classmate's jobs, and do not reuse a classmate's email or password.
- Do not commit `profile.json`, `resume.pdf`, `resume.txt`, `transcript.pdf`, `.env`, or `applications.csv`.
- Do not put a password in source code. The worker reads it from the local profile at runtime.
- Stop on a captcha. Do not add a bypass during class.
- If a form requires a social security number, government ID, or a signature they have not agreed to, the correct result is `manual_question`, not a guess.

## Suggested assignment

1. Run `sync-simplify` and report how many new non-iCIMS jobs arrived.
2. Edit the prompt so the school, degree, and work-authorization answers match their profile. Show the diff, not the profile file.
3. Dry-run one application and write down every field the agent filled.
4. Explain one failure from the queue without pasting personal contact details.

Grade the explanation and the diff. Do not collect resumes or portal passwords.
