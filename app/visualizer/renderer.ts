// WebGL2 フルスクリーンシェーダーで「音 × 生成アート」を描画する

import { FFT_BINS } from "./audio";
import type { Traits } from "./art";

const VERT = `#version 300 es
in vec2 aPos;
void main() { gl_Position = vec4(aPos, 0.0, 1.0); }
`;

const FRAG = `#version 300 es
precision highp float;

uniform vec2 uRes;
uniform float uTime;
uniform float uBass, uMid, uTreble, uLevel, uBeat;
uniform float uRot, uHue;
uniform vec2 uMouse;
uniform sampler2D uFFT;

uniform int uForm;
uniform float uSym, uWarp, uOffset;
uniform vec3 uPa, uPb, uPc, uPd;

out vec4 outColor;

const float TAU = 6.28318530718;

float hash(vec2 p) {
  p = fract(p * vec2(123.34, 456.21));
  p += dot(p, p + 45.32);
  return fract(p.x * p.y);
}

float noise(vec2 p) {
  vec2 i = floor(p), f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(hash(i), hash(i + vec2(1, 0)), u.x),
             mix(hash(i + vec2(0, 1)), hash(i + vec2(1, 1)), u.x), u.y);
}

float fbm(vec2 p) {
  float v = 0.0, a = 0.5;
  mat2 m = mat2(1.6, 1.2, -1.2, 1.6);
  for (int i = 0; i < 5; i++) { v += a * noise(p); p = m * p; a *= 0.5; }
  return v;
}

// 戻り値: x = 最近点距離, y = 境界までの距離
vec2 voronoi(vec2 p, float t) {
  vec2 n = floor(p), f = fract(p);
  vec2 mg, mr;
  float md = 8.0;
  for (int j = -1; j <= 1; j++)
  for (int i = -1; i <= 1; i++) {
    vec2 g = vec2(i, j);
    vec2 o = vec2(hash(n + g), hash(n + g + 17.0));
    o = 0.5 + 0.5 * sin(t + TAU * o);
    vec2 r = g + o - f;
    float d = dot(r, r);
    if (d < md) { md = d; mr = r; mg = g; }
  }
  float edge = 8.0;
  for (int j = -2; j <= 2; j++)
  for (int i = -2; i <= 2; i++) {
    vec2 g = mg + vec2(i, j);
    vec2 o = vec2(hash(n + g), hash(n + g + 17.0));
    o = 0.5 + 0.5 * sin(t + TAU * o);
    vec2 r = g + o - f;
    if (dot(mr - r, mr - r) > 0.00001)
      edge = min(edge, dot(0.5 * (mr + r), normalize(r - mr)));
  }
  return vec2(sqrt(md), edge);
}

vec3 palette(float t) {
  return uPa + uPb * cos(TAU * (uPc * t + uPd));
}

void main() {
  vec2 uv = (gl_FragCoord.xy - 0.5 * uRes) / min(uRes.x, uRes.y);
  uv *= 1.0 - 0.12 * uBass; // 低音でズームパルス

  float r = length(uv);
  float a = atan(uv.y, uv.x) + uRot;
  float ringCoord; // 0..1 スペクトルリングの参照位置

  if (uSym > 0.5) {
    // 万華鏡: 角度を折り返す
    float seg = TAU / uSym;
    a = mod(a, seg);
    a = abs(a - 0.5 * seg);
    ringCoord = a / (0.5 * seg);
  } else {
    ringCoord = abs(fract(a / TAU) * 2.0 - 1.0);
  }
  vec2 p = vec2(cos(a), sin(a)) * r;

  float t = uTime * 0.08;
  vec2 sp = p * 2.2 + uOffset + uMouse * 0.4;

  // ドメインワープした fbm がベースの質感
  float w = uWarp * (1.0 + 1.6 * uBass);
  vec2 q = vec2(fbm(sp + t), fbm(sp + vec2(5.2, 1.3) - t));
  vec2 s = vec2(fbm(sp + w * q + vec2(1.7, 9.2) + 0.15 * t),
                fbm(sp + w * q + vec2(8.3, 2.8) - 0.12 * t));
  float n = fbm(sp + 2.0 * w * s);

  float shade = n;
  float tone = n + 0.6 * length(q) + uHue;

  if (uForm == 1) {
    // Ripple: ノイズで揺らいだ同心円
    float rip = sin(r * (20.0 + 10.0 * uMid) - uTime * 2.5 + n * 7.0 * uWarp);
    shade = mix(n, 0.5 + 0.5 * rip, 0.55);
    tone += 0.25 * rip;
  } else if (uForm == 2) {
    // Cells: ワープした空間のボロノイ
    vec2 v = voronoi(sp * 1.6 + 1.5 * s, uTime * 0.6 + uBass * 2.0);
    float edge = smoothstep(0.0, 0.06 + 0.05 * uBass, v.y);
    shade = mix(1.2, n * (1.0 - 0.6 * v.x), edge);
    tone += 0.4 * v.x;
  }

  vec3 col = palette(tone) * (0.25 + 1.1 * shade * shade);
  col *= 0.75 + 0.6 * uLevel;

  // スペクトルリング
  float amp = texture(uFFT, vec2(ringCoord * 0.85 + 0.02, 0.5)).r;
  float ringR = 0.28 + 0.05 * uBass + 0.22 * amp;
  float d = abs(r - ringR);
  col += palette(tone + 0.5) * (0.006 / (d + 0.006)) * (0.45 + 1.1 * amp);
  // 内側の光
  col += palette(uHue + 0.2) * smoothstep(ringR, 0.0, r) * 0.25 * uBass;

  // 高音できらめく粒子
  vec2 cell = floor(gl_FragCoord.xy / 3.0);
  float sparkle = step(1.0 - 0.012 * uTreble * uTreble, hash(cell + floor(uTime * 24.0)));
  col += sparkle * vec3(1.0) * uTreble;

  // ビートでフラッシュ
  col += uBeat * 0.18 * palette(uHue + 0.35);

  // ビネット + グレイン
  col *= smoothstep(1.35, 0.25, r);
  col += (hash(gl_FragCoord.xy + fract(uTime)) - 0.5) * 0.04;

  // トーンマップ
  col = col / (1.0 + col);
  col = pow(col, vec3(0.85));
  outColor = vec4(col, 1.0);
}
`;

export type FrameUniforms = {
  time: number;
  bass: number;
  mid: number;
  treble: number;
  level: number;
  beat: number;
  rot: number;
  hue: number;
  mouse: [number, number];
  spectrum: Uint8Array;
};

function compile(gl: WebGL2RenderingContext, type: number, src: string) {
  const sh = gl.createShader(type)!;
  gl.shaderSource(sh, src);
  gl.compileShader(sh);
  if (!gl.getShaderParameter(sh, gl.COMPILE_STATUS)) {
    throw new Error(gl.getShaderInfoLog(sh) ?? "shader compile error");
  }
  return sh;
}

export class Renderer {
  readonly canvas: HTMLCanvasElement;
  private gl: WebGL2RenderingContext;
  private loc: Record<string, WebGLUniformLocation | null> = {};
  private fftTex: WebGLTexture;

  constructor(canvas: HTMLCanvasElement) {
    this.canvas = canvas;
    const gl = canvas.getContext("webgl2", { antialias: false, alpha: false });
    if (!gl) throw new Error("WebGL2 に対応していないブラウザです");
    this.gl = gl;

    const prog = gl.createProgram()!;
    gl.attachShader(prog, compile(gl, gl.VERTEX_SHADER, VERT));
    gl.attachShader(prog, compile(gl, gl.FRAGMENT_SHADER, FRAG));
    gl.linkProgram(prog);
    if (!gl.getProgramParameter(prog, gl.LINK_STATUS)) {
      throw new Error(gl.getProgramInfoLog(prog) ?? "program link error");
    }
    gl.useProgram(prog);

    const buf = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buf);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    const aPos = gl.getAttribLocation(prog, "aPos");
    gl.enableVertexAttribArray(aPos);
    gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);

    const names = [
      "uRes", "uTime", "uBass", "uMid", "uTreble", "uLevel", "uBeat", "uRot", "uHue", "uMouse",
      "uFFT", "uForm", "uSym", "uWarp", "uOffset", "uPa", "uPb", "uPc", "uPd",
    ];
    for (const n of names) this.loc[n] = gl.getUniformLocation(prog, n);

    this.fftTex = gl.createTexture()!;
    gl.activeTexture(gl.TEXTURE0);
    gl.bindTexture(gl.TEXTURE_2D, this.fftTex);
    gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8, FFT_BINS, 1, 0, gl.RED, gl.UNSIGNED_BYTE, new Uint8Array(FFT_BINS));
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.LINEAR);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    gl.uniform1i(this.loc.uFFT, 0);
  }

  setTraits(t: Traits) {
    const gl = this.gl;
    gl.uniform1i(this.loc.uForm, t.form);
    gl.uniform1f(this.loc.uSym, t.symmetry);
    gl.uniform1f(this.loc.uWarp, t.warp);
    gl.uniform1f(this.loc.uOffset, t.offset);
    gl.uniform3fv(this.loc.uPa, t.palette.a);
    gl.uniform3fv(this.loc.uPb, t.palette.b);
    gl.uniform3fv(this.loc.uPc, t.palette.c);
    gl.uniform3fv(this.loc.uPd, t.palette.d);
  }

  resize(scale: number) {
    const w = Math.floor(this.canvas.clientWidth * scale);
    const h = Math.floor(this.canvas.clientHeight * scale);
    if (this.canvas.width !== w || this.canvas.height !== h) {
      this.canvas.width = w;
      this.canvas.height = h;
      this.gl.viewport(0, 0, w, h);
    }
  }

  draw(u: FrameUniforms) {
    const gl = this.gl;
    gl.uniform2f(this.loc.uRes, this.canvas.width, this.canvas.height);
    gl.uniform1f(this.loc.uTime, u.time);
    gl.uniform1f(this.loc.uBass, u.bass);
    gl.uniform1f(this.loc.uMid, u.mid);
    gl.uniform1f(this.loc.uTreble, u.treble);
    gl.uniform1f(this.loc.uLevel, u.level);
    gl.uniform1f(this.loc.uBeat, u.beat);
    gl.uniform1f(this.loc.uRot, u.rot);
    gl.uniform1f(this.loc.uHue, u.hue);
    gl.uniform2f(this.loc.uMouse, u.mouse[0], u.mouse[1]);
    gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, FFT_BINS, 1, gl.RED, gl.UNSIGNED_BYTE, u.spectrum);
    gl.drawArrays(gl.TRIANGLES, 0, 3);
  }
}
