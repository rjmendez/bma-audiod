/* ui-lab AudioWorklet processors (loaded with audioWorklet.addModule). Original code.
   'pcmring' : live PCM in, N channels out. The main thread posts {frames, data} (planar Float32: channel 0 frames, channel 1 frames, ...);
               a ring buffer of `capacity` frames absorbs jitter; playback starts after `prebuffer` frames; each underrun rebuffers with a x1.5 wider
               prebuffer (up to maxPrebuffer, default 500 ms). Posts {fill, under, over, pre} about once a second.
   'freqdiv' : frequency division (the bat-detector divide-by-N): the output square wave flips every N/2 positive zero crossings, so its frequency is the input's divided by N (Schmitt trigger,
               threshold 0.3 x the input envelope) and takes the input's envelope as its amplitude; parameter `div` (k-rate). */
class PcmRing extends AudioWorkletProcessor {
  constructor(o) {
    super();
    const p = (o && o.processorOptions) || {};
    this.ch = p.channels || 8; this.cap = p.capacity || 48000; this.pre = p.prebuffer || 5760; this.maxPre = p.maxPrebuffer || 24000;
    this.buf = []; for (let c = 0; c < this.ch; c++) this.buf.push(new Float32Array(this.cap));
    this.r = 0; this.w = 0; this.fill = 0; this.playing = false; this.under = 0; this.over = 0; this.tick = 0;
    this.port.onmessage = (e) => {
      const m = e.data; if (!m || !m.data) return; const n = m.frames, d = m.data;
      for (let c = 0; c < this.ch; c++) { const b = this.buf[c]; let w = this.w; for (let i = 0; i < n; i++) { b[w] = d[c * n + i]; w = (w + 1) % this.cap; } }
      this.w = (this.w + n) % this.cap; this.fill += n;
      if (this.fill > this.cap) { const drop = this.fill - this.cap; this.r = (this.r + drop) % this.cap; this.fill = this.cap; this.over += drop; }
    };
  }
  process(inputs, outputs) {
    const out = outputs[0], n = out[0].length;
    if (!this.playing && this.fill >= this.pre) this.playing = true;
    if (this.playing && this.fill < n) { this.playing = false; this.under++; this.pre = Math.min(this.maxPre, Math.round(this.pre * 1.5)); }     // widen the jitter buffer after every underrun
    if (this.playing) {
      for (let c = 0; c < Math.min(this.ch, out.length); c++) { const b = this.buf[c], o = out[c]; let r = this.r; for (let i = 0; i < n; i++) { o[i] = b[r]; r = (r + 1) % this.cap; } }
      this.r = (this.r + n) % this.cap; this.fill -= n;
    }
    this.tick += n;
    if (this.tick >= sampleRate) { this.tick = 0; this.port.postMessage({ fill: this.fill, under: this.under, over: this.over, pre: this.pre }); }
    return true;
  }
}
registerProcessor('pcmring', PcmRing);

class FreqDiv extends AudioWorkletProcessor {
  static get parameterDescriptors() { return [{ name: 'div', defaultValue: 8, minValue: 1, maxValue: 64, automationRate: 'k-rate' }]; }
  constructor() { super(); this.env = 0; this.armed = false; this.count = 0; this.sign = 1; }
  process(inputs, outputs, params) {
    const inp = inputs[0] && inputs[0][0], out = outputs[0][0];
    if (!inp) { out.fill(0); return true; }
    const N = Math.max(1, Math.round(params.div[0]));
    for (let i = 0; i < out.length; i++) {
      const x = inp[i], a = Math.abs(x);
      this.env += (a - this.env) * 0.02;                           // symmetric one-pole mean of |x| (an attack faster than the release would bias it high)
      const thr = 0.3 * this.env;
      if (x < -thr) this.armed = true;
      else if (this.armed && x > thr) { this.armed = false; this.count += 2; if (this.count >= N) { this.count -= N; this.sign = -this.sign; } }   // flips every N/2 crossings: output = input / N
      out[i] = this.sign * this.env * 1.5708;                  // pi/2: a sine of amplitude A has mean |x| = 2A/pi, so the square wave reads A
    }
    return true;
  }
}
registerProcessor('freqdiv', FreqDiv);
