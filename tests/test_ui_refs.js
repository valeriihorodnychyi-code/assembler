// Every function the UI CALLS must be DEFINED somewhere in web/index.html — offline check.
// Run from the repo root:   node tests/test_ui_refs.js
// Catches the 1.9.93 regression: removing "Apply trim" also removed ensureSessionClip(), which
// Cut segment, Transcribe-with-trim and auto-detect still call — the page loaded fine and only
// the button press failed ("ensureSessionClip is not defined").
const fs = require("fs"), path = require("path");
const html = fs.readFileSync(path.join(__dirname, "..", "web", "index.html"), "utf8");
const raw = [...html.matchAll(/<script(?![^>]*src)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]).join("\n");
// calls are read from code with strings removed FIRST (so "//" inside a URL isn't a comment), then comments
const js = raw.replace(/"(?:[^"\\\n]|\\.)*"|'(?:[^'\\\n]|\\.)*'|`(?:[^`\\]|\\.)*`/g, '""').replace(/\/\/[^\n]*/g, "");
const defined = new Set();
for (const m of raw.matchAll(/function\s+([A-Za-z_$][\w$]*)/g)) defined.add(m[1]);
for (const m of raw.matchAll(/(?:const|let|var)\s+([A-Za-z_$][\w$]*)/g)) defined.add(m[1]);
for (const m of raw.matchAll(/,\s*([A-Za-z_$][\w$]*)\s*=(?!=)/g)) defined.add(m[1]);   // let a=1, b=2
for (const m of js.matchAll(/function\s+([A-Za-z_$][\w$]*)/g)) defined.add(m[1]);
for (const m of js.matchAll(/(?:const|let|var)\s+([A-Za-z_$][\w$]*)/g)) defined.add(m[1]);
for (const m of js.matchAll(/(?:const|let|var)\s*\{([^}]*)\}/g)) m[1].split(",").forEach(x => defined.add(x.split(":").pop().trim()));
for (const m of js.matchAll(/[(,]\s*([A-Za-z_$][\w$]*)\s*(?=[,)]\s*=>|=>)/g)) defined.add(m[1]);
for (const m of js.matchAll(/\(([^()]*)\)\s*=>/g)) m[1].split(",").forEach(x => defined.add(x.replace(/=.*/, "").trim()));
for (const m of js.matchAll(/function\s*[A-Za-z_$]*\s*\(([^)]*)\)/g)) m[1].split(",").forEach(x => defined.add(x.replace(/=.*/, "").trim()));
const builtin = new Set(("if for while switch catch return typeof new function await async Math JSON Object Array String Number " +
  "Boolean Date Promise Set Map parseInt parseFloat isNaN isFinite setTimeout clearTimeout setInterval clearInterval " +
  "requestAnimationFrame fetch alert confirm prompt encodeURIComponent decodeURIComponent FormData Blob URL Image Audio " +
  "Event CustomEvent getComputedStyle structuredClone AbortController TextDecoder Uint8Array Int32Array Float32Array " +
  "AudioContext webkitAudioContext OfflineAudioContext File FileReader Error RegExp Symbol BigInt queueMicrotask " +
  "atob btoa escape unescape MutationObserver ResizeObserver IntersectionObserver Path2D DOMParser XMLHttpRequest " +
  "WebSocket Worker Intl Reflect Proxy WeakMap WeakSet ArrayBuffer DataView console super import FontFace cancelAnimationFrame").split(/\s+/));
for (const m of raw.matchAll(/typeof\s+([A-Za-z_$][\w$]*)\s*[!=]==?\s*["']function["']/g)) defined.add(m[1]);   // guarded = optional by design
const missing = new Set();
for (const m of js.matchAll(/(^|[^.\w$])([A-Za-z_$][\w$]*)\s*\(/g)) {
  const n = m[2];
  if (!defined.has(n) && !builtin.has(n)) missing.add(n);
}
if (missing.size) { console.log("✗ called but never defined:", [...missing].sort().join(", ")); process.exit(1); }
console.log("✓ every called function is defined (" + defined.size + " names)");
