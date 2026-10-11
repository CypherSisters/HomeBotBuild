/**
 * PuffPAI Avatar Generator
 * ------------------------
 * Uses DiceBear to generate an avatar and outputs PNG (base64) to stdout.
 * Called by avatar_generator.py via subprocess.
 *
 * Usage:
 *   node generate_avatar.js <style> <seed> [backgroundColor] [size]
 *
 * Styles: adventurer, bottts, personas, avataaars, lorelei, notionists,
 *         open-peeps, pixel-art, thumbs
 *
 * Output: base64-encoded PNG written to stdout.
 *
 * Example:
 *   node generate_avatar.js adventurer Puff b6e3f4 128
 */

const { createAvatar } = require('@dicebear/core');
const collection = require('@dicebear/collection');
const { Resvg } = require('@resvg/resvg-js');

const args = process.argv.slice(2);
const style = args[0] || 'adventurer';
const seed  = args[1] || 'PuffPAI';
const bg    = args[2] || 'b6e3f4';
const size  = parseInt(args[3], 10) || 128;

const styleMap = {
  'adventurer':  collection.adventurer,
  'bottts':      collection.bottts,
  'personas':    collection.personas,
  'avataaars':   collection.avataaars,
  'lorelei':     collection.lorelei,
  'notionists':  collection.notionists,
  'open-peeps':  collection['openPeeps'] || collection['open-peeps'],
  'pixel-art':   collection['pixelArt']  || collection['pixel-art'],
  'thumbs':      collection.thumbs,
};

const avatarStyle = styleMap[style] || collection.adventurer;

if (!avatarStyle) {
  process.stderr.write(`Unknown style "${style}". Available: ${Object.keys(styleMap).join(', ')}\n`);
  process.exit(1);
}

try {
  const avatar = createAvatar(avatarStyle, {
    seed: seed,
    backgroundColor: [bg],
  });

  const svgString = avatar.toString();

  // Convert SVG → PNG using resvg (Rust-based, no system deps)
  const resvg = new Resvg(svgString, {
    fitTo: { mode: 'width', value: size },
  });
  const pngData = resvg.render();
  const pngBuffer = pngData.asPng();

  // Output as base64 so Python can decode it
  process.stdout.write(pngBuffer.toString('base64'));
} catch (err) {
  process.stderr.write(`Error generating avatar: ${err.message}\n`);
  process.exit(1);
}
