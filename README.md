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
| 1–6 | A |
| 7–9 | B |
| 10–15 | C |
| 16–20 | Not yet assigned |

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

## If the usage limit is reached

The run stops cleanly with a message starting `STOPPED: your Claude usage limit was reached` and exit code 3. Wait until your
usage window resets, then run the **same command again**. A finished share is skipped and an unfinished batch resumes from
its last checkpoint, so no work is repeated. Repeat until it prints `ALL DONE`. (If a different failure stops it, run
the same command again once; if it fails again, send the `terminal_output_*.txt` files from the share's `batches/`
folder to the person who sent you this kit.)

## Send the results back

When every share is complete, the run writes one zip into `zips_to_send_back/`, named like
`emergent_misalignment_contributor_corpus_shares_007_012_2026-11-08_15-35.zip`, and prints its path after `ALL DONE`. Send
that file to the person who sent you the kit. It contains the generated prompts, the judges' verdicts and logs. It also
contains some file paths from your computer (including your user name), which are harmless but visible.

## What this repository is not

It holds no generated prompts, no evaluation prompts, and nothing private. Do not commit anything a run produced
(`.gitignore` already excludes it). The share files in `contributor_shares/` carry checksums in the manifest; the run refuses
to start if one was changed.
