// 音声入力（デモ音源 / マイク / 音声ファイル）を解析して帯域ごとの強さとビートを返す

import type { Traits } from "./art";

export const FFT_BINS = 128;

export type AudioFrame = {
  bass: number;
  mid: number;
  treble: number;
  level: number;
  beat: boolean;
  spectrum: Uint8Array; // FFT_BINS 個、対数スケールに並べ替え済み
};

export type SourceKind = "demo" | "mic" | "file";

export class AudioEngine {
  private ctx: AudioContext;
  private analyser: AnalyserNode;
  private raw: Uint8Array<ArrayBuffer>;
  private spectrum = new Uint8Array(FFT_BINS);
  private binMap: number[] = [];
  private smooth = { bass: 0, mid: 0, treble: 0, level: 0 };
  private bassAvg = 0;
  private lastBeat = 0;
  private cleanup: (() => void) | null = null;
  kind: SourceKind | null = null;

  constructor() {
    this.ctx = new AudioContext();
    this.analyser = this.ctx.createAnalyser();
    this.analyser.fftSize = 2048;
    this.analyser.smoothingTimeConstant = 0.75;
    this.raw = new Uint8Array(this.analyser.frequencyBinCount);

    // 20Hz〜16kHz を対数的に FFT_BINS 個へ割り当てる
    const nyquist = this.ctx.sampleRate / 2;
    const n = this.analyser.frequencyBinCount;
    for (let i = 0; i <= FFT_BINS; i++) {
      const hz = 20 * Math.pow(16000 / 20, i / FFT_BINS);
      this.binMap.push(Math.min(n - 1, Math.round((hz / nyquist) * n)));
    }
  }

  private bandEnergy(fromHz: number, toHz: number): number {
    const n = this.raw.length;
    const nyquist = this.ctx.sampleRate / 2;
    const a = Math.max(1, Math.floor((fromHz / nyquist) * n));
    const b = Math.min(n - 1, Math.ceil((toHz / nyquist) * n));
    let sum = 0;
    for (let i = a; i <= b; i++) sum += this.raw[i];
    return sum / ((b - a + 1) * 255);
  }

  analyse(now: number): AudioFrame {
    this.analyser.getByteFrequencyData(this.raw);

    for (let i = 0; i < FFT_BINS; i++) {
      const a = this.binMap[i];
      const b = Math.max(a, this.binMap[i + 1] - 1);
      let m = 0;
      for (let j = a; j <= b; j++) m = Math.max(m, this.raw[j]);
      this.spectrum[i] = m;
    }

    const bands = {
      bass: this.bandEnergy(20, 150),
      mid: this.bandEnergy(150, 2000),
      treble: this.bandEnergy(2000, 10000) * 1.6,
      level: this.bandEnergy(20, 10000),
    };

    // 立ち上がりは速く、減衰はゆっくり
    for (const key of Object.keys(bands) as (keyof typeof bands)[]) {
      const target = Math.min(1, bands[key]);
      const k = target > this.smooth[key] ? 0.5 : 0.08;
      this.smooth[key] += (target - this.smooth[key]) * k;
    }

    // 低音が直近平均を大きく上回ったらビート
    const beat =
      bands.bass > this.bassAvg * 1.25 &&
      bands.bass > 0.3 &&
      now - this.lastBeat > 280;
    if (beat) this.lastBeat = now;
    this.bassAvg += (bands.bass - this.bassAvg) * 0.05;

    return { ...this.smooth, beat, spectrum: this.spectrum };
  }

  private async switchTo(kind: SourceKind, setup: () => Promise<() => void>) {
    this.stop();
    await this.ctx.resume();
    this.cleanup = await setup();
    this.kind = kind;
  }

  stop() {
    this.cleanup?.();
    this.cleanup = null;
    this.kind = null;
  }

  useMic() {
    return this.switchTo("mic", async () => {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: false, noiseSuppression: false, autoGainControl: true },
      });
      const src = this.ctx.createMediaStreamSource(stream);
      src.connect(this.analyser); // ハウリング防止のためスピーカーには出さない
      return () => {
        src.disconnect();
        stream.getTracks().forEach((t) => t.stop());
      };
    });
  }

  useFile(file: File) {
    return this.switchTo("file", async () => {
      const url = URL.createObjectURL(file);
      const el = new Audio(url);
      el.loop = true;
      const src = this.ctx.createMediaElementSource(el);
      src.connect(this.analyser);
      this.analyser.connect(this.ctx.destination);
      await el.play();
      return () => {
        el.pause();
        src.disconnect();
        this.analyser.disconnect();
        URL.revokeObjectURL(url);
      };
    });
  }

  useDemo(traits: Traits) {
    return this.switchTo("demo", async () => {
      const synth = new DemoSynth(this.ctx, traits);
      synth.out.connect(this.analyser);
      this.analyser.connect(this.ctx.destination);
      synth.start();
      return () => {
        synth.stop();
        this.analyser.disconnect();
      };
    });
  }

  close() {
    this.stop();
    this.ctx.close();
  }
}

// 作品のシードから曲のキー・スケール・テンポが決まる小さなシーケンサー
class DemoSynth {
  out: GainNode;
  private ctx: AudioContext;
  private traits: Traits;
  private timer: ReturnType<typeof setInterval> | null = null;
  private step = 0;
  private nextTime = 0;
  private noise: AudioBuffer;
  private delay: DelayNode;

  constructor(ctx: AudioContext, traits: Traits) {
    this.ctx = ctx;
    this.traits = traits;
    this.out = ctx.createGain();
    this.out.gain.value = 0.55;

    // ハイハット用ホワイトノイズ
    this.noise = ctx.createBuffer(1, ctx.sampleRate, ctx.sampleRate);
    const data = this.noise.getChannelData(0);
    for (let i = 0; i < data.length; i++) data[i] = Math.random() * 2 - 1;

    // リード用のフィードバックディレイ
    this.delay = ctx.createDelay(1);
    this.delay.delayTime.value = (60 / traits.bpm) * 0.75;
    const fb = ctx.createGain();
    fb.gain.value = 0.35;
    this.delay.connect(fb).connect(this.delay);
    this.delay.connect(this.out);
  }

  private freq(degree: number, octave = 0) {
    const { scale, rootNote } = this.traits;
    const len = scale.length;
    const oct = Math.floor(degree / len) + octave;
    const note = rootNote + scale[((degree % len) + len) % len] + oct * 12;
    return 440 * Math.pow(2, (note - 69) / 12);
  }

  private kick(t: number) {
    const o = this.ctx.createOscillator();
    const g = this.ctx.createGain();
    o.frequency.setValueAtTime(140, t);
    o.frequency.exponentialRampToValueAtTime(42, t + 0.12);
    g.gain.setValueAtTime(1, t);
    g.gain.exponentialRampToValueAtTime(0.001, t + 0.4);
    o.connect(g).connect(this.out);
    o.start(t);
    o.stop(t + 0.42);
  }

  private hat(t: number, open: boolean) {
    const s = this.ctx.createBufferSource();
    s.buffer = this.noise;
    const f = this.ctx.createBiquadFilter();
    f.type = "highpass";
    f.frequency.value = 7000;
    const g = this.ctx.createGain();
    const len = open ? 0.18 : 0.04;
    g.gain.setValueAtTime(open ? 0.22 : 0.15, t);
    g.gain.exponentialRampToValueAtTime(0.001, t + len);
    s.connect(f).connect(g).connect(this.out);
    s.start(t);
    s.stop(t + len + 0.01);
  }

  private tone(t: number, hz: number, dur: number, opts: { type: OscillatorType; gain: number; cutoff: number; send?: boolean }) {
    const o = this.ctx.createOscillator();
    o.type = opts.type;
    o.frequency.value = hz;
    const f = this.ctx.createBiquadFilter();
    f.type = "lowpass";
    f.frequency.setValueAtTime(opts.cutoff * 3, t);
    f.frequency.exponentialRampToValueAtTime(opts.cutoff, t + dur);
    f.Q.value = 6;
    const g = this.ctx.createGain();
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(opts.gain, t + 0.01);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    o.connect(f).connect(g).connect(this.out);
    if (opts.send) g.connect(this.delay);
    o.start(t);
    o.stop(t + dur + 0.02);
  }

  private schedule(step: number, t: number) {
    const s = step % 16;
    const bar = Math.floor(step / 16) % 4;
    const chord = [0, 5, 3, 4][bar]; // i - VI - iv - v 風の進行
    const sixteenth = 60 / this.traits.bpm / 4;

    if (s % 4 === 0) this.kick(t);
    if (s % 2 === 1) this.hat(t, s % 8 === 7);

    if ([0, 3, 6, 10, 11, 14].includes(s)) {
      this.tone(t, this.freq(chord, -1), sixteenth * 1.8, { type: "sawtooth", gain: 0.28, cutoff: 260 });
    }
    if (s === 0) {
      for (const d of [0, 2, 4]) {
        this.tone(t, this.freq(chord + d, 1), sixteenth * 15, { type: "triangle", gain: 0.06, cutoff: 1400 });
      }
    }
    // 小節ごとに変わるアルペジオ
    const arp = [0, 2, 4, 7, 4, 2, 7, 9];
    if (s % 2 === 0 && (bar % 2 === 1 || s < 8)) {
      this.tone(t, this.freq(chord + arp[(s / 2 + bar) % arp.length], 2), sixteenth * 1.5, {
        type: "square",
        gain: 0.05,
        cutoff: 2200,
        send: true,
      });
    }
  }

  start() {
    const sixteenth = 60 / this.traits.bpm / 4;
    this.nextTime = this.ctx.currentTime + 0.05;
    this.timer = setInterval(() => {
      while (this.nextTime < this.ctx.currentTime + 0.12) {
        this.schedule(this.step++, this.nextTime);
        this.nextTime += sixteenth;
      }
    }, 25);
  }

  stop() {
    if (this.timer) clearInterval(this.timer);
    this.out.disconnect();
    this.delay.disconnect();
  }
}
