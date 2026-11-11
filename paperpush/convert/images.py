"""Figure files: what Word and LaTeX can each embed, and turning the rest into PNG."""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess  # nosec B404 -- fixed argv, local tools, no shell
from pathlib import Path

from PIL import Image as PILImage

from .model import Log

MAX_WIDTH_IN = 6.5
MAX_HEIGHT_IN = 8.5
DPI = 300
WORD_READY = {".png", ".jpg", ".jpeg"}
LATEX_READY = {".png", ".jpg", ".jpeg", ".pdf"}
_PILLOW_OK = {".tif", ".tiff", ".bmp", ".gif", ".webp"}


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:20]


def _run(argv: list[str], cwd: Path | None = None, timeout: int = 120) -> bool:
    try:
        return subprocess.run(argv, capture_output=True, timeout=timeout, cwd=cwd, check=False).returncode == 0  # nosec B603
    except (OSError, subprocess.TimeoutExpired):
        return False


def _pdf_to_png(src: Path, target: Path) -> bool:
    tool = shutil.which("pdftoppm")
    if tool and _run([tool, "-r", str(DPI), "-png", "-singlefile", "-f", "1", "-l", "1", str(src), str(target.with_suffix(""))]) and target.is_file():
        return True
    tool = shutil.which("sips")  # macOS; renders at 72 dpi unless told otherwise
    return bool(tool and _run([tool, "-s", "format", "png", "-s", "dpiWidth", str(DPI), "-s", "dpiHeight", str(DPI), str(src), "--out", str(target)]) and target.is_file())


def _eps_to_pdf(src: Path, target: Path) -> bool:
    tool = shutil.which("epstopdf")
    if tool and _run([tool, f"--outfile={target}", str(src)]) and target.is_file():
        return True
    tool = shutil.which("ps2pdf")
    return bool(tool and _run([tool, "-dEPSCrop", str(src), str(target)]) and target.is_file())


def to_raster(src: Path, work: Path, log: Log) -> Path | None:
    """A PNG or JPEG of ``src`` for Word, or None (with a warning) when none can be made."""
    suffix = src.suffix.lower()
    if suffix in WORD_READY:
        return src
    work.mkdir(parents=True, exist_ok=True)
    target = work / (src.stem + f"-{sha(src)[:6]}.png")
    if suffix == ".pdf":
        if _pdf_to_png(src, target):
            log.count("rendered PDF figures to 300 dpi PNG for Word")
            return target
        log.warn(f"could not render {src.name} for Word: install poppler (pdftoppm), or export the figure as PNG")
        return None
    if suffix in (".eps", ".ps"):
        middle = work / (src.stem + ".pdf")
        if _eps_to_pdf(src, middle) and _pdf_to_png(middle, target):
            log.count("rendered EPS figures to 300 dpi PNG for Word")
            return target
        log.warn(f"could not render {src.name} for Word: install Ghostscript and poppler, or export the figure as PNG")
        return None
    if suffix in _PILLOW_OK:
        try:
            with PILImage.open(src) as img:
                img.convert("RGBA" if "A" in img.getbands() else "RGB").save(target, dpi=img.info.get("dpi", (DPI, DPI)))
            log.count("converted figures to PNG")
            return target
        except OSError as exc:
            log.warn(f"could not read {src.name}: {exc}")
            return None
    log.warn(f"{src.name}: Word cannot embed {suffix} figures here; export the figure as PNG")
    return None


def for_latex(src: Path, work: Path, log: Log) -> Path | None:
    """A file pdfLaTeX can include for ``src``, or None (with a warning)."""
    suffix = src.suffix.lower()
    if suffix in LATEX_READY:
        return src
    work.mkdir(parents=True, exist_ok=True)
    if suffix in (".eps", ".ps"):
        target = work / (src.stem + ".pdf")
        if _eps_to_pdf(src, target):
            log.count("converted EPS figures to PDF")
            return target
    if suffix in _PILLOW_OK:
        return to_raster(src, work, log)
    if suffix in (".emf", ".wmf"):
        target = work / (src.stem + ".png")
        tool = shutil.which("soffice") or shutil.which("libreoffice") or shutil.which("inkscape")
        if tool and "inkscape" not in tool and _run([tool, "--headless", "--convert-to", "png", "--outdir", str(work), str(src)]) and target.is_file():
            log.count("converted EMF/WMF figures to PNG")
            return target
        log.warn(f"{src.name} is a Windows vector image, which LaTeX cannot include: export the figure from Word as PNG or PDF and replace it")
        return None
    log.warn(f"{src.name}: LaTeX cannot include {suffix} figures; export it as PNG or PDF")
    return None


def size_inches(path: Path, wanted_width: float | None) -> tuple[float, float]:
    """The size to show ``path`` at: its own (or the wanted) width, kept inside the page."""
    with PILImage.open(path) as img:
        width_px, height_px = img.size
        dpi = img.info.get("dpi", (DPI, DPI))[0] or DPI
    if dpi < 30:  # a junk resolution tag
        dpi = DPI
    width = wanted_width or width_px / float(dpi)
    width = min(width, MAX_WIDTH_IN)
    height = width * height_px / max(width_px, 1)
    if height > MAX_HEIGHT_IN:
        width, height = width * MAX_HEIGHT_IN / height, MAX_HEIGHT_IN
    return width, height


def render_tikz(tikz: str, preamble: str, root: Path, work: Path, n: int, log: Log, *, quiet: bool = False) -> Path | None:
    """Compile a picture or other environment with the manuscript's own preamble, to PNG for Word."""
    tool = shutil.which("pdflatex")
    if not tool:
        if not quiet:
            log.warn("a TikZ figure could not be rendered for Word: no pdflatex found")
        return None
    build = work / f"tikz{n}"
    build.mkdir(parents=True, exist_ok=True)
    source = build / "picture.tex"
    head = preamble.rstrip() + "\n\\usepackage[active,tightpage,floats]{preview}\n\\PreviewEnvironment{tikzpicture}\n\\PreviewEnvironment{pgfpicture}\n\\PreviewEnvironment{tikzcd}\n\\PreviewEnvironment{forest}\n"
    env = re.match(r"\s*\\begin\{([^{}]+)\}", tikz)
    if env and env.group(1) not in ("tikzpicture", "pgfpicture", "tikzcd", "forest"):
        head += f"\\PreviewEnvironment{{{env.group(1)}}}\n"
    source.write_text(head + "\\pagestyle{empty}\n\\begin{document}\n" + tikz + "\n\\end{document}\n", encoding="utf-8")
    _run([tool, "-interaction=nonstopmode", "-halt-on-error", f"-output-directory={build}", str(source)], cwd=root, timeout=180)
    pdf = build / "picture.pdf"
    target = work / f"tikz{n}.png"
    if pdf.is_file() and _pdf_to_png(pdf, target):
        log.count("rendered LaTeX pictures and algorithms to PNG for Word")
        return target
    if not quiet:
        log.warn("a TikZ figure did not compile on its own, so Word shows a placeholder: export it as an image")
    return None
