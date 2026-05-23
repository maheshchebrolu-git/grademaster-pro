# How to Grade a New Assignment

Every new assignment requires exactly **two things from you**: a `context.txt` file and a `solutions.pdf`. The pipeline handles everything else automatically.

---

## Step 1 — Create the Assignment Folder

On your Mac, create this exact folder structure before running anything:

```
~/documents/grading/<assignment_name>/
└── prompts/
    ├── context.txt      ← rubric + grading instructions (you write this)
    └── solutions.pdf    ← the answer key PDF (you place this)
```

`<assignment_name>` must be lowercase with underscores (e.g., `lab3`, `problem_set_4`). This name is used throughout the pipeline to find files and store results.

---

## Step 2 — Write `context.txt`

This is the only file you write per assignment. It has two parts.

### Part A — Rubric Context

Write your grading philosophy and per-section/question answers in plain English. The system reads this and passes it directly to the AI model as context. Follow this structure:

```
### <ASSIGNMENT NAME> GRADING CONTEXT: INSTRUCTIONS FOR THE AGENT ###

**CORE GRADING PHILOSOPHY**
- **Scale**: Total <N> points.
- **Leniency**: Award partial "step marks" for effort/logical attempts; do not deduct full marks for single calculation errors.
- **Strict Penalty**: Award 0 points ONLY if a question is completely missing or unattended.
- **Reference**: Compare all student work against "Solutions.pdf".

---

**SECTION A: <SECTION NAME> (<N> PTS)**
- **Requirement**: <what the student must do>
- **Key Answer**: <correct answer>

**SECTION B: CHAPTER <N> EXERCISES (<N> PTS TOTAL / <N> PTS EACH)**
- **Ex 1**: <correct answer>
- **Ex 3**: <correct answer>

**SECTION C: CHAPTER <N> PROBLEMS (<N> PTS TOTAL / <N> PTS EACH)**
- **Prb 1**: <correct answer>

**SECTION D: CHAPTER <N> EXERCISES (<N> PTS TOTAL)**
- **Ex 2 (<N> pts)**: <correct answer>
- **Ex 5 (<N> pts)**: <correct answer>
```

**Critical formatting rules the parser depends on:**

| Pattern | What it does |
|---|---|
| `Total <N> points.` | Sets `max_points` for the whole assignment |
| `(<N> PTS TOTAL / <N> PTS EACH)` | Sets per-question default points for that section |
| `(<N> pts)` next to an exercise | Sets per-question points explicitly |
| `CHAPTER <N> EXERCISES` or `CHAPTER <N> PROBLEMS` in a section header | Sets the chapter number for all questions that follow |
| `- **Ex <N>**: ...` | Registers a question with that chapter's default pts |
| `- **Prb <N> (<N> pts)**: ...` | Registers a question with explicit pts |

### Part B — Output Format Schema

After your rubric context, add this block with the `questions` array updated to match **your specific assignment's questions and point values**:

```
YOUR GOAL:
Compare student work against the provided solutions.pdf.

OUTPUT FORMAT:
Return ONLY valid JSON (no markdown, no prose) with per-question breakdown:
{
  "questions": [
    {"id": "Sec A <Activity Name>", "max_points": <N>, "awarded": <0-N>, "status": "correct|partial|missing|incorrect", "issue": "<6-8 word issue or empty if correct>"},
    {"id": "Ch <N> Ex <N>", "max_points": <N>, "awarded": <0-N>, "status": "...", "issue": "..."},
    {"id": "Ch <N> Prob <N>", "max_points": <N>, "awarded": <0-N>, "status": "...", "issue": "..."}
  ]
}

Rules:
- "status": "correct" if fully right, "partial" for partial credit, "missing" if unanswered, "incorrect" if wrong.
- "issue": 6-8 word description of the specific mistake or missing item. Empty string if correct.
- Issue text must name the concrete mistake — no praise, no vague phrases.

Additional constraints:
- Never compare students across sections.
- Follow assignment instructions exactly.
```

> **Tip**: The `id` values in the `questions` array become the comment prefixes in Canvas (e.g., `Ch 5 Ex 1: Missing character device example`). Keep them short and consistent.

---

## Step 3 — Run Setup

From the project root:

```bash
python src/setup_and_run.py
```

It will prompt you for:

1. **Assignment name** — must match the folder name you created in Step 1
2. **Number of students** to harvest (enter `3` for a smoke test)
3. **SpeedGrader URLs** for each section (DL2, DL3, DL4)

What setup does automatically:
- Parses `context.txt` to extract `max_points`, per-question rubric, and comment constraints
- Copies `solutions.pdf` into the working `assets/` folder
- Generates a `active_system_prompt.txt` with the full per-question schema
- Saves all configuration into `config.json`
- Sets env vars in `.env` pointing to the solution and context files
- Launches the harvest + upload pipeline for all sections

---

## Step 4 — Run Each Phase Manually (if needed)

After setup, you can run individual phases:

```bash
# Harvest student submissions from Canvas SpeedGrader
python -m src.main <assignment_name> <section> --phase harvest --total-students 45

# Upload submissions to Google AI Studio + trigger batch job
python -m src.main <assignment_name> <section> --phase upload

# Poll for batch results, compute scores, write to Supabase
python -m src.main <assignment_name> <section> --phase sync

# Force re-sync (overwrite existing completed rows)
python -m src.main <assignment_name> <section> --phase sync --force

# Deliver grades + comments to Canvas SpeedGrader
python -m src.main <assignment_name> <section> --phase deliver

# Check status dashboard
python -m src.main --status
python -m src.main <assignment_name> <section> --status

# Wait until ALL Gemini batch jobs (every section in config) succeed, then Pushover notifies
python -m src.main <assignment_name> --phase wait-batches
# (equivalent: python -m src.tools.wait_all_batches <assignment_name>)
```

### Pushover: when things are ready

1. **After upload** — `setup_and_run` already notifies when all sections are **submitted** to Gemini (not when jobs finish).
2. **When every batch job has finished** — run `--phase wait-batches` (leave it running or in a `tmux` session). When DL2/DL3/DL4 (or whatever is in `config.json`) are all `JOB_STATE_SUCCEEDED`, you get **“Batches complete”** → then run `--phase sync` per section.
3. **When every section has been synced to Supabase** — after the **last** section’s `--phase sync` completes successfully, you get **“Ready for delivery”** (one-time). Then run `--phase deliver` per section.

To reset the “ready for delivery” tracker for a new grading run, delete:

`~/documents/grading/<assignment_name>/batch_files/pipeline_state.json`

---

## Step 5 — Review Before Delivery

Before running `--phase deliver`, check Supabase for:

- `status = "needs_review"` — student has a non-perfect score but no comment was generated. These need manual review before delivery.
- `status = "parse_failed"` or `status = "model_error"` — the AI batch failed for this student. Re-run `--phase sync --force` or grade manually.

---

## How Scoring Works (Per-Question Architecture)

The AI model grades each rubric question independently and returns:

```json
{
  "questions": [
    {"id": "Ch 5 Ex 1", "max_points": 2, "awarded": 1, "status": "partial", "issue": "Missing character device example for keyboards"},
    {"id": "Ch 5 Prob 1", "max_points": 3, "awarded": 3, "status": "correct", "issue": ""}
  ]
}
```

The pipeline then:
1. **Score** = sum of all `awarded` values (deterministic, no AI rounding)
2. **Comment** = one line per question where `status != "correct"`, formatted as `<id>: <issue>`
3. **Clamped** to `[0, max_points]`

---

## `config.json` — What Gets Stored

After setup runs, `config.json` gets an entry like:

```json
"problem_set_4": {
  "id": "<canvas_assignment_id>",
  "max_points": 50,
  "comment_min_words": 6,
  "comment_max_words": 8,
  "comment_style": "firm but encouraging",
  "url": "https://canvas.gmu.edu/...",
  "section_urls": { "dl2": "...", "dl3": "...", "dl4": "..." },
  "sections": ["dl2", "dl3", "dl4"],
  "section_meta": { ... }
}
```

You do not need to edit this manually — setup writes it.

**Multi-section courses:** `section_meta` stores the real Canvas `course_id` and `assignment_id` per section. **Delivery** (`--phase deliver`) uses these for SpeedGrader URLs so you don’t get “Page not found” when each section is a different Canvas course.

### DOCX → PDF (LibreOffice) before upload

On **upload**, each `.docx` is converted to a **single PDF** with **LibreOffice** (`soffice --headless --convert-to pdf`) so layout and embedded images stay on the page (better for multimodal grading than raw text + loose images).

1. Install LibreOffice (macOS): `brew install --cask libreoffice`, or from [libreoffice.org](https://www.libreoffice.org/). Ensure `soffice` works (often on `PATH` after install; the uploader also checks `/Applications/LibreOffice.app/Contents/MacOS/soffice`).
2. Optional env (in `.env`):
   - `GRADEMASTER_DOCX_CONVERSION=libreoffice` — default; try PDF first, then fall back to text+images if conversion fails.
   - `GRADEMASTER_DOCX_CONVERSION=legacy` — skip LibreOffice; use the old text + `word/media` extraction only.
   - `GRADEMASTER_LIBREOFFICE_TIMEOUT_SEC=180` — per-file conversion timeout (seconds).

If conversion fails, the uploader prints a warning and uses the **legacy** extraction for that file.

---

## Cleaning Up After Grading

To fully reset an assignment's data (Supabase rows, GCS files, local grading folder):

```bash
python clean_assignment.py <assignment_name>

# To also delete the config.json entry:
python clean_assignment.py <assignment_name> --remove-config-file
```

### When grading is finished — archive & tidy (`completed.py`)

After you are done with an assignment and want a **record of grades** plus a **clean tree** (keep **only** `prompts/` and `grades/` under the assignment folder):

1. Install Excel support once: `pip install openpyxl`
2. From the project root:

```bash
python completed.py <assignment_name>
```

**What it does:**

1. Exports all `grading_audit` rows for that assignment to  
   `~/documents/grading/<assignment_name>/grades/grading_audit_export_<UTC_timestamp>.xlsx`
2. Deletes those rows from Supabase
3. Deletes tagged Gemini files and known GCS prefixes (same idea as `clean_assignment.py`)
4. Under `~/documents/grading/<assignment_name>/`, **removes everything except** the **`prompts/`** and **`grades/`** directories (e.g. removes `batch_files/`, `assets/`, stray files)
5. Removes that assignment’s entry from **`config.json`**

If `openpyxl` is missing, the script **stops before** deleting Supabase data.

---

## Checklist for Each New Assignment

- [ ] Create `~/documents/grading/<assignment_name>/prompts/context.txt`
- [ ] Create `~/documents/grading/<assignment_name>/prompts/solutions.pdf`
- [ ] Update the `OUTPUT FORMAT` schema in `context.txt` to match this assignment's exact questions and point values
- [ ] Verify `Total <N> points.` line is present in `context.txt` so `max_points` is parsed correctly
- [ ] Run `python src/setup_and_run.py` and confirm parsed `max_points` matches expected value
- [ ] Smoke test with 3 students before running full section
- [ ] Review `needs_review` rows in Supabase before delivering grades

---

## Example: `context.txt` for a 50-Point Assignment

```
### PS 4 GRADING CONTEXT: INSTRUCTIONS FOR THE AGENT ###

**CORE GRADING PHILOSOPHY**
- **Scale**: Total 50 points.
- **Leniency**: Award partial "step marks" for effort/logical attempts.
- **Strict Penalty**: Award 0 points ONLY if a question is completely missing.
- **Reference**: Compare all student work against "Solutions.pdf".

---

**SECTION A: COMPUTER ACTIVITY (10 PTS)**
- **Requirement**: Install tool, run command, screenshot output.
- **Key Answer**: Output must show process list with PID column.

**SECTION B: CHAPTER 7 EXERCISES (10 PTS TOTAL / 2 PTS EACH)**
- **Ex 1**: Processes vs Threads: processes have separate memory, threads share.
- **Ex 3**: Deadlock requires mutual exclusion, hold-and-wait, no preemption, circular wait.
- **Ex 5**: Semaphore vs Mutex: semaphore allows N concurrent, mutex allows 1.
- **Ex 7**: Context switch saves CPU registers to PCB, restores next process state.
- **Ex 9**: Fork creates child with copy-on-write; exec replaces process image.

**SECTION C: CHAPTER 7 PROBLEMS (12 PTS TOTAL / 4 PTS EACH)**
- **Prb 1**: Producer-consumer with bounded buffer. Solution uses two semaphores.
- **Prb 3**: Dining philosophers. Solution: break circular wait by ordering forks.
- **Prb 5**: Banker's algorithm. Safe sequence: P1, P3, P0, P2, P4.

**SECTION D: CHAPTER 8 EXERCISES (9 PTS TOTAL)**
- **Ex 1 (3 pts)**: Virtual memory maps logical to physical addresses via page table.
- **Ex 3 (3 pts)**: TLB is a cache for page table entries; reduces memory accesses.
- **Ex 5 (3 pts)**: Page fault handler: check valid bit, load from disk, update page table.

**SECTION E: CHAPTER 8 PROBLEMS (9 PTS TOTAL)**
- **Prb 1 (3 pts)**: Page replacement: FIFO gives 4 faults, LRU gives 3 faults.
- **Prb 3 (3 pts)**: Working set size for thrashing prevention = 12 pages.
- **Prb 5 (3 pts)**: Translation lookaside buffer hit ratio = 95%, effective access = 105 ns.

---

YOUR GOAL:
Compare student work against the provided solutions.pdf.

OUTPUT FORMAT:
Return ONLY valid JSON (no markdown, no prose) with per-question breakdown:
{
  "questions": [
    {"id": "Sec A Computer Activity", "max_points": 10, "awarded": <0-10>, "status": "correct|partial|missing|incorrect", "issue": "<6-8 word issue or empty if correct>"},
    {"id": "Ch 7 Ex 1", "max_points": 2, "awarded": <0-2>, "status": "...", "issue": "..."},
    {"id": "Ch 7 Ex 3", "max_points": 2, "awarded": <0-2>, "status": "...", "issue": "..."},
    {"id": "Ch 7 Ex 5", "max_points": 2, "awarded": <0-2>, "status": "...", "issue": "..."},
    {"id": "Ch 7 Ex 7", "max_points": 2, "awarded": <0-2>, "status": "...", "issue": "..."},
    {"id": "Ch 7 Ex 9", "max_points": 2, "awarded": <0-2>, "status": "...", "issue": "..."},
    {"id": "Ch 7 Prob 1", "max_points": 4, "awarded": <0-4>, "status": "...", "issue": "..."},
    {"id": "Ch 7 Prob 3", "max_points": 4, "awarded": <0-4>, "status": "...", "issue": "..."},
    {"id": "Ch 7 Prob 5", "max_points": 4, "awarded": <0-4>, "status": "...", "issue": "..."},
    {"id": "Ch 8 Ex 1", "max_points": 3, "awarded": <0-3>, "status": "...", "issue": "..."},
    {"id": "Ch 8 Ex 3", "max_points": 3, "awarded": <0-3>, "status": "...", "issue": "..."},
    {"id": "Ch 8 Ex 5", "max_points": 3, "awarded": <0-3>, "status": "...", "issue": "..."},
    {"id": "Ch 8 Prob 1", "max_points": 3, "awarded": <0-3>, "status": "...", "issue": "..."},
    {"id": "Ch 8 Prob 3", "max_points": 3, "awarded": <0-3>, "status": "...", "issue": "..."},
    {"id": "Ch 8 Prob 5", "max_points": 3, "awarded": <0-3>, "status": "...", "issue": "..."}
  ]
}

Rules:
- "status": "correct" if fully right, "partial" for partial credit, "missing" if unanswered, "incorrect" if wrong.
- "issue": 6-8 word description of the specific mistake or missing item. Empty string if correct.
- Issue text must name the concrete mistake — no praise, no vague phrases.

Additional constraints:
- Never compare students across sections.
- Follow assignment instructions exactly.
```
