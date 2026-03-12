#!/usr/bin/env python3
"""digest - Compile a list of URLs into a formatted LaTeX/PDF document."""

import argparse
import io
import json
import math
import re
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import requests
import trafilatura
from bs4 import BeautifulSoup
from jinja2 import BaseLoader, Environment


# ---------------------------------------------------------------------------
# Tone detection
# ---------------------------------------------------------------------------

def parse_url_arg(arg: str) -> str:
    """Return the URL from a CLI argument, stripping any legacy [tone] tag."""
    m = re.match(r'^(.+?)\s+\[\w+\]\s*$', arg.strip())
    if m:
        print("  [NOTE] Tone tags are no longer used; ignoring.", file=sys.stderr)
        return m.group(1).strip()
    return arg.strip()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Article:
    title: str
    body_latex: str          # pre-rendered LaTeX (text + inline image commands)
    url: str = ""
    author_name: str = "Unknown"
    publication_name: str = ""
    author_bio: str = ""
    avatar_path: str = ""
    favicon_path: str = ""
    hero_path: str = ""      # local path to hero image, or ""
    published_date: str = ""
    word_count: int = 0
    hero_is_portrait: bool = False  # True if image height > width

    @property
    def reading_time(self) -> int:
        return max(1, math.ceil(self.word_count / 200))


# ---------------------------------------------------------------------------
# Image downloading
# ---------------------------------------------------------------------------

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0 Safari/537.36"
    )
}

# Formats pdflatex cannot read natively; we convert them to PNG via Pillow.
_CONVERT_TYPES = {"webp", "ico", "gif", "bmp", "tiff", "tif", "avif"}


def _download_content_image(url: str, dest_stem: Path, timeout: int = 15) -> str:
    """
    Download an image, converting to PNG/JPEG as needed.
    *dest_stem* is the path without extension; the actual extension is appended.
    Returns the final local path on success, '' on failure or skip.
    """
    if not url:
        return ""
    try:
        r = requests.get(url, headers=_HEADERS, timeout=timeout, stream=True)
        r.raise_for_status()
        data = r.content
        ct = r.headers.get("content-type", "").lower().split(";")[0].strip()
        url_lower = url.lower().split("?")[0]

        # Skip SVG — pdflatex cannot include it
        if "svg" in ct or url_lower.endswith(".svg"):
            return ""

        # Detect format
        url_ext = url_lower.rsplit(".", 1)[-1] if "." in url_lower else ""
        need_convert = (
            any(t in ct for t in _CONVERT_TYPES)
            or url_ext in _CONVERT_TYPES
        )

        if need_convert:
            try:
                from PIL import Image
                img = Image.open(io.BytesIO(data))
                buf = io.BytesIO()
                img.convert("RGBA" if img.mode in ("P", "RGBA") else "RGB").save(
                    buf, format="PNG"
                )
                data = buf.getvalue()
                dest = dest_stem.with_suffix(".png")
            except Exception:
                return ""
        elif "jpeg" in ct or "jpg" in ct or url_ext in ("jpg", "jpeg"):
            dest = dest_stem.with_suffix(".jpg")
        else:
            dest = dest_stem.with_suffix(".png")

        dest.write_bytes(data)
        return dest.as_posix()
    except Exception:
        return ""


def _get_image_is_portrait(path: str) -> bool:
    """Return True if the image is not clearly landscape (width < 1.5 × height).
    Covers portrait, square, and mild landscape images — all of which look
    better confined to a single column than stretched to full page width.
    """
    if not path:
        return False
    try:
        from PIL import Image
        with Image.open(path) as img:
            w, h = img.size
            return w < h * 1.5
    except Exception:
        return False


def _download_icon(url: str, dest: Path, timeout: int = 10) -> bool:
    """Download a small icon (avatar/favicon), converting ICO → PNG. Returns True on success."""
    if not url:
        return False
    try:
        r = requests.get(url, headers=_HEADERS, timeout=timeout)
        r.raise_for_status()
        ct = r.headers.get("content-type", "").lower()
        if "svg" in ct or url.lower().endswith(".svg"):
            return False
        data = r.content
        if "ico" in ct or url.lower().endswith(".ico"):
            try:
                from PIL import Image
                img = Image.open(io.BytesIO(data))
                buf = io.BytesIO()
                img.save(buf, format="PNG")
                data = buf.getvalue()
            except Exception:
                return False
        dest.write_bytes(data)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Per-article image set (deduplicates by normalized URL)
# ---------------------------------------------------------------------------

class ImageSet:
    """
    Manages content-image downloads for one article.
    Deduplication key: URL with query string and fragment stripped.
    """

    def __init__(self, img_dir: Path, prefix: str):
        self._img_dir = img_dir
        self._prefix = prefix
        self._seen: dict[str, str] = {}   # norm_url -> local_path_or_""
        self._count = 0

    @staticmethod
    def _norm(url: str) -> str:
        """Normalize URL for dedup: drop query string and fragment."""
        if not url:
            return ""
        p = urllib.parse.urlparse(url.strip())
        return urllib.parse.urlunparse((p.scheme, p.netloc, p.path, "", "", ""))

    def get(self, url: str) -> str:
        """
        Return local path for *url*, downloading if not already cached.
        Returns '' if the download fails or the URL is empty/unsupported.
        """
        norm = self._norm(url)
        if not norm:
            return ""
        if norm in self._seen:
            return self._seen[norm]
        dest_stem = self._img_dir / f"{self._prefix}_{self._count}"
        self._count += 1
        path = _download_content_image(url, dest_stem)
        self._seen[norm] = path
        return path

    def preload(self, url: str, dest_stem: Path) -> str:
        """
        Download *url* to a specific path stem (used for the hero image so
        it gets a predictable filename).  Registers the URL so that any
        subsequent get() call for the same URL returns this path instead of
        downloading again.
        """
        norm = self._norm(url)
        if not norm:
            return ""
        if norm in self._seen:
            return self._seen[norm]
        path = _download_content_image(url, dest_stem)
        self._seen[norm] = path
        return path


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

def _get_json_ld(soup: BeautifulSoup) -> dict:
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
            if isinstance(data, list):
                data = data[0]
            if isinstance(data, dict):
                return data
        except (json.JSONDecodeError, TypeError):
            pass
    return {}


def _og(soup: BeautifulSoup, prop: str) -> str:
    tag = (
        soup.find("meta", property=f"og:{prop}")
        or soup.find("meta", attrs={"name": f"og:{prop}"})
    )
    return tag["content"].strip() if tag and tag.get("content") else ""


def _meta(soup: BeautifulSoup, name: str) -> str:
    tag = soup.find("meta", attrs={"name": name})
    return tag["content"].strip() if tag and tag.get("content") else ""


def extract_metadata(url: str, soup: BeautifulSoup) -> dict:
    ld = _get_json_ld(soup)

    def ld_str(*keys):
        for k in keys:
            v = ld.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
            if isinstance(v, dict):
                n = v.get("name", "")
                if n:
                    return n.strip()
            if isinstance(v, list) and v:
                first = v[0]
                if isinstance(first, str):
                    return first.strip()
                if isinstance(first, dict):
                    n = first.get("name", "")
                    if n:
                        return n.strip()
        return ""

    title = (
        ld_str("headline", "name")
        or _og(soup, "title")
        or (soup.title.string.strip() if soup.title else "Untitled")
    )

    author_name = ""
    author_bio = ""
    author_avatar_url = ""
    author_raw = ld.get("author")
    if isinstance(author_raw, list) and author_raw:
        author_raw = author_raw[0]
    if isinstance(author_raw, dict):
        author_name = author_raw.get("name", "").strip()
        author_bio = author_raw.get("description", "").strip()
        author_avatar_url = author_raw.get("image", "")
        if isinstance(author_avatar_url, dict):
            author_avatar_url = author_avatar_url.get("url", "")
    elif isinstance(author_raw, str):
        author_name = author_raw.strip()

    if not author_name:
        author_name = (
            _meta(soup, "author")
            or _og(soup, "article:author")
            or "Unknown"
        )

    pub_name = ""
    pub_raw = ld.get("publisher") or ld.get("isPartOf")
    if isinstance(pub_raw, dict):
        pub_name = pub_raw.get("name", "").strip()
    if not pub_name:
        pub_name = _og(soup, "site_name") or _meta(soup, "application-name") or ""

    pub_date = (
        ld_str("datePublished")
        or _og(soup, "article:published_time")
        or _meta(soup, "date")
        or ""
    )
    if pub_date:
        try:
            dt = datetime.fromisoformat(pub_date.replace("Z", "+00:00"))
            pub_date = _fmt_date(dt)
        except (ValueError, AttributeError):
            pub_date = pub_date[:10]

    # og:image — hero image for the article
    og_image = _og(soup, "image")

    return {
        "title": title,
        "author_name": author_name,
        "author_bio": author_bio,
        "author_avatar_url": author_avatar_url,
        "pub_name": pub_name,
        "pub_date": pub_date,
        "favicon_url": _get_favicon_url(url, soup),
        "og_image": og_image,
    }


def _fmt_date(dt: datetime) -> str:
    return f"{dt.day} {dt.strftime('%B')} {dt.year}"


def _toc_url(url: str) -> str:
    """
    Return a LaTeX-safe URL string for display in the TOC.
    Strips the scheme (https://) and query/fragment; removes leading www.;
    inserts \\allowbreak{} after each slash so long paths can wrap.
    """
    if not url:
        return ""
    p = urllib.parse.urlparse(url)
    display = re.sub(r"^www\.", "", p.netloc) + p.path.rstrip("/")
    escaped = tex(display)
    return escaped.replace("/", "/\\allowbreak{}")


def _get_favicon_url(page_url: str, soup: BeautifulSoup) -> str:
    for rel in ("icon", "shortcut icon", "apple-touch-icon"):
        tag = soup.find(
            "link",
            rel=lambda r, _rel=rel: r and _rel in " ".join(r).lower() if r else False,
        )
        if tag and tag.get("href"):
            return urllib.parse.urljoin(page_url, tag["href"])
    parsed = urllib.parse.urlparse(page_url)
    return f"{parsed.scheme}://{parsed.netloc}/favicon.ico"


# ---------------------------------------------------------------------------
# LaTeX escaping
# ---------------------------------------------------------------------------

_LATEX_SPECIAL: dict[str, str] = {
    "\\": r"\textbackslash{}",
    "&":  r"\&",
    "%":  r"\%",
    "$":  r"\$",
    "#":  r"\#",
    "_":  r"\_",
    "{":  r"\{",
    "}":  r"\}",
    "~":  r"\textasciitilde{}",
    "^":  r"\^{}",
    "<":  r"\textless{}",
    ">":  r"\textgreater{}",
    "|":  r"\textbar{}",
    '"':  "''",
    "\u2019": "'",
    "\u2018": "`",
    "\u201c": "``",
    "\u201d": "''",
    "\u2010": "-",    # hyphen
    "\u2011": "-",    # non-breaking hyphen
    "\u2012": "--",   # figure dash
    "\u2013": "--",
    "\u2014": "---",
    "\u2015": "---",  # horizontal bar
    "\u2032": "'",    # prime
    "\u2033": "''",   # double prime
    "\u00a0": "~",
    "\u2026": r"\ldots{}",
}

_LATEX_RE = re.compile("|".join(re.escape(k) for k in _LATEX_SPECIAL))

# ---------------------------------------------------------------------------
# Unicode filtering: emoji stripping and non-T1 fallback font wrapping
# ---------------------------------------------------------------------------

# Emoji and pictographic symbols — strip entirely (pdflatex cannot render them)
_EMOJI_RE = re.compile(
    "["
    "\U0001F300-\U0001FFFF"   # Emoji, symbols, pictographs, transport, flags
    "\u2600-\u26FF"            # Miscellaneous symbols (☀ ☎ ♠ ★ …)
    "\u2700-\u27BF"            # Dingbats (✂ ✈ ✓ …)
    "\uFE0F"                   # Variation selector-16 (emoji presentation)
    "\u200D"                   # Zero-width joiner (emoji sequences)
    "\u20E3"                   # Combining enclosing keycap
    "]+",
    re.UNICODE,
)

# Scripts that pdflatex with T1+T2A fundamentally cannot render —
# no fallback font in this configuration covers them.
_STRIP_SCRIPTS_RE = re.compile(
    "["
    "\u0590-\u05FF"    # Hebrew
    "\u0600-\u06FF"    # Arabic
    "\u0700-\u08FF"    # Syriac, Thaana, NKo, …
    "\u0900-\u0DFF"    # Indic scripts (Devanagari, Bengali, Tamil, …)
    "\u0E00-\u0FFF"    # Thai, Lao, Tibetan
    "\u1000-\u109F"    # Myanmar
    "\u1100-\u11FF"    # Hangul Jamo
    "\u1200-\u137F"    # Ethiopic
    "\u2E80-\u2FFF"    # CJK radicals, Kangxi
    "\u3000-\u9FFF"    # CJK Unified Ideographs, Japanese kana, …
    "\uA000-\uA48F"    # Yi
    "\uAC00-\uD7AF"    # Hangul syllables
    "]+",
    re.UNICODE,
)


def _needs_fallback(cp: int) -> bool:
    r"""
    True if *cp* survived the strip filters but still cannot be rendered
    by the T1 tone fonts and needs \digestfallback{} (Noto Serif, T2A).
    """
    if cp <= 0x024F:              # Basic Latin → Latin Extended-B: T1 covers all
        return False
    if 0x0300 <= cp <= 0x036F:   # Combining diacritical marks: fine in T1
        return False
    if 0x1E00 <= cp <= 0x1EFF:   # Latin Extended Additional: T1 covers all
        return False
    return True                   # Everything else (Cyrillic, Greek, …) needs fallback


def _wrap_fallback(s: str) -> str:
    """
    Wrap runs of non-T1 characters in \\digestfallback{}.
    Called after LaTeX metachar escaping, so all ASCII-range LaTeX
    commands are already present and safe from modification.
    """
    result: list[str] = []
    run:    list[str] = []
    for ch in s:
        cp = ord(ch)
        if cp > 0x7F and _needs_fallback(cp):
            run.append(ch)
        else:
            if run:
                result.append(r"\digestfallback{" + "".join(run) + "}")
                run.clear()
            result.append(ch)
    if run:
        result.append(r"\digestfallback{" + "".join(run) + "}")
    return "".join(result)


def tex(s: str) -> str:
    if not s:
        return ""
    # 1. Strip emoji (pdflatex cannot render colour/pictographic glyphs)
    s = _EMOJI_RE.sub("", s)
    # 2. Strip scripts that have no pdflatex support in this configuration
    s = _STRIP_SCRIPTS_RE.sub("", s)
    # 3. Escape LaTeX metacharacters
    s = _LATEX_RE.sub(lambda m: _LATEX_SPECIAL[m.group()], s)
    # 4. Wrap surviving non-T1 chars (Cyrillic, Greek, …) in fallback font
    return _wrap_fallback(s)


# ---------------------------------------------------------------------------
# Body extraction: trafilatura XML → LaTeX with inline images
# ---------------------------------------------------------------------------

def _inline_figure_latex(path: str, is_landscape: bool = False) -> str:
    """
    Inline body image.  Landscape images use figure*[t] to span both columns
    as a float anchored to the page top — safer than the cuted strip
    environment, which can cause images to overlap preceding text.
    Narrow images stay within a single column.
    """
    if is_landscape:
        return (
            "\\begin{figure*}[t]\n"
            "  \\centering\n"
            f"  \\includegraphics[width=\\textwidth,height=0.4\\textheight,keepaspectratio]{{{path}}}\n"
            "\\end{figure*}"
        )
    return (
        "\\begin{figure}[htbp]\n"
        "  \\centering\n"
        f"  \\includegraphics[width=\\columnwidth,height=5cm,keepaspectratio]{{{path}}}\n"
        "\\end{figure}"
    )


def _visit_xml_elem(
    elem: ET.Element,
    img_set: ImageSet,
    out: list[str],
    word_count: list[int],
) -> None:
    """Recursively convert a trafilatura XML element to LaTeX."""
    # Strip any namespace prefix (e.g. {http://...}p → p)
    tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag

    if tag in ("p", "ab"):
        text = "".join(elem.itertext()).strip()
        if text:
            word_count[0] += len(text.split())
            out.append(tex(text))
            out.append("")

    elif tag == "graphic":
        src = (elem.get("src") or "").strip()
        path = img_set.get(src)
        if path:
            is_landscape = not _get_image_is_portrait(path)
            out.append(_inline_figure_latex(path, is_landscape))
            out.append("")
        # No fallback emitted — failed images are silently skipped

    elif tag == "head":
        text = "".join(elem.itertext()).strip()
        if text:
            word_count[0] += len(text.split())
            out.append(r"\medskip\noindent{\bfseries " + tex(text) + r"}\par\smallskip")
            out.append("")

    elif tag == "list":
        items_tex = []
        for child in elem:
            child_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if child_tag == "item":
                text = "".join(child.itertext()).strip()
                if text:
                    word_count[0] += len(text.split())
                    items_tex.append(r"  \item " + tex(text))
        if items_tex:
            out.append(r"\begin{itemize}")
            out.extend(items_tex)
            out.append(r"\end{itemize}")
            out.append("")

    elif tag == "quote":
        text = "".join(elem.itertext()).strip()
        if text:
            word_count[0] += len(text.split())
            out.append(r"\begin{quote}")
            out.append(tex(text))
            out.append(r"\end{quote}")
            out.append("")

    else:
        # Unknown structural element: recurse into children
        for child in elem:
            _visit_xml_elem(child, img_set, out, word_count)


def extract_body(html: str, img_set: ImageSet) -> tuple[str, int]:
    """
    Extract article body as LaTeX, embedding inline images.
    Tries trafilatura XML output first; falls back to plain text.
    Returns (latex_str, word_count).
    """
    xml_str = trafilatura.extract(
        html,
        include_images=True,
        include_comments=False,
        include_tables=False,
        output_format="xml",
    )

    if xml_str:
        try:
            root = ET.fromstring(xml_str)
            main = root.find(".//main") or root
            out: list[str] = []
            wc: list[int] = [0]
            for child in main:
                _visit_xml_elem(child, img_set, out, wc)
            body = "\n".join(out).strip()
            if body:
                return body, wc[0]
        except ET.ParseError:
            pass

    # Fallback: plain text
    text = (
        trafilatura.extract(html, include_comments=False, include_tables=False)
        or ""
    )
    return _plain_body_to_latex(text), len(text.split())


def _plain_body_to_latex(text: str) -> str:
    """Convert plain extracted text to LaTeX paragraphs (fallback path)."""
    lines = text.splitlines()
    out: list[str] = []
    para: list[str] = []

    def flush():
        if para:
            out.append(tex(" ".join(para)))
            out.append("")
            para.clear()

    for line in lines:
        stripped = line.strip()
        if stripped:
            para.append(stripped)
        else:
            flush()
    flush()
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Drop cap
# ---------------------------------------------------------------------------

def _apply_dropcap(body_latex: str) -> str:
    """
    Wrap the first letter of the article body in \\digestdropcap{LETTER}{REST}.
    Skipped if the body opens with a LaTeX command (e.g. a figure or heading),
    since we can't reliably extract the initial letter in those cases.
    """
    stripped = body_latex.lstrip()
    # If the body begins with a LaTeX environment or command, leave it alone
    if stripped.startswith("\\"):
        return body_latex
    # Match: optional leading whitespace, first letter, rest of first word
    m = re.match(
        r"(\s*)([A-Za-z\u00C0-\u04FF])([\w\u00C0-\u04FF]*)(.*)",
        stripped,
        re.DOTALL | re.UNICODE,
    )
    if not m:
        return body_latex
    ws, first_letter, word_tail, remainder = m.groups()
    prefix = body_latex[: len(body_latex) - len(stripped)]
    return (
        f"{prefix}{ws}"
        f"\\digestdropcap{{{first_letter}}}{{{word_tail}}}"
        f"{remainder}"
    )


# ---------------------------------------------------------------------------
# Jinja2 LaTeX template
# Delimiters: (( )) for variables, (% %) for blocks
#
# Layout strategy
# ===============
# Each article uses \twocolumn[{HEADER}] to produce a full-width one-column
# header (title block + hero image) before the two-column body.  This is the
# only reliable mechanism in standard pdflatex for placing a spanning element
# *immediately* after a specific inline position — figure* is a float and will
# drift to the top of whatever page LaTeX chooses, which may precede the title
# block.  The \twocolumn[...] argument is typeset at the top of a fresh page
# spanning the full text width, followed immediately by the two-column body.
#
# Inline images from the body use figure[h!] (single-column float).
# ---------------------------------------------------------------------------

LATEX_TEMPLATE = r"""
\documentclass[twocolumn,a5paper,10pt]{article}

\usepackage[a5paper, top=15mm, bottom=15mm, left=12mm, right=12mm]{geometry}
\usepackage{iftex}

%% =======================================================================
%% Font setup — EB Garamond throughout
%% =======================================================================
\ifPDFTeX
%% --- pdflatex -----------------------------------------------------------
  \usepackage[T2A,T1]{fontenc}
  \usepackage[utf8]{inputenc}
  \usepackage{ebgaramond}
  %% Unicode fallback: Noto Serif in T2A encoding for Cyrillic, etc.
  \usepackage{noto-serif}
  \newcommand{\digestfallback}[1]{%
    {\fontencoding{T2A}\fontfamily{NotoSerif-TLF}\selectfont #1}%
  }
  %% Drop caps — Goudy Initials (Type-1, pdflatex-only).
  %% Guard with A–Z range: Goudy In covers only uppercase Latin A–Z (65–90).
  \usepackage{lettrine}
  \usepackage{GoudyIn}
  \newcommand{\digestdropcap}[2]{%
    \begingroup
    \ifnum`#1>64\ifnum`#1<91
      \renewcommand{\LettrineFontHook}{\GoudyInfamily}%
    \fi\fi
    \lettrine[lines=2,lraise=0.05,nindent=0em]{#1}{#2}%
    \endgroup
  }
\else
%% --- XeLaTeX / LuaLaTeX ------------------------------------------------
  \usepackage{fontspec}
  \IfFontExistsTF{EB Garamond}{%
    \setmainfont{EB Garamond}[Ligatures=TeX]%
  }{}
  \setsansfont{Latin Modern Sans}[Ligatures=TeX]
  \setmonofont{Latin Modern Mono}
  %% Unicode fallback: Noto Serif for Cyrillic, Greek, etc.
  \IfFontExistsTF{Noto Serif}{%
    \newfontfamily\digestfallbackfont{Noto Serif}[Scale=MatchLowercase]%
    \newcommand{\digestfallback}[1]{{\digestfallbackfont #1}}%
  }{\newcommand{\digestfallback}[1]{#1}}
  %% Drop caps — EBGaramond-Initials.otf is an initials-only font and
  %% intentionally lacks a space glyph; \tracinglostchars=0 suppresses the
  %% harmless "no U+0020" warning within the drop-cap group only.
  \usepackage{lettrine}
  \IfFontExistsTF{EBGaramond-Initials.otf}{%
    \newfontfamily\DigestInitialFont{EBGaramond-Initials.otf}%
    \newcommand{\digestdropcap}[2]{%
      \begingroup
      \iffontchar\DigestInitialFont`#1
        \renewcommand{\LettrineFontHook}{\DigestInitialFont}%
        \tracinglostchars=0 %
      \fi
      \lettrine[lines=2,lraise=0.05,nindent=0em]{#1}{#2}%
      \endgroup
    }%
  }{%
    \newcommand{\digestdropcap}[2]{%
      \lettrine[lines=2,lraise=0.05,nindent=0em]{#1}{#2}%
    }%
  }
\fi
%% =======================================================================

\usepackage{microtype}
\usepackage{graphicx}
\usepackage{hyperref}
\usepackage{parskip}
\usepackage{xcolor}
\usepackage{float}
\usepackage{fancyhdr}

\hypersetup{
  colorlinks=true,
  linkcolor=black,
  urlcolor=black,
  pdftitle={Digest},
}

%% -----------------------------------------------------------------------
%% \articleblock{title}{author}{pub}{bio}{date}{readtime}{avatar}{favicon}
%% Always runs in Latin Modern via \normalfont.
%% -----------------------------------------------------------------------
\newcommand{\articleblock}[8]{%
  \begingroup
  \normalfont
  \setlength{\parindent}{0pt}%
  {\large\bfseries #1\par}%
  \smallskip
  \ifx&#7&\else
    \raisebox{-0.5ex}{\includegraphics[height=1.4em]{#7}}\,%
  \fi
  {\small\itshape #2}%
  \ifx&#3&\else
    {\small\ ---\ #3}%
  \fi
  \par
  \ifx&#4&\else
    {\footnotesize #4\par}%
  \fi
  \smallskip
  \ifx&#8&\else
    \raisebox{-0.3ex}{\includegraphics[height=0.9em]{#8}}\,%
  \fi
  {\footnotesize #5\quad $\cdot$\quad #6\ min\ read}%
  \par
  \medskip
  \hrule
  \medskip
  \endgroup
}

\setcounter{tocdepth}{1}

%% -----------------------------------------------------------------------
%% Page style: right-aligned footer with current author name + page number.
%% \markright is set at the start of each article so the footer reflects
%% whoever is on that page.
%% -----------------------------------------------------------------------
\pagestyle{fancy}
\fancyhf{}
\fancyfoot[R]{%
  \small\normalfont\itshape\rightmark\upshape
  \ifx\rightmark\empty\else\enspace---\enspace\fi
  \thepage}
\renewcommand{\headrulewidth}{0pt}
\renewcommand{\footrulewidth}{0pt}

%% TOC URL sub-entry: small gray line below each section title.
%% Written via \addtocontents (not \addcontentsline) so hyperref does not
%% treat it as a PDF bookmark.  The URL is pre-processed in Python:
%% scheme and query stripped, \allowbreak{} inserted after each slash.
\newcommand{\tocurl}[1]{%
  \vspace{-4pt}%
  {\small\normalfont\color{gray}\hspace{1.5em}#1\par}%
  \vspace{3pt}%
}

%% End-of-article motif (three centred asterisks).
%% \nopagebreak keeps the motif attached to the last line of the article
%% body so it cannot float onto an otherwise-blank following page.
\newcommand{\digestmotif}{%
  \par\nopagebreak\medskip
  \nopagebreak{\centering\normalfont\small$*\quad*\quad*$\par}%
  \medskip
}

\begin{document}

\onecolumn
\thispagestyle{empty}
{\normalfont\huge\bfseries Digest\par}
\smallskip
{\normalfont\large (( date ))\par}
\bigskip
\tableofcontents
\twocolumn

(% for article in articles %)
(% if article.hero_is_portrait or not article.hero_path %)
%% ── Inline article: narrow/square/no hero ────────────────────────────────
%% Header flows in-column after the previous article (no new page).
%% The first article gets a little extra breathing room below the TOC.
(% if loop.first %)
\bigskip
(% endif %)
\phantomsection
\addcontentsline{toc}{section}{\texorpdfstring{%
  (( article.title_tex )){\normalfont\itshape{} --- (( article.author_tex ))}%
}{%
  (( article.title_tex )) -- (( article.author_tex ))%
}}%
\addtocontents{toc}{\protect\tocurl{(( article.url_toc ))}}%
\markright{(( article.author_tex ))}%
{\setlength{\parskip}{0pt}%
\articleblock%
  {(( article.title_tex ))}%
  {(( article.author_tex ))}%
  {(( article.pub_tex ))}%
  {(( article.bio_tex ))}%
  {(( article.date_tex ))}%
  {(( article.reading_time ))}%
  {(( article.avatar_path ))}%
  {(( article.favicon_path ))}%
}%
%% Portrait/square hero leads the left column; body text follows.
(% if article.hero_path %)
\noindent\includegraphics%
  [width=\columnwidth,height=0.4\textheight,keepaspectratio]%
  {(( article.hero_path ))}%
\par\vspace{6pt}%
(% endif %)
(( article.body_latex ))
(% else %)
%% ── Landscape article: new page + full-width header ─────────────────────
%% \twocolumn[{...}] flushes to a new page and typesets the header at full
%% page width; body text wraps in two columns immediately below.
\twocolumn[{%
  \setlength{\parskip}{0pt}%
  \articleblock%
    {(( article.title_tex ))}%
    {(( article.author_tex ))}%
    {(( article.pub_tex ))}%
    {(( article.bio_tex ))}%
    {(( article.date_tex ))}%
    {(( article.reading_time ))}%
    {(( article.avatar_path ))}%
    {(( article.favicon_path ))}%
  \noindent\includegraphics%
    [width=\linewidth,height=0.5\textheight,keepaspectratio]%
    {(( article.hero_path ))}%
  \par\vspace{6pt}%
  \vspace{4pt}%
}]%
%% TOC entry in the body (same page as the header) — reliable \write timing.
\phantomsection
\addcontentsline{toc}{section}{\texorpdfstring{%
  (( article.title_tex )){\normalfont\itshape{} --- (( article.author_tex ))}%
}{%
  (( article.title_tex )) -- (( article.author_tex ))%
}}%
\addtocontents{toc}{\protect\tocurl{(( article.url_toc ))}}%
\markright{(( article.author_tex ))}%
(( article.body_latex ))
(% endif %)
%% End-of-article motif
\digestmotif
(% endfor %)

\end{document}
""".strip()


# ---------------------------------------------------------------------------
# Scraping pipeline
# ---------------------------------------------------------------------------

def scrape(
    url: str,
    img_dir: Path,
    idx: int,
) -> Optional[Article]:
    print(f"  Fetching {url} ...", flush=True)
    try:
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            print(f"  [WARN] Could not fetch {url}", file=sys.stderr)
            return None

        soup = BeautifulSoup(downloaded, "html.parser")
        meta = extract_metadata(url, soup)

        # Image set for this article — all content images share it for dedup
        img_set = ImageSet(img_dir, f"art{idx}")

        # Hero image: preload into img_set so the same URL in the body is deduped
        hero_path = ""
        hero_is_portrait = False
        if meta["og_image"]:
            print("    downloading hero image...", flush=True)
            hero_path = img_set.preload(
                meta["og_image"],
                img_dir / f"hero_{idx}",
            )
            if hero_path:
                hero_is_portrait = _get_image_is_portrait(hero_path)
            else:
                print("    [WARN] hero image download failed", file=sys.stderr)

        # Avatar and favicon (small UI images — separate from content ImageSet)
        avatar_path = ""
        avatar_dest = img_dir / f"avatar_{idx}.png"
        if _download_icon(meta["author_avatar_url"], avatar_dest):
            avatar_path = avatar_dest.as_posix()

        favicon_path = ""
        favicon_dest = img_dir / f"favicon_{idx}.png"
        if _download_icon(meta["favicon_url"], favicon_dest):
            favicon_path = favicon_dest.as_posix()

        # Body: structured XML extraction with inline images
        print("    extracting body...", flush=True)
        body_latex, word_count = extract_body(downloaded, img_set)

        inline_count = img_set._count - (1 if hero_path else 0)
        if inline_count > 0:
            print(f"    embedded {inline_count} inline image(s)", flush=True)

        if not body_latex.strip():
            print(f"  [WARN] No text extracted from {url}", file=sys.stderr)
            return None

        return Article(
            title=meta["title"],
            body_latex=body_latex,
            url=url,
            author_name=meta["author_name"],
            publication_name=meta["pub_name"],
            author_bio=meta["author_bio"],
            avatar_path=avatar_path,
            favicon_path=favicon_path,
            hero_path=hero_path,
            published_date=meta["pub_date"],
            word_count=word_count,
            hero_is_portrait=hero_is_portrait,
        )
    except Exception as e:
        print(f"  [ERROR] {url}: {e}", file=sys.stderr)
        return None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_latex(articles: list[Article], today: str) -> str:
    env = Environment(
        loader=BaseLoader(),
        autoescape=False,
        block_start_string="(%",
        block_end_string="%)",
        variable_start_string="((",
        variable_end_string="))",
        comment_start_string="(#",
        comment_end_string="#)",
    )
    tmpl = env.from_string(LATEX_TEMPLATE)

    article_ctx = []
    for a in articles:
        article_ctx.append({
            "title_tex":    tex(a.title),
            "author_tex":   tex(a.author_name),
            "pub_tex":      tex(a.publication_name),
            "bio_tex":      tex(a.author_bio[:120]),
            "date_tex":     tex(a.published_date),
            "reading_time": a.reading_time,
            "avatar_path":  a.avatar_path,
            "favicon_path": a.favicon_path,
            "hero_path":       a.hero_path,
            "hero_is_portrait": a.hero_is_portrait,
            "body_latex":      _apply_dropcap(a.body_latex),
            "url_toc":         _toc_url(a.url),
        })

    return tmpl.render(articles=article_ctx, date=today)


# ---------------------------------------------------------------------------
# pdflatex
# ---------------------------------------------------------------------------

def _run_latex(tex_path: Path, engine: str = "xelatex"):
    result = subprocess.run(
        [engine, "-interaction=nonstopmode", tex_path.name],
        capture_output=True,
        text=True,
        cwd=tex_path.parent.resolve(),
    )
    if result.returncode != 0:
        log = result.stdout[-3000:] or result.stderr[-3000:]
        print(f"[{engine} output tail]\n" + log, file=sys.stderr)


# ---------------------------------------------------------------------------
# Interactive metadata confirmation
# ---------------------------------------------------------------------------

def _confirm_articles(articles: list[Article]) -> list[Article]:
    """
    Print the scraped metadata and let the user edit titles, authors, and
    publication names before rendering.  Articles with an unknown author are
    flagged.  Called only when stdin is a TTY; silently skipped otherwise.
    """
    UNKNOWN = {"Unknown", "unknown", ""}

    def _print_list() -> None:
        print()
        for i, a in enumerate(articles):
            flag = " (!)" if a.author_name in UNKNOWN else "    "
            title = a.title[:60] + "\u2026" if len(a.title) > 60 else a.title
            pub   = f"  \u2014  {a.publication_name}" if a.publication_name else ""
            print(f"  {i + 1:>2}.{flag}{title}")
            print(f"        {a.author_name}{pub}")
        print()

    while True:
        _print_list()

        n_unknown = sum(1 for a in articles if a.author_name in UNKNOWN)
        if n_unknown:
            print(
                f"  (!) {n_unknown} article(s) have an unknown author — "
                "enter the article number to edit."
            )

        print("Enter article number to edit, or press Enter to proceed: ", end="", flush=True)
        try:
            line = input().strip()
        except EOFError:
            break

        if not line:
            break

        try:
            idx = int(line) - 1
        except ValueError:
            print("  Please enter a number.\n")
            continue

        if not (0 <= idx < len(articles)):
            print(f"  Please enter a number between 1 and {len(articles)}.\n")
            continue

        a = articles[idx]
        print(f"\n  Editing article {idx + 1} — press Enter to keep the current value.")

        v = input(f"  Title       [{a.title}]: ").strip()
        if v:
            a.title = v

        v = input(f"  Author      [{a.author_name}]: ").strip()
        if v:
            a.author_name = v

        v = input(f"  Publication [{a.publication_name}]: ").strip()
        if v:
            a.publication_name = v

        print()

    return articles


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Compile a list of URLs into a formatted LaTeX/PDF digest.",
    )
    parser.add_argument("urls", nargs="+", metavar="URL", help="URLs to include")
    parser.add_argument(
        "-o", "--output", default="digest",
        help="Output base name (default: digest)",
    )
    parser.add_argument(
        "--no-pdf", action="store_true", help="Skip running the LaTeX engine"
    )
    parser.add_argument(
        "--engine", default="xelatex",
        metavar="ENGINE",
        help="LaTeX engine to use (default: xelatex). Use 'pdflatex' for pdflatex.",
    )
    parser.add_argument(
        "-y", "--yes", action="store_true",
        help="Skip the interactive metadata confirmation step.",
    )
    args = parser.parse_args()

    today = _fmt_date(datetime.today())
    output_base = Path(args.output).resolve()
    tex_path = output_base.with_suffix(".tex")
    img_dir = output_base.parent / (output_base.stem + "_images")
    img_dir.mkdir(exist_ok=True)

    print("Scraping articles...")
    articles: list[Article] = []
    for i, raw_arg in enumerate(args.urls):
        url = parse_url_arg(raw_arg)
        a = scrape(url, img_dir, i)
        if a:
            articles.append(a)

    if not articles:
        print("No articles could be scraped. Aborting.", file=sys.stderr)
        sys.exit(1)

    if not args.yes and sys.stdin.isatty():
        articles = _confirm_articles(articles)

    print(f"\nRendering LaTeX for {len(articles)} article(s)...")
    latex_src = render_latex(articles, today)
    tex_path.write_text(latex_src, encoding="utf-8")
    print(f"  Written: {tex_path}")

    if not args.no_pdf:
        engine = args.engine
        print(f"\nRunning {engine} (pass 1)...")
        _run_latex(tex_path, engine)
        print(f"Running {engine} (pass 2, for TOC)...")
        _run_latex(tex_path, engine)
        pdf_path = output_base.with_suffix(".pdf")
        if pdf_path.exists():
            print(f"\nDone!  PDF: {pdf_path}")
        else:
            print(
                "\n[WARN] pdflatex did not produce a PDF — "
                "check the .log file for errors.",
                file=sys.stderr,
            )
    else:
        print("\nDone! (PDF compilation skipped)")


if __name__ == "__main__":
    main()
