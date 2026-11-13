# Emergent misalignment prompt corpus: contributor kit

This kit lets you generate part of a prompt corpus on your own Claude subscription. You are given one or more numbered
**shares**. Each share is a fixed list of about 200 scenario specifications (a kind of animal, a kind of harm, and the practical
advantage that tempts someone toward it). For each share the kit asks Claude to write realistic user messages for those
specs, filters them, and has two Claude judges check each message. Shares are disjoint, so nothing you generate duplicates
what anyone else generates.

Everything is billed to your **Claude subscription** through the Claude Code command line tool. No API key is used or needed,
and the kit does not read one. A share takes a lot of usage (roughly ten Claude calls per scenario, so on the order of two
thousand calls per share; this is an estimate), so expect to hit your usage limit and rerun after it resets. Nothing is lost
when that happens.

## Share assignments

Run only the shares assigned to you.

| Shares | Assigned to |
|---|---|
| 1 | A |
| 2–6 | D |
| 7–9 | B |
| 10–20 | C |

## Setup (once)

1. Python 3.10 or newer (`python3 --version`).
2. Claude Code installed and signed in with your **Claude subscription**: install it from
   https://docs.claude.com/en/docs/claude-code, then run `claude auth login`. Check with `claude auth status`
   (it should say `loggedIn: true`, `authMethod: claude.ai`). Checking makes no model call.
3. From the kit folder:
   ```
   python3 -m venv .venv
   source .venv/bin/activate          # on Windows PowerShell: .venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
   (Developed and tested on macOS and Linux.)

## Run your shares

First look at the plan; this makes no model call and creates nothing:

```
python run_my_shares.py --shares 7,12 --dry-run
```

Then run for real, in the background so it survives closing the terminal window (use your own share numbers):

```
nohup python run_my_shares.py --shares 7,12 > run_my_shares_log.txt 2>&1 &
```

(From inside Claude Code you can instead ask it to run that command in the background.) Shares run one after another. The
log prints a progress line about every three minutes with the elapsed time. Watch it with `tail -f run_my_shares_log.txt`,
or see which shares are finished with `python run_my_shares.py --shares 7,12 --status`.

**Keep the computer awake.** On macOS the run keeps the computer awake by itself (`caffeinate`) until it ends, but closing a
laptop's lid still puts it to sleep, so keep the lid open and the charger plugged in. On other systems, turn off sleep while
it runs. If the computer sleeps or restarts anyway, run the same command again: it resumes from the last checkpoint.

Your results are written to `my_share_corpora/contributor_corpus_share_NNN/` (one folder per share). Do not edit anything in
there or in `contributor_shares/`.

If the model refuses a scenario while planning it, that scenario is now rejected automatically and the batch carries on. Do not add
rejection records by hand.

## Second pass over a share

Share 021 is different: it holds specs the main corpus already dealt, so running it (a normal first pass, without `--second-pass`) is itself a second pass over those specs.

The kit keeps a record of every scenario seed it has started (`scenario_seeds_already_started_in_this_kit.json`), and a run
refuses to repeat a share it has already run, even if you paste the same command twice or move its folder: running it again
would only duplicate prompts. If every share you asked for is already done, it runs nothing, writes no new zip (the earlier zip
is still in `zips_to_send_back/`) and exits with code 4. Run the next of your assigned shares that is not complete instead
(`--status` shows which). A second pass is only for once **every** one of your assigned shares has been run once.

A second pass deals the same specs of a share again, so each spec gets a second prompt with different dealt variables (the
kind of person, the form of the message and so on; only the spec itself is the same). It must use a **distinct scenario seed**
(so the dealt variables differ) and **its own output folder** (so the first pass is never touched). Do it only when asked.

For one or more shares, use `--second-pass`. It uses each share's seed base plus 500 and writes to `second_pass_share_corpora/second_pass_contributor_corpus_share_NNN/` (a second pass started with an earlier version of this kit, in `second_pass_share_corpora/contributor_corpus_share_NNN/`, is still found and continued by the same command, and is named `second_pass_...` inside the zip):

```
python run_my_shares.py --shares 7,12 --second-pass --dry-run
nohup python run_my_shares.py --shares 7,12 --second-pass > run_my_shares_second_pass_log.txt 2>&1 &
```

Because the deal changes with the seed, the run first simulates the deal offline (no model call) to find how many specs it can
deal without running out of a category, prints that count (the dry run shows it too), and records it in
`deal_count_found_by_offline_simulation.json` in each corpus folder. The usage limit, resume, `--status` and zip steps work as
above (add `--second-pass` to every command, including `--status`).

To choose the seed yourself, for exactly one share, give an explicit seed base and a new output folder (the run refuses a seed
base that any first pass, standard second pass or the main corpus already used):

```
python run_my_shares.py --shares 7 --scenario-seed-base 20287700 --output-folder my_second_pass_corpora --dry-run
```

The zip is named `..._second_pass_shares_...` and is sent back like any other, but it is **not merged as a duplicate** of the
first pass: it is kept separately.

## If the usage limit is reached

The run stops cleanly with a message starting `STOPPED: your Claude usage limit was reached` and exit code 3. Wait until your
usage window resets, then run the **same command again**. A finished share is skipped and an unfinished batch resumes from
its last checkpoint, so no work is repeated. Repeat until it prints `ALL DONE`. (If a different failure stops it, run
the same command again once; if it fails again, send the `terminal_output_*.txt` files from the share's `batches/`
folder to the person who sent you this kit.)

## Long folder paths on Windows

The kit handles long Windows folder paths by itself: you do not need to change any Windows setting, move the kit, or shorten
any name. If a share stopped on Windows with `FileNotFoundError: [WinError 3]` (the path was too long), pull the updated kit
(`git pull`) and run **exactly the same command again**, with the same `--output-folder` if you gave one, and without moving
the kit folder. It resumes that share in place.

## Send the results back

When every share is complete, the run writes one zip into `zips_to_send_back/`, named like
`emergent_misalignment_contributor_corpus_shares_007_012_2026-11-08_15-35.zip`, and prints its path after `ALL DONE`. Send
that file to the person who sent you the kit. It contains the generated prompts, the judges' verdicts and logs. It also
contains some file paths from your computer (including your user name), which are harmless but visible.

## What this repository is not

It holds no generated prompts, no evaluation prompts, and nothing private. Do not commit anything a run produced
(`.gitignore` already excludes it). The share files in `contributor_shares/` carry checksums in the manifest; the run refuses
to start if one was changed.
