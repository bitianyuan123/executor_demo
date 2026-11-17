#!/usr/bin/env python3
"""Render the Markdown review as a self-contained HTML and a navigable CJK PDF.

Usage:
    python3 reports/final_review/build_report.py
    python3 reports/final_review/build_report.py --source /tmp/sample.md --output-dir /tmp/review

Dependencies: markdown-it-py, reportlab; a Chinese TrueType font. All images are
read locally. No browser, network service, JavaScript or remote font is needed.
The Markdown remains the source of truth; this program does not alter it.
"""
from __future__ import annotations

import argparse
import base64
import html
import math
import mimetypes
import re
import shutil
import subprocess
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

from markdown_it import MarkdownIt
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    BaseDocTemplate, Frame, HRFlowable, Image, LongTable, PageBreak,
    PageTemplate, Paragraph, Spacer, TableStyle, NextPageTemplate,
)
from reportlab.platypus.tableofcontents import TableOfContents

ROOT = Path(__file__).resolve().parent
FONT_NAME = "ReportCJK"
FONT_CANDIDATES = [
    Path("/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc"),
    Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
    Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
]


def markdown_parser() -> MarkdownIt:
    return MarkdownIt("commonmark", {"html": True}).enable("table").enable("strikethrough")


def plain_inline(token) -> str:
    if token.type == "inline":
        return "".join(plain_inline(child) for child in (token.children or []))
    if token.type in {"text", "code_inline"}:
        return token.content
    if token.type == "image":
        return token.content
    if token.type in {"softbreak", "hardbreak"}:
        return " "
    return ""


def prepare_tokens(parser: MarkdownIt, source: str):
    tokens = parser.parse(source)
    headings = []
    used = set()
    for index, token in enumerate(tokens):
        if token.type != "heading_open":
            continue
        title = plain_inline(tokens[index + 1])
        base = re.sub(r"[^\w\-]+", "-", title.lower(), flags=re.UNICODE).strip("-") or "section"
        key, suffix = base, 2
        while key in used:
            key = f"{base}-{suffix}"
            suffix += 1
        used.add(key)
        token.attrSet("id", key)
        headings.append((int(token.tag[1:]), title, key))
    return tokens, headings


def local_image(source: str, base_dir: Path) -> Path:
    parts = urlsplit(source)
    if parts.scheme or parts.netloc:
        raise ValueError(f"Only local images are allowed, got: {source}")
    path = Path(unquote(parts.path))
    if not path.is_absolute():
        path = base_dir / path
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Image does not exist: {path}")
    return path


def iter_children(tokens):
    for token in tokens:
        yield token
        if token.children:
            yield from iter_children(token.children)


def render_html(tokens, headings, parser, source_path: Path, output: Path):
    # Parse independently so replacing images does not affect PDF input.
    html_tokens, _ = prepare_tokens(parser, source_path.read_text(encoding="utf-8"))
    for token in iter_children(html_tokens):
        if token.type == "image":
            path = local_image(token.attrGet("src") or "", source_path.parent)
            mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            encoded = base64.b64encode(path.read_bytes()).decode("ascii")
            token.attrSet("src", f"data:{mime};base64,{encoded}")
        if token.type in {"html_block", "html_inline"}:
            if "<!-- pagebreak -->" in token.content:
                token.content = '<div class="pagebreak"></div>'
            elif token.content.strip().startswith("<!--"):
                token.content = ""
            elif not re.fullmatch(r"\s*</?(?:br|sub|sup)\s*/?>\s*", token.content):
                token.content = html.escape(token.content)
    body = parser.renderer.render(html_tokens, parser.options, {})
    title = headings[0][1] if headings else source_path.stem
    toc = "\n".join(
        f'<a class="level-{level}" href="#{html.escape(key, quote=True)}">{html.escape(text)}</a>'
        for level, text, key in headings if level <= 3
    )
    document = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
:root{--ink:#172b41;--muted:#536b81;--accent:#166e85;--rule:#d8e3eb;--paper:#fff;--canvas:#eef3f7}
*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:var(--canvas);color:var(--ink);font:16px/1.78 system-ui,-apple-system,"Noto Sans CJK SC","Microsoft YaHei",sans-serif}
nav{position:fixed;inset:0 auto 0 0;width:270px;padding:28px 22px;overflow:auto;background:#183345;color:#fff;font-size:13px;line-height:1.5}
nav strong{display:block;font-size:17px;margin-bottom:20px}nav a{display:block;color:#e0edf5;text-decoration:none;padding:5px 0}nav a:hover{color:#86dce6}.level-2{padding-left:10px!important}.level-3{padding-left:22px!important;font-size:12px}
main{margin:28px 30px 60px 300px;background:var(--paper);max-width:1160px;padding:40px 48px;box-shadow:0 2px 12px #213c4b10;border-radius:8px}
h1,h2,h3,h4{line-height:1.35;scroll-margin-top:24px}h1{font-size:31px;letter-spacing:.02em;margin-top:0}h2{font-size:24px;margin-top:46px;padding-top:18px;border-top:2px solid var(--rule)}h3{font-size:19px;color:var(--accent);margin-top:28px}h4{font-size:17px}p{margin:14px 0}a{color:#075d7c;overflow-wrap:anywhere}strong{font-weight:700}img{display:block;max-width:100%;height:auto;margin:20px auto}table{border-collapse:collapse;width:100%;font-size:14px;line-height:1.55;margin:20px 0}th,td{padding:10px 12px;border:1px solid var(--rule);text-align:left;vertical-align:top;overflow-wrap:anywhere}th{background:#e9f2f6;color:#123b4c}tbody tr:nth-child(even){background:#f7fafc}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f0f5f8;padding:16px;border-left:3px solid #91aebf;font-size:13px;line-height:1.55}code{font-family:ui-monospace,"DejaVu Sans Mono",monospace;font-size:.9em;background:#edf3f6;padding:2px 4px;border-radius:3px}pre code{padding:0;background:none}blockquote{margin:18px 0;padding:3px 18px;border-left:4px solid var(--accent);background:#eef7f9;color:#264b5a}li{margin:6px 0}.pagebreak{break-before:page}footer{font-size:12px;color:var(--muted);margin-top:40px;border-top:1px solid var(--rule);padding-top:12px}
@media(max-width:1000px){nav{position:relative;width:auto;max-height:280px}main{margin:16px;padding:22px}h1{font-size:26px}table{font-size:12px}}
@media print{body{background:#fff;font-size:10pt}nav{display:none}main{box-shadow:none;margin:0;padding:0;max-width:none}h2{break-after:avoid}h3{break-after:avoid}img{break-inside:avoid}th{display:table-cell}thead{display:table-header-group}pre{white-space:pre-wrap}a{color:inherit;text-decoration:none}footer{display:none}}
</style></head><body><nav aria-label="目录"><strong>报告目录</strong>__TOC__</nav><main>__BODY__
<footer>离线报告：图片已内嵌，无需网络加载。文中外部来源链接仅供进一步核查。</footer>
</main></body></html>"""
    output.write_text(document.replace("__TITLE__", html.escape(title)).replace("__TOC__", toc).replace("__BODY__", body), encoding="utf-8")


def register_font(font_path: Path | None):
    candidates = [font_path] if font_path else FONT_CANDIDATES
    errors = []
    for path in candidates:
        if path and path.is_file():
            try:
                pdfmetrics.registerFont(TTFont(FONT_NAME, str(path), subfontIndex=0))
                # The installed CJK family has no separate bold/italic face.
                # Reuse the glyph-complete font; heading weight also uses size/color.
                pdfmetrics.registerFontFamily(FONT_NAME, normal=FONT_NAME, bold=FONT_NAME, italic=FONT_NAME, boldItalic=FONT_NAME)
                return path
            except Exception as exc:
                errors.append(f"{path}: {exc}")
    raise RuntimeError("A usable CJK TrueType font is required (--font). " + "; ".join(errors))


def pdf_styles():
    base = dict(fontName=FONT_NAME, wordWrap="CJK", splitLongWords=True, textColor=colors.HexColor("#172b41"))
    styles = {}
    styles["body"] = ParagraphStyle("ReviewBody", fontSize=9.4, leading=14.3, spaceAfter=7, **base)
    styles["title"] = ParagraphStyle("ReviewTitle", fontSize=23, leading=31, spaceAfter=20, **base)
    for level, size in [(1, 18), (2, 15), (3, 12), (4, 10.4), (5, 10), (6, 9.8)]:
        styles[f"h{level}"] = ParagraphStyle(f"ReviewHeading{level}", fontSize=size, leading=size * 1.45,
            spaceBefore=18 if level <= 2 else 11, spaceAfter=8, keepWithNext=True,
            **dict(base, textColor=colors.HexColor("#124f67") if level > 1 else colors.HexColor("#172b41")))
    styles["caption"] = ParagraphStyle("ReviewCaption", fontSize=8.2, leading=11.8, alignment=TA_CENTER,
        spaceAfter=10, **dict(base, textColor=colors.HexColor("#536b81")))
    styles["code"] = ParagraphStyle("ReviewCode", fontSize=7.6, leading=10.6, leftIndent=8, rightIndent=8,
        borderPadding=7, borderWidth=.4, borderColor=colors.HexColor("#d8e3eb"),
        backColor=colors.HexColor("#f1f5f8"), spaceBefore=5, spaceAfter=9, **base)
    styles["quote"] = ParagraphStyle("ReviewQuote", parent=styles["body"], leftIndent=15, rightIndent=8,
        borderColor=colors.HexColor("#6695a9"), borderWidth=.5, borderPadding=7, backColor=colors.HexColor("#f0f7f9"))
    styles["cell"] = ParagraphStyle("ReviewCell", fontSize=7.9, leading=11.2, spaceAfter=0, **base)
    styles["header"] = ParagraphStyle("ReviewTableHeader", parent=styles["cell"], textColor=colors.HexColor("#143e53"))
    styles["small"] = ParagraphStyle("ReviewSmall", fontSize=8.1, leading=11.4, spaceAfter=5,
        **dict(base, textColor=colors.HexColor("#536b81")))
    for level in range(3):
        styles[f"toc{level}"] = ParagraphStyle(f"ReviewTOC{level}", fontSize=10 if level == 0 else 9,
            leading=15, leftIndent=level * 14, firstLineIndent=0, spaceBefore=6 if level == 0 else 2, **base)
    return styles


def inline_pdf(token) -> str:
    content = []
    link_tags = []
    for child in token.children or []:
        kind = child.type
        if kind == "text":
            content.append(html.escape(child.content))
        elif kind in {"softbreak", "hardbreak"}:
            content.append(" " if kind == "softbreak" else "<br/>")
        elif kind == "code_inline":
            content.append('<font backColor="#edf3f6">' + html.escape(child.content) + "</font>")
        elif kind == "image":
            content.append(html.escape(child.content))
        elif kind == "link_open":
            href = child.attrGet("href") or ""
            scheme = urlsplit(href).scheme.lower()
            if href.startswith("#"):
                href = "#" + unquote(href[1:])
            # Relative repository references remain readable labels in the
            # standalone PDF; inventing a file URI would not make them portable.
            if (scheme in {"http", "https", "mailto"}) or href.startswith("#"):
                content.append('<link color="#075d7c" href="' + html.escape(href, quote=True) + '">')
                link_tags.append("link")
            else:
                content.append('<font color="#075d7c">')
                link_tags.append("font")
        elif kind == "link_close":
            content.append("</" + link_tags.pop() + ">")
        elif kind in {"strong_open", "strong_close", "em_open", "em_close", "s_open", "s_close"}:
            content.append({"strong_open": "<b>", "strong_close": "</b>", "em_open": "<i>", "em_close": "</i>", "s_open": "<strike>", "s_close": "</strike>"}[kind])
        elif kind == "html_inline":
            if re.fullmatch(r"\s*</?(?:br|sub|sup)\s*/?>\s*", child.content):
                content.append(child.content.replace("<br>", "<br/>"))
            else:
                content.append(html.escape(child.content))
    return "".join(content)


class ReviewDocument(BaseDocTemplate):
    def __init__(self, filename, title, styles, **kwargs):
        super().__init__(filename, pagesize=A4, leftMargin=41, rightMargin=41,
                         topMargin=43, bottomMargin=43, title=title, author="executor_demo", **kwargs)
        self.report_title = title
        self.styles = styles
        self.outline_level = -1
        landscape_width, landscape_height = landscape(A4)
        self.addPageTemplates([
            PageTemplate(id="review", pagesize=A4, frames=[Frame(self.leftMargin, self.bottomMargin,
                self.width, self.height, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)], onPage=self.page_chrome),
            PageTemplate(id="figure", pagesize=landscape(A4), frames=[Frame(36, 36,
                landscape_width - 72, landscape_height - 76, leftPadding=0, rightPadding=0,
                topPadding=0, bottomPadding=0)], onPage=self.page_chrome),
        ])

    def beforeDocument(self):
        self.outline_level = -1
        self.rendered_figures = []

    def page_chrome(self, canvas, doc):
        canvas.saveState()
        canvas.setFont(FONT_NAME, 7.2)
        canvas.setFillColor(colors.HexColor("#617b8b"))
        label = self.report_title if len(self.report_title) <= 50 else self.report_title[:49] + "…"
        page_width, page_height = canvas._pagesize
        margin = 36 if doc.pageTemplate.id == "figure" else self.leftMargin
        canvas.drawString(margin, page_height - 24, label)
        canvas.setStrokeColor(colors.HexColor("#d8e3eb"))
        canvas.line(margin, 29, page_width - margin, 29)
        canvas.drawString(margin, 18, "executor_demo · 阶段综合报告")
        canvas.drawRightString(page_width - margin, 18, str(doc.page))
        canvas.restoreState()

    def afterFlowable(self, flowable):
        figure = getattr(flowable, "report_figure", None)
        if figure:
            self.rendered_figures.append((self.page, figure))
        heading = getattr(flowable, "report_heading", None)
        if not heading:
            return
        level, text, key = heading
        self.canv.bookmarkPage(key)
        if level <= 3:
            outline_level = min(level - 1, self.outline_level + 1)
            self.canv.addOutlineEntry(text, key, level=outline_level, closed=False)
            self.outline_level = outline_level
            self.notify("TOCEntry", (level - 1, html.escape(text), self.page, key))


class PdfBuilder:
    def __init__(self, tokens, source_path, styles, width, figure_layout="landscape"):
        self.figure_layout = figure_layout
        self.tokens = tokens
        self.source_path = source_path
        self.styles = styles
        self.width = width
        self.list_stack = []
        self.item_prefix = None
        self.quote_depth = 0

    def paragraph(self, token):
        images = [child for child in (token.children or []) if child.type == "image"]
        if images and all(child.type in {"image", "softbreak", "hardbreak"} or
                          (child.type == "text" and not child.content.strip()) for child in token.children):
            result = []
            for child in images:
                result.extend(self.image(child))
            return result
        style = self.styles["quote" if self.quote_depth else "body"]
        text = inline_pdf(token)
        if self.list_stack:
            depth = len(self.list_stack)
            style = ParagraphStyle(f"ListDepth{depth}", parent=style, leftIndent=depth * 14,
                                   firstLineIndent=-11, spaceAfter=4)
            prefix = self.item_prefix or ""
            text = html.escape(prefix) + " " + text
            self.item_prefix = None
        return [Paragraph(text or "&#160;", style)]

    def image(self, token):
        path = local_image(token.attrGet("src") or "", self.source_path.parent)
        if path.suffix.lower() == ".svg":
            png = path.with_suffix(".png")
            if not png.is_file():
                raise ValueError(f"PDF requires a PNG companion for SVG: {path}")
            path = png
        width, height = ImageReader(str(path)).getSize()
        if self.figure_layout == "landscape":
            # Large diagrams keep a dedicated printable page. The fixed 36pt
            # side margins and reserved caption height are independent of the
            # portrait text frame; the next page switches back explicitly.
            page_width, page_height = landscape(A4)
            factor = min((page_width - 72) / width, (page_height - 91) / height)
            picture = Image(str(path), width=width * factor, height=height * factor, lazy=2)
            picture.hAlign = "CENTER"
            picture.report_figure = path.name
            picture.spaceBefore = 0
            picture.spaceAfter = 2
            picture.keepWithNext = bool(token.content)
            result = [NextPageTemplate("figure"), PageBreak(), picture]
            if token.content:
                caption = ParagraphStyle("LandscapeCaption", parent=self.styles["caption"],
                    fontSize=7.4, leading=10, spaceAfter=0)
                result.append(Paragraph(html.escape(token.content), caption))
            result.extend([NextPageTemplate("review"), PageBreak()])
            return result
        # Optional compact version for screen comparison or small illustrations.
        factor = min(self.width / width, 610 / height)
        picture = Image(str(path), width=width * factor, height=height * factor, lazy=2)
        picture.hAlign = "CENTER"
        picture.report_figure = path.name
        picture.spaceBefore = 8
        picture.spaceAfter = 5
        picture.keepWithNext = bool(token.content)
        result = [picture]
        if token.content:
            result.append(Paragraph(html.escape(token.content), self.styles["caption"]))
        return result

    def table(self, start):
        rows, current, headers = [], [], 0
        index = start + 1
        head = False
        while index < len(self.tokens):
            token = self.tokens[index]
            if token.type == "table_close":
                break
            if token.type == "thead_open":
                head = True
            elif token.type == "thead_close":
                head = False
            elif token.type == "tr_open":
                current = []
            elif token.type in {"th_open", "td_open"}:
                inline = self.tokens[index + 1]
                current.append((inline_pdf(inline), plain_inline(inline)))
            elif token.type == "tr_close":
                rows.append(current)
                if head:
                    headers += 1
            index += 1
        if not rows:
            return [], index + 1
        count = max(len(row) for row in rows)
        # A smooth content-weighted allocation avoids a single long cell taking
        # nearly all the width. CJK wrapping and splitInRow cover long rows.
        lengths = [max(sum(1 if ord(c) < 128 else 1.8 for c in row[col][1])
                       for row in rows if col < len(row)) for col in range(count)]
        weights = [min(3.5, max(1.0, math.sqrt(length / 16))) for length in lengths]
        minimum = min(48, self.width / count * .72)
        free = self.width - minimum * count
        widths = [minimum + free * weight / sum(weights) for weight in weights]
        cells = []
        for row_index, row in enumerate(rows):
            style = self.styles["header"] if row_index < headers else self.styles["cell"]
            cells.append([Paragraph(value[0] or "&#160;", style) for value in row] + [""] * (count - len(row)))
        table = LongTable(cells, colWidths=widths, repeatRows=headers, hAlign="LEFT", splitByRow=1, splitInRow=1,
                          spaceBefore=7, spaceAfter=12)
        commands = [("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("GRID", (0, 0), (-1, -1), .35, colors.HexColor("#d8e3eb")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5), ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 5), ("BOTTOMPADDING", (0, 0), (-1, -1), 5)]
        if headers:
            commands.append(("BACKGROUND", (0, 0), (-1, headers - 1), colors.HexColor("#e9f2f6")))
        for row in range(headers, len(rows)):
            if (row - headers) % 2:
                commands.append(("BACKGROUND", (0, row), (-1, row), colors.HexColor("#f7fafc")))
        table.setStyle(TableStyle(commands))
        return [table], index + 1

    def build(self, skip_first_title=True):
        story, index, skipped_title = [], 0, False
        while index < len(self.tokens):
            token = self.tokens[index]
            kind = token.type
            if kind == "heading_open":
                inline = self.tokens[index + 1]
                level = int(token.tag[1:])
                if skip_first_title and not skipped_title and level == 1:
                    skipped_title = True
                    index += 3
                    continue
                paragraph = Paragraph(inline_pdf(inline), self.styles[f"h{level}"])
                paragraph.report_heading = (level, plain_inline(inline), token.attrGet("id"))
                story.append(paragraph)
                index += 3
                continue
            if kind == "paragraph_open":
                story.extend(self.paragraph(self.tokens[index + 1]))
                index += 3
                continue
            if kind == "table_open":
                result, index = self.table(index)
                story.extend(result)
                continue
            if kind in {"bullet_list_open", "ordered_list_open"}:
                self.list_stack.append({"ordered": kind == "ordered_list_open", "number": int(token.attrGet("start") or 1)})
            elif kind in {"bullet_list_close", "ordered_list_close"}:
                self.list_stack.pop()
                self.item_prefix = None
            elif kind == "list_item_open":
                current = self.list_stack[-1]
                self.item_prefix = f'{current["number"]}.' if current["ordered"] else "·"
                current["number"] += 1
            elif kind == "list_item_close":
                self.item_prefix = None
            elif kind == "blockquote_open":
                self.quote_depth += 1
            elif kind == "blockquote_close":
                self.quote_depth -= 1
            elif kind in {"fence", "code_block"}:
                # Paragraph's CJK wrapping also breaks overlong ASCII identifiers.
                text = html.escape(token.content.rstrip("\n")).replace(" ", "&#160;").replace("\n", "<br/>")
                story.append(Paragraph(text or "&#160;", self.styles["code"]))
            elif kind == "hr":
                story.append(HRFlowable(width="100%", thickness=.5, color=colors.HexColor("#c4d7e1"), spaceBefore=8, spaceAfter=8))
            elif kind == "html_block" and "<!-- pagebreak -->" in token.content:
                story.append(PageBreak())
            index += 1
        return story


def render_pdf(tokens, headings, source_path, output, figure_layout="landscape"):
    styles = pdf_styles()
    title = headings[0][1] if headings else source_path.stem
    doc = ReviewDocument(str(output), title, styles)
    title_para = Paragraph(html.escape(title), styles["title"])
    if headings:
        title_para.report_heading = headings[0]
    toc = TableOfContents()
    toc.levelStyles = [styles[f"toc{level}"] for level in range(3)]
    toc.dotsMinLevel = 0
    toc_heading = ParagraphStyle("ContentsHeading", parent=styles["h2"], keepWithNext=False)
    story = [title_para, Paragraph("目录", toc_heading), toc, PageBreak()]
    story.extend(PdfBuilder(tokens, source_path, styles, doc.width, figure_layout).build())
    doc.multiBuild(story)
    return doc.rendered_figures


def verify_pdf_pages(path: Path, figures, layout: str):
    """Inspect the written PDF's actual page boxes, not template intentions.

    Poppler is optional for rendering but used whenever available for this
    independent check. An unsupported/failed audit is never printed as PASS.
    """
    if not shutil.which("pdfinfo"):
        print("PDF page audit unavailable: install poppler-utils to inspect actual page sizes.")
        return
    environment = dict(os.environ, LC_ALL="C")
    result = subprocess.run(["pdfinfo", "-f", "1", "-l", "100000", str(path)],
                            check=True, capture_output=True, text=True, env=environment)
    pages = {int(number): (float(width), float(height)) for number, width, height in
             re.findall(r"Page\s+(\d+) size:\s+([\d.]+) x ([\d.]+)", result.stdout)}
    count = re.search(r"^Pages:\s+(\d+)", result.stdout, flags=re.MULTILINE)
    if not count or len(pages) != int(count.group(1)):
        raise RuntimeError("PDF audit could not identify all actual page dimensions")
    landscape_pages = [page for page, (width, height) in pages.items() if width > height]
    figure_pages = [page for page, _ in figures]
    expected = figure_pages if layout == "landscape" else []
    if landscape_pages != expected or (layout == "landscape" and len(set(figure_pages)) != len(figure_pages)):
        raise RuntimeError(f"PDF figure layout mismatch: actual landscape={landscape_pages}, "
                           f"expected={expected}, rendered figures={figures}")
    for page, (width, height) in pages.items():
        desired = landscape(A4) if page in landscape_pages else A4
        if abs(width - desired[0]) > .01 or abs(height - desired[1]) > .01:
            raise RuntimeError(f"PDF page {page} is not the expected A4 size: {width} x {height}")
    print(f"PDF page audit PASS: {len(pages)} pages; {len(landscape_pages)} landscape figure pages "
          + ",".join(map(str, landscape_pages)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "REPORT.md")
    parser.add_argument("--output-dir", type=Path, default=ROOT)
    parser.add_argument("--basename", default="executor_affinity_report")
    parser.add_argument("--font", type=Path, help="Chinese .ttf/.ttc file; defaults to installed WQY")
    parser.add_argument("--figure-layout", choices=("landscape", "inline"), default="landscape",
                        help="PDF figures use dedicated A4 landscape pages by default")
    args = parser.parse_args()
    source = args.source.resolve()
    if not source.is_file():
        parser.error(f"Markdown source does not exist: {source}")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", args.basename):
        parser.error("--basename must be a plain filename without directories")
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    font = register_font(args.font)
    md = markdown_parser()
    tokens, headings = prepare_tokens(md, source.read_text(encoding="utf-8"))
    html_path = output_dir / f"{args.basename}.html"
    pdf_path = output_dir / f"{args.basename}.pdf"
    render_html(tokens, headings, md, source, html_path)
    figures = render_pdf(tokens, headings, source, pdf_path, args.figure_layout)
    verify_pdf_pages(pdf_path, figures, args.figure_layout)
    print(f"HTML: {html_path}")
    print(f"PDF:  {pdf_path}")
    print(f"CJK font: {font}")
    print(f"Headings: {len(headings)}; local images: {sum(t.type == 'image' for t in iter_children(tokens))}")


if __name__ == "__main__":
    main()
