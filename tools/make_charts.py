#!/usr/bin/env python3
"""
Generate the SVG charts embedded in README.md, straight from the cited numbers.

Pure stdlib, no dependencies. Run:  python3 tools/make_charts.py
Every datapoint below is traceable to a source listed in README.md → "Evidence".
Charts use a solid light card background so they render legibly on GitHub in
both light and dark themes.
"""
import os
import xml.dom.minidom as minidom

OUT = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "docs", "charts"))

INK, MUTED, LINE, CARD = "#1d2421", "#6b7670", "#e4e0d7", "#ffffff"
TEAL, AMBER, RED = "#0f7b6c", "#b9821f", "#a4453a"
FONT = "-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif"


def esc(s):
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _val(v, kind):
    return f"{v:g}%" if kind == "pct" else f"{v:g} days"


def _axis(v, kind):
    return f"{int(v)}%" if kind == "pct" else f"{int(v)}"


def write(fname, svg):
    minidom.parseString(svg)  # fail loudly if not well-formed XML
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, fname)
    with open(path, "w", encoding="utf-8") as f:
        f.write(svg)
    print(f"  wrote {os.path.relpath(path)}")


def vbar(title, subtitle, items, ymax, ystep, kind, caption, fname, W=720):
    """Vertical bar chart. items = [(label_with_\n, value, color), ...]"""
    H = 400
    left, right, top, bottom = 70, W - 28, 86, H - 86
    plot_h, plot_w = bottom - top, right - left
    n = len(items)
    slot = plot_w / n
    barw = min(110, slot * 0.5)
    p = [f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" font-family="{FONT}">']
    p.append(f'<rect x="1" y="1" width="{W-2}" height="{H-2}" rx="14" fill="{CARD}" stroke="{LINE}"/>')
    p.append(f'<text x="32" y="40" font-size="19" font-weight="700" fill="{INK}">{esc(title)}</text>')
    p.append(f'<text x="32" y="63" font-size="13" fill="{MUTED}">{esc(subtitle)}</text>')
    v = 0
    while v <= ymax + 1e-6:
        y = bottom - (v / ymax) * plot_h
        p.append(f'<line x1="{left}" y1="{y:.1f}" x2="{right}" y2="{y:.1f}" stroke="{LINE}"/>')
        p.append(f'<text x="{left-10}" y="{y+4:.1f}" font-size="11" fill="{MUTED}" text-anchor="end">{_axis(v, kind)}</text>')
        v += ystep
    for i, (label, value, color) in enumerate(items):
        cx = left + slot * i + slot / 2
        bh = (value / ymax) * plot_h
        by = bottom - bh
        p.append(f'<rect x="{cx-barw/2:.1f}" y="{by:.1f}" width="{barw:.1f}" height="{bh:.1f}" rx="5" fill="{color}"/>')
        p.append(f'<text x="{cx:.1f}" y="{by-9:.1f}" font-size="15" font-weight="700" fill="{INK}" text-anchor="middle">{esc(_val(value, kind))}</text>')
        for j, ln in enumerate(label.split("\n")):
            p.append(f'<text x="{cx:.1f}" y="{bottom+20+j*15:.1f}" font-size="12" fill="{MUTED}" text-anchor="middle">{esc(ln)}</text>')
    p.append(f'<text x="32" y="{H-20}" font-size="10.5" fill="{MUTED}">{esc(caption)}</text>')
    p.append("</svg>")
    write(fname, "\n".join(p))


def funnel(title, subtitle, stages, caption, fname, W=720):
    """Horizontal funnel. stages = [(label, pct, color), ...]"""
    rowh, gap = 40, 22
    top = 92
    H = top + len(stages) * (rowh + gap) + 40
    barx, barmax = 270, W - 270 - 70
    p = [f'<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}" xmlns="http://www.w3.org/2000/svg" font-family="{FONT}">']
    p.append(f'<rect x="1" y="1" width="{W-2}" height="{H-2}" rx="14" fill="{CARD}" stroke="{LINE}"/>')
    p.append(f'<text x="32" y="40" font-size="19" font-weight="700" fill="{INK}">{esc(title)}</text>')
    p.append(f'<text x="32" y="63" font-size="13" fill="{MUTED}">{esc(subtitle)}</text>')
    for i, (label, pct, color) in enumerate(stages):
        y = top + i * (rowh + gap)
        bw = max(6, pct / 100 * barmax)
        p.append(f'<text x="{barx-14}" y="{y+rowh/2+5:.0f}" font-size="13" fill="{INK}" text-anchor="end">{esc(label)}</text>')
        p.append(f'<rect x="{barx}" y="{y}" width="{barmax}" height="{rowh}" rx="6" fill="{LINE}" opacity="0.5"/>')
        p.append(f'<rect x="{barx}" y="{y}" width="{bw:.1f}" height="{rowh}" rx="6" fill="{color}"/>')
        p.append(f'<text x="{barx+bw+12:.1f}" y="{y+rowh/2+5:.0f}" font-size="15" font-weight="700" fill="{INK}">{esc(f"{pct:g}%")}</text>')
    p.append(f'<text x="32" y="{H-18}" font-size="10.5" fill="{MUTED}">{esc(caption)}</text>')
    p.append("</svg>")
    write(fname, "\n".join(p))


if __name__ == "__main__":
    SRC_EVAL = "Source: PMMVY state-wide evaluation, BMC Pregnancy & Childbirth (2025)."
    SRC_MIX = "Sources: NFHS-5 (delivery, immunisation); PMMVY state-wide evaluation, BMC Pregnancy & Childbirth (2025)."

    vbar(
        "Where the help leaks",
        "Share of mothers / children who actually receive each benefit",
        [("Free hospital\ndelivery", 89, TEAL),
         ("Full child\nimmunisation", 77, TEAL),
         ("Maternity cash:\n1st installment", 47.7, AMBER),
         ("Maternity cash:\nall 3 installments", 10, RED)],
        100, 25, "pct", SRC_MIX, "benefit_gap.svg")

    funnel(
        "How far a PMMVY mother actually gets",
        "Of mothers enrolled in the maternity-cash scheme, share reaching each stage",
        [("Enrolled in PMMVY", 100, TEAL),
         ("Received 1st installment", 47.7, AMBER),
         ("Received all 3 installments", 10, RED)],
        SRC_EVAL, "pmmvy_funnel.svg")

    vbar(
        "Even when paid, the cash comes late",
        "Average days to receive each PMMVY installment after the claim",
        [("1st\ninstallment", 90, AMBER),
         ("2nd\ninstallment", 113, AMBER),
         ("3rd\ninstallment", 137, RED)],
        150, 30, "days", SRC_EVAL, "payment_delays.svg")

    print("done.")
