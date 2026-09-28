"""PDF rendering helpers, including Tamil/Latin font support."""
import os
import logging
from flask_login import current_user
from models import CompanySettings

logger = logging.getLogger(__name__)

def _get_company_settings_for_pdf():
    """Return the logged-in user's company print header settings."""
    try:
        settings = CompanySettings.query.filter_by(user_id=current_user.id).first()
        if not settings:
            return {"company_name": "", "address": "", "phone": ""}
        return {
            "company_name": (settings.company_name or "").strip(),
            "address": (settings.address or "").strip(),
            "phone": (settings.phone or "").strip(),
        }
    except Exception:
        # PDF generation must still work if optional company-header data
        # cannot be read.
        logger.exception("Could not load company settings for PDF header")
        return {"company_name": "", "address": "", "phone": ""}


def _add_company_pdf_header(elements, normal_style, title, company=None):
    """Add company details above the report title on every generated PDF."""
    # ParagraphStyle is imported inside this helper because each PDF route
    # historically imported ReportLab styling classes locally.
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.platypus import Paragraph, Spacer, Table, TableStyle
    from reportlab.lib import colors

    company = company or _get_company_settings_for_pdf()

    if company["company_name"]:
        company_style = ParagraphStyle(
            "PdfCompanyName",
            parent=normal_style,
            fontSize=16,
            leading=19,
            alignment=1,
            spaceAfter=3,
        )
        elements.append(Paragraph(_pdf_text(company["company_name"]), company_style))

    contact_parts = [p for p in (company["address"], company["phone"]) if p]
    if contact_parts:
        contact_style = ParagraphStyle(
            "PdfCompanyContact",
            parent=normal_style,
            fontSize=9,
            leading=12,
            alignment=1,
            spaceAfter=6,
        )
        elements.append(Paragraph(_pdf_text(" | ".join(contact_parts)), contact_style))

    title_style = ParagraphStyle(
        "PdfReportTitle",
        parent=normal_style,
        fontName=_FONT_CACHE.get("result", (None, "Helvetica", "Helvetica-Bold", False))[2],
        fontSize=14,
        leading=17,
        alignment=1,
        textColor=colors.black,
        spaceAfter=0,
    )
    # Print-friendly title: white background with black text. This avoids
    # the dark navy header consuming ink and makes the title clearly visible
    # on normal office printers.
    title_box = Table(
        [[Paragraph(_pdf_text(title), title_style)]],
        colWidths=[None],
        hAlign="CENTER",
    )
    title_box.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), colors.white),
        ("BOX", (0, 0), (-1, -1), 0.8, colors.HexColor("#6b7280")),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    elements.append(title_box)
    elements.append(Spacer(1, 8))





def _find_font_file(candidates):
    for path in candidates:
        if path and os.path.exists(path):
            return path
    return None


def _download_font(url, save_path):
    import urllib.request
    try:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        print(f"[PDF font] Downloading {os.path.basename(save_path)} …")
        urllib.request.urlretrieve(url, save_path)
        print(f"[PDF font] Saved to {save_path}")
        return True
    except Exception as e:
        print(f"[PDF font] Download failed: {e}")
        return False


def _ensure_font(fname, github_subpath):
    """Locate font on disk across common paths, or auto-download it."""
    app_fonts = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts")
    windir = os.environ.get("WINDIR", "C:\\Windows")

    candidates = [
        os.path.join(app_fonts, fname),
        os.path.join("C:\\FinanceManager", "fonts", fname),
        os.path.join(windir, "Fonts", fname),
        f"/usr/share/fonts/truetype/noto/{fname}",
        f"/usr/share/fonts/opentype/noto/{fname}",
        f"/Library/Fonts/{fname}",
        f"/System/Library/Fonts/{fname}",
    ]
    path = _find_font_file(candidates)
    if path:
        return path

    save_path = os.path.join(app_fonts, fname)
    url = (
        "https://github.com/googlefonts/noto-fonts/raw/main/"
        f"hinted/ttf/{github_subpath}"
    )
    return save_path if _download_font(url, save_path) else None


def _try_register(reg_name, path):
    """Register a TTFont. Returns True if already registered or success."""
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    try:
        pdfmetrics.getFont(reg_name)
        return True
    except KeyError:
        pass
    if path and os.path.exists(path):
        try:
            pdfmetrics.registerFont(TTFont(reg_name, path))
            return True
        except Exception as e:
            print(f"[PDF font] Cannot register {reg_name}: {e}")
    return False


def _register_pdf_fonts():
    """
    Register both NotoSansTamil and NotoSans with ReportLab.
    Returns (tamil_font, latin_font, bold_font, has_tamil).
    Always call this before building any PDF.
    """
    if "result" in _FONT_CACHE:
        return _FONT_CACHE["result"]

    # ── Tamil font (NotoSansTamil covers U+0B80–U+0BFF) ────────────────────
    tamil_path = _ensure_font(
        "NotoSansTamil-Regular.ttf",
        "NotoSansTamil/NotoSansTamil-Regular.ttf"
    )
    tamil_ok = _try_register("NotoSansTamil", tamil_path)
    if tamil_ok:
        _try_register("NotoSansTamil-Bold", tamil_path)  # reuse same file

    # ── Latin font (NotoSans covers A–Z, a–z, digits, punctuation) ─────────
    latin_path = _ensure_font(
        "NotoSans-Regular.ttf",
        "NotoSans/NotoSans-Regular.ttf"
    )
    latin_bold_path = _ensure_font(
        "NotoSans-Bold.ttf",
        "NotoSans/NotoSans-Bold.ttf"
    )
    latin_ok = _try_register("NotoSans", latin_path)
    if latin_ok:
        if not _try_register("NotoSans-Bold", latin_bold_path or latin_path):
            pass  # bold falls back to regular below

    if tamil_ok and latin_ok:
        print("[PDF font] Dual-font mode: NotoSansTamil + NotoSans (Tamil + Latin)")
        result = ("NotoSansTamil", "NotoSans", "NotoSans-Bold", True)
    elif tamil_ok:
        print("[PDF font] Tamil-only mode: NotoSansTamil (Latin may look basic)")
        result = ("NotoSansTamil", "NotoSansTamil", "NotoSansTamil-Bold", True)
    elif latin_ok:
        print("[PDF font] Latin-only mode: NotoSans (Tamil will not render)")
        result = ("NotoSans", "NotoSans", "NotoSans-Bold", False)
    else:
        print("[PDF font] WARNING: No Unicode fonts found. Using Helvetica.")
        result = ("Helvetica", "Helvetica", "Helvetica-Bold", False)

    _FONT_CACHE["result"] = result
    return result


def _pdf_text(text):
    """
    Wrap each character in the correct <font> tag so ReportLab Paragraph
    renders both Tamil and Latin characters correctly in the same string.

    Tamil Unicode block: U+0B80 – U+0BFF
    Everything else (Latin, digits, spaces, punctuation) uses the Latin font.
    """
    cached = _FONT_CACHE.get("result")
    if cached is None:
        _register_pdf_fonts()
        cached = _FONT_CACHE["result"]

    tamil_font = cached[0]
    latin_font = cached[1]

    # If no dual-font support, just return plain text
    if tamil_font == latin_font:
        return text

    result = []
    current_font = None
    for ch in str(text):
        cp = ord(ch)
        font = tamil_font if 0x0B80 <= cp <= 0x0BFF else latin_font
        if font != current_font:
            if current_font is not None:
                result.append("</font>")
            result.append(f'<font name="{font}">')
            current_font = font
        # Escape XML special characters
        if ch == "&":
            result.append("&amp;")
        elif ch == "<":
            result.append("&lt;")
        elif ch == ">":
            result.append("&gt;")
        else:
            result.append(ch)
    if current_font:
        result.append("</font>")
    return "".join(result)


# Backward-compat alias used throughout the rest of the file
def _register_tamil_font():
    tamil_font, latin_font, bold_font, has_tamil = _register_pdf_fonts()
    # Return (normal_font, bold_font, has_tamil) as before — callers use
    # the normal_font for body text; _pdf_text() handles per-char switching.
    return tamil_font, bold_font, has_tamil


# Pre-register at startup so the first PDF is fast
try:
    _register_pdf_fonts()
except Exception:
    pass


# ==========================
# CUSTOMER SORT HELPER
# Sorts customers by customer_id numerically (1, 2, 8, 22, 88, 888, 11112)
# instead of alphabetically (1, 11112, 2, 22, 8, 88, 888), since
# customer_id is stored as a string in the database.
# ==========================
