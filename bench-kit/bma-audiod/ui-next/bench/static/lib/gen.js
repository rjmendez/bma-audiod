// Deterministic synthetic spectrum columns (uint8 levels, 0.5 dB steps from -100 dB) for benchmarks and the mock client. No audio, no recordings.
export function mulberry(seed) { let a = seed >>> 0; return () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1); t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }

/** Column generator: noise floor near -85 dB, three steady lines, one slow sweep and a burst every few seconds that is strongest on one beam. */
export function makeGen(bins, beams, seed = 7) {
  const rnd = mulberry(seed), lines = [0.12, 0.38, 0.77].map(f => Math.floor(f * bins));
  let k = 0;
  return function next(beam) {
    const col = new Uint8Array(bins), t = k / 10, burst = ((Math.floor(t / 4) % beams) === beam) && (t % 4 < 0.8);
    const sweep = Math.floor(((t * 0.07) % 1) * bins);
    for (let b = 0; b < bins; b++) {
      let v = 30 + 6 * rnd() + (beam * 0.7);
      if (b === lines[0] || b === lines[1] || b === lines[2]) v += 28;
      if (Math.abs(b - sweep) < 2) v += 40;
      if (burst && b > bins * 0.45 && b < bins * 0.8) v += 55 * Math.max(0, 1 - Math.abs(b - bins * 0.62) / (bins * 0.2));
      col[b] = v > 255 ? 255 : v | 0;
    }
    if (beam === beams - 1) k++;
    return col;
  };
}
