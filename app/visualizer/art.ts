// シードから「作品の個性（トレイト）」を決定論的に生成する

export type Vec3 = [number, number, number];

export type Traits = {
  seed: string;
  form: number; // 0: Nebula, 1: Ripple, 2: Cells
  formName: string;
  symmetry: number; // 0 = 対称なし
  warp: number;
  paletteName: string;
  palette: { a: Vec3; b: Vec3; c: Vec3; d: Vec3 };
  offset: number;
  rootNote: number; // デモ音源のキー（MIDIノート番号）
  scale: number[];
  scaleName: string;
  bpm: number;
};

// 文字列 -> 32bit ハッシュ
function hashString(str: string): number {
  let h = 1779033703 ^ str.length;
  for (let i = 0; i < str.length; i++) {
    h = Math.imul(h ^ str.charCodeAt(i), 3432918353);
    h = (h << 13) | (h >>> 19);
  }
  return h >>> 0;
}

// シード付き乱数（mulberry32）
export function createRng(seed: string) {
  let a = hashString(seed);
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export function randomSeed(): string {
  return Math.floor(Math.random() * 36 ** 6)
    .toString(36)
    .padStart(6, "0");
}

// Inigo Quilez のコサインパレット: color(t) = a + b * cos(2π(c t + d))
const PALETTES: { name: string; a: Vec3; b: Vec3; c: Vec3; d: Vec3 }[] = [
  { name: "Ember", a: [0.5, 0.3, 0.2], b: [0.5, 0.35, 0.25], c: [1.0, 1.0, 1.0], d: [0.0, 0.1, 0.2] },
  { name: "Lagoon", a: [0.25, 0.45, 0.55], b: [0.35, 0.45, 0.45], c: [1.0, 1.0, 1.0], d: [0.3, 0.2, 0.2] },
  { name: "Aurora", a: [0.4, 0.5, 0.45], b: [0.4, 0.45, 0.4], c: [1.0, 1.0, 0.5], d: [0.8, 0.9, 0.3] },
  { name: "Neon", a: [0.5, 0.5, 0.5], b: [0.5, 0.5, 0.5], c: [2.0, 1.0, 0.0], d: [0.5, 0.2, 0.25] },
  { name: "Candy", a: [0.8, 0.5, 0.6], b: [0.2, 0.4, 0.3], c: [2.0, 1.0, 1.0], d: [0.0, 0.25, 0.25] },
  { name: "Moss", a: [0.35, 0.45, 0.25], b: [0.3, 0.35, 0.25], c: [1.0, 1.0, 1.0], d: [0.15, 0.2, 0.35] },
  { name: "Ultraviolet", a: [0.45, 0.3, 0.6], b: [0.45, 0.35, 0.4], c: [1.0, 1.0, 1.0], d: [0.6, 0.8, 0.9] },
  { name: "Solar", a: [0.6, 0.45, 0.25], b: [0.4, 0.4, 0.3], c: [1.0, 0.7, 0.4], d: [0.0, 0.15, 0.2] },
];

const FORMS = ["Nebula", "Ripple", "Cells"];
const SYMMETRIES = [0, 3, 4, 5, 6, 7, 8, 12];

const SCALES: { name: string; steps: number[] }[] = [
  { name: "Minor", steps: [0, 2, 3, 5, 7, 8, 10] },
  { name: "Dorian", steps: [0, 2, 3, 5, 7, 9, 10] },
  { name: "Pentatonic", steps: [0, 3, 5, 7, 10] },
  { name: "Lydian", steps: [0, 2, 4, 6, 7, 9, 11] },
];

export function generateTraits(seed: string): Traits {
  const rng = createRng(seed);
  const pick = <T,>(arr: T[]) => arr[Math.floor(rng() * arr.length)];
  const jitter = (v: Vec3, amt: number): Vec3 =>
    v.map((x) => x + (rng() - 0.5) * amt) as Vec3;

  const form = Math.floor(rng() * FORMS.length);
  const pal = pick(PALETTES);
  const scale = pick(SCALES);

  return {
    seed,
    form,
    formName: FORMS[form],
    symmetry: pick(SYMMETRIES),
    warp: 0.7 + rng() * 1.3,
    paletteName: pal.name,
    palette: { a: pal.a, b: pal.b, c: pal.c, d: jitter(pal.d, 0.15) },
    offset: rng() * 100,
    rootNote: 45 + Math.floor(rng() * 8), // A2〜E3
    scale: scale.steps,
    scaleName: scale.name,
    bpm: 108 + Math.floor(rng() * 24),
  };
}

export function traitLabel(t: Traits): string {
  const sym = t.symmetry === 0 ? "Free" : `${t.symmetry}-fold`;
  return `${t.formName} · ${sym} · ${t.paletteName}`;
}
