# digest

Compile a list of URLs into a formatted, print-ready PDF digest using LaTeX.

## What it does

Give `digest` a list of article URLs and it produces a typeset two-column PDF on A5 paper — like a personal magazine. For each article it:

- Scrapes the body text with [trafilatura](https://trafilatura.readthedocs.io/)
- Extracts metadata (title, author, publication, date) from schema.org JSON-LD, falling back to Open Graph tags
- Downloads the hero image (`og:image`), author avatar, publication favicon, and any inline body images
- Detects the editorial tone and sets the body font accordingly
- Generates a `.tex` file and compiles it to PDF with XeLaTeX (or pdflatex)

The output includes a table of contents (title + author per entry) followed by one article per page, each with a full-width hero image immediately beneath the title block.

## Requirements

**Python packages**

```
pip install -r requirements.txt
```

```
trafilatura>=1.8
beautifulsoup4>=4.12
requests>=2.31
jinja2>=3.1
Pillow>=10.0
lxml>=5.0
```

**System**

- `xelatex` (TeX Live or MiKTeX) — default engine; `pdflatex` also supported via `--engine pdflatex`
- The following LaTeX packages (all included in a standard TeX Live install):
  `geometry`, `iftex`, `fontspec`, `microtype`, `graphicx`, `hyperref`,
  `parskip`, `tocloft`, `xcolor`, `float`, `lettrine`
- For XeLaTeX: the fonts **EB Garamond**, **Linux Libertine O**, **XCharter**,
  **TeX Gyre Pagella**, **Latin Modern**, **Noto Serif**, and
  **EBGaramond-Initials.otf** must be installed as system fonts
- For pdflatex: `ebgaramond`, `libertine`, `charter`, `mathpazo`, `noto-serif`,
  `GoudyIn`, `fontenc`, `inputenc`

## Usage

```
python digest.py URL [URL ...] [-o OUTPUT] [--no-pdf] [--engine ENGINE]
```

### Basic example

```sh
python digest.py \
  https://www.theatlantic.com/some-article \
  https://arstechnica.com/some-story \
  https://www.nature.com/some-paper
```

Produces `digest.tex`, `digest.pdf`, and a `digest_images/` directory in the current folder.

### Options

| Flag | Default | Description |
|---|---|---|
| `-o OUTPUT` | `digest` | Base name for output files (`.tex`, `.pdf`, `_images/`) |
| `--no-pdf` | off | Write the `.tex` file but skip running the LaTeX engine |
| `--engine ENGINE` | `xelatex` | LaTeX engine: `xelatex` or `pdflatex` |

### Tone tags

Append `[tone]` after any URL to override automatic tone detection:

```sh
python digest.py \
  'https://lithub.com/some-essay [literary]' \
  'https://arxiv.org/abs/1234 [academic]' \
  'https://techcrunch.com/story [technical]'
```

Valid tones: `literary`, `journalistic`, `technical`, `academic`.

Without a tag, tone is inferred by scoring the article title and source domain against four keyword lists. If no list wins clearly, `journalistic` is used as the default.

## Tone → font mapping

| Tone | Font (XeLaTeX) | Font (pdflatex) |
|---|---|---|
| `literary` | EB Garamond | ebgaramond |
| `journalistic` | Linux Libertine O | libertine |
| `technical` | XCharter | charter |
| `academic` | TeX Gyre Pagella | mathpazo |

The document title, table of contents, and all article title blocks always use Latin Modern regardless of tone. Only the article body text switches font.

## Output layout

```
digest.tex          — LaTeX source
digest.pdf          — compiled PDF
digest_images/
  hero_0.jpg        — hero image for article 0
  art0_0.png        — first inline image in article 0
  art0_1.jpg        — second inline image in article 0
  avatar_0.png      — author avatar for article 0
  favicon_0.png     — publication favicon for article 0
  ...
```

Each article page:

```
┌─────────────────────────────┐
│  Title                      │  ← Latin Modern, bold
│  [avatar] Author — Pub      │  ← small, italic
│  One-line author bio        │
│  [favicon] Date · N min read│
│  ─────────────────────────  │
│  [hero image, full width]   │
├──────────────┬──────────────┤
│ Body text in │ two columns, │
│ tone-matched │ font         │
│ ...          │ ...          │
└──────────────┴──────────────┘
```

## Notes

- **Duplicate images are skipped.** If the hero image URL also appears inline in the article body, it is not downloaded or embedded twice.
- **Failed image downloads are silently skipped.** The document is produced regardless of whether individual images can be fetched.
- **SVG images are skipped** (XeLaTeX/pdflatex cannot include them). WEBP, ICO, GIF, and BMP images are converted to PNG via Pillow if available; otherwise skipped.
- The LaTeX engine is run twice to resolve table-of-contents page numbers.
