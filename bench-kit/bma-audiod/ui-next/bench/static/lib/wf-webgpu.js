// Waterfall renderer, WebGPU. Same interface and layout as wf-webgl.js: one r8unorm texture per tile written a column at a time (queue.writeTexture),
// the colour table is a 256x1 rgba8 texture, range changes rewrite only that table. create() is async (adapter + device request) and throws when
// there is no adapter: callers fall back to WebGL2.
import { levelTable } from "./colormaps.js";

const WGSL = `
struct U { rect: vec4f, head: f32, cols: f32, pad: vec2f };
@group(0) @binding(0) var<uniform> u: U;
@group(0) @binding(1) var data: texture_2d<f32>;
@group(0) @binding(2) var lut: texture_2d<f32>;
struct V { @builtin(position) pos: vec4f, @location(0) uv: vec2f };
@vertex fn vs(@builtin(vertex_index) i: u32) -> V {
  var p = array<vec2f, 4>(vec2f(0, 0), vec2f(1, 0), vec2f(0, 1), vec2f(1, 1));
  let q = mix(u.rect.xy, u.rect.zw, p[i]);
  var o: V; o.pos = vec4f(q, 0, 1); o.uv = p[i]; return o;
}
@fragment fn fs(v: V) -> @location(0) vec4f {
  let x = fract(v.uv.x + (u.head + 1.0) / u.cols);
  let dims = vec2f(textureDimensions(data));
  let val = textureLoad(data, vec2i(i32(floor(x * dims.x)), i32(floor(v.uv.y * dims.y))), 0).r;
  let idx = i32(round(val * 255.0));
  return vec4f(textureLoad(lut, vec2i(idx, 0), 0).rgb, 1.0);
}`;

export async function create(canvas, o) {
  if (!navigator.gpu) throw new Error("webgpu unavailable");
  const ad = await navigator.gpu.requestAdapter({ powerPreference: "low-power" });
  if (!ad) throw new Error("no webgpu adapter");
  const dev = await ad.requestDevice();
  const ctx = canvas.getContext("webgpu"), fmt = navigator.gpu.getPreferredCanvasFormat();
  ctx.configure({ device: dev, format: fmt, alphaMode: "opaque" });
  const mod = dev.createShaderModule({ code: WGSL });
  const pipe = dev.createRenderPipeline({ layout: "auto", vertex: { module: mod, entryPoint: "vs" }, fragment: { module: mod, entryPoint: "fs", targets: [{ format: fmt }] }, primitive: { topology: "triangle-strip" } });
  const lutTex = dev.createTexture({ size: [256, 1], format: "rgba8unorm", usage: GPUTextureUsage.TEXTURE_BINDING | GPUTextureUsage.COPY_DST });
  const setLut = (cmap, lo, hi) => dev.queue.writeTexture({ texture: lutTex }, levelTable(cmap || "viridis", lo, hi), { bytesPerRow: 1024 }, [256, 1]);
  setLut(o.cmap, o.lo ?? -90, o.hi ?? -30);
  const T = o.tiles.map(() => {
    const tex = dev.createTexture({ size: [o.cols, o.bins], format: "r8unorm", usage: GPUTextureUsage.TEXTURE_BINDING | GPUTextureUsage.COPY_DST });
    const ub = dev.createBuffer({ size: 32, usage: GPUBufferUsage.UNIFORM | GPUBufferUsage.COPY_DST });
    const bg = dev.createBindGroup({ layout: pipe.getBindGroupLayout(0), entries: [{ binding: 0, resource: { buffer: ub } }, { binding: 1, resource: tex.createView() }, { binding: 2, resource: lutTex.createView() }] });
    return { tex, ub, bg, head: 0 };
  });
  const bindFor = (i, s) => dev.createBindGroup({ layout: pipe.getBindGroupLayout(0), entries: [{ binding: 0, resource: { buffer: T[i].ub } }, { binding: 1, resource: T[s].tex.createView() }, { binding: 2, resource: lutTex.createView() }] });
  const col = new Uint8Array(o.bins);
  return {
    name: "webgpu",
    push(i, u8) {
      const t = T[i]; t.head = (t.head + 1) % o.cols;
      col.set(u8); dev.queue.writeTexture({ texture: t.tex, origin: [t.head, 0] }, col, { bytesPerRow: 1 }, [1, o.bins]);   // queue.writeTexture has no 256-byte row alignment (only buffer copies do)
    },
    draw() {
      const W = canvas.width, H = canvas.height, enc = dev.createCommandEncoder();
      const pass = enc.beginRenderPass({ colorAttachments: [{ view: ctx.getCurrentTexture().createView(), loadOp: "clear", storeOp: "store", clearValue: { r: 0, g: 0, b: 0, a: 1 } }] });
      pass.setPipeline(pipe);
      for (let i = 0; i < T.length; i++) {
        const r = o.tiles[i];
        dev.queue.writeBuffer(T[i].ub, 0, new Float32Array([2 * r.x / W - 1, 1 - 2 * (r.y + r.h) / H, 2 * (r.x + r.w) / W - 1, 1 - 2 * r.y / H, T[r.src ?? i].head, o.cols, 0, 0]));
        pass.setBindGroup(0, T[i].bg); pass.draw(4);
      }
      pass.end(); dev.queue.submit([enc.finish()]);
    },
    setRange(lo, hi, cmap) { setLut(cmap, lo, hi); },
    setSource(i, src) { o.tiles[i].src = src; T[i].bg = bindFor(i, src); },
    destroy() { dev.destroy(); },
  };
}
