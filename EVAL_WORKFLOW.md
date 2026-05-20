# Eval Workflow Guide

This guide walks you through running the FreightPOP Copilot eval from start to finish.
No coding experience needed — every step is explained exactly.

---

## Section 1: One-Time Setup

You only need to do this once, the first time you set up the eval on a new computer.

### Step 1: Install the required packages

Open **Terminal** (on Mac: press Command + Space, type "Terminal", press Enter).

Type or paste this line, then press Enter:

```
pip install langfuse python-dotenv
```

Wait until it finishes. You'll know it's done when you see a new `$` prompt appear.

### Step 2: Create your `.env` file

The `.env` file is where you store your Langfuse login keys.
It lives in the same folder as the eval scripts.

In Terminal, navigate to the eval folder. If you're not sure where it is, ask.
Then run:

```
cp .env.example .env
```

This creates your personal `.env` file. Now open it in any text editor (TextEdit on Mac is fine) and fill in your three Langfuse keys.

### Step 3: Find your Langfuse keys

1. Go to [Langfuse](https://us.cloud.langfuse.com) and log in.
2. Click **Settings** (gear icon, bottom-left).
3. Click **API Keys**.
4. Copy the **Public Key** (starts with `pk-lf-`) into the `LANGFUSE_PUBLIC_KEY=` line in your `.env` file.
5. Copy the **Secret Key** (starts with `sk-lf-`) into the `LANGFUSE_SECRET_KEY=` line.
6. Leave `LANGFUSE_BASE_URL` as `https://us.cloud.langfuse.com` unless you use a different region.

Your `.env` file should look like this (with your real keys):

```
LANGFUSE_SECRET_KEY="sk-lf-xxxxxxxx"
LANGFUSE_PUBLIC_KEY="pk-lf-xxxxxxxx"
LANGFUSE_BASE_URL="https://us.cloud.langfuse.com"
```

Save the file.

### Step 4: Smoke test

Run this to confirm everything is connected:

```
python run_eval.py --help
```

You should see a list of options printed. If you see an error instead, re-check your `.env` file.

---

## Section 2: Running a Full Eval

### Step 1: Open Langfuse and verify the dataset exists

1. Go to [Langfuse](https://us.cloud.langfuse.com) and log in.
2. Click **Datasets** in the left sidebar.
3. Find the dataset you want to evaluate against (for example: `dataset_v2_full_17`).
4. Make sure it shows the expected number of cases (e.g. 17 items).

If the dataset is missing, see Section 3 for how to add cases.

### Step 2: Run the dataset cases in the Copilot UI

This is the manual step — you need to run each test case yourself in the browser.

**How to tag each case so the eval script can find it:**

When you type a message into the Copilot chat, add `[case_XX]` at the very beginning.
Replace `XX` with the two-digit case number (use leading zero: case 3 → `[case_03]`, case 10 → `[case_10]`).

**Example:**
- Case 3 → type: `[case_03] Create a truckload shipment from Chicago to Dallas`
- Case 10 → type: `[case_10] Add a stop in Memphis`

> **Important warning before you start all 50 cases:**
> Test ONE case first — run it with the `[case_XX]` prefix, and then run the same case again without the prefix.
> Compare how the agent behaves in both attempts.
> If the agent seems confused or behaves differently because of the prefix, **stop immediately** and contact Claude.
> We'll switch to a different matching method (using session metadata instead of the prefix).
> Only proceed with all cases once you've confirmed the prefix doesn't change the agent's behavior.

### Step 3: Run the eval script

Open Terminal and run this command (replace the values with your actual run name and dataset name):

```
python run_eval.py \
  --run-name "phase0_5.4mini_2026_05_20" \
  --dataset-name "dataset_v2_full_17" \
  --hours 24
```

**What each part means:**
- `--run-name` is the label you're giving this eval run. Use a name that includes the model and date so you can compare runs later. Example: `phase0_5.4mini_2026_05_20`.
- `--dataset-name` is the exact name of the dataset in Langfuse.
- `--hours 24` means "fetch traces from the last 24 hours." If you ran your cases more than 24 hours ago, increase this number (e.g. `--hours 48`).

Optional flags:
- Add `--dry-run` to see the scoring results without saving anything to Langfuse. Good for testing.
- Add `--debug` if something goes wrong — it shows more detail about what failed.

### Step 4: Read the terminal output

After the script runs, you'll see something like:

```
─────────────────────────────────────────
Run: phase0_5.4mini_2026_05_20
Dataset: dataset_v2_full_17 (17 cases)
─────────────────────────────────────────
Traces fetched: 17
Traces matched to cases: 16
Traces skipped (no marker): 1

Scoring results:
  PASS: 11
  FAIL: 5
  Pass rate: 68.8% (11/16)
  Not run: 1 case (case_14)

Failed cases:
  - case_03: tool_name_mismatch — expected updateFields, got navigateToPage
  - case_07: missing_required_param — stops_count
  ...

Written to Langfuse run: phase0_5.4mini_2026_05_20
View at: https://us.cloud.langfuse.com/datasets
─────────────────────────────────────────
```

**What each line means:**

| Line | What it means |
|---|---|
| `Traces fetched: 17` | The script found 17 conversations in Langfuse from the last 24 hours |
| `Traces matched to cases: 16` | 16 of those had a `[case_XX]` marker that matched a dataset item |
| `Traces skipped (no marker): 1` | 1 trace had no `[case_XX]` marker (maybe a test conversation you ran outside the eval) |
| `PASS: 11` | 11 cases the agent did the right thing |
| `FAIL: 5` | 5 cases the agent made a mistake |
| `Pass rate: 68.8%` | Percentage of matched cases that passed |
| `Not run: 1 case (case_14)` | Case 14 was never executed — no trace found for it |

For each failed case, the line shows the reason:
- `tool_name_mismatch` — the agent called the wrong tool
- `missing_required_param` — the agent forgot a required parameter
- `forbidden_tool_called` — the agent called a tool it was not supposed to
- `must_not violated` — a condition that should never happen did happen
- `no expected_trajectory matched` — the agent's sequence of actions didn't match any expected pattern
- `no_tool_calls_in_trace` — the agent didn't call any tools at all (something may have broken)

### Step 5: Review results in Langfuse

1. Go to [Langfuse](https://us.cloud.langfuse.com).
2. Click **Datasets** in the left sidebar.
3. Click the dataset name (e.g. `dataset_v2_full_17`).
4. Click the **Runs** tab.
5. Find your run by name (e.g. `phase0_5.4mini_2026_05_20`).
6. Click it to see pass/fail for each case.

**To compare two runs:**
1. From the Runs tab, select two run names using the checkboxes.
2. Click **Compare** (top right).
3. This shows you side-by-side which cases improved or regressed between runs.

---

## Section 3: Adding New Dataset Cases

**Important:** You do NOT edit any file in this folder to add cases.
The dataset lives entirely in Langfuse. This folder never stores dataset data.

### How to add a new case

1. **Ask Claude (in a separate chat — not Claude Code)** to write the case for you.

   Copy and paste this prompt, editing the scenario to match what you need:

   > I need a new case for dataset_v2. Scenario: user on truckload quote page asks to clear all stops. Expected behavior: agent calls `clearStops` tool with no arguments. Forbidden tools (must_not): `deleteRecord`, `navigateToPage`. Please give me a Langfuse-format CSV with just this one row, ready to import.

2. **Download the CSV** that Claude gives you.

3. **Import it into Langfuse:**
   - Go to [Langfuse](https://us.cloud.langfuse.com) → **Datasets**.
   - Click the dataset name.
   - Click **Import** (or **+ Add items**).
   - Upload the CSV file.

4. **Verify it appears** in the dataset item list.

### How to edit an existing case

1. Go to [Langfuse](https://us.cloud.langfuse.com) → **Datasets**.
2. Click the dataset name.
3. Click the item you want to edit.
4. Edit the fields directly in the Langfuse UI.
5. Save.

---

## Section 4: Monthly Backup

Langfuse is the source of truth for your dataset. Back it up monthly in case of account or service issues.

**How to export:**

1. Go to [Langfuse](https://us.cloud.langfuse.com) → **Datasets**.
2. Click the dataset name.
3. Click **Export** (or the download icon).
4. Save the CSV file to a folder **outside this repo**, like:
   ```
   ~/freightpop-eval-backups/2026-05/
   ```

Keep at least 3 months of backups. Name files with the date so you know which is newest.

---

## Section 5: Troubleshooting

**Q: I see "Auth failed" or "Invalid credentials"**

Your `.env` file may have a problem. Check:
- All three keys are present: `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL`
- No extra spaces before or after the `=` sign
- No extra quote marks (the file should have `"sk-lf-..."`, not `'"sk-lf-..."'`)

---

**Q: I see "No traces found"**

Possible causes:
- The `--hours` window is too short. If you ran cases yesterday, try `--hours 48`.
- You forgot to add `[case_XX]` markers when running the cases.
- The agent conversation didn't make it into Langfuse (check the Langfuse Traces page manually).

---

**Q: I see "marker not matched" warnings**

The script found a `[case_XX]` marker in a trace but couldn't find that case number in the dataset.

Check:
- Did you use two digits? `[case_03]` not `[case_3]`. The script tries both but it's safest to always use two digits.
- Does the case number exist in the Langfuse dataset? Open the dataset and count.

---

**Q: I see "Scorer error" or a Python error mentioning `expected_trajectory_options`**

A dataset item is missing its evaluation criteria. Open the failing case in Langfuse, check its `metadata` field, and make sure it has `expected_trajectory_options` defined.

---

**Q: The agent behaves differently when I add `[case_XX]` to the message**

Stop using the marker prefix. Contact Claude and ask to switch to the fallback matching method (matching by Langfuse session metadata instead of message prefix).
