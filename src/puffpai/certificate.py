"""Adoption certificate — a keepsake for a hatched Puff.

Generates a dense, dark, editorial SVG (the dashboard's night/galaxy palette)
recording the Puff's name, class, crew roster with display names and
specialties, wake word, hatch date, and hardware line — sealed with a
wax-seal-style paw print whose geometry derives **only** from
``sha256(name + hatch_date)``. Deterministic on purpose: the same Puff always
presses the same paw, and nothing about the seal leaks build config or
hostname (the hardware line comes from config/platform constants and never
from a hostname).

Built natively as a string template — no headless browser, no external
renderer. PNG export is optional and pure Pillow
(:func:`render_png`); when Pillow is absent the certificate ships SVG-only
and callers should say so (cairosvg is knowingly broken on the Windows dev
box — missing native cairo — so Pillow re-draw is the only PNG path).

Copyright (c) 2025 Cypher Sisters. Apache-2.0 License.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import platform
from datetime import datetime, timezone
from xml.sax.saxutils import escape as _sax_escape

#: Quote-aware XML escaping. Interpolated values land in BOTH element text
#: and attribute values (e.g. the root aria-label), and bare
#: ``saxutils.escape`` leaves quotes alone — a name containing ``"`` would
#: break the attribute and serve malformed XML as image/svg+xml.
_XML_ENTITIES = {'"': "&quot;", "'": "&apos;"}


def escape(value: str) -> str:
    return _sax_escape(value, _XML_ENTITIES)

from puffpai.logging_config import get_logger

logger = get_logger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

#: Canvas size (portrait, roughly US-letter proportions).
WIDTH, HEIGHT = 860, 1150

#: The dashboard's night/galaxy tokens (css/_theme.css), inlined because an
#: SVG keepsake must stand alone — it cannot read the dashboard's CSS vars.
NIGHT = {
    "bg": "#0B0714",
    "bg2": "#150C24",
    "text": "#F7EEF8",
    "text2": "rgba(247,238,248,0.68)",
    "text3": "rgba(247,238,248,0.42)",
    "pink": "#F06FD0",
    "magenta": "#C64BD8",
    "sky": "#62D8F0",
    "mint": "#6EE7B7",
    "lemon": "#FCD97A",
    "lav": "#A88BE8",
    "line": "rgba(255,255,255,0.10)",
    "chip": "rgba(255,255,255,0.07)",
}

#: Font stacks mirror the dashboard (Fredoka display / Nunito body /
#: JetBrains Mono numerals) with honest fallbacks for viewers without them.
FONT_DISPLAY = "Fredoka, Comfortaa, 'Trebuchet MS', sans-serif"
FONT_BODY = "Nunito, 'Segoe UI', sans-serif"
FONT_MONO = "'JetBrains Mono', 'Cascadia Mono', Consolas, monospace"


class CertificateUnavailable(RuntimeError):
    """No hatch record and no explicit fields — nothing to certify yet."""


# ── data assembly ───────────────────────────────────────────────────


def _default_hardware() -> str:
    """Hardware line from platform constants — never the hostname.

    ``identity.hardware`` is read from the config dir the loader resolves, not
    from ``<repo>/config``: on a relocated install the owner's copy is the one
    that carries their edit, and a certificate naming shipped-default hardware
    is a certificate about a different appliance.
    """
    try:
        try:
            from puffpai.config import resolve_config_dir
            _config_dir = resolve_config_dir()
        except Exception:
            _config_dir = os.path.join(_REPO_ROOT, "config")
        with open(os.path.join(_config_dir, "puffpai_config.json"),
                  "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        configured = (raw.get("identity") or {}).get("hardware")
        if isinstance(configured, str) and configured.strip():
            return configured.strip()[:80]
    except (OSError, ValueError):
        pass
    return (f"{platform.system()} · {platform.machine()} — "
            "local sovereign build")


def certificate_data(*, name: str | None = None,
                     hatch_date: str | None = None,
                     wake_word: str | None = None,
                     hardware: str | None = None,
                     crew: list[dict] | None = None,
                     classing: str | None = None) -> dict:
    """Assemble everything the certificate states, from the live build unless
    a field is passed explicitly (tests pass everything explicitly)."""
    from puffpai import hatch

    state = hatch.load_state()
    if name is None:
        name = hatch.current_name()
    if hatch_date is None and state:
        hatch_date = state.get("hatch_date", state["hatched_at"][:10])
    if not name or not hatch_date:
        raise CertificateUnavailable(
            "this build has not hatched yet — run the hatch ritual first "
            "(or pass name and hatch_date explicitly)")

    if wake_word is None:
        try:
            from puffpai.config import get_config
            wake_word = get_config().nabu.wake_word
        except Exception:
            wake_word = "hey puffpai"
    if hardware is None:
        hardware = _default_hardware()

    if crew is None:
        crew = []
        try:
            from puffpai.agents.preferences import display_names
            from puffpai.agents.registry import ROSTER
            names = display_names()
            for entry in ROSTER:
                crew.append({
                    "id": entry.name,
                    "display_name": names.get(entry.name, entry.name),
                    "specialty": hatch.SPECIALTIES.get(entry.name, ""),
                    "workflow": entry.workflow,
                })
        except Exception as exc:
            logger.warning("certificate: crew roster unavailable: %s", exc)

    if classing is None and any(c.get("id") == "concierge" for c in crew):
        # The roster's front door is a Concierge, so the Puff carries the
        # class; a build with a different roster simply omits the line.
        classing = "Concierge-class companion"

    return {
        "name": hatch.clean_name(name) or str(name)[:40],
        "hatch_date": str(hatch_date)[:10],
        "wake_word": str(wake_word),
        "hardware": str(hardware)[:80],
        "crew": crew,
        "classing": classing,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ── the paw seal (sha256-only geometry) ─────────────────────────────


def seal_geometry(name: str, hatch_date: str) -> dict:
    """Deterministic seal parameters from ``sha256(name + hatch_date)``.

    Every number here comes from the digest — no build config, no hostname,
    no randomness. Same Puff, same paw, forever.
    """
    digest = hashlib.sha256(f"{name}|{hatch_date}".encode("utf-8")).digest()

    blob_radii = [56 + (digest[1 + i] % 16) for i in range(10)]
    toe_base = (-52, -18, 18, 52)
    toes = []
    for i in range(4):
        toes.append({
            "angle": toe_base[i] + (digest[12 + i] % 9) - 4,
            "dist": 30 + (digest[16 + i] % 6),
            "rx": 7 + (digest[20 + i] % 3),
            "ry": 9 + (digest[24 + i] % 3),
        })
    return {
        "rotation": round(((digest[0] / 255.0) - 0.5) * 24, 2),
        "blob_radii": blob_radii,
        "pad": {
            "rx": 19 + (digest[28] % 6),
            "ry": 15 + (digest[29] % 5),
            "dx": (digest[30] % 7) - 3,
            "dy": (digest[31] % 7) - 3,
        },
        "toes": toes,
    }


def _smooth_closed_path(points: list[tuple[float, float]]) -> str:
    """A closed cubic-bezier path through *points* (the wax blob outline)."""
    n = len(points)
    d = [f"M {points[0][0]:.1f} {points[0][1]:.1f}"]
    for i in range(n):
        p0 = points[(i - 1) % n]
        p1 = points[i]
        p2 = points[(i + 1) % n]
        p3 = points[(i + 2) % n]
        c1 = (p1[0] + (p2[0] - p0[0]) / 6.0, p1[1] + (p2[1] - p0[1]) / 6.0)
        c2 = (p2[0] - (p3[0] - p1[0]) / 6.0, p2[1] - (p3[1] - p1[1]) / 6.0)
        d.append(f"C {c1[0]:.1f} {c1[1]:.1f} {c2[0]:.1f} {c2[1]:.1f} "
                 f"{p2[0]:.1f} {p2[1]:.1f}")
    d.append("Z")
    return " ".join(d)


def _seal_svg(name: str, hatch_date: str, cx: float, cy: float) -> str:
    geo = seal_geometry(name, hatch_date)
    pts = []
    for i, radius in enumerate(geo["blob_radii"]):
        angle = math.radians(i * 36)
        pts.append((math.cos(angle) * radius, math.sin(angle) * radius))
    blob = _smooth_closed_path(pts)

    toes = []
    for toe in geo["toes"]:
        rad = math.radians(toe["angle"] - 90)
        tx = math.cos(rad) * toe["dist"]
        ty = math.sin(rad) * toe["dist"]
        toes.append(
            f'<ellipse cx="{tx:.1f}" cy="{ty:.1f}" rx="{toe["rx"]}" '
            f'ry="{toe["ry"]}" transform="rotate({toe["angle"]:.1f} '
            f'{tx:.1f} {ty:.1f})" fill="{NIGHT["bg"]}" opacity="0.85"/>')
    pad = geo["pad"]
    paw = (
        f'<ellipse cx="{pad["dx"]}" cy="{8 + pad["dy"]}" rx="{pad["rx"]}" '
        f'ry="{pad["ry"]}" fill="{NIGHT["bg"]}" opacity="0.85"/>'
        + "".join(toes))

    return (
        f'<g transform="translate({cx} {cy}) rotate({geo["rotation"]})">'
        f'<path d="{blob}" fill="url(#sealwax)" stroke="{NIGHT["pink"]}" '
        f'stroke-opacity="0.5" stroke-width="1.5"/>'
        f'{paw}'
        f'</g>')


# ── star field (same digest, purely decorative) ─────────────────────


def _stars(name: str, hatch_date: str) -> str:
    seed = int.from_bytes(
        hashlib.sha256(f"stars|{name}|{hatch_date}".encode()).digest()[:8],
        "big")
    out = []
    state = seed
    for _ in range(34):
        state = (state * 6364136223846793005 + 1442695040888963407) % (1 << 64)
        x = 40 + (state >> 40) % (WIDTH - 80)
        y = 40 + (state >> 24) % (HEIGHT - 80)
        r = 0.6 + ((state >> 16) % 10) / 12.0
        opacity = 0.12 + ((state >> 8) % 20) / 100.0
        out.append(f'<circle cx="{x}" cy="{y}" r="{r:.2f}" '
                   f'fill="{NIGHT["text"]}" opacity="{opacity:.2f}"/>')
    return "".join(out)


# ── SVG template ────────────────────────────────────────────────────


def render_svg(data: dict) -> str:
    """The certificate as a self-contained SVG string."""
    name = escape(data["name"])
    hatch_date = escape(data["hatch_date"])
    wake_word = escape(data["wake_word"])
    hardware = escape(data["hardware"])
    # Uppercasing must happen BEFORE escaping: entity names are
    # case-sensitive, and "&apos;".upper() == "&APOS;" is malformed XML.
    hardware_upper = escape(data["hardware"].upper())
    classing = (escape(data["classing"].upper())
                if data.get("classing") else None)
    crew = data.get("crew", [])

    crew_rows = []
    row_y = 640
    for member in crew:
        display = escape(str(member.get("display_name", "")))
        specialty = escape(str(member.get("specialty", "")))
        workflow = escape(str(member.get("workflow", "")))
        crew_rows.append(f'''
  <g>
    <text x="84" y="{row_y}" font-family="{FONT_DISPLAY}" font-size="19"
          font-weight="500" fill="{NIGHT["text"]}">{display}</text>
    <text x="84" y="{row_y + 19}" font-family="{FONT_BODY}" font-size="12.5"
          fill="{NIGHT["text2"]}">{specialty}</text>
    <rect x="706" y="{row_y - 15}" rx="9" ry="9" width="70" height="20"
          fill="{NIGHT["chip"]}" stroke="{NIGHT["line"]}"/>
    <text x="741" y="{row_y - 1}" text-anchor="middle"
          font-family="{FONT_MONO}" font-size="10.5"
          fill="{NIGHT["lav"]}">{workflow}</text>
    <line x1="84" y1="{row_y + 32}" x2="776" y2="{row_y + 32}"
          stroke="{NIGHT["line"]}" stroke-width="0.6"/>
  </g>''')
        row_y += 52

    classing_block = ""
    if classing:
        classing_block = f'''
  <text x="84" y="352" font-family="{FONT_MONO}" font-size="13.5"
        letter-spacing="3" fill="{NIGHT["sky"]}">{classing}</text>'''

    seal = _seal_svg(data["name"], data["hatch_date"], 700, 1022)
    stars = _stars(data["name"], data["hatch_date"])

    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" height="{HEIGHT}"
     viewBox="0 0 {WIDTH} {HEIGHT}" role="img"
     aria-label="Adoption certificate for {name}">
  <defs>
    <radialGradient id="glowpink" cx="18%" cy="10%" r="55%">
      <stop offset="0%" stop-color="{NIGHT["pink"]}" stop-opacity="0.16"/>
      <stop offset="100%" stop-color="{NIGHT["pink"]}" stop-opacity="0"/>
    </radialGradient>
    <radialGradient id="glowsky" cx="85%" cy="92%" r="60%">
      <stop offset="0%" stop-color="{NIGHT["sky"]}" stop-opacity="0.12"/>
      <stop offset="100%" stop-color="{NIGHT["sky"]}" stop-opacity="0"/>
    </radialGradient>
    <linearGradient id="accent" x1="0" y1="0" x2="1" y2="0">
      <stop offset="0%" stop-color="{NIGHT["pink"]}"/>
      <stop offset="100%" stop-color="{NIGHT["magenta"]}"/>
    </linearGradient>
    <radialGradient id="sealwax" cx="38%" cy="34%" r="80%">
      <stop offset="0%" stop-color="{NIGHT["pink"]}"/>
      <stop offset="62%" stop-color="{NIGHT["magenta"]}"/>
      <stop offset="100%" stop-color="#7A2B8F"/>
    </radialGradient>
  </defs>

  <rect width="{WIDTH}" height="{HEIGHT}" fill="{NIGHT["bg"]}"/>
  <rect width="{WIDTH}" height="{HEIGHT}" fill="url(#glowpink)"/>
  <rect width="{WIDTH}" height="{HEIGHT}" fill="url(#glowsky)"/>
  {stars}

  <rect x="26" y="26" width="{WIDTH - 52}" height="{HEIGHT - 52}"
        fill="none" stroke="{NIGHT["line"]}" stroke-width="1.4"/>
  <rect x="36" y="36" width="{WIDTH - 72}" height="{HEIGHT - 72}"
        fill="none" stroke="rgba(255,255,255,0.05)" stroke-width="1"/>

  <text x="84" y="108" font-family="{FONT_MONO}" font-size="11.5"
        letter-spacing="4" fill="{NIGHT["text3"]}">PUFFPAI — SOVEREIGN COMPANION</text>
  <text x="776" y="108" text-anchor="end" font-family="{FONT_MONO}"
        font-size="11.5" letter-spacing="4"
        fill="{NIGHT["lemon"]}">CERTIFICATE OF ADOPTION</text>
  <line x1="84" y1="126" x2="776" y2="126" stroke="{NIGHT["line"]}"/>

  <text x="84" y="200" font-family="{FONT_BODY}" font-size="15"
        fill="{NIGHT["text2"]}">This certifies that the household adopted, named,
        and hatched</text>
  <text x="84" y="296" font-family="{FONT_DISPLAY}" font-size="72"
        font-weight="600" fill="{NIGHT["text"]}">{name}</text>
  <rect x="84" y="316" width="270" height="4" rx="2" fill="url(#accent)"/>
{classing_block}

  <g font-family="{FONT_MONO}" font-size="10" letter-spacing="2.5"
     fill="{NIGHT["text3"]}">
    <text x="84" y="428">HATCHED</text>
    <text x="290" y="428">WAKE WORD</text>
    <text x="500" y="428">HARDWARE</text>
  </g>
  <g font-family="{FONT_BODY}" font-size="16" fill="{NIGHT["text"]}">
    <text x="84" y="454">{hatch_date}</text>
    <text x="290" y="454">&#8220;{wake_word}&#8221;</text>
    <text x="500" y="454" font-size="13">{hardware}</text>
  </g>
  <line x1="84" y1="486" x2="776" y2="486" stroke="{NIGHT["line"]}"/>

  <text x="84" y="546" font-family="{FONT_MONO}" font-size="11.5"
        letter-spacing="4" fill="{NIGHT["text3"]}">THE CREW</text>
  <text x="776" y="546" text-anchor="end" font-family="{FONT_MONO}"
        font-size="11.5" fill="{NIGHT["mint"]}">{len(crew)} AGENTS · ONE HOUSEHOLD</text>
  <line x1="84" y1="562" x2="776" y2="562" stroke="{NIGHT["line"]}"/>
{"".join(crew_rows)}

  {seal}

  <text x="84" y="1064" font-family="{FONT_BODY}" font-size="14"
        fill="{NIGHT["text2"]}">Everything this Puff is told stays in this box.</text>
  <text x="84" y="1088" font-family="{FONT_MONO}" font-size="10.5"
        letter-spacing="1.5" fill="{NIGHT["text3"]}">HATCHED {hatch_date} · {hardware_upper}</text>
  <text x="700" y="1104" text-anchor="middle" font-family="{FONT_MONO}"
        font-size="9.5" letter-spacing="2"
        fill="{NIGHT["text3"]}">PAW-SEALED · SHA-256</text>
</svg>
'''


# ── optional PNG (pure Pillow) ──────────────────────────────────────


def png_available() -> bool:
    """True when Pillow can render the PNG export (SVG needs nothing)."""
    try:
        import PIL.Image  # noqa: F401
        import PIL.ImageDraw  # noqa: F401
        return True
    except ImportError:
        return False


_FONT_CANDIDATES = {
    "display": ["fredoka.ttf", "seguisb.ttf", "segoeuib.ttf", "arialbd.ttf",
                "DejaVuSans-Bold.ttf"],
    "body": ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"],
    "mono": ["consola.ttf", "JetBrainsMono-Regular.ttf", "DejaVuSansMono.ttf"],
}


def _font(kind: str, size: int):
    from PIL import ImageFont
    for candidate in _FONT_CANDIDATES.get(kind, []):
        try:
            return ImageFont.truetype(candidate, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # very old Pillow
        return ImageFont.load_default()


def _rgba(color: str) -> tuple:
    if color.startswith("rgba"):
        parts = color[color.index("(") + 1:color.index(")")].split(",")
        r, g, b = (int(p) for p in parts[:3])
        a = int(float(parts[3]) * 255)
        return (r, g, b, a)
    color = color.lstrip("#")
    return tuple(int(color[i:i + 2], 16) for i in (0, 2, 4)) + (255,)


def render_png(data: dict, *, scale: int = 2) -> bytes | None:
    """The certificate as PNG bytes, or ``None`` when Pillow is missing.

    A faithful re-draw, not an SVG rasterization: cairosvg and renderPM are
    both import-broken on the reference dev box, so Pillow draws the same
    layout natively. Fonts fall back gracefully; the SVG stays the canonical
    artifact.
    """
    if not png_available():
        return None
    from PIL import Image, ImageDraw

    s = scale
    img = Image.new("RGB", (WIDTH * s, HEIGHT * s), _rgba(NIGHT["bg"])[:3])
    draw = ImageDraw.Draw(img, "RGBA")

    line = _rgba(NIGHT["line"])
    text = _rgba(NIGHT["text"])
    text2 = _rgba(NIGHT["text2"])
    text3 = _rgba(NIGHT["text3"])

    def rule(x1, y1, x2, y2, color=line, width=1):
        draw.line([(x1 * s, y1 * s), (x2 * s, y2 * s)], fill=color,
                  width=max(1, width * s // 2))

    # frame
    draw.rectangle([26 * s, 26 * s, (WIDTH - 26) * s, (HEIGHT - 26) * s],
                   outline=line, width=s)
    draw.rectangle([36 * s, 36 * s, (WIDTH - 36) * s, (HEIGHT - 36) * s],
                   outline=(255, 255, 255, 12), width=max(1, s // 2))

    # header
    draw.text((84 * s, 98 * s), "PUFFPAI — SOVEREIGN COMPANION",
              font=_font("mono", 12 * s), fill=text3)
    header = "CERTIFICATE OF ADOPTION"
    hw = draw.textlength(header, font=_font("mono", 12 * s))
    draw.text((776 * s - hw, 98 * s), header, font=_font("mono", 12 * s),
              fill=_rgba(NIGHT["lemon"]))
    rule(84, 126, 776, 126)

    draw.text((84 * s, 186 * s),
              "This certifies that the household adopted, named, and hatched",
              font=_font("body", 15 * s), fill=text2)
    draw.text((84 * s, 232 * s), data["name"],
              font=_font("display", 64 * s), fill=text)
    draw.rectangle([84 * s, 318 * s, 354 * s, 322 * s],
                   fill=_rgba(NIGHT["pink"]))
    if data.get("classing"):
        draw.text((84 * s, 340 * s), data["classing"].upper(),
                  font=_font("mono", 14 * s), fill=_rgba(NIGHT["sky"]))

    # meta
    for x, label, value in ((84, "HATCHED", data["hatch_date"]),
                            (290, "WAKE WORD", f'"{data["wake_word"]}"'),
                            (500, "HARDWARE", data["hardware"])):
        draw.text((x * s, 418 * s), label, font=_font("mono", 10 * s),
                  fill=text3)
        size = 13 if x == 500 else 16
        draw.text((x * s, 440 * s), value, font=_font("body", size * s),
                  fill=text)
    rule(84, 486, 776, 486)

    draw.text((84 * s, 536 * s), "THE CREW", font=_font("mono", 12 * s),
              fill=text3)
    crew = data.get("crew", [])
    tail = f"{len(crew)} AGENTS · ONE HOUSEHOLD"
    tw = draw.textlength(tail, font=_font("mono", 12 * s))
    draw.text((776 * s - tw, 536 * s), tail, font=_font("mono", 12 * s),
              fill=_rgba(NIGHT["mint"]))
    rule(84, 562, 776, 562)

    row_y = 620
    for member in crew:
        draw.text((84 * s, row_y * s), str(member.get("display_name", "")),
                  font=_font("display", 19 * s), fill=text)
        draw.text((84 * s, (row_y + 24) * s), str(member.get("specialty", "")),
                  font=_font("body", 12 * s), fill=text2)
        wf = str(member.get("workflow", ""))
        draw.rounded_rectangle(
            [706 * s, (row_y + 2) * s, 776 * s, (row_y + 22) * s],
            radius=9 * s, fill=_rgba(NIGHT["chip"]), outline=line)
        wfw = draw.textlength(wf, font=_font("mono", 10 * s))
        draw.text((741 * s - wfw / 2, (row_y + 6) * s), wf,
                  font=_font("mono", 10 * s), fill=_rgba(NIGHT["lav"]))
        rule(84, row_y + 44, 776, row_y + 44)
        row_y += 52

    # seal — drawn unrotated on its own layer, then rotated as one piece
    geo = seal_geometry(data["name"], data["hatch_date"])
    seal_size = 190 * s
    layer = Image.new("RGBA", (seal_size, seal_size), (0, 0, 0, 0))
    sdraw = ImageDraw.Draw(layer)
    center = seal_size // 2
    pts = []
    for i, radius in enumerate(geo["blob_radii"]):
        angle = math.radians(i * 36)
        pts.append((center + math.cos(angle) * radius * s,
                    center + math.sin(angle) * radius * s))
    sdraw.polygon(pts, fill=_rgba(NIGHT["magenta"]),
                  outline=_rgba(NIGHT["pink"]))
    pad = geo["pad"]
    bg = _rgba(NIGHT["bg"])[:3] + (216,)
    sdraw.ellipse([center + (pad["dx"] - pad["rx"]) * s,
                   center + (8 + pad["dy"] - pad["ry"]) * s,
                   center + (pad["dx"] + pad["rx"]) * s,
                   center + (8 + pad["dy"] + pad["ry"]) * s], fill=bg)
    for toe in geo["toes"]:
        rad = math.radians(toe["angle"] - 90)
        tx = center + math.cos(rad) * toe["dist"] * s
        ty = center + math.sin(rad) * toe["dist"] * s
        sdraw.ellipse([tx - toe["rx"] * s, ty - toe["ry"] * s,
                       tx + toe["rx"] * s, ty + toe["ry"] * s], fill=bg)
    layer = layer.rotate(-geo["rotation"], resample=Image.BICUBIC)
    img.paste(layer, (700 * s - seal_size // 2, 1022 * s - seal_size // 2),
              layer)

    draw.text((84 * s, 1050 * s),
              "Everything this Puff is told stays in this box.",
              font=_font("body", 14 * s), fill=text2)
    draw.text((84 * s, 1078 * s),
              f"HATCHED {data['hatch_date']} · {data['hardware'].upper()}",
              font=_font("mono", 10 * s), fill=text3)

    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def generate(*, fmt: str = "svg", **fields) -> bytes:
    """One-call generation: assemble live data, render *fmt*, return bytes."""
    data = certificate_data(**fields)
    if fmt == "png":
        png = render_png(data)
        if png is None:
            raise CertificateUnavailable(
                "PNG export needs Pillow, which is not installed — "
                "the SVG certificate is always available")
        return png
    return render_svg(data).encode("utf-8")


__all__ = [
    "NIGHT",
    "CertificateUnavailable",
    "certificate_data",
    "generate",
    "png_available",
    "render_png",
    "render_svg",
    "seal_geometry",
]
