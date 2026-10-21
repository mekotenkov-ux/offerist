"""Render the fixed proposal contract as editable offline HTML and data-derived SVG."""
from __future__ import annotations

import argparse
import base64
import html
import json
import math
from pathlib import Path
import textwrap

from proposal_contract import heading, image_info, linked_cell, load_template, validate
from proposal_docs import specification, table_column_proportions


def esc(value):
    return html.escape(str(value), quote=True)


def number(value):
    return f"{value:g}".replace(".", ",") if isinstance(value, (int, float)) else str(value)


def svg_text(x, y, value, *, size=14, color="#17212B", anchor="start", weight=400):
    return (f'<text x="{x:g}" y="{y:g}" font-size="{size}" fill="{color}" '
            f'text-anchor="{anchor}" font-weight="{weight}">{esc(value)}</text>')


def wrapped_svg_text(x, y, value, *, width=25, size=14, color="#17212B", anchor="start", weight=400):
    lines = textwrap.wrap(str(value), width=width, break_long_words=True) or [""]
    return "".join(svg_text(x, y + i * (size + 4), line, size=size, color=color,
                            anchor=anchor, weight=weight) for i, line in enumerate(lines))


def visual_svg(section, *, demo=False):
    """One chart from one visual object. No external resources or raster text."""
    visual = section.get("visual")
    if not visual:
        return ""
    if visual["type"] == "image":
        mime, source_width, source_height = image_info(visual["path"])
        payload = base64.b64encode(Path(visual["path"]).read_bytes()).decode("ascii")
        width = 650
        height = width * source_height / source_width
        return (f'<svg xmlns="http://www.w3.org/2000/svg" role="img" '
                f'data-visual="{esc(section["id"])}" aria-label="{esc(visual["alt"])}" '
                f'viewBox="0 0 {width} {height:g}" width="{width}" height="{height:g}">'
                f'<title>{esc(visual["alt"])}</title>'
                f'<image width="{width}" height="{height:g}" preserveAspectRatio="xMidYMid meet" '
                f'href="data:{mime};base64,{payload}"/></svg>')
    kind, labels, values = visual["type"], visual["labels"], visual.get("values", [])
    unit = visual.get("unit", "")
    width = 650
    drawing = []
    badge = "DEMO · Демонстрационные данные" if demo else "Данные раздела"
    drawing.append(svg_text(16, 24, badge, size=12, color="#52606D"))
    if unit:
        drawing.append(svg_text(width - 16, 24, unit, size=12, color="#52606D", anchor="end"))
    palette = ["#3183BF", "#6DA855", "#E2AD42", "#8C9AA7"]
    if kind == "bars":
        row_heights = [max(38, len(textwrap.wrap(label, 24)) * 17 + 12) for label in labels]
        height = 60 + sum(row_heights)
        maximum = max(values or [0]) or 1
        y = 49
        for index, (label, value, row_height) in enumerate(zip(labels, values, row_heights)):
            drawing.append(wrapped_svg_text(16, y + 13, label, width=24, size=13))
            drawing.append(f'<rect x="225" y="{y}" width="350" height="21" rx="4" fill="#EDF2F5"/>')
            drawing.append(f'<rect x="225" y="{y}" width="{350 * value / maximum:.3f}" height="21" rx="4" fill="{palette[index % len(palette)]}"/>')
            drawing.append(svg_text(624, y + 16, number(value), size=14, anchor="end", weight=700))
            y += row_height
    elif kind == "line":
        height = 264
        maximum = max(values or [0]) or 1
        left, right, top, bottom = 56, 620, 55, 202
        for tick in range(5):
            y = bottom - (bottom - top) * tick / 4
            drawing.append(f'<line x1="{left}" y1="{y}" x2="{right}" y2="{y}" stroke="#D8E1E8"/>')
            drawing.append(svg_text(left - 9, y + 4, number(round(maximum * tick / 4, 2)),
                                    size=11, color="#52606D", anchor="end"))
        points = [(left + (right-left) * i / max(1, len(values)-1),
                   bottom - (bottom-top) * v / maximum) for i, v in enumerate(values)]
        drawing.append(f'<polyline points="{" ".join(f"{x:.3f},{y:.3f}" for x,y in points)}" '
                       'fill="none" stroke="#3183BF" stroke-width="3.5" stroke-linejoin="round"/>')
        for index, ((x, y), label, value) in enumerate(zip(points, labels, values)):
            drawing.append(f'<circle cx="{x:.3f}" cy="{y:.3f}" r="4.5" fill="#3183BF" stroke="white" stroke-width="2"/>')
            label_x = x + 9 if index == 0 else x - 9 if index == len(points) - 1 else x
            value_anchor = "start" if index == 0 else "end" if index == len(points) - 1 else "middle"
            drawing.append(svg_text(label_x, y - 10, number(value), size=12, anchor=value_anchor, weight=700))
            axis_anchor = "start" if index == 0 else "end" if index == len(points) - 1 else "middle"
            drawing.append(wrapped_svg_text(x, bottom + 23, label, width=max(6, 65 // max(1, len(labels))),
                                            size=11, color="#52606D", anchor=axis_anchor))
    elif kind in ("steps", "matrix"):
        columns = min(4 if kind == "steps" and len(labels) <= 4 else 3, max(1, len(labels)))
        rows = math.ceil(len(labels) / columns)
        cell_width = (width - 32 - (columns - 1) * 12) / columns
        wrap_width = 18 if columns == 4 else 22
        max_lines = max((len(textwrap.wrap(label, wrap_width)) for label in labels), default=1)
        cell_height = max(90, max_lines * 17 + 52)
        height = 45 + rows * (cell_height + 12)
        for index, label in enumerate(labels):
            x, y = 16 + index % columns * (cell_width + 12), 45 + index // columns * (cell_height + 12)
            drawing.append(f'<rect x="{x}" y="{y}" width="{cell_width}" height="{cell_height}" '
                           'rx="8" fill="#F3F7FA" stroke="#D8E1E8"/>')
            metric = f"{index + 1:02}" if kind == "steps" else number(values[index])
            drawing.append(svg_text(x + 14, y + 32, metric, size=24, color="#3183BF", weight=700))
            drawing.append(wrapped_svg_text(x + 14, y + 57, label, width=wrap_width, size=13))
    else:
        raise ValueError(f"Unsupported visual type: {kind}")
    return (f'<svg xmlns="http://www.w3.org/2000/svg" role="img" '
            f'data-visual="{esc(section["id"])}" aria-label="{esc(kind + ": " + ", ".join(labels))}" '
            f'viewBox="0 0 {width} {height}" width="{width}" height="{height}" '
            'style="font-family:Arial,sans-serif;background:white">'
            f'<rect width="{width}" height="{height}" rx="8" fill="white"/>'
            + "".join(drawing) + "</svg>")


def score_card(label, score):
    value = score["value"]
    reason = esc(score["reason"])
    if value is None:
        display, status = "Нет оценки", "unknown"
    else:
        display = f'{number(value)}<span> / 10</span>'
        status = "positive" if value >= 7 else ("warning" if value >= 4 else "negative")
    return (f'<div class="score {status}"><div class="score-label">{label}</div>'
            f'<div class="score-number">{display}</div><p>{reason}</p></div>')


def table_html(definition, table, section_id=""):
    columns = definition["columns"]
    types = definition.get("column_types", [])
    headings = "".join(f'<th scope="col">{esc(column)}</th>' for column in columns)
    rows = []
    notes = []
    for row in table["rows"]:
        cells = []
        for index, value in enumerate(row):
            cls = ' class="numeric"' if index < len(types) and types[index] in ("number", "integer", "numeric", "human") else ""
            text, href = linked_cell(value, section_id)
            if isinstance(value, dict) and value.get("value") is None:
                cls = ' class="missing"'
                text = "Н/Д"
                reason = " ".join(value.get("reason", "").split())
                if reason and reason not in notes:
                    notes.append(reason)
            content = f'<a href="{esc(href)}">{esc(text)}</a>' if href else esc(text)
            cells.append(f"<td{cls}>{content}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    notes_html = f'<p class="table-notes">Н/Д — {esc("; ".join(notes))}</p>' if notes else ""
    widths = {"analysis_scope": [24, 25, 35, 16], "roadmap": [55, 15, 15, 15],
              "scores": [52, 24, 24], "estimate": [44, 12, 11, 11, 11, 11],
              "external": [40, 20, 20, 20]}.get(definition["id"])
    colgroup = ("<colgroup>" + "".join(f'<col style="width:{width}%">' for width in widths)
                + "</colgroup>") if widths and len(widths) == len(columns) else ""
    return (f'<div class="table-block" data-table="{esc(definition["id"])}">'
            f'<h3>{esc(definition["title"])}</h3><table>{colgroup}<thead><tr>{headings}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table>{notes_html}</div>')


def planned_table_html(table):
    """Render the same editable grid and URLs as the native Docs compiler."""
    proportions = table_column_proportions(table["key"], len(table["rows"][0]))
    colgroup = "<colgroup>" + "".join(
        f'<col style="width:{fraction * 100:g}%">' for fraction in proportions) + "</colgroup>"
    rows = []
    for ri, row in enumerate(table["rows"]):
        tag = "th" if ri == 0 else "td"
        cells = []
        for ci, value in enumerate(row):
            href = table.get("cell_links", {}).get(f"{ri}:{ci}")
            content = f'<a href="{esc(href)}">{esc(value)}</a>' if href else esc(value)
            attributes = ' scope="col"' if ri == 0 else ""
            if table["key"] == "roadmap:roadmap" and ci > 0:
                attributes += ' class="month"'
            cells.append(f"<{tag}{attributes}>{content}</{tag}>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return (f'<div class="table-block" data-table="{esc(table["key"].split(":")[-1])}" '
            f'data-table-key="{esc(table["key"])}"><table>{colgroup}'
            f'<thead>{rows[0]}</thead><tbody>{"".join(rows[1:])}</tbody></table></div>')


def planned_block_html(block, section, *, demo=False):
    kind = block["type"]
    if kind == "table":
        table = block["table"]
        if table["kind"] == "scores":
            return ('<div class="scores">' + score_card("Состояние", section["state"])
                    + score_card("Потенциал работ", section["opportunity"]) + "</div>")
        return planned_table_html(table)
    if kind == "image":
        return ('<div class="chart evidence-image" data-image-key="' + esc(block["key"]) + '">'
                + visual_svg({"id": block["key"], "visual": block["visual"]}, demo=demo) + "</div>")
    role, text = block["role"], esc(block["text"])
    if role == "takeaway":
        return f'<div class="takeaway">{text}</div>'
    if role in {"table_title", "detail_title"}:
        return f'<h3>{text}</h3>'
    if role == "link":
        return f'<p class="analysis-link"><a href="{esc(block["url"])}">{text}</a></p>'
    css = {"intro": "analysis-intro", "interpretation": "interpretation",
           "caption": "evidence-caption", "note": "table-notes"}.get(role, "narrative")
    return f'<p class="{css}">{text}</p>'


def stylesheet(style):
    colors = style["colors"]
    scale = lambda field: float(style[field]) * 4 / 3
    head = style["heading_sizes_pt"]
    return f"""
*{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:#EAF0F4;color:{colors['text']};font-family:Arial,sans-serif;font-size:{scale('body_pt'):.3f}px;line-height:1.4}}
a{{color:#236B9C;text-underline-offset:3px}}.catalog-nav{{position:fixed;top:0;left:0;bottom:0;width:250px;padding:28px 20px;overflow:auto;background:#17212B;color:white}}
.catalog-nav h2{{font-size:19px;margin:0 0 5px}}.catalog-nav p{{font-size:12px;color:#BBC9D4;margin:0 0 20px}}.catalog-nav a{{display:block;color:#E4ECF2;text-decoration:none;font-size:12px;line-height:1.35;margin:0 0 11px}}
main{{margin-left:250px;padding:36px 30px}}.sheet{{position:relative;background:{colors['background']};width:{scale('page_width_pt'):.3f}px;min-height:{scale('page_height_pt'):.3f}px;padding:{scale('margin_pt'):.3f}px;box-shadow:0 8px 35px #17212B15;margin:0 auto 36px;display:flex;flex-direction:column;break-after:page;overflow:visible}}
.sheet-top{{display:flex;justify-content:space-between;align-items:center;gap:15px;margin-bottom:24px;font-size:11px;letter-spacing:1px;color:{colors['muted']};font-weight:700}}
.demo-badge{{letter-spacing:.4px;color:#245D24;background:{colors['positive']};border-radius:3px;padding:5px 8px;font-size:10px;white-space:nowrap}}
h1{{font-size:{head[1]*4/3:.3f}px;line-height:1.17;letter-spacing:-.4px;margin:0 0 20px;font-weight:700;overflow-wrap:anywhere}}
.level-2 h1,.level-3 h1{{font-size:{head[2]*4/3:.3f}px;letter-spacing:0}}
.cover h1{{font-size:{head[0]*4/3:.3f}px;line-height:1.12;margin-top:40px}}.client-name{{font-size:28px;line-height:1.2;font-weight:700;margin:6px 0 7px}}.client-meta{{color:{colors['muted']};margin:0 0 22px}}
.takeaway{{background:{colors['positive']};border:1px solid #6DA855;border-left-width:4px;padding:13px 16px;margin:0 0 20px;font-weight:600;line-height:1.45}}
.analysis-intro,.narrative,.interpretation{{margin:0 0 12px;font-weight:400}}.analysis-link{{margin:0 0 10px;font-weight:400}}.evidence-caption{{font-size:{scale('caption_pt'):.3f}px;color:{colors['muted']};margin:-10px 0 10px}}.month{{text-align:center}}
.scores{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin:0 0 20px}}.score{{padding:13px 15px;border:1px solid {colors['border']};border-radius:6px;background:#F5F8FA}}
.score-label{{font-size:12px;color:{colors['muted']};font-weight:700}}.score-number{{font-size:29px;line-height:1.2;font-weight:700;margin:6px 0}}.score-number span{{font-size:15px;font-weight:400;color:{colors['muted']}}}.score p{{font-size:12px;line-height:1.4;margin:0}}
.score.positive{{border-top:4px solid #6DA855}}.score.warning{{border-top:4px solid #E2AD42}}.score.negative{{border-top:4px solid #D07D63}}.score.unknown{{border-top:4px solid #8C9AA7}}.score.unknown .score-number{{font-size:23px}}
h3{{font-size:{scale('body_pt'):.3f}px;line-height:1.3;margin:0 0 8px;font-weight:700}}.table-block{{margin:0 0 18px;break-inside:avoid}}
table{{border-collapse:collapse;table-layout:fixed;width:100%;font-size:{scale('table_pt'):.3f}px;line-height:1.35}}th,td{{text-align:left;vertical-align:top;padding:8px 10px;border:1px solid {colors['border']};overflow-wrap:anywhere}}
th{{background:{colors['table_header']};font-weight:700}}tbody tr:nth-child(even){{background:#F7FAFC}}td.numeric{{font-variant-numeric:tabular-nums}}td.missing{{color:{colors['muted']};font-size:12px}}
.table-notes{{font-size:{scale('caption_pt'):.3f}px;line-height:1.35;color:{colors['muted']};margin:7px 0 0}}
tr{{break-inside:avoid}}thead{{display:table-header-group}}.chart{{margin:0 0 20px;break-inside:avoid}}.chart svg{{display:block;width:100%;height:auto;max-height:{style['page_height_pt']-2*style['margin_pt']-80}pt}}.chart-unavailable{{background:#F3F7FA;border:1px solid {colors['border']};padding:14px;margin-bottom:20px;color:{colors['muted']}}}
.sheet-footer{{margin-top:auto;padding-top:24px;display:flex;justify-content:space-between;gap:20px;color:{colors['muted']};font-size:11px;border-top:1px solid {colors['border']}}}
@media(max-width:1140px){{.catalog-nav{{position:static;width:auto;max-width:100%}}.catalog-nav a{{display:inline-block;max-width:220px;margin:6px 16px 6px 0}}main{{margin-left:0;padding:24px 12px;overflow:auto}}}}
@media print{{@page{{size:A4;margin:{style['margin_pt']}pt}}body{{background:white}}.catalog-nav{{display:none}}main{{margin:0;padding:0}}.sheet{{display:block;margin:0;box-shadow:none;width:100%;padding:0;min-height:{style['page_height_pt']-2*style['margin_pt']}pt}}.sheet:last-child{{break-after:auto}}.sheet-footer{{display:none}}.sheet-top{{margin-bottom:16px}}h1{{margin-bottom:14px}}.takeaway,.scores,.chart{{margin-bottom:14px}}.takeaway{{padding:10px 14px}}.table-block{{break-inside:auto;margin-bottom:14px}}.table-block[data-table="external"]{{break-inside:avoid}}th,td{{padding:6px 8px}}h1,h3{{break-after:avoid}}.scores,.score,.table-notes,.evidence-caption{{break-inside:avoid}}.analysis-intro,.analysis-link,.chart,.evidence-caption{{break-after:avoid}}a{{color:inherit;text-decoration:none}}}}
"""


def render(data, template=None):
    template = template or load_template()
    errors = validate(data, template)
    if errors:
        raise ValueError(json.dumps(errors, ensure_ascii=False))
    definitions = template["sections"]
    sections = {section["id"]: section for section in data["sections"]}
    demo = data["mode"] == "demo"
    client = data["client"]
    plan = specification(data, template)
    plans = {section["id"]: section for section in plan["sections"]}
    pages, navigation = [], []
    for index, definition in enumerate(definitions, 1):
        section = sections[definition["id"]]
        title = heading(definition)
        navigation.append(f'<a href="#{esc(definition["id"])}">{esc(title)}</a>')
        top_badge = '<span class="demo-badge">DEMO · Демонстрационный пример</span>' if demo else esc(client["date"])
        body = [f'<header class="sheet-top"><span>SEOMEAT / ОФФЕРИСТ</span>{top_badge}</header>',
                f'<h1>{esc(title)}</h1>']
        if definition["id"] == "cover":
            body += [f'<div class="client-name">{esc(client["name"])}</div>',
                     f'<p class="client-meta">{esc(client["domain"])} · {esc(client["date"])}</p>']
        body.extend(planned_block_html(block, section, demo=demo)
                    for block in plans[definition["id"]]["blocks"])
        body.append(f'<footer class="sheet-footer"><span>{esc(client["name"])} · SEO и GEO</span>'
                    f'<span>{index:02} / {len(definitions):02}</span></footer>')
        pages.append(f'<section class="sheet level-{definition.get("level", 1)} {"cover" if definition["id"] == "cover" else ""}" '
                     f'id="{esc(definition["id"])}" data-section="{esc(definition["id"])}">{"".join(body)}</section>')
    csp = "default-src 'none'; style-src 'unsafe-inline'; img-src data:; font-src data:; script-src 'none'; connect-src 'none'"
    return ('<!doctype html><html lang="ru"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<meta http-equiv="Content-Security-Policy" content="{csp}">'
            '<title>Офферист · Строгий шаблон КП</title><style>' + stylesheet(template["style"])
            + '</style></head><body><nav class="catalog-nav" aria-label="Разделы">'
            f'<h2>Шаблон Оффериста</h2><p>{len(definitions)} раздел · {esc(template["schema_version"])}</p>'
            + "".join(navigation) + "</nav><main>" + "".join(pages) + "</main></body></html>")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--template", type=Path)
    args = parser.parse_args()
    template = load_template(args.template)
    output = render(json.loads(args.input.read_text(encoding="utf-8-sig")), template)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(output, encoding="utf-8")
    print(json.dumps({"html": str(args.output.resolve()), "sections": len(template["sections"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
