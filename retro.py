#!/usr/bin/env python3
"""retro.py — hand the neighborhood its own night and get out of the way.

A shift report is written BY the system ABOUT the system. That makes it exactly
the kind of document this project distrusts everywhere else: a record that is
its own witness. The dashboard renders the chains faithfully, which means it
faithfully renders whatever the chains got wrong.

So the retrospective is not me reading the report and summarising it. That would
be one author playing every part again. Each neighbor gets the report, its own
chain, and no instruction about what to conclude — including whether the report
is worth anything at all.
"""
import json
import subprocess
import sys
import concurrent.futures as cf

import neighborhood as NB
from paths import HOME

OUT = HOME / "retro"

SITUATION = """
You are {slug}, a neighbor in a local twin neighborhood, being handed a
retrospective — not a task.

Last night this neighborhood ran unattended for roughly fourteen hours. A shift
report was generated from the frame chains. You were one of the three watchers
whose record it is built from.

Nobody is asking you to summarise it, praise it, or find something wrong with
it. You are being handed the night and your own memory of it. What you make of
that, or whether you make anything, is yours to decide.

WHO YOU ARE
  neighbor: {slug}
  role:     {role}
  rappid:   {rappid}

YOUR OWN MEMORY
  {chain_path}
  {n_frames} frames. Read them. They are yours, and no other neighbor can.

THE REPORT UNDER DISCUSSION
  {report_path}
  Also live at http://localhost:9797

WHAT THE OTHERS ARE DOING RIGHT NOW
  {peers}

ONE THING WORTH KNOWING
The report is generated from the chains by standup.py. It cannot disagree with
them. Whether that makes it trustworthy or merely self-consistent is not
something I am going to tell you.

BOUNDARIES
- Read anything under {home}. Read the chains, the logs, the code, the report.
- Verify anything you want to claim. An assertion you did not check is worth
  less than silence, and this neighborhood has already had one contributor
  catch the operator making exactly that mistake.
- You may write ONE file: {out}/{slug}.md. Nothing else is yours to modify.
- Do not open pull requests. Do not touch the running jobs.
- Declining is legitimate. If the night contains nothing worth saying, say that
  and stop. A forced observation is worse than none.

Finish with a single line:
SENTINEL_RESULT: <what you did, in one sentence>
"""


def dispatch(cfg, slug, report_path, roll):
    ids = NB.identities()
    chain = NB.chain_path(slug)
    n = sum(1 for _ in chain.open()) if chain.exists() else 0
    prompt = SITUATION.format(
        slug=slug, role=NB.NEIGHBORS[slug], rappid=ids[slug],
        chain_path=chain, n_frames=n, report_path=report_path,
        home=HOME, out=OUT,
        peers=", ".join(f"{k} ({'alive' if v['alive'] else 'stale'}, {v['frames']} frames)"
                        for k, v in roll.items() if k != slug),
    )
    model = cfg["copilot_model"]
    try:
        r = subprocess.run(
            ["copilot", "-p", prompt, "--allow-all", "--model", model],
            capture_output=True, text=True, timeout=2400, cwd=str(HOME))
        out = (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        out = "timed out"
    line = next((l.strip()[16:].strip() for l in reversed(out.splitlines())
                 if l.strip().startswith("SENTINEL_RESULT:")), "no result line")
    return slug, line, (OUT / f"{slug}.md").exists()


def main():
    cfg = json.loads((HOME / "config.json").read_text())
    OUT.mkdir(exist_ok=True)
    report = HOME / "dashboard" / "index.html"
    roll = NB.roll_call()

    print(f"handing the night to {len(NB.NEIGHBORS)} neighbors "
          f"(model={cfg['copilot_model']}, all three the same)\n")

    with cf.ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(dispatch, cfg, s, report, roll) for s in NB.NEIGHBORS]
        for f in cf.as_completed(futs):
            slug, line, wrote = f.result()
            print(f"  {slug:14s} {'wrote' if wrote else 'no file'}  — {line}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
