"""Render the QNX 8.0 codelab into Markdown and GitHub Pages HTML.

    python3 codelab/build.py

Reads ``codelab/content.py`` and writes ``codelab/README.md`` and
``docs/index.html``. Both are generated from one source so the Devpost codelab
URL and the in-repo tutorial can never disagree.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from content import (ANSWERS, AUTHORED, DURATION, LEVEL, OBJECTIVES,
                     PREREQS, STEPS, SUBTITLE, TITLE)

ROOT = Path(__file__).resolve().parent.parent
MD_OUT = Path(__file__).resolve().parent / "README.md"
HTML_OUT = ROOT / "docs" / "index.html"

REPO_URL = "https://github.com/mimimoneydev/qnx-chronoscope"


# ---------------------------------------------------------------------------
# tiny inline markup shared by both renderers
# ---------------------------------------------------------------------------
def inline_html(s: str) -> str:
    e = html.escape(s, quote=False)
    e = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", e)
    e = re.sub(r"`(.+?)`", r"<code>\1</code>", e)
    def link(m: re.Match) -> str:
        url = m.group(0).rstrip(".,;:")
        tail = m.group(0)[len(url):]
        return f'<a href="{url}">{url}</a>{tail}'
    e = re.sub(r"https://[^\s<&]+", link, e)
    return e


def strip_inline(s: str) -> str:
    return s.replace("**", "").replace("`", "")


def total_minutes() -> int:
    return sum(int(s["minutes"]) for s in STEPS)


# ---------------------------------------------------------------------------
# Markdown renderer
# ---------------------------------------------------------------------------
def render_markdown() -> str:
    L: list[str] = []
    L += [
        f"# {TITLE}",
        "",
        f"*{SUBTITLE}*",
        "",
        f"| | |",
        f"|---|---|",
        f"| Level | {LEVEL} |",
        f"| Duration | {DURATION} ({total_minutes()} min of steps) |",
        f"| Target OS | QNX SDP 8.0 / QNX Everywhere |",
        f"| Authored | {AUTHORED} |",
        f"| Repository | {REPO_URL} |",
        "",
        "> This codelab teaches a task **on and for QNX 8.0**: reproducing,",
        "> diagnosing and fixing a priority inversion using the QNX Neutrino",
        "> scheduler's native message passing, then verifying the result",
        "> against real `traceprinter` / `slog2info` / `pidin` output.",
        "",
        "## What you will learn",
        "",
    ]
    L += [f"{i}. {o}" for i, o in enumerate(OBJECTIVES, 1)]
    L += ["", "## Prerequisites", ""]
    L += [f"- {p}" for p in PREREQS]
    L += ["", "## Steps", ""]
    for i, step in enumerate(STEPS, 1):
        L.append(f"{i}. [{step['title']}](#step-{i}-{step['id']}) "
                 f"— {step['minutes']} min")
    L += [""]

    for i, step in enumerate(STEPS, 1):
        L += [
            f'<a id="step-{i}-{step["id"]}"></a>',
            "",
            f"## Step {i}: {step['title']}",
            "",
            f"*{step['minutes']} minutes*",
            "",
        ]
        for blk in step["blocks"]:
            t = blk["type"]
            if t == "text":
                L += [blk["text"], ""]
            elif t == "list":
                L += [f"- {x}" for x in blk["items"]] + [""]
            elif t in ("code", "output"):
                cap = blk.get("caption", "")
                if cap:
                    L += [f"**{cap}**", ""]
                L += [f"```{blk.get('lang', 'text')}",
                      blk["code"], "```", ""]
            elif t == "note":
                icon = {"tip": "Tip", "warning": "Warning"}.get(blk["kind"],
                                                                 "Note")
                L += [f"> **{icon}:** {blk['text']}", ""]
            elif t == "checkpoint":
                L += [f"- [ ] **Checkpoint:** {blk['text']}", ""]
        if step["refs"]:
            L += ["**QNX references:** " + "; ".join(f"`{r}`"
                                                     for r in step["refs"]), ""]
        L += ["---", ""]

    L += ["## Knowledge check — answers", "", "<details>", "<summary>Show answers</summary>", ""]
    L += [f"{i}. {a}" for i, a in enumerate(ANSWERS, 1)]
    L += ["", "</details>", "",
          "## License", "",
          "This codelab is part of Chronoscope for QNX 8.0 and is licensed "
          "under the [MIT License](../LICENSE). QNX is a trademark of "
          "BlackBerry Limited.", ""]
    return "\n".join(L)


# ---------------------------------------------------------------------------
# HTML renderer (GitHub Pages)
# ---------------------------------------------------------------------------
CSS = """
:root{--bg:#0b0f14;--panel:#131920;--panel2:#171f28;--line:#26313d;
--txt:#e6edf3;--muted:#8b98a5;--accent:#58a6ff;--ok:#3fb950;
--warn:#d29922;--bad:#f85149;--purple:#bc8cff;}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--txt);
font:15px/1.65 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
code,pre{font:13px/1.6 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
code{background:#1c2530;border:1px solid var(--line);border-radius:5px;
padding:1px 5px;color:#a5d6ff;font-size:.9em}
pre code{display:block;padding:0;background:none;border:0;color:var(--txt)}
.layout{display:grid;grid-template-columns:268px minmax(0,1fr);gap:0;
max-width:1400px;margin:0 auto}
aside{position:sticky;top:0;height:100vh;overflow:auto;border-right:1px solid var(--line);
background:#0d1218;padding:24px 18px}
aside .brand{font-size:11px;letter-spacing:.22em;color:var(--accent);font-weight:800}
aside h2{font-size:16px;line-height:1.35;margin:10px 0 4px}
aside .meta{font-size:12px;color:var(--muted);margin-bottom:18px}
.progress{height:6px;border-radius:6px;background:#1c2530;overflow:hidden;margin:8px 0 4px}
.progress i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--ok));width:0}
aside .pct{font-size:11px;color:var(--muted);margin-bottom:18px}
nav a{display:flex;gap:9px;align-items:flex-start;padding:7px 9px;border-radius:8px;
color:var(--muted);font-size:12.5px;line-height:1.4}
nav a:hover{background:#18212b;color:var(--txt);text-decoration:none}
nav a .n{color:var(--accent);font-weight:700;min-width:18px}
nav a.done .n{color:var(--ok)}
aside .foot{font-size:11px;color:var(--muted);margin-top:22px;border-top:1px solid var(--line);padding-top:14px}
main{padding:0 40px 100px;min-width:0}
header{padding:44px 0 26px;border-bottom:1px solid var(--line);margin-bottom:28px}
.kicker{font-size:11px;letter-spacing:.25em;color:var(--accent);font-weight:800}
h1{font-size:36px;line-height:1.15;letter-spacing:-1px;margin:12px 0 10px}
header .sub{font-size:16px;color:var(--muted);max-width:820px}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:18px}
.chip{border:1px solid var(--line);background:var(--panel);border-radius:999px;
padding:5px 12px;font-size:12px;color:var(--muted)}
.chip b{color:var(--txt)}
section.card{background:var(--panel);border:1px solid var(--line);border-radius:14px;
padding:22px 24px;margin:0 0 16px}
section.card>h2{font-size:20px;margin:0 0 14px;letter-spacing:-.3px}
section.card li{margin:6px 0}
.msg{border-left:3px solid var(--accent);background:#111922;border-radius:0 8px 8px 0;
padding:12px 16px;margin:14px 0;color:var(--muted)}
.msg b{color:var(--txt)}
.msg.tip{border-color:var(--ok)}.msg.warn{border-color:var(--warn)}
.step{background:var(--panel);border:1px solid var(--line);border-radius:14px;
margin:0 0 22px;overflow:hidden;scroll-margin-top:16px}
.step>.hd{display:flex;align-items:center;gap:14px;padding:18px 24px;
background:linear-gradient(90deg,#111c28,#131920);border-bottom:1px solid var(--line)}
.step>.hd .num{width:34px;height:34px;flex:0 0 34px;border-radius:9px;
background:#17304a;color:var(--accent);display:grid;place-items:center;font-weight:800}
.step>.hd h2{font-size:20px;margin:0;letter-spacing:-.3px}
.step>.hd .t{margin-left:auto;font-size:12px;color:var(--muted);white-space:nowrap}
.step .body{padding:8px 24px 22px}
.step p{margin:14px 0}
.codebox{position:relative;margin:14px 0;border:1px solid var(--line);border-radius:10px;
background:#0c1117;overflow:hidden}
.codebox .cap{font-size:11px;letter-spacing:.08em;text-transform:uppercase;
color:var(--muted);padding:9px 14px;background:#111820;border-bottom:1px solid var(--line);
display:flex;justify-content:space-between;align-items:center}
.codebox pre{margin:0;padding:14px 16px;overflow:auto;color:#d6e2ee}
.codebox.out pre{color:#9fe0b0}
button.copy{border:1px solid var(--line);background:#18222c;color:var(--muted);
border-radius:6px;font-size:11px;padding:3px 8px;cursor:pointer;letter-spacing:0;
text-transform:none}
button.copy:hover{color:var(--txt);border-color:var(--accent)}
.checkpoint{display:flex;gap:10px;align-items:flex-start;background:#101a16;
border:1px solid #244331;border-radius:9px;padding:12px 14px;margin:16px 0;font-size:13.5px}
.checkpoint input{margin-top:4px;accent-color:var(--ok);width:16px;height:16px;cursor:pointer}
.refs{margin-top:16px;font-size:12px;color:var(--muted);border-top:1px solid var(--line);padding-top:12px}
.refs b{color:var(--txt)}
details{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:14px 18px}
summary{cursor:pointer;color:var(--accent);font-weight:600}
details ol{margin:14px 0 0;padding-left:22px}details li{margin:9px 0}
footer{border-top:1px solid var(--line);margin-top:36px;padding-top:18px;
font-size:12px;color:var(--muted)}
@media(max-width:900px){.layout{grid-template-columns:1fr}aside{position:static;height:auto;
border-right:0;border-bottom:1px solid var(--line)}main{padding:0 18px 60px}h1{font-size:28px}}
"""

JS = """
(function(){
  var keys=[], boxes=[];
  document.querySelectorAll('.checkpoint input').forEach(function(el,i){
    var k='chrono-codelab-'+i; keys.push(k); boxes.push(el);
    try{ el.checked = localStorage.getItem(k)==='1'; }catch(e){}
    el.addEventListener('change',function(){
      try{ localStorage.setItem(k, el.checked?'1':'0'); }catch(e){}
      paint();
    });
    var step=el.closest('.step');
    el.addEventListener('change',function(){
      var all=[].slice.call(step.querySelectorAll('.checkpoint input'));
      var done=all.length && all.every(function(x){return x.checked;});
      var link=document.querySelector('nav a[href="#'+step.id+'"]');
      if(link) link.classList.toggle('done',!!done);
    });
  });
  function paint(){
    var n=boxes.filter(function(b){return b.checked;}).length;
    document.getElementById('pbar').style.width=(100*n/Math.max(boxes.length,1))+'%';
    document.getElementById('pct').textContent=n+' of '+boxes.length+' checkpoints complete';
  }
  paint();
  boxes.forEach(function(b){
    var step=b.closest('.step');
    var all=[].slice.call(step.querySelectorAll('.checkpoint input'));
    if(all.length && all.every(function(x){return x.checked;})){
      var link=document.querySelector('nav a[href="#'+step.id+'"]');
      if(link) link.classList.add('done');
    }
  });
  document.querySelectorAll('button.copy').forEach(function(btn){
    btn.addEventListener('click',function(){
      var pre=btn.closest('.codebox').querySelector('pre');
      var txt=pre?pre.innerText:'';
      navigator.clipboard.writeText(txt).then(function(){
        btn.textContent='copied'; setTimeout(function(){btn.textContent='copy';},1200);
      });
    });
  });
})();
"""


def block_html(blk: dict, idx: int) -> str:
    t = blk["type"]
    if t == "text":
        return f"<p>{inline_html(blk['text'])}</p>"
    if t == "list":
        li = "".join(f"<li>{inline_html(x)}</li>" for x in blk["items"])
        return f"<ul>{li}</ul>"
    if t in ("code", "output"):
        cap = blk.get("caption", "Terminal" if t == "code" else "Expected output")
        lang = blk.get("lang", "text") if t == "code" else "text"
        body = html.escape(blk["code"], quote=False)
        cls = "codebox out" if t == "output" else "codebox"
        return (f'<div class="{cls}" data-lang="{html.escape(lang)}">'
                f'<div class="cap"><span>{inline_html(cap)}</span>'
                f'<button class="copy" type="button">copy</button></div>'
                f"<pre><code>{body}</code></pre></div>")
    if t == "note":
        label = {"tip": "Tip", "warning": "Warning"}.get(blk["kind"], "Note")
        return (f'<div class="msg {html.escape(blk["kind"])}">'
                f"<b>{label}:</b> {inline_html(blk['text'])}</div>")
    if t == "checkpoint":
        return ('<label class="checkpoint"><input type="checkbox">'
                f"<span><b>Checkpoint:</b> {inline_html(blk['text'])}</span></label>")
    return ""


def render_html() -> str:
    nav = "".join(
        f'<a href="#step-{i}-{s["id"]}" id="nav-{i}"><span class="n">{i:02d}</span>'
        f'<span>{html.escape(s["title"])}<br><small>{s["minutes"]} min</small></span></a>'
        for i, s in enumerate(STEPS, 1))
    objectives = "".join(f"<li>{inline_html(o)}</li>" for o in OBJECTIVES)
    prereqs = "".join(f"<li>{inline_html(p)}</li>" for p in PREREQS)
    steps_html: list[str] = []
    for i, s in enumerate(STEPS, 1):
        body = "".join(block_html(b, i) for b in s["blocks"])
        refs = ("<div class=\"refs\"><b>QNX references:</b> " +
                "; ".join(html.escape(r) for r in s["refs"]) + "</div>") if s["refs"] else ""
        steps_html.append(f"""
<section class="step" id="step-{i}-{s['id']}">
  <div class="hd"><div class="num">{i}</div><h2>{html.escape(s['title'])}</h2>
    <div class="t">{s['minutes']} min</div></div>
  <div class="body">{body}{refs}</div>
</section>""")
    answers = "".join(f"<li>{inline_html(a)}</li>" for a in ANSWERS)
    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="QNX 8.0 codelab: find and fix a priority inversion with Chronoscope.">
<title>{html.escape(TITLE)} | QNX 8.0 Codelab</title>
<style>{CSS}</style></head><body>
<div class="layout">
<aside>
  <div class="brand">QNX 8.0 CODELAB</div>
  <h2>{html.escape(TITLE)}</h2>
  <div class="meta">{DURATION} &middot; {LEVEL}<br>{html.escape(AUTHORED)}</div>
  <div class="progress"><i id="pbar"></i></div>
  <div class="pct" id="pct"></div>
  <nav>{nav}</nav>
  <div class="foot">Chronoscope for QNX 8.0<br>
    <a href="{REPO_URL}">Repository</a> &middot;
    <a href="{REPO_URL}/blob/main/codelab/README.md">Markdown version</a><br>
    MIT licensed. QNX is a trademark of BlackBerry Limited.</div>
</aside>
<main>
<header>
  <div class="kicker">QNX EVERYWHERE &middot; SDP 8.0 &middot; HANDS-ON CODELAB</div>
  <h1>{html.escape(TITLE)}</h1>
  <div class="sub">{inline_html(SUBTITLE)}</div>
  <div class="chips">
    <span class="chip">Duration <b>{DURATION}</b></span>
    <span class="chip">Level <b>{LEVEL}</b></span>
    <span class="chip">Target <b>QNX 8.0 / QEMU / Raspberry Pi 5</b></span>
    <span class="chip">Authored <b>November 2026</b></span>
  </div>
</header>

<section class="card">
  <h2>What you will learn</h2>
  <ol>{objectives}</ol>
  <div class="msg"><b>Why this matters:</b> on a hard real-time operating
  system, "it usually finishes in 2 ms" is not an engineering statement. This
  codelab produces a machine-checkable answer to <em>when</em> code runs, what
  blocked it, and what happens if you change the QNX configuration.</div>
</section>

<section class="card">
  <h2>Prerequisites</h2>
  <ul>{prereqs}</ul>
</section>

{''.join(steps_html)}

<section class="card">
  <h2>Knowledge check &mdash; answers</h2>
  <details><summary>Show answers</summary><ol>{answers}</ol></details>
</section>

<footer>
  Chronoscope for QNX 8.0 &middot; Codelab authored {html.escape(AUTHORED)}
  &middot; <a href="{REPO_URL}">{REPO_URL}</a> &middot; MIT License.
  QNX is a trademark of BlackBerry Limited; this project is not affiliated
  with or endorsed by BlackBerry/QNX.
</footer>
</main></div>
<script>{JS}</script></body></html>
"""


def main() -> None:
    MD_OUT.write_text(render_markdown(), encoding="utf-8")
    HTML_OUT.parent.mkdir(parents=True, exist_ok=True)
    HTML_OUT.write_text(render_html(), encoding="utf-8")
    print(f"wrote {MD_OUT.relative_to(ROOT)}  ({MD_OUT.stat().st_size:,} bytes)")
    print(f"wrote {HTML_OUT.relative_to(ROOT)} ({HTML_OUT.stat().st_size:,} bytes)")
    print(f"{len(STEPS)} steps, {total_minutes()} minutes, {len(ANSWERS)} quiz answers")


if __name__ == "__main__":
    main()
