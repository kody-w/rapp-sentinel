#!/usr/bin/env python3
"""Build docs/index.html from the post markdown.

The HTML is generated, never hand-edited. A second hand-maintained copy of the
same prose is how `docs/index.html` in another repo of mine drifted 268 lines
ahead of its own source and nobody noticed for weeks. One source, one build
step, and a --check mode so CI can fail on a stale output.

    python3 docs/build.py           # write docs/index.html
    python3 docs/build.py --check   # exit 1 if the output is out of date
"""
import html
import re
import sys
from pathlib import Path

import markdown

HOME = Path(__file__).resolve().parent.parent
SRC = HOME / "BLOG-above-not-beside.md"
OUT = HOME / "docs" / "index.html"
MP4 = "above-not-beside.mp4"

TITLE = "RAPP and the new way of working: above AI, not beside it"
DESC = ("A system that can refuse you is worth more than a system that obeys you — "
        "and refusal only comes from handing over situations instead of tasks.")
URL = "https://kody-w.github.io/rapp-sentinel/"

CSS = """
:root{--ground:#0B0D0E;--bone:#E8E4DC;--dim:#8A9199;--green:#3FB950;
--amber:#D29922;--rule:#1E2428}
*{box-sizing:border-box}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--ground);color:var(--bone);
 font:400 19px/1.68 ui-serif,Georgia,"Iowan Old Style",serif;padding:0 1.5rem 8rem}
.wrap{max-width:44rem;margin:0 auto}
header{padding:5.5rem 0 2.5rem}
h1{font-size:clamp(2rem,5.2vw,3rem);line-height:1.14;margin:0 0 1rem;letter-spacing:-.018em}
.sub{color:var(--dim);font-size:1.05rem;font-style:italic;margin:0}
.meta{color:var(--dim);font-size:.83rem;margin-top:1.6rem;
 font-family:ui-monospace,SFMono-Regular,Menlo,monospace}
h2{font-size:1.5rem;margin:3.2rem 0 1rem;line-height:1.25;letter-spacing:-.01em}
h3{font-size:1.13rem;margin:2.2rem 0 .7rem}
p{margin:0 0 1.25rem}
a{color:var(--green);text-decoration:none;border-bottom:1px solid rgba(63,185,80,.35)}
a:hover{border-bottom-color:var(--green)}
hr{border:0;border-top:1px solid var(--rule);margin:3.2rem 0}
code{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:.86em;
 background:#14181B;padding:.12em .38em;border-radius:4px;color:#C9D1D9}
pre{background:#101416;border:1px solid var(--rule);border-radius:9px;
 padding:1.1rem 1.2rem;overflow-x:auto;font-size:.83rem;line-height:1.62}
pre code{background:none;padding:0;font-size:inherit}
blockquote{margin:1.6rem 0;padding:.2rem 0 .2rem 1.3rem;
 border-left:2px solid var(--amber);color:#C6C1B8;font-style:italic}
table{width:100%;border-collapse:collapse;margin:1.8rem 0;font-size:.92rem}
th,td{text-align:left;padding:.62rem .8rem;border-bottom:1px solid var(--rule);vertical-align:top}
th{color:var(--dim);font-weight:600;font-size:.8rem;text-transform:uppercase;letter-spacing:.05em}
ul,ol{margin:0 0 1.25rem;padding-left:1.3rem}
li{margin-bottom:.5rem}
figure.video{margin:2.5rem 0 3rem}
video{width:100%;border-radius:10px;border:1px solid var(--rule);display:block;background:#000}
figcaption{color:var(--dim);font-size:.83rem;margin-top:.7rem;font-style:italic}
footer{margin-top:5rem;padding-top:2rem;border-top:1px solid var(--rule);
 color:var(--dim);font-size:.85rem}
"""


def build() -> str:
    md = SRC.read_text(encoding="utf-8")
    # h1 + italic standfirst become the header; strip them from the body so the
    # title is not rendered twice.
    body = re.sub(r"\A# .*?\n", "", md, count=1)
    body = re.sub(r"\A\s*\*(.+?)\*\s*\n", "", body, count=1)
    body = body.lstrip("\n")
    body = re.sub(r"\A---\s*\n", "", body, count=1)

    rendered = markdown.markdown(
        body, extensions=["tables", "fenced_code", "sane_lists", "attr_list"]
    )

    video = ""
    if (HOME / "docs" / MP4).exists():
        video = f"""
  <figure class="video">
    <video controls preload="metadata" playsinline>
      <source src="{MP4}" type="video/mp4">
    </video>
    <figcaption>90 seconds: the night the system audited its own supervisor.</figcaption>
  </figure>"""

    t, d = html.escape(TITLE), html.escape(DESC)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t}</title>
<meta name="description" content="{d}">
<meta property="og:type" content="article">
<meta property="og:title" content="{t}">
<meta property="og:description" content="{d}">
<meta property="og:url" content="{URL}">
<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="{t}">
<meta name="twitter:description" content="{d}">
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header>
  <h1>{t}</h1>
  <p class="sub">A defense of a pattern, written by the person it kept correcting.</p>
  <p class="meta">kody-w &middot; <a href="https://github.com/kody-w/rapp-sentinel">rapp-sentinel</a></p>
</header>{video}
{rendered}
<footer>
  Every figure in this post is traceable to a public repository. This page is
  generated from
  <a href="https://github.com/kody-w/rapp-sentinel/blob/main/BLOG-above-not-beside.md">BLOG-above-not-beside.md</a>
  by <code>docs/build.py</code>, so the two cannot drift.
</footer>
</div>
</body>
</html>
"""


def main() -> int:
    out = build()
    if "--check" in sys.argv:
        cur = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if cur != out:
            print("docs/index.html is STALE — run: python3 docs/build.py", file=sys.stderr)
            return 1
        print("docs/index.html is up to date")
        return 0
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(out, encoding="utf-8")
    print(f"{OUT} — {len(out):,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
