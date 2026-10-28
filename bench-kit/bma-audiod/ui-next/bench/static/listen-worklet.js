// AudioWorklet player for the live listen stream: signed 16-bit mono PCM chunks in, a small jitter buffer, linear resampling to the context rate.
// Messages in: ArrayBuffer of PCM16 (little endian). The buffer holds at most 1 s; older audio is dropped so a stalled tab never builds latency.
class PcmPlayer extends AudioWorkletProcessor {
  constructor(opts) {
    super();
    this.inRate = opts.processorOptions.inRate; this.buf = new Float32Array(this.inRate); this.r = 0; this.w = 0; this.n = 0; this.pos = 0; this.primed = false;
    this.port.onmessage = e => {
      const s = new Int16Array(e.data);
      for (let i = 0; i < s.length; i++) { this.buf[this.w] = s[i] / 32768; this.w = (this.w + 1) % this.buf.length; if (this.n < this.buf.length) this.n++; else this.r = (this.r + 1) % this.buf.length; }
      if (!this.primed && this.n >= this.inRate * 0.2) this.primed = true;                          // 200 ms before the first sample plays
    };
  }
  process(_in, out) {
    const o = out[0][0], step = this.inRate / sampleRate;
    for (let i = 0; i < o.length; i++) {
      if (!this.primed || this.n < 2) { o[i] = 0; if (this.n < 2) this.primed = false; continue; }
      const a = this.buf[this.r], b = this.buf[(this.r + 1) % this.buf.length], f = this.pos;
      o[i] = a + (b - a) * f; this.pos += step;
      while (this.pos >= 1 && this.n > 1) { this.pos -= 1; this.r = (this.r + 1) % this.buf.length; this.n--; }
    }
    for (let c = 1; c < out[0].length; c++) out[0][c].set(o);
    return true;
  }
}
registerProcessor("pcm-player", PcmPlayer);
