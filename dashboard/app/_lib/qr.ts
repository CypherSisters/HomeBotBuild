/**
 * A small, dependency-free QR Code encoder (ISO/IEC 18004).
 *
 * Written here rather than pulled from npm because this appliance is the whole
 * point of the product: it is an offline box, and a code that renders a QR on
 * the wall must not reach the network, ship a CDN tag, or add a supply-chain
 * dependency to do it. The structure follows the well-known public-domain /
 * MIT reference designs (Nayuki's in particular): segment, error correction,
 * matrix, mask, and it is deliberately written to be re-used (device pairing
 * will want a QR next).
 *
 * Scope: byte mode only, versions 1 to 10, all four error-correction levels.
 * Byte mode covers URLs, which is every use we have; versions above 10 are
 * rejected loudly rather than silently mis-encoded, because the block tables
 * beyond that range are not exercised by any test here. That is a deliberate
 * limit, not an oversight.
 *
 * Correctness is asserted by scripts/check-qr.js: the error-correction
 * codewords are checked to have zero Reed-Solomon syndromes (the mathematical
 * definition of a valid codeword), and an INDEPENDENT decoder in that test
 * reads the finished matrix back (format info, unmask, zigzag, de-interleave,
 * segment parse) and must recover the original string.
 */

export type QrEcc = "L" | "M" | "Q" | "H";

export interface QrCode {
  /** Module count per side, 17 + 4 * version. Excludes the quiet zone. */
  size: number;
  /** modules[y][x], true = dark. */
  modules: boolean[][];
  version: number;
  ecc: QrEcc;
  /** Which of the eight mask patterns won on penalty score. */
  mask: number;
}

/* ═══════════════ published tables (ISO/IEC 18004) ═══════════════ */

/** Two-bit field written into the format information. */
const ECC_FORMAT_BITS: Record<QrEcc, number> = { L: 1, M: 0, Q: 3, H: 2 };
const ECC_ORDER: QrEcc[] = ["L", "M", "Q", "H"];

/** Error-correction codewords per block, [ecc][version]. Version 1 at index 1. */
const ECC_CODEWORDS_PER_BLOCK: Record<QrEcc, number[]> = {
  L: [0, 7, 10, 15, 20, 26, 18, 20, 24, 30, 18],
  M: [0, 10, 16, 26, 18, 24, 16, 18, 22, 22, 26],
  Q: [0, 13, 22, 18, 26, 18, 24, 18, 22, 20, 24],
  H: [0, 17, 28, 22, 16, 22, 28, 26, 26, 24, 28],
};

/** Number of error-correction blocks, [ecc][version]. */
const NUM_BLOCKS: Record<QrEcc, number[]> = {
  L: [0, 1, 1, 1, 1, 1, 2, 2, 2, 2, 4],
  M: [0, 1, 1, 1, 2, 2, 4, 4, 4, 5, 5],
  Q: [0, 1, 1, 2, 2, 4, 4, 6, 6, 8, 8],
  H: [0, 1, 1, 2, 4, 4, 4, 5, 6, 8, 8],
};

export const MAX_VERSION = 10;

/* ═══════════════ geometry ═══════════════ */

/**
 * Total data + error-correction modules for a version, derived from the
 * geometry rather than tabulated: the square, minus the three finder patterns
 * and separators, minus timing, alignment and format/version areas.
 */
function rawDataModules(version: number): number {
  let result = (16 * version + 128) * version + 64;
  if (version >= 2) {
    const numAlign = Math.floor(version / 7) + 2;
    result -= (25 * numAlign - 10) * numAlign - 55;
    if (version >= 7) result -= 36;
  }
  return result;
}

/** Total codewords (data + ecc) a version holds. */
function totalCodewords(version: number): number {
  return Math.floor(rawDataModules(version) / 8);
}

/** Data codewords available at a version and level. */
export function dataCodewords(version: number, ecc: QrEcc): number {
  return (
    totalCodewords(version) - ECC_CODEWORDS_PER_BLOCK[ecc][version] * NUM_BLOCKS[ecc][version]
  );
}

/** Centre coordinates of the alignment patterns for a version. */
function alignmentPatternPositions(version: number): number[] {
  if (version === 1) return [];
  const numAlign = Math.floor(version / 7) + 2;
  const step = Math.ceil((version * 4 + 4) / (numAlign * 2 - 2)) * 2;
  const result: number[] = [6];
  for (let pos = version * 4 + 10; result.length < numAlign; pos -= step) result.splice(1, 0, pos);
  return result;
}

/** Bits used by the character-count field in byte mode. */
function charCountBits(version: number): number {
  return version <= 9 ? 8 : 16;
}

/* ═══════════════ GF(256) and Reed-Solomon ═══════════════ */

/** Multiply in GF(256) modulo the QR primitive polynomial x^8+x^4+x^3+x^2+1. */
function gfMul(a: number, b: number): number {
  let z = 0;
  for (let i = 7; i >= 0; i--) {
    z = (z << 1) ^ ((z >>> 7) * 0x11d);
    z ^= ((b >>> i) & 1) * a;
  }
  return z & 0xff;
}

/** Divisor polynomial for `degree` error-correction codewords. */
export function rsGeneratorPoly(degree: number): number[] {
  const result = new Array<number>(degree).fill(0);
  result[degree - 1] = 1;
  let root = 1;
  for (let i = 0; i < degree; i++) {
    for (let j = 0; j < degree; j++) {
      result[j] = gfMul(result[j], root);
      if (j + 1 < degree) result[j] ^= result[j + 1];
    }
    root = gfMul(root, 0x02);
  }
  return result;
}

/** Remainder of data divided by the generator: the error-correction codewords. */
export function rsRemainder(data: number[], degree: number): number[] {
  const generator = rsGeneratorPoly(degree);
  const result = new Array<number>(degree).fill(0);
  for (const b of data) {
    const factor = b ^ (result.shift() as number);
    result.push(0);
    for (let i = 0; i < degree; i++) result[i] ^= gfMul(generator[i], factor);
  }
  return result;
}

/* ═══════════════ bit buffer ═══════════════ */

function appendBits(bits: number[], value: number, len: number): void {
  for (let i = len - 1; i >= 0; i--) bits.push((value >>> i) & 1);
}

/** UTF-8 bytes of a string, so non-ASCII URLs encode correctly. */
export function utf8Bytes(text: string): number[] {
  const out: number[] = [];
  for (const ch of text) {
    let cp = ch.codePointAt(0) as number;
    if (cp < 0x80) out.push(cp);
    else if (cp < 0x800) out.push(0xc0 | (cp >> 6), 0x80 | (cp & 0x3f));
    else if (cp < 0x10000) out.push(0xe0 | (cp >> 12), 0x80 | ((cp >> 6) & 0x3f), 0x80 | (cp & 0x3f));
    else {
      out.push(
        0xf0 | (cp >> 18),
        0x80 | ((cp >> 12) & 0x3f),
        0x80 | ((cp >> 6) & 0x3f),
        0x80 | (cp & 0x3f),
      );
    }
    cp = 0;
  }
  return out;
}

/* ═══════════════ encoding ═══════════════ */

/** Smallest version that fits `byteLen` bytes at this level, or null. */
export function chooseVersion(byteLen: number, ecc: QrEcc): number | null {
  for (let v = 1; v <= MAX_VERSION; v++) {
    const capacityBits = dataCodewords(v, ecc) * 8;
    const needed = 4 + charCountBits(v) + byteLen * 8;
    if (needed <= capacityBits) return v;
  }
  return null;
}

/** Data codewords: segment header, payload, terminator, padding. */
function buildDataCodewords(bytes: number[], version: number, ecc: QrEcc): number[] {
  const capacity = dataCodewords(version, ecc);
  const bits: number[] = [];
  appendBits(bits, 0b0100, 4); // byte mode
  appendBits(bits, bytes.length, charCountBits(version));
  for (const b of bytes) appendBits(bits, b, 8);

  const capacityBits = capacity * 8;
  appendBits(bits, 0, Math.min(4, capacityBits - bits.length)); // terminator
  while (bits.length % 8 !== 0) bits.push(0);

  const words: number[] = [];
  for (let i = 0; i < bits.length; i += 8) {
    let b = 0;
    for (let j = 0; j < 8; j++) b = (b << 1) | bits[i + j];
    words.push(b);
  }
  // Alternating pad bytes, per the standard.
  for (let pad = 0xec; words.length < capacity; pad ^= 0xec ^ 0x11) words.push(pad);
  return words;
}

/** Split into blocks, add error correction, interleave. */
function interleaveBlocks(data: number[], version: number, ecc: QrEcc): number[] {
  const numBlocks = NUM_BLOCKS[ecc][version];
  const eccLen = ECC_CODEWORDS_PER_BLOCK[ecc][version];
  const rawCodewords = totalCodewords(version);
  const numShort = numBlocks - (rawCodewords % numBlocks);
  const shortLen = Math.floor(rawCodewords / numBlocks) - eccLen;

  const blocks: number[][] = [];
  const eccBlocks: number[][] = [];
  for (let i = 0, k = 0; i < numBlocks; i++) {
    const len = shortLen + (i < numShort ? 0 : 1);
    const block = data.slice(k, k + len);
    k += len;
    blocks.push(block);
    eccBlocks.push(rsRemainder(block, eccLen));
  }

  const out: number[] = [];
  for (let i = 0; i < shortLen + 1; i++) {
    for (let b = 0; b < numBlocks; b++) if (i < blocks[b].length) out.push(blocks[b][i]);
  }
  for (let i = 0; i < eccLen; i++) {
    for (let b = 0; b < numBlocks; b++) out.push(eccBlocks[b][i]);
  }
  return out;
}

/* ═══════════════ matrix ═══════════════ */

type Grid = { modules: boolean[][]; reserved: boolean[][]; size: number };

function newGrid(size: number): Grid {
  const modules = Array.from({ length: size }, () => new Array<boolean>(size).fill(false));
  const reserved = Array.from({ length: size }, () => new Array<boolean>(size).fill(false));
  return { modules, reserved, size };
}

function setFunction(g: Grid, x: number, y: number, dark: boolean): void {
  if (x < 0 || y < 0 || x >= g.size || y >= g.size) return;
  g.modules[y][x] = dark;
  g.reserved[y][x] = true;
}

function drawFinder(g: Grid, cx: number, cy: number): void {
  for (let dy = -4; dy <= 4; dy++) {
    for (let dx = -4; dx <= 4; dx++) {
      const dist = Math.max(Math.abs(dx), Math.abs(dy));
      setFunction(g, cx + dx, cy + dy, dist !== 2 && dist !== 4);
    }
  }
}

function drawAlignment(g: Grid, cx: number, cy: number): void {
  for (let dy = -2; dy <= 2; dy++) {
    for (let dx = -2; dx <= 2; dx++) {
      setFunction(g, cx + dx, cy + dy, Math.max(Math.abs(dx), Math.abs(dy)) !== 1);
    }
  }
}

/** BCH(15,5) format information, masked with the published 0x5412. */
export function formatBits(ecc: QrEcc, mask: number): number {
  const data = (ECC_FORMAT_BITS[ecc] << 3) | mask;
  let rem = data;
  for (let i = 0; i < 10; i++) rem = (rem << 1) ^ ((rem >>> 9) * 0x537);
  return (((data << 10) | rem) ^ 0x5412) & 0x7fff;
}

/** BCH(18,6) version information, versions 7 and up. */
export function versionBits(version: number): number {
  let rem = version;
  for (let i = 0; i < 12; i++) rem = (rem << 1) ^ ((rem >>> 11) * 0x1f25);
  return ((version << 12) | rem) & 0x3ffff;
}

function drawFunctionPatterns(g: Grid, version: number): void {
  const size = g.size;
  // Timing patterns.
  for (let i = 0; i < size; i++) {
    setFunction(g, 6, i, i % 2 === 0);
    setFunction(g, i, 6, i % 2 === 0);
  }
  drawFinder(g, 3, 3);
  drawFinder(g, size - 4, 3);
  drawFinder(g, 3, size - 4);

  const align = alignmentPatternPositions(version);
  for (let i = 0; i < align.length; i++) {
    for (let j = 0; j < align.length; j++) {
      // The three finder corners have no alignment pattern.
      const corner =
        (i === 0 && j === 0) ||
        (i === 0 && j === align.length - 1) ||
        (i === align.length - 1 && j === 0);
      if (!corner) drawAlignment(g, align[i], align[j]);
    }
  }

  // Reserve the format areas (filled in once the mask is chosen).
  //
  // Index 6 is skipped in both directions: (6,8) and (8,6) belong to the
  // timing patterns, which run underneath the format band and are NOT part of
  // it. Writing the reservation over them breaks timing continuity, which is
  // one of the first things a scanner locks onto. Both modules are already
  // reserved by the timing pass above, so skipping only preserves their value.
  for (let i = 0; i < 9; i++) {
    if (i === 6) continue;
    setFunction(g, i, 8, false);
    setFunction(g, 8, i, false);
  }
  for (let i = 0; i < 8; i++) {
    setFunction(g, size - 1 - i, 8, false);
    setFunction(g, 8, size - 1 - i, false);
  }
  setFunction(g, 8, size - 8, true); // the always-dark module

  if (version >= 7) {
    const bits = versionBits(version);
    for (let i = 0; i < 18; i++) {
      const bit = ((bits >>> i) & 1) === 1;
      const a = size - 11 + (i % 3);
      const b = Math.floor(i / 3);
      setFunction(g, a, b, bit);
      setFunction(g, b, a, bit);
    }
  }
}

function drawFormatBits(g: Grid, ecc: QrEcc, mask: number): void {
  const bits = formatBits(ecc, mask);
  const size = g.size;
  for (let i = 0; i <= 5; i++) setFunction(g, 8, i, ((bits >>> i) & 1) === 1);
  setFunction(g, 8, 7, ((bits >>> 6) & 1) === 1);
  setFunction(g, 8, 8, ((bits >>> 7) & 1) === 1);
  setFunction(g, 7, 8, ((bits >>> 8) & 1) === 1);
  for (let i = 9; i < 15; i++) setFunction(g, 14 - i, 8, ((bits >>> i) & 1) === 1);

  for (let i = 0; i < 8; i++) setFunction(g, size - 1 - i, 8, ((bits >>> i) & 1) === 1);
  for (let i = 8; i < 15; i++) setFunction(g, 8, size - 15 + i, ((bits >>> i) & 1) === 1);
  setFunction(g, 8, size - 8, true);
}

/** Zigzag placement, two columns at a time, upward then downward. */
function drawCodewords(g: Grid, codewords: number[]): void {
  const size = g.size;
  let i = 0;
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5; // the vertical timing column is skipped
    for (let vert = 0; vert < size; vert++) {
      for (let j = 0; j < 2; j++) {
        const x = right - j;
        const upward = ((right + 1) & 2) === 0;
        const y = upward ? size - 1 - vert : vert;
        if (!g.reserved[y][x] && i < codewords.length * 8) {
          g.modules[y][x] = ((codewords[i >>> 3] >>> (7 - (i & 7))) & 1) === 1;
          i++;
        }
      }
    }
  }
}

/** The eight published mask predicates. */
export function maskPredicate(mask: number, x: number, y: number): boolean {
  switch (mask) {
    case 0: return (x + y) % 2 === 0;
    case 1: return y % 2 === 0;
    case 2: return x % 3 === 0;
    case 3: return (x + y) % 3 === 0;
    case 4: return (Math.floor(x / 3) + Math.floor(y / 2)) % 2 === 0;
    case 5: return ((x * y) % 2) + ((x * y) % 3) === 0;
    case 6: return (((x * y) % 2) + ((x * y) % 3)) % 2 === 0;
    case 7: return (((x + y) % 2) + ((x * y) % 3)) % 2 === 0;
    default: throw new Error(`bad mask ${mask}`);
  }
}

function applyMask(g: Grid, mask: number): void {
  for (let y = 0; y < g.size; y++) {
    for (let x = 0; x < g.size; x++) {
      if (!g.reserved[y][x] && maskPredicate(mask, x, y)) g.modules[y][x] = !g.modules[y][x];
    }
  }
}

/** The four published penalty rules. Lower is better. */
function penaltyScore(g: Grid): number {
  const size = g.size;
  const m = g.modules;
  let score = 0;

  const runScore = (run: number) => (run >= 5 ? 3 + (run - 5) : 0);

  for (let y = 0; y < size; y++) {
    let run = 1;
    for (let x = 1; x < size; x++) {
      if (m[y][x] === m[y][x - 1]) run++;
      else {
        score += runScore(run);
        run = 1;
      }
    }
    score += runScore(run);
  }
  for (let x = 0; x < size; x++) {
    let run = 1;
    for (let y = 1; y < size; y++) {
      if (m[y][x] === m[y - 1][x]) run++;
      else {
        score += runScore(run);
        run = 1;
      }
    }
    score += runScore(run);
  }

  for (let y = 0; y < size - 1; y++) {
    for (let x = 0; x < size - 1; x++) {
      const c = m[y][x];
      if (c === m[y][x + 1] && c === m[y + 1][x] && c === m[y + 1][x + 1]) score += 3;
    }
  }

  // 1:1:3:1:1 finder-like pattern with four light modules on one side.
  const finder = [true, false, true, true, true, false, true];
  const matchAt = (get: (i: number) => boolean, i: number, len: number): boolean => {
    for (let k = 0; k < 7; k++) if (get(i + k) !== finder[k]) return false;
    const before = () => {
      for (let k = 1; k <= 4; k++) if (i - k < 0 || get(i - k)) return false;
      return true;
    };
    const after = () => {
      for (let k = 0; k < 4; k++) if (i + 7 + k >= len || get(i + 7 + k)) return false;
      return true;
    };
    return before() || after();
  };
  for (let y = 0; y < size; y++) {
    for (let x = 0; x + 7 <= size; x++) if (matchAt((i) => m[y][i], x, size)) score += 40;
  }
  for (let x = 0; x < size; x++) {
    for (let y = 0; y + 7 <= size; y++) if (matchAt((i) => m[i][x], y, size)) score += 40;
  }

  let dark = 0;
  for (let y = 0; y < size; y++) for (let x = 0; x < size; x++) if (m[y][x]) dark++;
  const total = size * size;
  const k = Math.floor((Math.abs(dark * 20 - total * 10) + total - 1) / total) - 1;
  score += Math.max(0, k) * 10;
  return score;
}

/**
 * Encode `text` as a QR code. Throws when it will not fit, rather than
 * returning something that looks like a code and is not one.
 */
export function encodeQr(
  text: string,
  options: { ecc?: QrEcc; minVersion?: number } = {},
): QrCode {
  const ecc = options.ecc ?? "M";
  const bytes = utf8Bytes(text);
  let version = chooseVersion(bytes.length, ecc);
  if (version == null) {
    throw new Error(
      `QR: ${bytes.length} bytes does not fit in version ${MAX_VERSION} at level ${ecc}`,
    );
  }
  if (options.minVersion && options.minVersion > version) {
    version = Math.min(options.minVersion, MAX_VERSION);
  }

  const data = buildDataCodewords(bytes, version, ecc);
  const codewords = interleaveBlocks(data, version, ecc);

  const size = version * 4 + 17;
  const g = newGrid(size);
  drawFunctionPatterns(g, version);
  drawCodewords(g, codewords);

  // Try every mask, keep the lowest penalty. Format bits are drawn each time
  // because the penalty is scored on the finished symbol.
  let bestMask = 0;
  let bestScore = Infinity;
  const dataOnly = g.modules.map((row) => row.slice());
  for (let mask = 0; mask < 8; mask++) {
    g.modules = dataOnly.map((row) => row.slice());
    drawFormatBits(g, ecc, mask);
    applyMask(g, mask);
    const score = penaltyScore(g);
    if (score < bestScore) {
      bestScore = score;
      bestMask = mask;
    }
  }
  g.modules = dataOnly.map((row) => row.slice());
  drawFormatBits(g, ecc, bestMask);
  applyMask(g, bestMask);

  return { size, modules: g.modules, version, ecc, mask: bestMask };
}

/* ═══════════════ rendering ═══════════════ */

/** SVG path data for the dark modules, in module units. */
export function qrPathData(qr: QrCode): string {
  const parts: string[] = [];
  for (let y = 0; y < qr.size; y++) {
    for (let x = 0; x < qr.size; x++) {
      if (qr.modules[y][x]) parts.push(`M${x} ${y}h1v1h-1z`);
    }
  }
  return parts.join("");
}

/**
 * A complete, self-contained SVG string. The quiet zone defaults to the four
 * modules the standard requires; scanners need it and it is the single most
 * common reason a hand-rolled QR does not read.
 */
export function qrToSvg(
  qr: QrCode,
  options: { quietZone?: number; dark?: string; light?: string; title?: string } = {},
): string {
  const quiet = options.quietZone ?? 4;
  const dim = qr.size + quiet * 2;
  const dark = options.dark ?? "#000000";
  const light = options.light ?? "#ffffff";
  const title = options.title ? `<title>${options.title}</title>` : "";
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${dim} ${dim}" shape-rendering="crispEdges">` +
    title +
    `<rect width="${dim}" height="${dim}" fill="${light}"/>` +
    `<g transform="translate(${quiet} ${quiet})" fill="${dark}">` +
    `<path d="${qrPathData(qr)}"/>` +
    `</g></svg>`
  );
}
