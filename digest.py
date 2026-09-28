#!/usr/bin/env python3
"""digest - Compile a list of URLs into a formatted EPUB/PDF digest."""

import argparse
import email.utils
import hashlib
import io
import json
import math
import re
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime
from html import escape as _html_escape
from pathlib import Path
from typing import Optional
from xml.etree import ElementTree as ET

import requests
import trafilatura
from bs4 import BeautifulSoup
from jinja2 import BaseLoader, Environment


# ---------------------------------------------------------------------------
# CLI URL arguments
# ---------------------------------------------------------------------------

def parse_url_arg(arg: str) -> str:
    """Return the URL from a CLI argument, stripping any trailing [tag]."""
    m = re.match(r"^(.+?)\s+\[(\w[\w-]*)\]\s*$", arg.strip())
    if m:
        print("  [NOTE] [tags] are not supported; ignoring.", file=sys.stderr)
        return m.group(1).strip()
    return arg.strip()


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Article:
    title: str
    body_latex: str          # pre-rendered LaTeX (text + inline image commands)
    body_html: str = ""      # XHTML fragment for EPUB chapters
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

# Images smaller than this (in either dimension) are skipped: they are
# almost always tracking pixels, lazy-load placeholders, or UI icons that
# would otherwise surface as stray floating figures in the output.
_MIN_IMAGE_DIM = 60


def _file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _image_dimensions(path: str) -> tuple[int, int]:
    """Return (width, height), or (0, 0) when the image cannot be read."""
    if not path:
        return (0, 0)
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.size
    except Exception:
        return (0, 0)


class ImageSet:
    """
    Manages content-image downloads for one article.

    Dedup works on two levels:
    * by normalized URL (query/fragment stripped, scheme and www unified),
      so resized/retargeted variants of the same file download once;
    * by content hash, so visually identical images served from different
      URLs share one file.
    """

    def __init__(self, img_dir: Path, prefix: str):
        self._img_dir = img_dir
        self._prefix = prefix
        self._seen: dict[str, str] = {}    # norm_url -> local_path_or_""
        self._hashes: dict[str, str] = {}  # sha256 -> local_path
        self._count = 0
        self.hero_path = ""                # set by preload(); body figures
        self.emitted: set[str] = set()     # body-image paths already used

    @staticmethod
    def _norm(url: str) -> str:
        """Normalize URL for dedup: lowercase host, drop scheme, www,
        query string, and fragment."""
        if not url:
            return ""
        p = urllib.parse.urlparse(url.strip())
        netloc = p.netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return urllib.parse.urlunparse(("", netloc, p.path, "", "", ""))

    def _register(self, norm: str, path: str) -> str:
        """
        Record a fresh download: drop files that are too small to be real
        content, fold content-duplicates onto the first file kept.
        Returns the path to use (possibly an earlier duplicate's).
        """
        if not path:
            self._seen[norm] = ""
            return ""
        w, h = _image_dimensions(path)
        if w < _MIN_IMAGE_DIM or h < _MIN_IMAGE_DIM:
            Path(path).unlink(missing_ok=True)
            self._seen[norm] = ""
            return ""
        digest = _file_hash(Path(path))
        if digest in self._hashes and self._hashes[digest] != path:
            Path(path).unlink(missing_ok=True)
            path = self._hashes[digest]
        else:
            self._hashes.setdefault(digest, path)
            self._count += 1
        self._seen[norm] = path
        return path

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
        path = _download_content_image(url, dest_stem)
        return self._register(norm, path)

    def preload(self, url: str, dest_stem: Path) -> str:
        """
        Download *url* to a specific path stem (used for the hero image so
        it gets a predictable filename).  Registers the URL so that any
        subsequent get() call for the same URL returns this path instead of
        downloading again, and records it as the hero path so the body
        extractor can skip inline repeats of the hero image.
        """
        norm = self._norm(url)
        if not norm:
            return ""
        if norm in self._seen:
            path = self._seen[norm]
        else:
            # The hero file uses its own hero_<idx> stem, so it must not
            # consume numbering from the art<idx>_<n> inline sequence.
            saved_count = self._count
            path = self._register(norm, _download_content_image(url, dest_stem))
            self._count = saved_count
        if path:
            self.hero_path = path
        return path

    def claim(self, path: str) -> bool:
        """
        Mark a body-image *path* as used.  Returns False when the path is
        the hero image or was already emitted — i.e. the caller should skip
        this figure instead of embedding a duplicate.
        """
        if not path or path == self.hero_path or path in self.emitted:
            return False
        self.emitted.add(path)
        return True


# ---------------------------------------------------------------------------
# Metadata extraction
# ---------------------------------------------------------------------------

# Schema.org types that carry article metadata (lowercased for comparison).
_ARTICLE_LD_TYPES = {
    "article", "newsarticle", "blogposting", "blog", "liveblogposting",
    "reportagenewsarticle", "analysisnewsarticle", "backgroundnewsarticle",
    "opinionnewsarticle", "reviewnewsarticle", "investigativearticle",
    "satiricalarticle", "scholarlyarticle", "medicalscholarlyarticle",
    "socialmediaposting", "discussionforumposting",
}


def _iter_json_ld(soup: BeautifulSoup):
    """
    Yield every candidate metadata object from all ld+json blocks,
    expanding top-level lists and @graph arrays.  Objects without any
    type or content hints (bare @context wrappers, etc.) are skipped.
    """
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except (json.JSONDecodeError, TypeError):
            continue
        stack = [data]
        while stack:
            node = stack.pop()
            if isinstance(node, list):
                stack.extend(node)
            elif isinstance(node, dict):
                graph = node.get("@graph")
                if isinstance(graph, list):
                    stack.extend(graph)
                if (
                    "@type" in node
                    or "headline" in node
                    or "author" in node
                    or "datePublished" in node
                ):
                    yield node


def _pick_json_ld(soup: BeautifulSoup) -> dict:
    """
    Return the most article-like JSON-LD object: the first whose @type is
    an article type, else the first candidate, else {}.
    (Previously only the first script block was read, so pages listing a
    WebSite or Organization block first lost all article metadata.)
    """
    first: dict = {}
    for obj in _iter_json_ld(soup):
        if not first:
            first = obj
        types = obj.get("@type", [])
        if isinstance(types, str):
            types = [types]
        if any(str(t).split("/")[-1].lower() in _ARTICLE_LD_TYPES for t in types):
            return obj
    return first


def _ld_str(obj: dict, *keys) -> str:
    """First non-empty string value for *keys* (str, {name: ...}, or [first])."""
    for k in keys:
        v = obj.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            n = str(v.get("name", "")).strip()
            if n:
                return n
        if isinstance(v, list) and v:
            first = v[0]
            if isinstance(first, str) and first.strip():
                return first.strip()
            if isinstance(first, dict):
                n = str(first.get("name", "")).strip()
                if n:
                    return n
    return ""


def _ld_author(raw) -> tuple[str, str, str]:
    """
    Return (names, bio, avatar_url) from a JSON-LD author/creator value.
    Multiple authors are joined with ", "; bio/avatar come from the first
    entry that provides them.
    """
    items = raw if isinstance(raw, list) else [raw]
    names: list[str] = []
    bio = ""
    avatar = ""
    for it in items:
        if isinstance(it, dict):
            n = str(it.get("name", "")).strip()
            if n:
                names.append(n)
            if not bio:
                bio = str(it.get("description", "")).strip()
            if not avatar:
                im = it.get("image", "")
                if isinstance(im, dict):
                    im = im.get("url", "")
                if isinstance(im, list) and im:
                    im = im[0]
                    if isinstance(im, dict):
                        im = im.get("url", "")
                if isinstance(im, str) and im.strip():
                    avatar = im.strip()
        elif isinstance(it, str) and it.strip():
            names.append(it.strip())
    return ", ".join(names), bio, avatar


def _meta_values(soup: BeautifulSoup, *keys: str) -> str:
    """First non-empty content of <meta name=...> or <meta property=...>."""
    for k in keys:
        tag = soup.find("meta", attrs={"name": k}) or soup.find("meta", property=k)
        if tag and tag.get("content", "").strip():
            return tag["content"].strip()
    return ""


def _og(soup: BeautifulSoup, prop: str) -> str:
    """Open Graph value, also accepting the unprefixed property/name form
    (many sites emit property="article:published_time", not "og:...")."""
    return _meta_values(soup, f"og:{prop}", prop)


def _meta(soup: BeautifulSoup, name: str) -> str:
    tag = soup.find("meta", attrs={"name": name})
    return tag["content"].strip() if tag and tag.get("content") else ""


def _strip_site_suffix(title: str, *site_names: str) -> str:
    """
    Remove a trailing " - SiteName" / " | SiteName" style suffix when it
    matches a known site/publication name (case-insensitive).
    """
    for site in site_names:
        site = (site or "").strip()
        if not site or len(site) > len(title):
            continue
        for sep in (" - ", " | ", " – ", " — ", " :: ", ": ", " – ", " — "):
            suffix = sep + site
            if title.lower().endswith(suffix.lower()):
                stem = title[: -len(suffix)].strip()
                if stem:
                    return stem
    return title


def _time_tag_date(soup: BeautifulSoup) -> str:
    """datetime attribute of the first <time> element that has one."""
    tag = soup.find("time", attrs={"datetime": True})
    if tag and tag.get("datetime", "").strip():
        return tag["datetime"].strip()
    itemprop = soup.find(attrs={"itemprop": "datePublished"})
    if itemprop is not None:
        for attr in ("content", "datetime"):
            if itemprop.get(attr, "").strip():
                return itemprop.get(attr).strip()
        if itemprop.get_text(strip=True):
            return itemprop.get_text(strip=True)
    return ""


def _parse_date(raw: str) -> str:
    """
    Normalize a raw date string to "D Month YYYY".
    Tries ISO 8601, RFC 2822, then a bare YYYY-MM-DD; returns the raw
    string (trimmed) when nothing parses.
    """
    s = (raw or "").strip()
    if not s:
        return ""
    try:
        return _fmt_date(datetime.fromisoformat(s.replace("Z", "+00:00")))
    except ValueError:
        pass
    try:
        return _fmt_date(email.utils.parsedate_to_datetime(s))
    except (ValueError, TypeError):
        pass
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        try:
            return _fmt_date(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass
    return s[:30]


def _clean_author(name: str) -> str:
    """Strip a leading "By " and collapse whitespace."""
    name = re.sub(r"\s+", " ", (name or "").strip())
    name = re.sub(r"^(by|von|par)\s+", "", name, flags=re.IGNORECASE)
    return name.strip(" ,")


def extract_metadata(
    url: str,
    soup: BeautifulSoup,
    trafi: Optional[dict] = None,
) -> dict:
    """
    Extract title/author/publication/date/hero-image metadata.
    Layered fallbacks: JSON-LD → Open Graph/meta tags → <time>/itemprop →
    trafilatura's own metadata (*trafi*) → <title>.
    """
    trafi = trafi or {}
    ld = _pick_json_ld(soup)

    site_name = _og(soup, "site_name")

    pub_name = _ld_str(ld, "publisher", "isPartOf", "provider", "sourceOrganization")
    if not pub_name:
        pub_name = site_name or _meta(soup, "application-name")

    title = None
    ld_headline = _ld_str(ld, "headline")
    ld_name = _ld_str(ld, "name")
    og_title = _og(soup, "title")
    if ld_name and og_title:
        # Cross-check: some sites (e.g. Wikipedia) put a description in
        # `headline` while `name` holds the real title.  When `name`
        # matches the og:title (modulo site suffix), trust `name`.
        og_stripped = _strip_site_suffix(og_title, pub_name, site_name).lower()
        ld_name_l = ld_name.lower()
        if (
            og_stripped == ld_name_l
            or og_stripped.startswith(ld_name_l + " ")
            or ld_name_l.startswith(og_stripped + " ")
        ):
            title = ld_name
    title = (
        title
        or ld_headline
        or ld_name
        or og_title
        or _meta_values(soup, "twitter:title")
        or (trafi.get("title") or "").strip()
        or (soup.title.string.strip() if soup.title and soup.title.string else "")
        or "Untitled"
    )
    title = _strip_site_suffix(title, pub_name, site_name)

    author_name, author_bio, author_avatar_url = _ld_author(
        ld.get("author") or ld.get("creator") or ""
    )
    if not author_name:
        author_name = (
            _meta(soup, "author")
            or _meta_values(
                soup,
                "article:author",
                "twitter:creator",
                "parsely-author",
                "byl",
                "dcterms.creator",
                "DC.creator",
            )
            or (trafi.get("author") or "").strip()
        )
        author_link = soup.find("a", rel="author")
        if not author_name and author_link is not None:
            author_name = author_link.get_text(strip=True)
    author_name = _clean_author(author_name)
    if not author_name:
        author_name = "Unknown"
    else:
        author_name = _clean_author(author_name.lstrip("@"))

    pub_date = _parse_date(
        _ld_str(ld, "datePublished")
        or _meta_values(
            soup,
            "article:published_time",
            "date",
            "pubdate",
            "publish-date",
            "publish_date",
            "publishdate",
            "publication-date",
            "publication_date",
            "DC.date.issued",
            "dcterms.created",
            "dcterms.date",
            "parsely-pub-date",
            "datePublished",
        )
        or _time_tag_date(soup)
        or (trafi.get("date") or "").strip()
        or _ld_str(ld, "dateModified")
    )

    # og:image — hero image for the article
    og_image = _og(soup, "image") or _meta_values(soup, "twitter:image")

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


def _collect_blocks(
    elem: ET.Element,
    img_set: ImageSet,
    blocks: list,
    word_count: list[int],
) -> None:
    """
    Single traversal of a trafilatura XML element into format-neutral blocks.
    Block tuples: ("para", text), ("head", text), ("items", [texts]),
    ("quote", text), ("image", local_path, is_landscape).
    Both the LaTeX and EPUB renderers consume these, so image dedup
    (via ImageSet.claim) applies to both outputs.
    """
    # Strip any namespace prefix (e.g. {http://...}p → p)
    tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag

    if tag in ("p", "ab"):
        text = "".join(elem.itertext()).strip()
        if text:
            word_count[0] += len(text.split())
            blocks.append(("para", text))

    elif tag == "graphic":
        src = (elem.get("src") or "").strip()
        path = img_set.get(src)
        # claim() rejects the hero image, already-emitted repeats, and
        # failed downloads — all of which previously surfaced as
        # duplicate or stray figures in the output.
        if path and img_set.claim(path):
            blocks.append(("image", path, not _get_image_is_portrait(path)))
        # No fallback emitted — failed/duplicate images are silently skipped

    elif tag == "head":
        text = "".join(elem.itertext()).strip()
        if text:
            word_count[0] += len(text.split())
            blocks.append(("head", text))

    elif tag == "list":
        items = []
        for child in elem:
            child_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if child_tag == "item":
                text = "".join(child.itertext()).strip()
                if text:
                    word_count[0] += len(text.split())
                    items.append(text)
        if items:
            blocks.append(("items", items))

    elif tag == "quote":
        text = "".join(elem.itertext()).strip()
        if text:
            word_count[0] += len(text.split())
            blocks.append(("quote", text))

    else:
        # Unknown structural element: recurse into children
        for child in elem:
            _collect_blocks(child, img_set, blocks, word_count)


def _blocks_to_latex(blocks: list) -> str:
    """Render collected blocks as LaTeX (paragraphs + inline figures)."""
    out: list[str] = []
    for block in blocks:
        kind = block[0]
        if kind == "para":
            out.append(tex(block[1]))
            out.append("")
        elif kind == "head":
            out.append(r"\medskip\noindent{\bfseries " + tex(block[1]) + r"}\par\smallskip")
            out.append("")
        elif kind == "items":
            out.append(r"\begin{itemize}")
            out.extend(r"  \item " + tex(t) for t in block[1])
            out.append(r"\end{itemize}")
            out.append("")
        elif kind == "quote":
            out.append(r"\begin{quote}")
            out.append(tex(block[1]))
            out.append(r"\end{quote}")
            out.append("")
        elif kind == "image":
            _, path, is_landscape = block
            out.append(_inline_figure_latex(path, is_landscape))
            out.append("")
    return "\n".join(out).strip()


def _blocks_to_html(blocks: list) -> str:
    """
    Render collected blocks as an XHTML fragment for EPUB chapters.
    Image sources use the images/ basename form matching the EPUB manifest.
    """
    out: list[str] = []
    for block in blocks:
        kind = block[0]
        if kind == "para":
            out.append(f"<p>{_html_escape(block[1])}</p>")
        elif kind == "head":
            out.append(f"<h2>{_html_escape(block[1])}</h2>")
        elif kind == "items":
            out.append("<ul>")
            out.extend(f"<li>{_html_escape(t)}</li>" for t in block[1])
            out.append("</ul>")
        elif kind == "quote":
            out.append(f"<blockquote><p>{_html_escape(block[1])}</p></blockquote>")
        elif kind == "image":
            _, path, _landscape = block
            name = Path(path).name
            out.append(
                f'<div class="fig"><img src="images/{name}" alt=""/></div>'
            )
    return "\n".join(out)


def _trafilatura_main(html: str) -> Optional[ET.Element]:
    """Parse trafilatura XML output and return the <main> element, or None."""
    xml_str = trafilatura.extract(
        html,
        include_images=True,
        include_comments=False,
        include_tables=False,
        output_format="xml",
    )
    if not xml_str:
        return None
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return None
    main = root.find(".//main")
    return main if main is not None else root


def extract_bodies(html: str, img_set: ImageSet) -> tuple[str, str, int]:
    """
    Extract article body in both output forms.
    Tries trafilatura XML output first; falls back to plain text.
    Returns (latex_str, xhtml_fragment, word_count).
    """
    main = _trafilatura_main(html)
    if main is not None:
        blocks: list = []
        wc: list[int] = [0]
        for child in main:
            _collect_blocks(child, img_set, blocks, wc)
        latex = _blocks_to_latex(blocks)
        if latex.strip():
            return latex, _blocks_to_html(blocks), wc[0]

    # Fallback: plain text
    text = (
        trafilatura.extract(html, include_comments=False, include_tables=False)
        or ""
    )
    return (
        _plain_body_to_latex(text),
        _plain_body_to_html(text),
        len(text.split()),
    )


def extract_body(html: str, img_set: ImageSet) -> tuple[str, int]:
    """
    Extract article body as LaTeX, embedding inline images.
    Compatibility wrapper around extract_bodies.
    Returns (latex_str, word_count).
    """
    latex, _xhtml, wc = extract_bodies(html, img_set)
    return latex, wc


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


def _plain_body_to_html(text: str) -> str:
    """Convert plain extracted text to XHTML paragraphs (fallback path)."""
    paras: list[str] = []
    current: list[str] = []

    def flush():
        if current:
            paras.append(f"<p>{_html_escape(' '.join(current))}</p>")
            current.clear()

    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            current.append(stripped)
        else:
            flush()
    flush()
    return "\n".join(paras)


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
\usepackage{placeins}  %% \FloatBarrier: keep floats inside their article

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
%% Flush pending floats so images cannot drift into the next article.
\FloatBarrier
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

        # Trafilatura's own document metadata (title/author/date) as a
        # fallback layer inside extract_metadata.
        trafi: dict = {}
        try:
            doc = trafilatura.bare_extraction(downloaded)
            if doc is not None:
                trafi = {
                    "title": doc.title or "",
                    "author": doc.author or "",
                    "date": doc.date or "",
                }
        except Exception:
            pass

        meta = extract_metadata(url, soup, trafi)

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

        # Body: structured extraction with inline images (both outputs)
        print("    extracting body...", flush=True)
        body_latex, body_html, word_count = extract_bodies(downloaded, img_set)

        inline_count = len(img_set.emitted)
        if inline_count > 0:
            print(f"    embedded {inline_count} inline image(s)", flush=True)

        if not body_latex.strip():
            print(f"  [WARN] No text extracted from {url}", file=sys.stderr)
            return None

        return Article(
            title=meta["title"],
            body_latex=body_latex,
            body_html=body_html,
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
# EPUB rendering (ebooklib)
#
# Deliberately minimal styling: no font-family or fixed sizes, so each
# e-reader's own typography settings apply.  Unlike the print PDF, chapters
# link back to the original article URL (readers can follow links).
# ---------------------------------------------------------------------------

_EPUB_CSS = """\
body {
  margin: 0 4%;
  text-align: justify;
}
h1, h2 {
  text-align: left;
  line-height: 1.25;
}
.byline {
  color: #555;
}
.bio {
  color: #555;
  font-size: 0.9em;
}
.dateline {
  color: #555;
  font-size: 0.9em;
}
.source {
  font-size: 0.9em;
}
.fig {
  text-align: center;
  margin: 1em 0;
}
img {
  max-width: 100%;
  height: auto;
}
blockquote {
  margin-left: 1em;
  padding-left: 1em;
  border-left: 2px solid #999;
  color: #333;
}
"""

_EPUB_IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
}


def _chapter_image_names(article: Article) -> list[str]:
    """Basenames of images used by a chapter, hero first, duplicates removed."""
    names: list[str] = []
    if article.hero_path:
        names.append(Path(article.hero_path).name)
    for m in re.finditer(r'src="images/([^"]+)"', article.body_html):
        if m.group(1) not in names:
            names.append(m.group(1))
    return names


def _chapter_html(article: Article) -> str:
    """Full XHTML content for one article chapter."""
    parts = [f"<h1>{_html_escape(article.title)}</h1>"]

    byline = " — ".join(
        p for p in (article.author_name, article.publication_name) if p
    )
    if byline:
        parts.append(f'<p class="byline">{_html_escape(byline)}</p>')
    if article.author_bio:
        parts.append(f'<p class="bio">{_html_escape(article.author_bio[:200])}</p>')

    meta_bits = [b for b in (article.published_date,) if b]
    meta_bits.append(f"{article.reading_time} min read")
    parts.append(f'<p class="dateline">{_html_escape(" · ".join(meta_bits))}</p>')

    if article.url:
        domain = urllib.parse.urlparse(article.url).netloc
        parts.append(
            f'<p class="source">Source: '
            f'<a href="{_html_escape(article.url)}">'
            f"{_html_escape(domain or article.url)}</a></p>"
        )

    if article.hero_path:
        hero_name = Path(article.hero_path).name
        parts.append(
            f'<div class="fig hero">'
            f'<img src="images/{hero_name}" alt=""/></div>'
        )

    parts.append(article.body_html)
    return "\n".join(parts)


def render_epub(
    articles: list[Article],
    today: str,
    epub_path: Path,
    img_dir: Path,
) -> bool:
    """
    Write *epub_path* (one chapter per article, images embedded, cover from
    the first available hero image).  Returns True on success.
    """
    try:
        from ebooklib import epub
    except ImportError:
        print(
            "  [ERROR] ebooklib is not installed "
            "(pip install -r requirements.txt) — skipping EPUB.",
            file=sys.stderr,
        )
        return False

    book = epub.EpubBook()
    book.set_identifier(f"digest-{today}")
    book.set_title("Digest")
    book.set_language("en")
    for name in dict.fromkeys(
        a.author_name for a in articles
        if a.author_name and a.author_name != "Unknown"
    ):
        book.add_author(name)

    css = epub.EpubItem(
        uid="style",
        file_name="style/style.css",
        media_type="text/css",
        content=_EPUB_CSS.encode("utf-8"),
    )
    book.add_item(css)

    added_files: set[str] = set()
    chapters = []
    cover_set = False
    for i, a in enumerate(articles):
        ch = epub.EpubHtml(
            title=a.title or f"Article {i + 1}",
            file_name=f"article_{i}.xhtml",
            lang="en",
        )
        ch.content = _chapter_html(a)
        ch.add_item(css)
        for img_name in _chapter_image_names(a):
            arc_name = f"images/{img_name}"
            if arc_name in added_files:
                continue
            local = img_dir / img_name
            if not local.exists():
                continue
            if not cover_set and img_name == Path(a.hero_path or "").name:
                book.set_cover(arc_name, local.read_bytes())
                cover_set = True
            else:
                book.add_item(epub.EpubImage(
                    uid=f"img_{i}_{len(added_files)}",
                    file_name=arc_name,
                    media_type=_EPUB_IMAGE_TYPES.get(
                        local.suffix.lower(), "image/jpeg"
                    ),
                    content=local.read_bytes(),
                ))
            added_files.add(arc_name)
        book.add_item(ch)
        chapters.append(ch)

    book.toc = tuple(chapters)
    book.add_item(epub.EpubNcx())
    book.add_item(epub.EpubNav())
    book.spine = (["cover", "nav"] if cover_set else ["nav"]) + chapters

    epub.write_epub(str(epub_path), book)
    return True


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
        description="Compile a list of URLs into a formatted digest: "
                    "an EPUB for e-readers by default, "
                    "optionally a print-ready PDF via LaTeX.",
    )
    parser.add_argument("urls", nargs="+", metavar="URL", help="URLs to include")
    parser.add_argument(
        "-o", "--output", default="digest",
        help="Output base name (default: digest)",
    )
    parser.add_argument(
        "--format", default="epub", choices=("pdf", "epub", "both"),
        help="Output format(s) to produce (default: epub)",
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
    epub_path = output_base.with_suffix(".epub")
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

    if args.format in ("epub", "both"):
        print(f"\nRendering EPUB for {len(articles)} article(s)...")
        if render_epub(articles, today, epub_path, img_dir):
            print(f"  Written: {epub_path}")

    if args.format in ("pdf", "both"):
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
                    f"\n[WARN] {engine} did not produce a PDF — "
                    "check the .log file for errors.",
                    file=sys.stderr,
                )
        else:
            print("\nDone! (PDF compilation skipped)")


if __name__ == "__main__":
    main()
