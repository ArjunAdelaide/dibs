"""Build the Dibs roadmap page: a static SVG dependency map plus the plan."""
from html import escape

W, H = 1290, 806
NW, NH = 208, 40
COL = {"built": 104, "s1": 340, "s2": 576, "s3": 826, "s4": 1066}
ROW = {"1a": 76, "1b": 126, "2a": 188, "2b": 238, "3a": 300, "3b": 350, "3c": 400, "4a": 462, "4b": 512,
       "5a": 574, "5b": 624, "6a": 686, "6b": 736}
LANES = [("Channel", 64, 112), ("Brain", 176, 112), ("Booking", 288, 162), ("Money", 450, 112), ("Supply", 562, 112), ("Demand", 674, 112)]

# id: (column, row, status, title, sub)
N = {
    "imsg":   ("built", "1a", "done", "iMessage on your Mac", "own Apple ID"),
    "start":  ("built", "1b", "done", "Stays awake, restarts", "./start"),
    "chat":   ("built", "2a", "done", "Chat agent", "summary and YES are code"),
    "router": ("built", "2b", "done", "Model router", "one model list for each job"),
    "superv": ("built", "3a", "test", "Checkout window (Quick18)", "not proven on real pages"),
    "optext": ("built", "3b", "done", "You complete by text", "booked 3 REF"),
    "browse": ("built", "3c", "test", "Browser agent reads times", "result changes between runs"),
    "stripe": ("built", "4b", "done", "Stripe: card, hold, charge", "test mode"),
    "live4":  ("built", "5a", "done", "4 venues with live slots", "Quick18, MiClub"),
    "world":  ("built", "5b", "done", "Venues and events anywhere", "links outside Adelaide"),
    "alerts": ("built", "6a", "done", "Alerts, weekly, auto-book", "one YES, or none"),
    "memory": ("built", "6b", "done", "Memory, welcome, privacy", "privacy page is a draft"),

    "shanx":  ("s1", "3a", "you",  "1.1 Real booking at Shanx", "you, approx. $10"),
    "miclub": ("s1", "3b", "todo", "1.2 MiClub checkout script", "Claude"),
    "check":  ("s1", "5a", "you",  "1.3 Check the venue list", "you"),
    "arrive": ("s1", "5b", "you",  "1.4 Pay-on-arrival venues", "you, then Claude"),
    "video":  ("s1", "6b", "you",  "1.7 Demo video and post", "you, after 1.1"),

    "number": ("s2", "1a", "cost", "2.2 Provider phone number", "Sendblue or Linq"),
    "cloud":  ("s2", "1b", "cost", "2.3 Cloud server", "Claude"),
    "paid":   ("s2", "2b", "cost", "2.1 Paid AI models", "you pay, Claude sets"),
    "agentb": ("s2", "3c", "cost", "2.4 Agent completes bookings", "no payment sites"),

    "autop":  ("s3", "3b", "biz",  "3.4 Automatic venue payment", "Claude"),
    "dcard":  ("s3", "4a", "biz",  "3.3 Card that Dibs pays with", "provider not confirmed"),
    "livem":  ("s3", "4b", "biz",  "3.2 Stripe live mode", "real money"),
    "deals":  ("s3", "5a", "biz",  "3.5 Partner access, deals", "you"),

    "city2":  ("s4", "5a", "later", "4.2 Second home city", "you and Claude"),
    "groups": ("s4", "6a", "later", "4.1 Group bookings", "Claude"),
}
NS = (COL["s4"], 110, NW, 262)  # the North Star box

def box(i):
    c, r, *_ = N[i]
    return COL[c], ROW[r]

def right(i):
    x, y = box(i); return x + NW, y + NH / 2
def left(i):
    x, y = box(i); return x, y + NH / 2
def top(i):
    x, y = box(i); return x + NW / 2, y
def bottom(i):
    x, y = box(i); return x + NW / 2, y + NH

out = []
def line(points, label=None, lx=None, ly=None, anchor="middle", arrow=True):
    d = " ".join(f"{x:.0f},{y:.0f}" for x, y in points)
    out.append(f'<polyline class="e" points="{d}"' + (' marker-end="url(#arrow)"' if arrow else "") + "/>")
    if label:
        out.append(f'<text class="el" x="{lx:.0f}" y="{ly:.0f}" text-anchor="{anchor}">{escape(label)}</text>')

def h(a, b, label=None, dy=-6):
    (x1, y1), (x2, y2) = right(a), left(b)
    if abs(y1 - y2) < 1:
        line([(x1, y1), (x2 - 2, y2)], label, (x1 + x2) / 2, y1 + dy)
    else:  # one step: out, across the gap, in
        mx = x2 - 14
        line([(x1, y1), (mx, y1), (mx, y2), (x2 - 2, y2)], label, x1 + 8, y1 + dy, "start")

# --- lanes ---
svg = [f'<svg viewBox="0 0 {W} {H}" role="img" aria-label="Dependency map of Dibs: the parts that are built, and the steps in four stages that lead to the North Star, a booking with no further step from the user.">',
       '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse"><path class="ah" d="M0,0 L10,5 L0,10 z"/></marker></defs>']
for i, (name, y, hgt) in enumerate(LANES):
    if i % 2 == 0:
        svg.append(f'<rect class="lane" x="0" y="{y}" width="{W}" height="{hgt}"/>')
    svg.append(f'<text class="lanename" x="14" y="{y + hgt / 2 + 4}">{name}</text>')
heads = [("built", "BUILT"), ("s1", "STAGE 1 · $0"), ("s2", "STAGE 2 · SMALL MONTHLY COST"), ("s3", "STAGE 3 · BUSINESS"), ("s4", "STAGE 4 · GROW")]
for c, t in heads:
    svg.append(f'<text class="colhead" x="{COL[c]}" y="38">{t}</text>')

# --- the gate: every stage 3 step waits for a registered business ---
gx = 797
GATE = [f'<rect class="gate" x="{gx}" y="288" width="20" height="386" rx="5"/>',
        f'<text class="gatetext" transform="translate({gx + 14},481) rotate(-90)" text-anchor="middle">3.1 GATE · REGISTERED BUSINESS (ABN), TERMS, REFUND POLICY · YOU</text>']

# --- edges ---
h("imsg", "number", "provider replaces the Mac")
x, y = bottom("number"); line([(x, y), (x, ROW["1b"] - 2)])
(x1, y1) = right("cloud"); line([(x1, y1), (NS[0] - 2, y1)], "always on", (x1 + NS[0]) / 2, y1 - 6)
h("router", "paid", "change one line in .env")
(x1, y1) = right("paid"); line([(x1, y1), (NS[0] - 2, y1)], "fast, steady replies", (x1 + NS[0]) / 2, y1 - 6)
x, y = bottom("paid"); x -= 60; line([(x, y), (x, ROW["3c"] - 2)], "makes it reliable", x + 8, 330, "start")
h("superv", "shanx")
h("superv", "miclub")
h("browse", "agentb")
# scripts and the agent feed automatic payment
(x1, y1), (x2, y2) = right("shanx"), left("autop")
line([(x1, y1), (790, y1), (790, y2 - 8), (x2 - 2, y2 - 8)], "proven checkout scripts", x1 + 8, y1 - 6, "start")
(x1, y1) = right("miclub"); line([(x1, y1), (790, y1)], arrow=False)
(x1, y1) = right("agentb"); line([(x1, y1), (790, y1), (790, y2 + 8), (x2 - 2, y2 + 8)])
x, y = top("dcard"); line([(x, y), (x, ROW["3b"] + NH + 2)], "pays the venue", x + 8, y - 22, "start")
(x1, y1) = right("autop"); line([(x1, y1), (NS[0] - 2, y1)], None)
h("stripe", "livem", "same code, live key")
(x1, y1) = right("livem"); nx = NS[0] + NW / 2; line([(x1, y1), (nx, y1), (nx, NS[1] + NS[3] + 2)], "real money", x1 + 10, y1 - 6, "start")
h("live4", "check")
(x1, y1), (x2, y2) = right("check"), left("deals"); line([(x1, y1), (x2 - 2, y2)], "checked supply", (x1 + 576 + NW) / 2 - 60, y1 - 6)
(x1, y1), (x2, y2) = right("alerts"), left("deals")
line([(x1, y1), (790, y1), (790, y2 + 10), (x2 - 2, y2 + 10)], "demand data: what users want and cannot get", x1 + 244, y1 - 6, "start")
h("deals", "city2")
x, y = NS[0] + 40, NS[1] + NS[3]

svg += [e for e in out if e]
svg += GATE

# --- nodes ---
for i, (c, r, status, title, sub) in N.items():
    x, y = COL[c], ROW[r]
    svg.append(f'<rect class="n {status}" x="{x}" y="{y}" width="{NW}" height="{NH}" rx="7"/>')
    svg.append(f'<text class="nt" x="{x + 11}" y="{y + 17}">{escape(title)}</text>')
    svg.append(f'<text class="ns" x="{x + 11}" y="{y + 32}">{escape(sub)}</text>')
    if status == "done":
        svg.append(f'<text class="tick" x="{x + NW - 11}" y="{y + 18}" text-anchor="end">✓</text>')

x, y, w, hh = NS
svg.append(f'<rect class="star" x="{x}" y="{y}" width="{w}" height="{hh}" rx="10"/>')
svg.append(f'<text class="startag" x="{x + 16}" y="{y + 34}">NORTH STAR</text>')
for k, t in enumerate(["The user says", "what they want.", "Dibs books it.", "No more steps."]):
    svg.append(f'<text class="startext" x="{x + 16}" y="{y + 74 + k * 30}">{t}</text>')
svg.append(f'<text class="ns" x="{x + 16}" y="{y + hh - 46}">True now for: alerts,</text>')
svg.append(f'<text class="ns" x="{x + 16}" y="{y + hh - 31}">weekly bookings with</text>')
svg.append(f'<text class="ns" x="{x + 16}" y="{y + hh - 16}">auto-book (you still pay the venue).</text>')
svg.append("</svg>")
SVG = "\n".join(svg)

def table(rows):
    body = "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in rows)
    return ('<div class="scroll"><table><thead><tr><th>Step</th><th>Who</th><th>What it needs</th><th>Done when</th></tr></thead>'
            f"<tbody>{body}</tbody></table></div>")

PAGE = f'''<title>Dibs Roadmap</title>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,500;12..96,700&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
/* Layout: one column. A wide dependency map first (lanes = parts of the product, columns = stages), then the plan as stage tables. */
:root {{
  color-scheme: dark;
  --bg: #0c0f16; --surface: #141925; --lane: #10141d; --line: #273143; --fg: #e8ebf2; --muted: #8f99ac;
  --done: #4fc38a; --done-bg: #12261f; --test: #e6b455; --test-bg: #2a2212; --cost: #6fb1ff; --biz: #b79cff; --star: #ff7a59; --star-bg: #2a1512;
  --display: "Bricolage Grotesque", "Avenir Next", "Segoe UI", sans-serif;
  --body: "IBM Plex Sans", -apple-system, "Segoe UI", sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, Menlo, monospace;
}}
@media (prefers-color-scheme: light) {{ :root:not([data-theme="dark"]) {{
  color-scheme: light;
  --bg: #f5f6f9; --surface: #ffffff; --lane: #eceef3; --line: #d3d8e2; --fg: #151924; --muted: #5a6476;
  --done: #1c8553; --done-bg: #e3f4eb; --test: #9a6a08; --test-bg: #fbf1d9; --cost: #1d68c4; --biz: #6742c2; --star: #cf4424; --star-bg: #fdebe6;
}} }}
:root[data-theme="light"] {{
  color-scheme: light;
  --bg: #f5f6f9; --surface: #ffffff; --lane: #eceef3; --line: #d3d8e2; --fg: #151924; --muted: #5a6476;
  --done: #1c8553; --done-bg: #e3f4eb; --test: #9a6a08; --test-bg: #fbf1d9; --cost: #1d68c4; --biz: #6742c2; --star: #cf4424; --star-bg: #fdebe6;
}}
body {{ background: var(--bg); color: var(--fg); font: 16px/1.55 var(--body); padding-block: 40px 72px; padding-inline: 20px; }}
main {{ max-width: 1180px; margin: 0 auto; display: grid; gap: 40px; }}
header {{ display: grid; gap: 10px; }}
.eyebrow {{ font: 500 12px/1 var(--mono); letter-spacing: .12em; text-transform: uppercase; color: var(--muted); }}
h1 {{ font: 700 clamp(34px, 6vw, 54px)/1.02 var(--display); margin: 0; letter-spacing: -.02em; text-wrap: balance; }}
h2 {{ font: 700 24px/1.2 var(--display); margin: 0 0 4px; text-wrap: balance; }}
h3 {{ font: 600 17px/1.3 var(--body); margin: 0; }}
p {{ margin: 0; max-width: 66ch; }}
.lede {{ font-size: 18px; color: var(--fg); }}
.muted {{ color: var(--muted); }}
.counts {{ display: flex; flex-wrap: wrap; gap: 10px 28px; margin-top: 8px; font-variant-numeric: tabular-nums; }}
.counts div {{ display: grid; gap: 2px; }}
.counts b {{ font: 700 30px/1 var(--display); }}
.counts span {{ font: 500 11px/1.3 var(--mono); letter-spacing: .08em; text-transform: uppercase; color: var(--muted); }}
section {{ display: grid; gap: 14px; min-width: 0; }}
figure {{ margin: 0; display: grid; gap: 12px; min-width: 0; }}
.scroll {{ overflow-x: auto; border: 1px solid var(--line); border-radius: 12px; background: var(--surface); }}
.scroll svg {{ display: block; min-width: 1120px; width: 100%; height: auto; }}
figcaption {{ color: var(--muted); font-size: 14px; max-width: 80ch; }}
svg text {{ font-family: var(--body); fill: var(--fg); }}
.lane {{ fill: var(--lane); }}
.lanename {{ font: 500 11px var(--mono); letter-spacing: .1em; text-transform: uppercase; fill: var(--muted); }}
.colhead {{ font: 500 11px var(--mono); letter-spacing: .1em; fill: var(--muted); }}
.n {{ fill: var(--surface); stroke: var(--muted); stroke-width: 1.3; }}
.n.done {{ fill: var(--done-bg); stroke: var(--done); }}
.n.test {{ fill: var(--test-bg); stroke: var(--test); stroke-dasharray: 5 3; }}
.n.you, .n.todo {{ stroke: var(--fg); stroke-width: 1.6; }}
.n.cost {{ stroke: var(--cost); }}
.n.biz {{ stroke: var(--biz); }}
.n.later {{ stroke: var(--muted); stroke-dasharray: 2 3; }}
.nt {{ font-size: 12.5px; font-weight: 600; }}
.ns {{ font-size: 10.5px; fill: var(--muted); }}
.tick {{ font-size: 12px; fill: var(--done); font-weight: 600; }}
.e {{ fill: none; stroke: var(--muted); stroke-width: 1.2; }}
.ah {{ fill: var(--muted); }}
.el {{ font-size: 10.5px; fill: var(--muted); font-style: italic; }}
.gate {{ fill: var(--biz); }}
.gatetext {{ font: 500 10px var(--mono); letter-spacing: .08em; fill: var(--bg); }}
.star {{ fill: var(--star-bg); stroke: var(--star); stroke-width: 2; }}
.startag {{ font: 500 11px var(--mono); letter-spacing: .14em; fill: var(--star); }}
.startext {{ font: 700 22px var(--display); }}
.legend {{ display: flex; flex-wrap: wrap; gap: 8px 18px; font-size: 13.5px; color: var(--muted); }}
.legend span {{ display: inline-flex; align-items: center; gap: 8px; }}
.sw {{ width: 22px; height: 14px; border-radius: 4px; border: 1.5px solid var(--muted); background: var(--surface); flex: none; }}
.sw.done {{ border-color: var(--done); background: var(--done-bg); }}
.sw.test {{ border-color: var(--test); background: var(--test-bg); border-style: dashed; }}
.sw.todo {{ border-color: var(--fg); }}
.sw.cost {{ border-color: var(--cost); }}
.sw.biz {{ border-color: var(--biz); }}
.sw.later {{ border-style: dotted; }}
table {{ border-collapse: collapse; width: 100%; min-width: 760px; font-size: 14.5px; }}
th {{ text-align: left; font: 500 11px/1.3 var(--mono); letter-spacing: .08em; text-transform: uppercase; color: var(--muted); padding: 12px 14px; border-bottom: 1px solid var(--line); }}
td {{ padding: 12px 14px; vertical-align: top; border-bottom: 1px solid var(--line); }}
tr:last-child td {{ border-bottom: 0; }}
td:first-child {{ font-weight: 600; width: 27%; }}
td:nth-child(2) {{ width: 13%; color: var(--muted); }}
.tag {{ font: 500 11px/1 var(--mono); letter-spacing: .08em; text-transform: uppercase; padding: 4px 8px; border-radius: 99px; border: 1px solid var(--line); color: var(--muted); margin-left: 8px; white-space: nowrap; }}
ol, ul {{ margin: 0; padding-left: 22px; display: grid; gap: 8px; max-width: 70ch; }}
.two {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); gap: 28px 40px; }}
.two > div {{ display: grid; gap: 12px; align-content: start; min-width: 0; }}
footer {{ color: var(--muted); font-size: 13.5px; border-top: 1px solid var(--line); padding-top: 18px; }}
code {{ font: 13px var(--mono); background: var(--surface); border: 1px solid var(--line); border-radius: 5px; padding: 1px 5px; }}
</style>
<main>
<header>
  <div class="eyebrow">Dibs · plan · 7 October 2026</div>
  <h1>Dibs Roadmap</h1>
  <p class="lede">The user says what they want. Dibs books it. The user does nothing more. This page shows what is built, what is necessary, and the order.</p>
  <div class="counts">
    <div><b>10</b><span>parts built and tested</span></div>
    <div><b>2</b><span>parts built, not proven</span></div>
    <div><b>7</b><span>steps at $0</span></div>
    <div><b>4</b><span>steps with a monthly cost</span></div>
    <div><b>5</b><span>steps that need a business</span></div>
  </div>
</header>

<section>
  <h2>The dependency map</h2>
  <figure>
    <div class="scroll">{SVG}</div>
    <div class="legend">
      <span><i class="sw done"></i>Built and tested</span>
      <span><i class="sw test"></i>Built, not proven</span>
      <span><i class="sw todo"></i>To do, $0</span>
      <span><i class="sw cost"></i>Needs a monthly cost</span>
      <span><i class="sw biz"></i>Needs a registered business</span>
      <span><i class="sw later"></i>Later</span>
    </div>
    <figcaption>Each row is one part of the product. Each column is one stage. An arrow shows that the step at its point needs the step at its start. The checkout window leads to the Shanx test and to the MiClub script. All steps in stage 3 are behind one gate: a registered business. Three arrows go into the North Star: automatic payment to the venue, real money through Stripe, and operation that is always on.</figcaption>
  </figure>
</section>

<section>
  <h2>How to read the plan</h2>
  <div class="two">
    <div>
      <h3>The largest gaps are not the AI model</h3>
      <p>A better model repairs one part: the browser agent. The other gaps are a card that Dibs can pay with, access to venue systems, and a server that is always on.</p>
    </div>
    <div>
      <h3>One rule does not change</h3>
      <p>The code writes the booking summary, checks the YES, and holds the payment. The AI model does not do these steps. This rule stays in each stage.</p>
    </div>
  </div>
</section>

<section>
  <h2>Stage 1 <span class="tag">$0</span></h2>
  <p class="muted">Complete the product for one city with no cost. These steps do not need new services.</p>
  {table([
    ("1.1 Real booking test at Shanx", "You", "Approximately $10 of your money. Ten minutes at the Mac.", "The bar in the window shows “read reference”, and your phone gets the confirmation."),
    ("1.2 MiClub checkout script", "Claude", "Your permission to open the MiClub pages to the payment step.", "A golf booking opens a prepared window, as Shanx does now."),
    ("1.3 Check the Adelaide venue list", "You", "One to two hours. The file is <code>data/venues.json</code>. Today 3 of 50 venues are checked.", "Each venue that you want has a booking page and <code>\"verified\": true</code>."),
    ("1.4 Pay-on-arrival venues", "You, then Claude", "A list of venues that accept payment on arrival, with a booking email. An app password for the Dibs Gmail account.", "A YES sends the request to the venue. The user does nothing more."),
    ("1.5 Venue feeds read in advance", "Claude", "Nothing.", "The first request for a venue takes approximately 2.3 seconds, not 4.3 seconds."),
    ("1.6 “Delete my data” command", "Claude", "Nothing.", "A user removes their messages and notes with one text."),
    ("1.7 Demo video and post", "You", "Step 1.1 complete.", "A 30-second video is in the README and in one public post."),
  ])}
</section>

<section>
  <h2>Stage 2 <span class="tag">small monthly cost</span></h2>
  <p class="muted">Remove the limits of one Mac and of the free AI model.</p>
  {table([
    ("2.1 Paid AI models", "You pay, Claude sets", "An API key from a model provider. One line in <code>.env</code> for each job.", "The browser agent gives the same correct result in 5 of 5 runs on one site."),
    ("2.2 Provider phone number", "You pay, Claude connects", "A messaging provider. Sendblue is approximately $100 each month. Linq is approximately $250 each month plus $1,000 at the start.", "Users send texts to a phone number, not to an email address."),
    ("2.3 Cloud server", "Claude", "Step 2.2. The Mac bridge cannot run on a server.", "Dibs answers when your Mac is off."),
    ("2.4 Browser agent completes bookings", "Claude", "Step 2.1. Only for sites that need no payment and have no bot check.", "A booking on a site with no feed needs no person."),
  ])}
</section>

<section>
  <h2>Stage 3 <span class="tag">registered business</span></h2>
  <p class="muted">Real money, and no person between the YES and the confirmation. Get advice from an accountant before this stage.</p>
  {table([
    ("3.1 Business, terms, refund policy", "You", "A business registration (ABN). Claude can write a first draft of the terms.", "The business exists, and the three documents are public."),
    ("3.2 Stripe live mode", "You, then Claude", "Step 3.1. The code is the same; only the key changes.", "A real card pays for a real booking, and a failed booking releases the hold."),
    ("3.3 A card that Dibs pays with", "You", "Step 3.1. A provider of single-use cards. Stripe Issuing is not confirmed for Australia.", "Each booking has its own card with a limit equal to the held amount."),
    ("3.4 Automatic payment to the venue", "Claude", "Steps 1.1, 1.2, 2.4 and 3.3. The code compares the total on the page with the held amount before it pays.", "Dibs completes a paid booking, and you do nothing."),
    ("3.5 Partner access and venue deals", "You", "Step 3.1. The data from alerts: what users want and cannot get.", "One venue gives Dibs users a slot or a price that other agents do not have."),
  ])}
</section>

<section>
  <h2>Stage 4 <span class="tag">grow</span></h2>
  {table([
    ("4.1 Group bookings", "Claude", "Real users in group chats.", "A group agrees on a plan in the chat, and Dibs books it."),
    ("4.2 Second home city", "You and Claude", "Step 3.5 in the first city. A checked venue list for the new city.", "A second city has live slots, alerts and payment."),
  ])}
</section>

<section>
  <div class="two">
    <div>
      <h2>Your next five actions</h2>
      <ol>
        <li>Start Dibs with <code>./start</code> and keep it on.</li>
        <li>Make one real booking at Shanx through the prepared window (step 1.1).</li>
        <li>Check the venue list, and mark the venues that accept payment on arrival (steps 1.3 and 1.4).</li>
        <li>Read the privacy page. It speaks in your name.</li>
        <li>Record the 30-second video (step 1.7).</li>
      </ol>
    </div>
    <div>
      <h2>What is true today</h2>
      <ul>
        <li>4 venues have live slots. All are in Adelaide.</li>
        <li>Dibs cannot pay a venue. You complete each paid booking.</li>
        <li>Payment is in Stripe test mode. No real money moves.</li>
        <li>Dibs stops when the Mac is off.</li>
        <li>The AI model is on a free plan with a daily limit.</li>
        <li>Outside Adelaide, Dibs finds venues and events, and sends links.</li>
        <li>Dibs does not buy tickets and does not go around a queue or a bot check.</li>
      </ul>
    </div>
  </div>
</section>

<footer>Source: the Dibs repository at github.com/ArjunAdelaide/dibs, 54 automatic tests, and live checks on 3 to 7 October 2026. Provider prices are from the Sendblue comparison page.</footer>
</main>
'''
open("dibs-roadmap.html", "w").write(PAGE)
print(len(PAGE), "bytes;", PAGE.count("<rect class=\"n "), "nodes")
