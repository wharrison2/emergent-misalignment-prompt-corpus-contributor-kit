# Instructions for a Claude Code session working in this kit

You are helping a contributor generate part of the emergent misalignment prompt corpus on their own Claude subscription.
Read `README.md` first; it has the setup and the commands. Your job is normally: set up, run the contributor's assigned
shares to completion in the background, and tell them where the zip is.

## Rules

- The only entry point is `run_my_shares.py`. Run it with `--dry-run` first. Run it for real only for the share numbers the
  contributor gave you, and only after they have said to go ahead (the real run uses their Claude subscription).
- Do not call any model yourself, write your own test calls, or call the Claude API. Never ask for, read, create or set an
  API key (`ANTHROPIC_API_KEY`, `.env`). If Claude Code is not logged in, tell the contributor to run `claude auth login`
  themselves. It must be a Claude subscription login.
- Do not edit any file in `scripts/`, `strictly_implicit_difficult_advice_pipeline_copy/` or `contributor_shares/`. If
  something is broken, report the exact error and the relevant `terminal_output_*.txt` rather than patching the code: any
  change would make this contributor's prompts differ from everyone else's.
- Do not commit or push anything. `my_share_corpora/` and `zips_to_send_back/` are git-ignored on purpose.
- Run in the background (`nohup python run_my_shares.py --shares <numbers> > run_my_shares_log.txt 2>&1 &`, or the background
  option of your shell tool) and monitor with `tail`. A run takes hours. Progress lines appear about every three minutes; check
  no more often than that.
- If the log says `STOPPED: your Claude usage limit was reached` (exit code 3), do nothing loud: tell the contributor, wait
  for the usage window to reset (do not poll in a loop), then rerun the same command; it resumes. If a failure has a different
  message, rerun once, and if it fails again report the error and file names to the contributor.
- Never run two copies of the command for the same share at once. The run refuses (it keeps a lock file); do not try to bypass it.
- When the log prints `ALL DONE`, report the zip path it printed (under `zips_to_send_back/`) and remind the contributor to
  send that file to whoever sent them the kit.
- If the run says a share was already run (exit code 4, or a refusal naming a seed already started), do not move, rename or
  delete any folder or record file to get around it. Run the contributor's next incomplete share instead, and start a
  `--second-pass` only if the contributor asks and all their assigned shares have been run once.

## Control rewrite shares

If the contributor was given a control rewrite share (a share of 400 prompts to rewrite, not specs to deal), everything above
applies with one change: add `--control-rewrite` to every command, for example
`nohup python run_my_shares.py --shares 3 --control-rewrite > run_my_shares_control_rewrite_log.txt 2>&1 &`, and run
`--dry-run` first, then `--status` to see progress. Its files are in `control_rewrite_shares/` (do not edit them) and its
results go to `control_rewrite_share_corpora/` (git-ignored). It needs no scenario seed, so never add `--second-pass` or
`--scenario-seed-base` to it (the run refuses). It makes about three model calls per prompt, against about ten per spec for a
spec share (an estimate, not measured); the usage limit, resume and zip steps are the same. When the log prints `ALL DONE`,
report the zip path (named `..._control_rewrite_shares_...`) for the contributor to send back.
