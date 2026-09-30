# ruff: noqa: E501, N806 -- SVG markup is written as it reads; W and H are the canvas
"""Draw the animated figures the README embeds, into ``docs/assets``.

    python scripts/make_readme_svgs.py

Plain SVG with CSS animation and no script, which is what a README image is
allowed to be. Each figure follows the viewer's colour scheme and stands still
for a viewer who has asked for reduced motion.
"""

import math
import pathlib
import xml.dom.minidom

OUT = pathlib.Path("docs/assets")
FONT = "ui-sans-serif, system-ui, -apple-system, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif"

BASE = f"""
  .bg{{fill:#ffffff;stroke:#d3d7dd}} .plate{{fill:#f4f5f7;stroke:#7c838d}}
  .ink{{fill:#1d2025}} .muted{{fill:#565c66}} .dot{{fill:#c9ced5}}
  .line{{stroke:#1d2025;fill:none}} .soft{{stroke:#7c838d;fill:none}} .hair{{stroke:#d3d7dd;fill:none}}
  .solid{{fill:#1d2025}} .onsolid{{fill:#ffffff}} .mark{{fill:#b42318}} .markline{{stroke:#b42318;fill:none}}
  text{{font-family:{FONT}}}
  @media (prefers-color-scheme: dark){{
    .bg{{fill:#26292e;stroke:#41464e}} .plate{{fill:#30343a;stroke:#8a919b}}
    .ink{{fill:#f2f3f5}} .muted{{fill:#b4bac3}} .dot{{fill:#3a3f47}}
    .line{{stroke:#f2f3f5}} .soft{{stroke:#8a919b}} .hair{{stroke:#41464e}}
    .solid{{fill:#f2f3f5}} .onsolid{{fill:#1c1e22}} .mark{{fill:#ff8a80}} .markline{{stroke:#ff8a80}}
  }}
  @media (prefers-reduced-motion: reduce){{ *{{animation:none !important}} }}
"""


def grid(w, h):
    return (
        f'<defs><pattern id="g" width="20" height="20" patternUnits="userSpaceOnUse">'
        f'<circle cx="1" cy="1" r="1" class="dot"/></pattern></defs>'
        f'<path class="bg" d="M0.5 0.5H{w - 28}L{w - 0.5} 28V{h - 0.5}H0.5Z"/>'
        f'<path fill="url(#g)" d="M1 1H{w - 28}L{w - 1} 28V{h - 1}H1Z"/>'
    )


def plate(x, y, w, h, c=12, cls="plate"):
    return f'<path class="{cls}" d="M{x} {y}H{x + w - c}L{x + w} {y + c}V{y + h}H{x}Z"/>'


# ------------------------------------------------------------------ banner
def banner():
    W, H = 1200, 320

    def y(k):
        return 236 - (108 * k * k - 40 * k)

    xs = [660 + i * 4 for i in range(116)]
    pts = [(x, y((x - 890) / 230)) for x in xs]
    length = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
    d = "M" + " L".join(f"{x:.1f} {yy:.1f}" for x, yy in pts)
    L = math.ceil(length) + 4
    dots = []
    ks = [-0.9 + 0.2 * i for i in range(10)]
    for i, k in enumerate(ks):
        x = 890 + 230 * k
        yy = y(k) + (3, -4, 2, -2, 4, -3, 2, -4, 3, -2)[i]
        half = 7 + 9 * abs(k)
        if i == 5:  # the quote the fitted surface does not explain
            continue
        dots.append(
            f'<g class="q" style="animation-delay:{0.9 + i * 0.13:.2f}s">'
            f'<path class="soft" stroke-width="1.5" d="M{x:.1f} {yy - half:.1f}V{yy + half:.1f}M{x - 4:.1f} {yy - half:.1f}h8M{x - 4:.1f} {yy + half:.1f}h8"/>'
            f'<circle class="solid" cx="{x:.1f}" cy="{yy:.1f}" r="4.5"/></g>'
        )
    ox = 890 + 230 * ks[5]
    oy = y(ks[5]) - 62
    css = (
        BASE
        + f"""
  .curve{{stroke-width:3;stroke-linecap:round;stroke-dasharray:{L};stroke-dashoffset:0;animation:draw 10s ease-in-out infinite}}
  @keyframes draw{{0%{{stroke-dashoffset:{L}}}26%,92%{{stroke-dashoffset:0;opacity:1}}100%{{stroke-dashoffset:0;opacity:0}}}}
  .q{{animation:pop 10s ease-out infinite both}}
  @keyframes pop{{0%{{opacity:0;transform:translateY(6px)}}7%,84%{{opacity:1;transform:none}}92%,100%{{opacity:0}}}}
  .out{{animation:pop 10s ease-out 2.6s infinite both}}
  .ring{{transform-origin:{ox:.1f}px {oy:.1f}px;animation:ring 2.2s ease-out 3.4s infinite}}
  @keyframes ring{{0%{{transform:scale(.5);opacity:.9}}100%{{transform:scale(2.3);opacity:0}}}}
"""
    )
    body = f"""{grid(W, H)}
<text class="ink" x="56" y="122" font-size="42" font-weight="700" letter-spacing="-0.5">Quant Intelligence</text>
<text class="ink" x="56" y="170" font-size="42" font-weight="300" letter-spacing="-0.5">Platform</text>
<text class="muted" x="56" y="218" font-size="19">Derivatives valuation, portfolio risk</text>
<text class="muted" x="56" y="244" font-size="19">and execution intelligence.</text>
<path class="solid" d="M56 266h178l10 10v22H56Z"/>
<text class="onsolid" x="70" y="289" font-size="16" font-weight="700">Analytics, not advice</text>
<path class="hair" d="M640 262H1150M640 60V262"/>
<text class="muted" x="1150" y="288" font-size="14" text-anchor="end">log-moneyness</text>
<text class="muted" x="650" y="52" font-size="14">implied volatility</text>
<path class="line curve" d="{d}"/>
{"".join(dots)}
<g class="out">
  <circle class="markline ring" cx="{ox:.1f}" cy="{oy:.1f}" r="9" stroke-width="2"/>
  <circle class="mark" cx="{ox:.1f}" cy="{oy:.1f}" r="5"/>
  <path class="markline" stroke-width="1.5" stroke-dasharray="3 4" d="M{ox:.1f} {oy + 8:.1f}V{y(ks[5]) - 4:.1f}"/>
  <text class="ink" x="{ox + 16:.1f}" y="{oy - 10:.1f}" font-size="15" font-weight="700">surface deviation</text>
  <text class="muted" x="{ox + 16:.1f}" y="{oy + 9:.1f}" font-size="13">measured, never rated</text>
</g>"""
    return (
        W,
        H,
        css,
        body,
        "Quant Intelligence Platform: a volatility smile is fitted through quoted options and one quote is flagged as a deviation from the surface.",
    )


# -------------------------------------------------------------------- flow
def flow():
    W, H = 1200, 420
    boxes = [
        (30, 175, "Market", "files, live feed, synthetic", "plate"),
        (260, 175, "Market data layer", "quality scores, row counts", "plate"),
        (490, 175, "MarketState", "one consistent snapshot", "solid"),
        (740, 45, "Valuation", "IV, SVI, SSVI, Heston", "plate"),
        (740, 175, "Risk", "VaR, stress, margin", "plate"),
        (740, 305, "Execution", "cost analysis, simulation", "plate"),
        (980, 175, "Decision context", "no recommendation field", "plate"),
    ]
    out = [grid(W, H)]
    paths = [
        "M220 210H260",
        "M450 210H490",
        "M680 210C710 210 710 80 740 80",
        "M680 210H740",
        "M680 210C710 210 710 340 740 340",
        "M930 80C955 80 955 210 980 210",
        "M930 210H980",
        "M930 340C955 340 955 210 980 210",
    ]
    for p in paths:
        out.append(f'<path class="soft flowline" stroke-width="2" d="{p}"/>')
    for x, y, title, sub, cls in boxes:
        w, h = 190, 70
        out.append(plate(x, y, w, h, 12, cls))
        tcls, scls = ("onsolid", "onsolid") if cls == "solid" else ("ink", "muted")
        out.append(
            f'<text class="{tcls}" x="{x + 14}" y="{y + 30}" font-size="18" font-weight="700">{title}</text>'
        )
        out.append(f'<text class="{scls}" x="{x + 14}" y="{y + 52}" font-size="13">{sub}</text>')
    # a pulse that travels the spine, so the direction of flow is unmistakable
    out.append(
        '<circle class="solid pulse" r="5"><animateMotion dur="5s" repeatCount="indefinite" '
        'path="M220 210H260M450 210H490M680 210H740M930 210H980" /></circle>'
    )
    out.append(
        '<text class="muted" x="30" y="392" font-size="14">Every engine reads the same MarketState, so differences between results come from the question, not from the clock.</text>'
    )
    css = (
        BASE
        + """
  .flowline{stroke-dasharray:6 8;animation:flow 1.1s linear infinite}
  @keyframes flow{to{stroke-dashoffset:-14}}
  @media (prefers-reduced-motion: reduce){ .pulse{display:none} }
"""
    )
    return (
        W,
        H,
        css,
        "\n".join(out),
        "Data flow: market data passes through a quality layer into one MarketState snapshot, which feeds valuation, risk and execution, which together form the decision context.",
    )


# ------------------------------------------------------------------ ingest
def ingest():
    W, H = 1200, 270
    steps = [
        ("1", "Upload", ["Drop in the chain as the", "exchange exported it."]),
        ("2", "The file is read", ["Layout, delimiter, header", "row and date order detected."]),
        (
            "3",
            "Ingested or refused",
            ["rows in = kept + excluded", "+ rejected, each with a reason."],
        ),
        ("4", "Analyse", ["Implied volatility, surface,", "deviation scan, portfolio risk."]),
    ]
    out = [grid(W, H)]
    w, h, gap, x0, y0 = 258, 150, 30, 36, 50
    for i, (n, title, lines) in enumerate(steps):
        x = x0 + i * (w + gap)
        out.append(plate(x, y0, w, h, 12))
        out.append(
            f'<path class="line step" style="animation-delay:{i * 2}s" stroke-width="3" d="M{x} {y0}H{x + w - 12}L{x + w} {y0 + 12}V{y0 + h}H{x}Z"/>'
        )
        out.append(f'<circle class="solid" cx="{x + 28}" cy="{y0 + 34}" r="15"/>')
        out.append(
            f'<text class="onsolid" x="{x + 28}" y="{y0 + 40}" font-size="17" font-weight="700" text-anchor="middle">{n}</text>'
        )
        out.append(
            f'<text class="ink" x="{x + 54}" y="{y0 + 41}" font-size="19" font-weight="700">{title}</text>'
        )
        for j, line in enumerate(lines):
            out.append(
                f'<text class="muted" x="{x + 16}" y="{y0 + 86 + j * 22}" font-size="15">{line}</text>'
            )
        if i < 3:
            ax = x + w
            out.append(
                f'<path class="soft arrow" stroke-width="2" d="M{ax + 4} {y0 + h / 2}H{ax + gap - 6}"/>'
            )
            out.append(f'<path class="solid" d="M{ax + gap - 8} {y0 + h / 2 - 5}l8 5l-8 5Z"/>')
    out.append(
        '<text class="muted" x="36" y="238" font-size="14">Nothing is said about the file. What was detected is shown before anything is stored, and a file that cannot be read one way is refused.</text>'
    )
    css = (
        BASE
        + """
  .step{opacity:0;animation:step 8s ease-in-out infinite}
  @keyframes step{0%{opacity:0}4%,22%{opacity:1}28%,100%{opacity:0}}
  .arrow{stroke-dasharray:4 5;animation:flow 0.9s linear infinite}
  @keyframes flow{to{stroke-dashoffset:-9}}
"""
    )
    return (
        W,
        H,
        css,
        "\n".join(out),
        "Four steps: upload a file, the file is read, it is ingested or refused, then analysed.",
    )


for name, build in (("banner", banner), ("flow", flow), ("ingest", ingest)):
    W, H, css, body, title = build()
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" role="img" aria-label="{title}">\n'
        f"<title>{title}</title>\n<style>{css}</style>\n{body}\n</svg>\n"
    )
    (OUT / f"{name}.svg").write_text(svg)
    xml.dom.minidom.parseString(svg)  # refuse to leave a malformed file behind
    print(name, len(svg))
