// Waterfall renderer, WebGL2 (WebGL1 fallback is not offered: iOS 15+, Android Chrome and desktop all have WebGL2).
// One R8 texture per tile (cols x bins, ring buffer in x); a column upload is texSubImage2D of 1 x bins bytes. The colour table is a 256x1 texture
// and the display range is a uniform, so changing range or colormap touches no per-pixel data. Same interface as wf-canvas2d.js.
import { levelTable } from "./colormaps.js";

const VS = `#version 300 es
in vec2 p; out vec2 uv; uniform vec4 rect; // x0,y0,x1,y1 in clip space
void main() { uv = p; vec2 q = mix(rect.xy, rect.zw, p); gl_Position = vec4(q, 0.0, 1.0); }`;
const FS = `#version 300 es
precision mediump float; in vec2 uv; out vec4 o; uniform sampler2D data; uniform sampler2D lut; uniform float head; uniform float cols;
void main() {
  float x = fract(uv.x + (head + 1.0) / cols);           // newest column at the right edge
  float v = texture(data, vec2(x, uv.y)).r;               // band 0 at the bottom (uv.y = 0 is the bottom row of the tile)
  o = vec4(texture(lut, vec2(v * (255.0 / 256.0) + 0.5 / 256.0, 0.5)).rgb, 1.0);
}`;

function sh(gl, type, src) { const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s); if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s)); return s; }

export function create(canvas, o) {
  const gl = canvas.getContext("webgl2", { alpha: false, antialias: false, powerPreference: "low-power", preserveDrawingBuffer: false });
  if (!gl) throw new Error("webgl2 unavailable");
  const pr = gl.createProgram(); gl.attachShader(pr, sh(gl, gl.VERTEX_SHADER, VS)); gl.attachShader(pr, sh(gl, gl.FRAGMENT_SHADER, FS)); gl.linkProgram(pr);
  if (!gl.getProgramParameter(pr, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(pr));
  gl.useProgram(pr);
  const buf = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, buf); gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([0, 0, 1, 0, 0, 1, 1, 1]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(pr, "p"); gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  const U = n => gl.getUniformLocation(pr, n);
  const lutTex = gl.createTexture();
  function setLut(cmap, lo, hi) {
    gl.activeTexture(gl.TEXTURE1); gl.bindTexture(gl.TEXTURE_2D, lutTex);
    const t = levelTable(cmap || "viridis", lo, hi);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, 256, 1, 0, gl.RGBA, gl.UNSIGNED_BYTE, t);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
  }
  setLut(o.cmap, o.lo ?? -90, o.hi ?? -30);
  gl.uniform1i(U("data"), 0); gl.uniform1i(U("lut"), 1); gl.uniform1f(U("cols"), o.cols);
  gl.pixelStorei(gl.UNPACK_ALIGNMENT, 1);
  const T = o.tiles.map(() => {
    const t = gl.createTexture(); gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, t);
    gl.texImage2D(gl.TEXTURE_2D, 0, gl.R8, o.cols, o.bins, 0, gl.RED, gl.UNSIGNED_BYTE, null);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MIN_FILTER, gl.NEAREST); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_MAG_FILTER, gl.NEAREST);
    gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_S, gl.REPEAT); gl.texParameteri(gl.TEXTURE_2D, gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE);
    return { tex: t, head: 0 };
  });
  return {
    name: "webgl2",
    push(i, u8) { const t = T[i]; t.head = (t.head + 1) % o.cols; gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, t.tex); gl.texSubImage2D(gl.TEXTURE_2D, 0, t.head, 0, 1, o.bins, gl.RED, gl.UNSIGNED_BYTE, u8); },
    draw() {
      gl.viewport(0, 0, canvas.width, canvas.height);
      const W = canvas.width, H = canvas.height;
      for (let i = 0; i < T.length; i++) {
        const r = o.tiles[i];
        gl.uniform4f(U("rect"), 2 * r.x / W - 1, 1 - 2 * (r.y + r.h) / H, 2 * (r.x + r.w) / W - 1, 1 - 2 * r.y / H);
        const t = T[r.src ?? i];
        gl.uniform1f(U("head"), t.head);
        gl.activeTexture(gl.TEXTURE0); gl.bindTexture(gl.TEXTURE_2D, t.tex);
        gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
      }
    },
    setRange(lo, hi, cmap) { setLut(cmap, lo, hi); },
    setSource(i, src) { o.tiles[i].src = src; },
    destroy() { const e = gl.getExtension("WEBGL_lose_context"); if (e) e.loseContext(); },
  };
}
