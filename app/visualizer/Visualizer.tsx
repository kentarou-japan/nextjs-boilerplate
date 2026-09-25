"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { generateTraits, randomSeed, traitLabel, type Traits } from "./art";
import { AudioEngine, FFT_BINS, type AudioFrame, type SourceKind } from "./audio";
import { Renderer } from "./renderer";

const SOURCE_LABEL: Record<SourceKind, string> = {
  demo: "デモ音源",
  mic: "マイク",
  file: "音声ファイル",
};

// 音声が無いときのゆるやかな疑似入力
function idleFrame(t: number, spectrum: Uint8Array): AudioFrame {
  for (let i = 0; i < FFT_BINS; i++) {
    spectrum[i] = 40 + 30 * Math.sin(t * 1.3 + i * 0.25) * Math.sin(t * 0.4 + i * 0.05);
  }
  return {
    bass: 0.25 + 0.15 * Math.sin(t * 1.1),
    mid: 0.2 + 0.1 * Math.sin(t * 0.7),
    treble: 0.1,
    level: 0.3,
    beat: false,
    spectrum,
  };
}

export default function Visualizer() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);
  const rendererRef = useRef<Renderer | null>(null);
  const engineRef = useRef<AudioEngine | null>(null);
  const traitsRef = useRef<Traits | null>(null);
  const mouseRef = useRef<[number, number]>([0, 0]);
  const saveRef = useRef(false);

  const [traits, setTraits] = useState<Traits | null>(null);
  const [source, setSource] = useState<SourceKind | null>(null);
  const [uiVisible, setUiVisible] = useState(true);
  const [toast, setToast] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const flash = (msg: string) => {
    setToast(msg);
    setTimeout(() => setToast((m) => (m === msg ? null : m)), 2000);
  };

  const applySeed = useCallback((seed: string) => {
    const t = generateTraits(seed);
    traitsRef.current = t;
    setTraits(t);
    rendererRef.current?.setTraits(t);
    const url = new URL(window.location.href);
    url.searchParams.set("seed", seed);
    window.history.replaceState(null, "", url);
    // デモ再生中なら曲も新しい作品に合わせて作り直す
    const engine = engineRef.current;
    if (engine?.kind === "demo") engine.useDemo(t);
  }, []);

  // 初期化 + 描画ループ
  useEffect(() => {
    const canvas = canvasRef.current!;
    let renderer: Renderer;
    try {
      renderer = new Renderer(canvas);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      return;
    }
    rendererRef.current = renderer;

    const initial = new URLSearchParams(window.location.search).get("seed") || randomSeed();
    applySeed(initial);

    const spectrum = new Uint8Array(FFT_BINS);
    const scale = Math.min(window.devicePixelRatio || 1, 1.5);
    let rot = 0;
    let hue = 0;
    let hueTarget = 0;
    let beat = 0;
    let last = performance.now();
    let raf = 0;

    const loop = (now: number) => {
      const dt = Math.min(0.05, (now - last) / 1000);
      last = now;
      const time = now / 1000;
      const engine = engineRef.current;
      const f = engine?.kind ? engine.analyse(now) : idleFrame(time, spectrum);

      if (f.beat) {
        beat = 1;
        hueTarget += 0.04 + f.bass * 0.06;
      }
      beat *= Math.pow(0.02, dt);
      hue += (hueTarget - hue) * Math.min(1, dt * 3);
      rot += dt * (0.04 + f.mid * 0.5);

      renderer.resize(scale);
      renderer.draw({
        time,
        bass: f.bass,
        mid: f.mid,
        treble: f.treble,
        level: f.level,
        beat,
        rot,
        hue,
        mouse: mouseRef.current,
        spectrum: f.spectrum,
      });

      if (saveRef.current) {
        saveRef.current = false;
        exportPng(canvas, traitsRef.current);
      }
      raf = requestAnimationFrame(loop);
    };
    raf = requestAnimationFrame(loop);

    return () => {
      cancelAnimationFrame(raf);
      engineRef.current?.close();
      engineRef.current = null;
    };
  }, [applySeed]);

  // 操作が止まったら UI を隠す
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout>;
    const wake = () => {
      setUiVisible(true);
      clearTimeout(timer);
      timer = setTimeout(() => setUiVisible(false), 3500);
    };
    const onMove = (e: PointerEvent) => {
      mouseRef.current = [e.clientX / window.innerWidth - 0.5, 0.5 - e.clientY / window.innerHeight];
      wake();
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerdown", wake);
    wake();
    return () => {
      clearTimeout(timer);
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerdown", wake);
    };
  }, []);

  const startSource = async (kind: SourceKind, file?: File) => {
    setError(null);
    try {
      engineRef.current ??= new AudioEngine();
      const engine = engineRef.current;
      if (kind === "demo") await engine.useDemo(traitsRef.current!);
      if (kind === "mic") await engine.useMic();
      if (kind === "file" && file) await engine.useFile(file);
      setSource(kind);
    } catch (e) {
      setSource(null);
      setError(
        kind === "mic"
          ? "マイクを使えませんでした。ブラウザの許可設定を確認してください。"
          : `音声を開始できませんでした: ${e instanceof Error ? e.message : e}`,
      );
    }
  };

  const stopSource = () => {
    engineRef.current?.stop();
    setSource(null);
  };

  const newSeed = useCallback(() => applySeed(randomSeed()), [applySeed]);

  const save = useCallback(() => {
    saveRef.current = true;
    flash("PNG を保存しました");
  }, []);

  const share = async () => {
    try {
      await navigator.clipboard.writeText(window.location.href);
      flash("この作品のリンクをコピーしました");
    } catch {
      flash(window.location.href);
    }
  };

  const toggleFullscreen = useCallback(() => {
    if (document.fullscreenElement) document.exitFullscreen();
    else document.documentElement.requestFullscreen?.();
  }, []);

  // キーボードショートカット
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.target instanceof HTMLInputElement) return;
      if (e.code === "Space") {
        e.preventDefault();
        newSeed();
      } else if (e.key === "s") save();
      else if (e.key === "f") toggleFullscreen();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [newSeed, save, toggleFullscreen]);

  const btn =
    "rounded-full border border-white/20 bg-white/10 px-4 py-2 text-sm text-white backdrop-blur-md transition hover:bg-white/25 active:scale-95";
  const btnActive = "rounded-full border border-white bg-white px-4 py-2 text-sm text-black transition active:scale-95";

  return (
    <div className={`fixed inset-0 overflow-hidden bg-black text-white ${uiVisible ? "" : "cursor-none"}`}>
      <canvas ref={canvasRef} className="absolute inset-0 h-full w-full" />

      <div
        className={`pointer-events-none absolute inset-0 flex flex-col justify-between p-4 transition-opacity duration-700 sm:p-8 ${
          uiVisible || !source ? "opacity-100" : "opacity-0"
        }`}
      >
        {/* 作品タイトル */}
        <header className="pointer-events-auto">
          {traits && (
            <>
              <p className="font-mono text-xs tracking-[0.3em] text-white/60">No. {traits.seed.toUpperCase()}</p>
              <h1 className="mt-1 text-2xl font-light tracking-wide drop-shadow sm:text-4xl">{traitLabel(traits)}</h1>
              <p className="mt-1 font-mono text-xs text-white/50">
                warp {traits.warp.toFixed(2)} · {traits.scaleName} · {traits.bpm} BPM
              </p>
            </>
          )}
        </header>

        {/* 開始前のイントロ */}
        {!source && !error && (
          <div className="pointer-events-auto mx-auto max-w-md rounded-3xl border border-white/15 bg-black/40 p-6 text-center backdrop-blur-xl">
            <h2 className="text-lg font-medium">音で描く、あなただけの一枚</h2>
            <p className="mt-2 text-sm leading-relaxed text-white/70">
              訪れるたびに違う作品が生まれ、音に合わせて呼吸します。
              デモ音源の曲も作品ごとにキーとテンポが変わります。
            </p>
            <div className="mt-5 flex flex-wrap justify-center gap-2">
              <button className={btnActive} onClick={() => startSource("demo")}>
                ▶ デモ音源で再生
              </button>
              <button className={btn} onClick={() => startSource("mic")}>
                🎤 マイク
              </button>
              <button className={btn} onClick={() => fileRef.current?.click()}>
                ♫ 音声ファイル
              </button>
            </div>
          </div>
        )}

        {error && (
          <div className="pointer-events-auto mx-auto max-w-md rounded-2xl bg-red-500/80 px-5 py-3 text-sm backdrop-blur">
            {error}
          </div>
        )}

        {/* 操作バー */}
        <footer className="pointer-events-auto flex flex-wrap items-center gap-2">
          {source && (
            <>
              {(Object.keys(SOURCE_LABEL) as SourceKind[]).map((k) => (
                <button
                  key={k}
                  className={source === k ? btnActive : btn}
                  onClick={() => (k === "file" ? fileRef.current?.click() : startSource(k))}
                >
                  {SOURCE_LABEL[k]}
                </button>
              ))}
              <button className={btn} onClick={stopSource}>
                ■ 停止
              </button>
              <span className="mx-1 hidden h-5 w-px bg-white/20 sm:block" />
            </>
          )}
          <button className={btn} onClick={newSeed} title="Space">
            ✦ 新しい作品
          </button>
          <button className={btn} onClick={save} title="S">
            保存
          </button>
          <button className={btn} onClick={share}>
            共有リンク
          </button>
          <button className={btn} onClick={toggleFullscreen} title="F">
            全画面
          </button>
          <span className="ml-auto hidden font-mono text-xs text-white/40 md:block">
            Space 新しい作品 · S 保存 · F 全画面
          </span>
        </footer>
      </div>

      {toast && (
        <div className="absolute left-1/2 top-6 -translate-x-1/2 rounded-full bg-white/90 px-4 py-2 text-sm text-black shadow-lg">
          {toast}
        </div>
      )}

      <input
        ref={fileRef}
        type="file"
        accept="audio/*"
        className="hidden"
        onChange={(e) => {
          const file = e.target.files?.[0];
          if (file) startSource("file", file);
          e.target.value = "";
        }}
      />
    </div>
  );
}

// 描画直後のフレームに署名を入れて PNG として書き出す
function exportPng(canvas: HTMLCanvasElement, traits: Traits | null) {
  const out = document.createElement("canvas");
  out.width = canvas.width;
  out.height = canvas.height;
  const ctx = out.getContext("2d")!;
  ctx.drawImage(canvas, 0, 0);
  if (traits) {
    const size = Math.max(12, Math.round(out.height / 60));
    ctx.font = `${size}px ui-monospace, monospace`;
    ctx.fillStyle = "rgba(255,255,255,0.75)";
    ctx.textBaseline = "bottom";
    ctx.fillText(`No. ${traits.seed.toUpperCase()} — ${traitLabel(traits)}`, size * 1.5, out.height - size * 1.5);
  }
  out.toBlob((blob) => {
    if (!blob) return;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `sound-art-${traits?.seed ?? "untitled"}.png`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  });
}
