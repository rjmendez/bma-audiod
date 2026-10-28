# Open choices (owner decides; the code does not decide law)

## Live listening

Behaviour in this change: off until a visible consent step; one listener; 10-minute session; red LISTENING pill with countdown and tab-title prefix; stops after 30 s in a hidden tab; the server keeps no copy (200 ms jitter buffer in the browser only); Freeze keeps 6 s in tab memory. Pennsylvania is an all-party-consent state: listening to a live feed is an interception question, so every row below defaults to the narrowest option.

| # | Choice | Options | Default |
|---|---|---|---|
| 1 | Feature present at all | off / on | **Off**: registry row `--live-listen`, Approved only when the owner says yes |
| 2 | Who may listen | any signed-in session / passkey only | signed-in session; passkey where enabled; never a tailnet identity header alone |
| 3 | Speech while listening | no guard / mute when the latest tagger window has speech_p >= 0.10 or is stale / forbid | **mute on speech or stale tagger** (the window is up to 5 s old, so the first seconds can pass: say so or forbid) |
| 4 | Server-side copy of live audio | none / rolling buffer | **none** |
| 5 | Keep-this-moment | none / request a clip through the clip pipeline (speech guard, purge, 24 h) | through the clip pipeline only; needs an analyzer endpoint |
| 6 | Browser-side recording or download of live audio | off / on | **off** (no button) |
| 7 | Clip download from Review | off / on | **off** |
| 8 | Audit trail | none / local log of start, stop, user, beam (no audio) | **on**, same rotation as other logs |
| 9 | Concurrent listeners | 1 / many | **1** |
| 10 | Hidden tab | keep playing / stop after N s | **stop after 30 s** |
| 11 | Consent wording | generic / names the place | names the place; the owner approves the text |
| 12 | Session length | 10 min / longer | **10 min**, new consent to continue |

## Other

| # | Choice | Default |
|---|---|---|
| 13 | Verdict `evidence` block as contract verdict-labels 1.2.0 (DATA-MODEL.md) | apply in phase 5 |
| 14 | Review blind by default (claim hidden until a verdict) | on; a checkbox reveals |
| 15 | WebGPU renderer | not used; WebGL2 with Canvas2D fallback measured sufficient (STREAMING.md) |
| 16 | Retire `ui/app.js` | after parity (MIGRATION.md phase 4) and the owner's acceptance |
