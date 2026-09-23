#!/usr/bin/env python3
"""harvest 提取层 —— HTML 解析、结构化提取、Markdown 转换

不依赖 lxml / bs4 / html2text:自带一棵迷你 DOM 树和一个够用的 CSS 选择器。
装上 lxml 也不会更快,因为这里处理的是单页而不是千万页。

对外接口:
    MiniDOM(html).select("div.item > a.title")   → 节点列表
    html_to_markdown(html)                       → Markdown
    html_to_text(html)                           → 纯文本
    extract_links(html, base)                    → [{href, text, rel}]
    extract_meta(html)                           → {title, description, ...}
    extract_tables(html)                         → [[[cell, ...], ...]]
"""

from __future__ import annotations

import re
from html.parser import HTMLParser
from urllib.parse import urljoin

VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}
SKIP_CONTENT = {"script", "style", "noscript", "template", "svg"}
BLOCK_TAGS = {"div", "p", "section", "article", "header", "footer", "main", "aside",
              "ul", "ol", "li", "table", "tr", "td", "th", "pre", "blockquote", "figure"}
HEADING_TAGS = {f"h{i}": i for i in range(1, 7)}


class Node:
    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []
        self.parent = parent

    def __repr__(self):
        return f"<{self.tag} {self.attrs.get('class', '')}>"

    @property
    def classes(self):
        return (self.attrs.get("class") or "").split()

    def text(self, strip=True):
        """递归取文本。"""
        out = []
        for child in self.children:
            out.append(child if isinstance(child, str) else child.text(False))
        joined = "".join(out)
        return re.sub(r"\s+", " ", joined).strip() if strip else joined

    def attr(self, name, default=""):
        return self.attrs.get(name, default)

    def iter(self):
        yield self
        for child in self.children:
            if not isinstance(child, str):
                yield from child.iter()

    def find(self, tag=None, klass=None, node_id=None):
        """第一个匹配的后代节点。"""
        for node in self.iter():
            if node is self:
                continue
            if tag and node.tag != tag:
                continue
            if klass and klass not in node.classes:
                continue
            if node_id and node.attr("id") != node_id:
                continue
            return node
        return None

    def find_all(self, tag=None, klass=None):
        out = []
        for node in self.iter():
            if node is self or (tag and node.tag != tag):
                continue
            if klass and klass not in node.classes:
                continue
            out.append(node)
        return out


class _TreeBuilder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("[root]")
        self.stack = [self.root]
        self.skip_depth = 0
        self.skip_tag = None

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth += 1
            return
        if tag in SKIP_CONTENT:
            self.skip_tag, self.skip_depth = tag, 1
            return
        node = Node(tag, {k.lower(): (v if v is not None else "") for k, v in attrs},
                    self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.skip_tag:
            if tag == self.skip_tag:
                self.skip_depth -= 1
                if self.skip_depth <= 0:
                    self.skip_tag = None
            return
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return

    def handle_data(self, data):
        if self.skip_tag or not data.strip():
            return
        self.stack[-1].children.append(data)


def parse_html(html):
    builder = _TreeBuilder()
    try:
        builder.feed(html)
    except Exception:
        pass
    return builder.root


# ---------------------------------------------------------------- CSS 选择器

_SIMPLE_RE = re.compile(
    r"^(?P<tag>[a-zA-Z][\w-]*|\*)?"
    r"(?P<rest>(?:[.#][\w-]+|\[[^\]]+\])*)$")


class MiniDOM:
    """够用就好的 CSS 选择器:tag、.class、#id、[attr]、[attr=val]、* 及其组合,
    支持后代(空格)与直接子代(>),支持逗号分组。"""

    def __init__(self, html):
        self.root = parse_html(html)

    def select(self, selector):
        out, seen = [], set()
        for group in selector.split(","):
            for node in self._select_group(group.strip()):
                if id(node) not in seen:
                    seen.add(id(node))
                    out.append(node)
        return out

    def _select_group(self, group):
        if not group:
            return []
        parts = re.split(r"\s*(>)\s*|\s+", group.strip())
        parts = [p for p in parts if p]
        steps, combinator = [], None
        for part in parts:
            if part == ">":
                combinator = ">"
                continue
            steps.append((combinator or " ", part))
            combinator = None
        if not steps:
            return []

        current = [self.root]
        for comb, simple in steps:
            nxt = []
            for base in current:
                candidates = ([c for c in base.children if not isinstance(c, str)]
                              if comb == ">" else list(base.iter()))
                for cand in candidates:
                    if cand is base:
                        continue
                    if self._match(cand, simple):
                        nxt.append(cand)
            current = nxt
            if not current:
                break
        return current

    @staticmethod
    def _match(node, simple):
        m = _SIMPLE_RE.match(simple)
        if not m:
            return False
        tag = m.group("tag")
        if tag and tag != "*" and node.tag != tag:
            return False
        rest = m.group("rest") or ""
        for token in re.findall(r"[.#][\w-]+|\[[^\]]+\]", rest):
            if token.startswith("."):
                if token[1:] not in node.classes:
                    return False
            elif token.startswith("#"):
                if node.attr("id") != token[1:]:
                    return False
            else:
                body = token[1:-1]
                if "=" in body:
                    k, v = body.split("=", 1)
                    if node.attr(k.strip()) != v.strip().strip("\"'"):
                        return False
                elif not node.attr(body.strip()):
                    return False
        return True


# ---------------------------------------------------------------- 提取

def html_to_text(html):
    return parse_html(html).text()


def _inline_md(node):
    """把一个节点的行内内容转成 Markdown 片段。"""
    parts = []
    for child in node.children:
        if isinstance(child, str):
            parts.append(re.sub(r"\s+", " ", child))
            continue
        tag = child.tag
        inner = _inline_md(child).strip()
        if tag in ("strong", "b") and inner:
            parts.append(f"**{inner}**")
        elif tag in ("em", "i") and inner:
            parts.append(f"*{inner}*")
        elif tag == "code" and inner:
            parts.append(f"`{inner}`")
        elif tag == "br":
            parts.append("\n")
        elif tag == "a":
            href = child.attr("href")
            parts.append(f"[{inner}]({href})" if href and inner else inner)
        elif tag == "img":
            src, alt = child.attr("src"), child.attr("alt")
            # 内联 data: URI 是 base64 占位图,写进 Markdown 只会变成一坨噪声
            if src and not src.startswith("data:"):
                parts.append(f"![{alt}]({src})")
        elif tag == "del" or tag == "s":
            parts.append(f"~~{inner}~~" if inner else "")
        else:
            parts.append(inner)
    return "".join(parts)


def _block_md(node, out, depth=0):
    for child in node.children:
        if isinstance(child, str):
            text = re.sub(r"\s+", " ", child).strip()
            if text:
                out.append(text)
            continue
        tag = child.tag
        if tag in HEADING_TAGS:
            level = HEADING_TAGS[tag]
            text = _inline_md(child).strip()
            if text:
                out.append(f"\n{'#' * level} {text}\n")
        elif tag == "p":
            text = _inline_md(child).strip()
            if text:
                out.append(f"\n{text}\n")
        elif tag == "pre":
            code = child.text(False).strip("\n")
            lang = ""
            code_node = child.find("code")
            if code_node:
                cls = " ".join(code_node.classes)
                m = re.search(r"(?:language|lang)-(\w+)", cls)
                lang = m.group(1) if m else ""
                code = code_node.text(False).strip("\n")
            if code.strip():
                out.append(f"\n```{lang}\n{code}\n```\n")
        elif tag == "blockquote":
            text = _inline_md(child).strip()
            if text:
                out.append("\n" + "\n".join(f"> {ln}" for ln in text.splitlines()) + "\n")
        elif tag in ("ul", "ol"):
            for i, li in enumerate([c for c in child.children
                                    if not isinstance(c, str) and c.tag == "li"], 1):
                marker = f"{i}." if tag == "ol" else "-"
                text = _inline_md(li).strip()
                if text:
                    out.append(f"{'  ' * depth}{marker} {text}")
            out.append("")
        elif tag == "table":
            rows, has_th = [], False
            for tr in child.find_all("tr"):
                cells = []
                for c in tr.children:
                    if isinstance(c, str) or c.tag not in ("td", "th"):
                        continue
                    if c.tag == "th":
                        has_th = True
                    cells.append(c)
                if cells:
                    rows.append(cells)
            widths = {len(r) for r in rows}
            # 只转真正的数据表:有表头且列数规整。老站点常拿 table 做页面布局
            # (HN 就是),硬转管道表格只会得到一堆对不齐的竖线
            if rows and has_th and len(widths) == 1 and len(rows) >= 2:
                width = widths.pop()
                text_rows = [[_inline_md(c).strip() for c in r] for r in rows]
                out.append("")
                out.append("| " + " | ".join(text_rows[0]) + " |")
                out.append("| " + " | ".join("---" for _ in range(width)) + " |")
                for r in text_rows[1:]:
                    out.append("| " + " | ".join(r) + " |")
                out.append("")
            else:
                _block_md(child, out, depth)
        elif tag == "img":
            # void 元素没有子节点,不会被 _inline_md 渲染出来,必须在这里接住
            src, alt = child.attr("src"), child.attr("alt")
            if src and not src.startswith("data:"):
                out.append(f"![{alt}]({src})")
        elif tag == "hr":
            out.append("\n---\n")
        elif tag in BLOCK_TAGS:
            _block_md(child, out, depth + (1 if tag in ("ul", "ol") else 0))
        else:
            # 容器里若还有块级元素就必须递归 —— 否则整棵子树会被 _inline_md
            # 拼成一行(HN 用 <center> 包表格,就栽在这里)
            has_block = any(
                not isinstance(c, str) and
                (c.tag in BLOCK_TAGS or c.tag in HEADING_TAGS
                 or c.tag in ("table", "pre", "blockquote", "ul", "ol", "hr", "img"))
                for c in child.children)
            if has_block:
                _block_md(child, out, depth)
            else:
                text = _inline_md(child).strip()
                if text:
                    out.append(text)


def html_to_markdown(html):
    """HTML → Markdown。喂给 LLM 或存档都比原始 HTML 省一个数量级。"""
    root = parse_html(html)
    out = []
    body = root.find("body") or root
    _block_md(body, out)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_meta(html):
    """标题与常见 meta。"""
    root = parse_html(html)
    meta = {}
    head = root.find("head") or root
    title = head.find("title") or root.find("title")
    if title:
        meta["title"] = title.text()
    html_node = root.find("html")
    if html_node and html_node.attr("lang"):
        meta["lang"] = html_node.attr("lang")
    for node in root.find_all("meta"):
        name = (node.attr("name") or node.attr("property") or "").lower()
        content = node.attr("content")
        if not content:
            continue
        if name in ("description", "og:description", "og:title", "og:type", "og:site_name",
                    "keywords", "author", "twitter:card", "twitter:title"):
            meta[name] = content[:500]
        elif name in ("og:image", "twitter:image"):
            meta.setdefault("images", [])
            if content not in meta["images"]:
                meta["images"].append(content)
    for node in root.find_all("link"):
        if (node.attr("rel") or "").lower() == "canonical":
            meta["canonical"] = node.attr("href")
        elif (node.attr("rel") or "").lower() == "alternate" and "rss" in (
                node.attr("type") or "").lower():
            meta.setdefault("feeds", []).append(node.attr("href"))
    return meta


def extract_links(html, base_url=""):
    """所有链接,带绝对化 href 与锚文本。"""
    root = parse_html(html)
    out, seen = [], set()
    for node in root.find_all("a"):
        href = node.attr("href").strip()
        if not href or href.startswith(("javascript:", "#", "mailto:", "tel:")):
            continue
        absolute = urljoin(base_url, href) if base_url else href
        if absolute in seen:
            continue
        seen.add(absolute)
        out.append({"href": absolute, "text": node.text()[:120],
                    "rel": node.attr("rel"), "external": bool(base_url) and
                    not absolute.startswith(base_url.split("//")[0] + "//" +
                                            base_url.split("//")[1].split("/")[0])})
    return out


def extract_tables(html):
    """表格转二维数组,第一行若全为 th 则视为表头。"""
    root = parse_html(html)
    tables = []
    for tbl in root.find_all("table"):
        rows = []
        for tr in tbl.find_all("tr"):
            cells = [c.text() for c in tr.children
                     if not isinstance(c, str) and c.tag in ("td", "th")]
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def apply_regex(text, pattern, group=None):
    r"""正则抽值。pattern 里有捕获组时默认取第 1 组 —— 写 (\d+) 的人要的是那个数字。"""
    out = []
    for m in re.finditer(pattern, text, re.S | re.M):
        idx = group if group is not None else (1 if m.groups() else 0)
        try:
            out.append(m.group(idx))
        except (IndexError, error):
            out.append(m.group(0))
    return out
