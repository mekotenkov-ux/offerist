"""Offline proposal charts: PNG, SVG and provenance. Python 3.10+ / Pillow.

No API calls, authentication, analytics UI or data interpolation.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
import hashlib
import html
import json
import math
from pathlib import Path
import re
from itertools import product

COLORS = ("#2186EB", "#16A085", "#EA9528", "#8C68CF", "#D65173", "#657A99", "#B1792A", "#56944D")
INK, MUTED, GRID = "#20252B", "#58636F", "#E8EDF2"
WIDTH = 1200


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def nonempty(obj, key):
    value = obj.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key}: nonempty text required")
    return value


def validate(spec, base):
    if "sample" in spec and not isinstance(spec["sample"], bool):
        raise ValueError("sample must be a JSON boolean, not a string or number")
    for key in ("title", "source", "period", "filters", "metric", "definition", "limitation", "derivation"):
        nonempty(spec, key)
    datetime.fromisoformat(nonempty(spec, "captured_at").replace("Z", "+00:00"))
    if spec["type"] not in {"line", "bar", "hbar", "stacked"}:
        raise ValueError("type must be line, bar, hbar or stacked")
    if spec["unit"] not in {"count", "number", "percent", "ratio"}:
        raise ValueError("unit must be count, number, percent or ratio")
    labels = spec["labels"]
    if not isinstance(labels, list) or not labels:
        raise ValueError("labels: at least one value required")
    if any(not isinstance(x, str) or not x.strip() for x in labels) or len(set(labels)) != len(labels):
        raise ValueError("labels must be distinct nonempty strings")
    if spec["type"] == "line":
        dates = [date.fromisoformat(x) for x in labels]
        if dates != sorted(dates) or len(dates) < 3:
            raise ValueError("line requires at least three ordered ISO dates; use bars for two periods")
    series = spec["series"]
    if not isinstance(series, list) or not series:
        raise ValueError("at least one comparable series required")
    names = [nonempty(s, "name") for s in series]
    if len(set(names)) != len(names):
        raise ValueError("series names must be distinct")
    observed = 0
    for s in series:
        values = s["values"]
        if len(values) != len(labels):
            raise ValueError("series length must match labels")
        for value in values:
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                raise ValueError("values must be nonnegative finite numbers or null")
            if spec["unit"] == "count" and int(value) != value:
                raise ValueError("count values must be integers")
            if spec["unit"] in {"ratio", "percent"} and value > (1 if spec["unit"] == "ratio" else 100):
                raise ValueError("rate exceeds unit range")
            observed += 1
    if not observed:
        raise ValueError("no observed values; use an access limitation table")
    if spec["type"] == "stacked":
        if spec.get("parts_exclusive") is not True or spec["unit"] not in {"count", "number"}:
            raise ValueError("stacked requires mutually exclusive additive parts")
        totals = spec.get("totals", [])
        if len(totals) != len(labels):
            raise ValueError("stacked requires a total for every population")
        for i, total in enumerate(totals):
            vals = [s["values"][i] for s in series]
            if any(v is None for v in vals) or isinstance(total, bool) or not isinstance(total, (int, float)) or not math.isfinite(total):
                raise ValueError("stacked totals and all parts must be known")
            if not math.isclose(sum(vals), total, rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError("stacked parts do not reconcile to total")
    if spec.get("sample") is True:
        return None
    ref = spec.get("source_data", {})
    path = (Path(base) / nonempty(ref, "path")).resolve()
    if not path.is_file() or digest(path) != ref.get("sha256"):
        raise ValueError("source_data file missing or SHA-256 mismatch")
    return path


def format_value(value, unit):
    if value is None:
        return "нет данных"
    scaled = value * 100 if unit == "ratio" else value
    suffix = "%" if unit in {"ratio", "percent"} else ""
    if scaled >= 1e12:
        return f"{scaled:.2e}".replace(".", ",") + suffix
    body = f"{scaled:,.0f}" if scaled == int(scaled) else f"{scaled:,.2f}".rstrip("0").rstrip(".")
    return body.replace(",", " ").replace(".", ",") + suffix


def axis_maximum(value, unit):
    """Readable zero-based ticks; count axes never suggest fractional visits."""
    if value >= 1e300:
        return value
    target = (value / 4 * 1.15) or .25
    power = 10 ** math.floor(math.log10(target))
    step = next(x*power for x in (1,1.5,2,2.5,3,4,5,6,8,10) if x*power >= target)
    if unit == "count":
        step = max(1, math.ceil(step))
    result = step*4
    if not math.isfinite(result):
        raise ValueError("values too large for a readable chart")
    if unit in {"ratio", "percent"}:
        result = min(result, 1 if unit == "ratio" else 100)
    return result


def font_path(bold=False):
    candidates = [Path("C:/Windows/Fonts/" + ("segoeuib.ttf" if bold else "segoeui.ttf")),
                  Path("/usr/share/fonts/truetype/dejavu/" + ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"))]
    for p in candidates:
        if p.is_file():
            return str(p)
    raise ValueError("Cyrillic font unavailable; install Segoe UI or DejaVu Sans")


class Canvas:
    """One geometry for both raster and vector outputs; render PNG at 2x."""
    def __init__(self, height):
        from PIL import Image, ImageDraw, ImageFont
        self.ImageFont = ImageFont
        self.image = Image.new("RGB", (WIDTH * 2, height * 2), "white")
        self.draw = ImageDraw.Draw(self.image)
        self.fonts = {}
        self.svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{height}" viewBox="0 0 {WIDTH} {height}">',
                    '<rect width="100%" height="100%" fill="white"/>']

    def font(self, size, bold=False):
        key = size, bold
        if key not in self.fonts:
            self.fonts[key] = self.ImageFont.truetype(font_path(bold), size * 2)
        return self.fonts[key]

    def text(self, x, y, value, size=18, fill=INK, bold=False, anchor="start"):
        value = str(value)
        width = self.draw.textlength(value,font=self.font(size,bold))/2
        start = x if anchor=="start" else x-width if anchor=="end" else x-width/2
        if start < 12 or start+width > WIDTH-12:
            raise ValueError("text exceeds chart width; shorten label or split chart")
        self.draw.text((x * 2, y * 2), value, font=self.font(size, bold), fill=fill,
                       anchor={"start": "lt", "middle": "mt", "end": "rt"}[anchor])
        self.svg.append(f'<text x="{x}" y="{y + size}" text-anchor="{anchor}" font-family="Segoe UI,DejaVu Sans,sans-serif" font-size="{size}" font-weight="{700 if bold else 400}" fill="{fill}">{html.escape(value)}</text>')

    def rect(self, x, y, w, h, color):
        self.draw.rectangle((x*2, y*2, (x+w)*2, (y+h)*2), fill=color)
        self.svg.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{color}"/>')

    def line(self, points, color=GRID, width=1):
        self.draw.line([(x*2,y*2) for x,y in points], fill=color, width=width*2)
        coords = " ".join(f"{x},{y}" for x,y in points)
        self.svg.append(f'<polyline points="{coords}" fill="none" stroke="{color}" stroke-width="{width}"/>')

    def dot(self, x, y, color, r=4):
        self.draw.ellipse(((x-r)*2,(y-r)*2,(x+r)*2,(y+r)*2), fill=color)
        self.svg.append(f'<circle cx="{x}" cy="{y}" r="{r}" fill="{color}"/>')

    def save(self, png, svg):
        self.image.save(png)
        svg.write_text("\n".join(self.svg + ["</svg>"]), encoding="utf-8")


def wrap_pixels(text, width, size, bold=False):
    from PIL import ImageFont
    font=ImageFont.truetype(font_path(bold),size*2)
    lines, line = [], ""
    for token in str(text).split():
        trial = line+" "+token if line else token
        if font.getlength(trial)/2 <= width:
            line=trial
            continue
        if line:
            lines.append(line)
            line=""
        # Long paths/URLs also wrap instead of silently extending off canvas.
        while font.getlength(token)/2 > width:
            cut=1
            while cut < len(token) and font.getlength(token[:cut+1])/2 <= width:
                cut+=1
            lines.append(token[:cut])
            token=token[cut:]
        line=token
    if line:
        lines.append(line)
    return lines


def _panel_layout(spec, rendered_type, label_indices, series_indices):
    """Measure one bounded raster page before allocating Pillow memory."""
    labels, series = spec["labels"], spec["series"]
    title_lines = wrap_pixels(spec["title"], 1120, 26, True)
    period_lines = wrap_pixels(spec["period"], 1120, 19)
    filter_lines = wrap_pixels("Срез: " + spec["filters"], 1120, 17)
    legend = [(i, wrap_pixels(series[i]["name"], 1060, 18))
              for i in series_indices]
    footer = ["Источник: " + spec["source"] + " · Получено: " + spec["captured_at"],
              "Определение: " + spec["definition"],
              "Ограничение: " + spec["limitation"]]
    if any(v is None for s in series for v in s["values"]):
        footer.append("Пропуски — нет данных; соединение через пропуск не строится.")
    if spec.get("sample") is True:
        footer.insert(0, "ДЕМОНСТРАЦИОННЫЕ ДАННЫЕ — не использовать как анализ сайта.")
    footer_lines = [line for paragraph in footer for line in wrap_pixels(paragraph, 1120, 16)]
    y = 28 + 34 * len(title_lines) + 12
    period_y = y
    y += 26 * len(period_lines) + 8
    filter_y = y
    y += 24 * len(filter_lines) + 12
    legend_y = y
    y += sum(24 * len(lines) + 8 for _, lines in legend) + 30
    metric_lines = wrap_pixels(spec["metric"], 1000 if rendered_type in {"line", "bar", "stacked"} else 560, 17)
    top = y + 25 * len(metric_lines) + 8
    if rendered_type in {"hbar", "hstacked"}:
        row_lines = {j: wrap_pixels(labels[j], 340, 17) for j in label_indices}
        row_heights = {j: max(60, len(row_lines[j]) * 22 + 12,
                              30 * len(series_indices) + 16 if rendered_type == "hbar" else 0)
                       for j in label_indices}
        bottom = top + sum(row_heights.values())
    else:
        row_lines, row_heights = {}, {}
        bottom = top + 360
    footer_y = bottom + 72
    height = footer_y + 25 * len(footer_lines) + 34
    return locals()


def _render_panel(spec, rendered_type, label_indices, series_indices, png, svg):
    layout = _panel_layout(spec, rendered_type, label_indices, series_indices)
    c = Canvas(layout["height"])
    for i, line in enumerate(layout["title_lines"]):
        c.text(40, 28 + 34 * i, line, size=26, bold=True)
    for i, line in enumerate(layout["period_lines"]):
        c.text(40, layout["period_y"] + 26 * i, line, size=19, fill=MUTED)
    for i, line in enumerate(layout["filter_lines"]):
        c.text(40, layout["filter_y"] + 24 * i, line, size=17, fill=MUTED)
    y = layout["legend_y"]
    for i, lines in layout["legend"]:
        color = COLORS[i % len(COLORS)]
        c.line([(40, y + 10), (60, y + 10)], color, 3)
        c.dot(50, y + 10, color)
        for line in lines:
            c.text(70, y, line, size=18)
            y += 24
        y += 8
    top, bottom = layout["top"], layout["bottom"]
    horizontal = rendered_type in {"hbar", "hstacked"}
    left, right = (400, 950) if horizontal else (120, 1130)
    for i, line in enumerate(layout["metric_lines"]):
        c.text(left, top - 25 * (len(layout["metric_lines"]) - i), line, size=17, fill=MUTED)
    if rendered_type in {"stacked", "hstacked"}:
        maximum = max(spec["totals"])
    else:
        # A common zero-based scale makes every page directly comparable.
        maximum = max((v for item in spec["series"] for v in item["values"] if v is not None), default=0)
    maximum = axis_maximum(maximum, spec["unit"])
    for i in range(5):
        value = maximum * (i / 4)
        if horizontal:
            x = left + (right - left) * i / 4
            c.line([(x, top), (x, bottom)])
            c.text(x, bottom + 18, format_value(value, spec["unit"]), size=16,
                   fill=MUTED, anchor="middle")
        else:
            y = bottom - (bottom - top) * i / 4
            c.line([(left, y), (right, y)])
            c.text(left - 14, y - 10, format_value(value, spec["unit"]), size=16,
                   fill=MUTED, anchor="end")
    if rendered_type == "line":
        days = [date.fromisoformat(spec["labels"][j]).toordinal() for j in label_indices]
        span = days[-1] - days[0]
        xs = [left + (right - left) * (d - days[0]) / span if span else (left + right) / 2
              for d in days]
        for i in series_indices:
            previous = None
            for j, x in zip(label_indices, xs):
                value = spec["series"][i]["values"][j]
                if value is None:
                    previous = None
                    continue
                point = x, bottom - (bottom - top) * (value / maximum)
                if previous is not None:
                    c.line([previous, point], COLORS[i % len(COLORS)], 3)
                c.dot(*point, COLORS[i % len(COLORS)], r=3)
                previous = point
        n = len(label_indices)
        tick_positions = sorted({round(k * (n - 1) / min(n - 1, 5))
                                 for k in range(min(n, 6))}) if n > 1 else [0]
        for k in tick_positions:
            c.text(xs[k], bottom + 22, date.fromisoformat(spec["labels"][label_indices[k]]).strftime("%d.%m"),
                   size=17, fill=MUTED, anchor="middle")
    elif horizontal:
        center_y = top
        for j in label_indices:
            row_height = layout["row_heights"][j]
            center = center_y + row_height / 2
            text_lines = layout["row_lines"][j]
            first_y = center - len(text_lines) * 11
            for k, line in enumerate(text_lines):
                c.text(left - 20, first_y + 22 * k, line, size=17, fill=MUTED, anchor="end")
            if rendered_type == "hstacked":
                running = sum(spec["series"][i]["values"][j] for i in range(series_indices[0]))
                for i in series_indices:
                    value = spec["series"][i]["values"][j]
                    length = (right - left) * (value / maximum)
                    if value > 0:
                        c.rect(left + (right - left) * running / maximum,
                               center - 13, length, 26, COLORS[i % len(COLORS)])
                    running += value
                c.text(right + 12, center - 10, format_value(spec["totals"][j], spec["unit"]), size=16)
            else:
                bar_height = min(24, (row_height - 12) / len(series_indices) * .75)
                for k, i in enumerate(series_indices):
                    value = spec["series"][i]["values"][j]
                    y = center - 15 * len(series_indices) + 30 * k
                    length = (right - left) * ((value or 0) / maximum)
                    if value is not None and value > 0:
                        c.rect(left, y, length, bar_height, COLORS[i % len(COLORS)])
                    c.text(left + length + 12, y, format_value(value, spec["unit"]), size=16)
            center_y += row_height
    else:
        step = (right - left) / len(label_indices)
        count = len(series_indices)
        bar_width = min(64, step * .72 / (count if rendered_type == "bar" else 1))
        for k, j in enumerate(label_indices):
            center = left + step * (k + .5)
            lines = wrap_pixels(spec["labels"][j], step * .88, 16)
            for l, line in enumerate(lines):
                c.text(center, bottom + 20 + 20 * l, line, size=16, fill=MUTED, anchor="middle")
            running = (sum(spec["series"][i]["values"][j] for i in range(series_indices[0]))
                       if rendered_type == "stacked" else 0)
            for t, i in enumerate(series_indices):
                value = spec["series"][i]["values"][j]
                x = center - bar_width / 2 + (t - (count - 1) / 2) * bar_width * 1.1 if rendered_type == "bar" else center - bar_width / 2
                h = (bottom - top) * ((value or 0) / maximum)
                y = bottom - (bottom - top) * (running / maximum) - h
                if value is not None and value > 0:
                    c.rect(x, y, bar_width * .9, h, COLORS[i % len(COLORS)])
                if rendered_type == "bar":
                    c.text(x + bar_width * .45, y - 24, format_value(value, spec["unit"]),
                           size=16, anchor="middle")
                running += value or 0
            if rendered_type == "stacked":
                y = bottom - (bottom - top) * spec["totals"][j] / maximum
                c.text(center, y - 25, format_value(spec["totals"][j], spec["unit"]), size=17, bold=True, anchor="middle")
    c.line([(40, layout["footer_y"] - 16), (1160, layout["footer_y"] - 16)])
    for i, line in enumerate(layout["footer_lines"]):
        c.text(40, layout["footer_y"] + 25 * i, line, size=16, fill=MUTED)
    c.save(png, svg)


def _display_spec(spec):
    """Keep raster pages bounded; put every expanded label on separate text pages."""
    display = spec.copy()
    display["labels"] = spec["labels"].copy()
    display["series"] = [item.copy() for item in spec["series"]]
    notes = []

    def excerpt(key, value, length):
        if len(value) <= length:
            return value
        notes.append((key, value))
        return value[:length].rstrip() + " … (полный текст на листах описания)"

    for key in ("title", "source", "period", "filters", "metric", "definition", "limitation"):
        display[key] = excerpt(key, spec[key], 220)
    for i, label in enumerate(spec["labels"]):
        display["labels"][i] = excerpt(f"labels[{i}]", label, 300)
    for i, item in enumerate(spec["series"]):
        display["series"][i]["name"] = excerpt(f"series[{i}].name", item["name"], 90)
    return display, notes


def _render_notes(lines, png, svg, number, count):
    c = Canvas(104 + 24 * len(lines))
    c.text(40, 26, f"Полные подписи и описание · лист {number}/{count}", size=24, bold=True)
    for i, line in enumerate(lines):
        if line:
            c.text(40, 76 + 24 * i, line, size=17)
    c.save(png, svg)


def render(spec, base, out, name):
    source_path = validate(spec, base)
    if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name) or name in {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}:
        raise ValueError("name must be a safe lower-case file ID")
    labels, series, kind = spec["labels"], spec["series"], spec["type"]
    display, notes = _display_spec(spec)
    rendered_type = kind
    if kind == "bar" and (len(labels) > 6 or len(series) > 3 or
                          any(len(wrap_pixels(label, 150, 16)) > 1 for label in labels)):
        rendered_type = "hbar"
    if kind == "stacked" and (len(labels) > 6 or
                              any(len(wrap_pixels(label, 150, 16)) > 1 for label in labels)):
        rendered_type = "hstacked"
    series_groups = [list(range(i, min(i + 8, len(series))))
                     for i in range(0, len(series), 8)]
    if rendered_type == "line":
        label_groups = [list(range(i, min(i + 500, len(labels))))
                        for i in range(0, len(labels), 500)]
    elif rendered_type in {"hbar", "hstacked"}:
        # Partition by measured row height, keeping each 2x RGB PNG reasonably small.
        label_groups = []
        for sg in series_groups:
            current, used = [], 0
            for j, label in enumerate(display["labels"]):
                rows = len(wrap_pixels(label, 340, 17))
                height = max(60, rows * 22 + 12, 30 * len(sg) + 16 if rendered_type == "hbar" else 0)
                if current and used + height > 800:
                    label_groups.append((current, sg))
                    current, used = [], 0
                current.append(j)
                used += height
            if current:
                label_groups.append((current, sg))
    else:
        label_groups = [list(range(i, min(i + 6, len(labels))))
                        for i in range(0, len(labels), 6)]
    panels = label_groups if rendered_type in {"hbar", "hstacked"} else list(product(label_groups, series_groups))
    out = Path(out)
    paths = []
    for index, (label_indices, series_indices) in enumerate(panels, 1):
        stem = name if len(panels) == 1 else f"{name}-p{index:03d}"
        paths.append((out / (stem + ".png"), out / (stem + ".svg"), label_indices, series_indices))
    note_lines = []
    for key, value in notes:
        note_lines.extend(wrap_pixels(f"{key}: {value}", 1120, 17))
        note_lines.append("")
    note_chunks = [note_lines[i:i + 75] for i in range(0, len(note_lines), 75)]
    note_paths = [(out / f"{name}-notes-p{i:03d}.png", out / f"{name}-notes-p{i:03d}.svg", chunk)
                  for i, chunk in enumerate(note_chunks, 1)]
    receipt_path = out / (name + ".json")
    all_images = [(png, svg) for png, svg, _, _ in paths] + [(png, svg) for png, svg, _ in note_paths]
    if receipt_path.exists() or any(p.exists() for pair in all_images for p in pair):
        raise ValueError("output already exists; use a new revision/name")
    # Layout is measured before allocation; text overflow moves to bounded note pages.
    for _, _, li, si in paths:
        if _panel_layout(display, rendered_type, li, si)["height"] > 2400:
            raise ValueError("chart panel exceeds safe raster memory; use fewer series per visual")
    out.mkdir(parents=True, exist_ok=True)
    for png, svg, li, si in paths:
        _render_panel(display, rendered_type, li, si, png, svg)
    for i, (png, svg, chunk) in enumerate(note_paths, 1):
        _render_notes(chunk, png, svg, i, len(note_paths))
    outputs = {p.name: digest(p) for pair in all_images for p in pair}
    panel_receipts = [{"kind": "chart", "png": png.name, "svg": svg.name,
                       "labels": li, "series": si}
                      for png, svg, li, si in paths]
    panel_receipts.extend({"kind": "notes", "png": png.name, "svg": svg.name,
                           "labels": [], "series": []} for png, svg, _ in note_paths)
    first_png, first_svg = paths[0][:2]
    receipt = {"schema_version": 1, "renderer": "offerist-metrika-inspired-v2",
               "generated_at": datetime.now(timezone.utc).isoformat(),
               "rendered_type": rendered_type, "panels": panel_receipts,
               "spec": spec, "source_data_sha256": digest(source_path) if source_path else None,
               "outputs": outputs,
               "evidence": {"id": name, "kind": "chart", "client_visible": True,
                            "status": "observed" if source_path else "unavailable",
                            "source": spec["source"], "captured_at": spec["captured_at"],
                            "scope": spec["filters"], "observation": spec["title"],
                            "caption": spec["period"] + "; " + spec["definition"] + "; " + spec["limitation"],
                            "limitation": spec["limitation"], "path": first_png.name,
                            "sha256": digest(first_png)}}
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    return {"png": str(first_png.resolve()), "svg": str(first_svg.resolve()),
            "receipt": str(receipt_path.resolve()),
            "panels": [{"png": str(png.resolve()), "svg": str(svg.resolve())} for png, svg in all_images]}

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input",required=True,type=Path)
    parser.add_argument("--out",required=True,type=Path)
    parser.add_argument("--name",required=True)
    args=parser.parse_args()
    try:
        spec=json.loads(args.input.read_text(encoding="utf-8-sig"))
        result=render(spec,args.input.parent,args.out,args.name)
    except (ValueError,KeyError,TypeError,OSError,ImportError) as error:
        parser.exit(2,f"Chart error: {error}\n")
    print(json.dumps(result,ensure_ascii=False))


if __name__=="__main__":
    main()
