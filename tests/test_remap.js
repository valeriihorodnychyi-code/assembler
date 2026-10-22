// Caption timing after a text edit — offline test.
// Run from the repo root:   node tests/test_remap.js
//
// Extracts remapWords() verbatim from web/index.html (between the @@remapWords markers), so the
// test always runs the code the app runs. Guards the 1.9.63 bug: an equal word count was mapped
// BY POSITION, so every word between two edits slid by one slot and the error compounded.
const fs = require("fs"), path = require("path");
const html = fs.readFileSync(path.join(__dirname, "..", "web", "index.html"), "utf8");
const a = html.indexOf("// @@remapWords:start"), b = html.indexOf("// @@remapWords:end");
if (a < 0 || b < 0) { console.error("✗ @@remapWords markers not found in web/index.html"); process.exit(1); }
const remapWords = new Function(html.slice(a, b) + "\nreturn remapWords;")();

const say = s => s.split(" ").map((w, i) => ({ word: w, start: +(i * 0.4).toFixed(2), end: +(i * 0.4 + 0.3).toFixed(2) }));
const text = ws => ws.map(w => w.word).join(" ");
const at = (ws, word, nth = 0) => ws.filter(w => w.word === word)[nth];
const close = (x, y) => Math.abs(x - y) < 1e-9;
const ORIG = say("I start the challenge this Monday and I feel great already");

const tests = {
  "same word count, edits far apart → words between keep their own time"() {
    const e = remapWords("I start challenge this Monday and I feel great already really", ORIG);
    for (const w of ["challenge", "this", "Monday", "and", "feel", "great", "already"])
      if (!close(at(e, w).start, at(ORIG, w).start)) throw Error(`${w}: ${at(e, w).start} ≠ ${at(ORIG, w).start}`);
  },
  "inserted word gets a real duration, not the 0.1 s pause"() {
    const e = remapWords("I start the new challenge this Monday and I feel great already", ORIG);
    const nw = at(e, "new");
    if (nw.end - nw.start < 0.12) throw Error(`"new" lasts ${(nw.end - nw.start).toFixed(2)} s`);
    if (!close(at(e, "challenge").start, at(ORIG, "challenge").start)) throw Error("next word moved");
  },
  "a corrected spelling keeps the exact time (Tie → Tai)"() {
    const src = say("I do Tie Chi every day"), e = remapWords("I do Tai Chi every day", src);
    if (!close(at(e, "Tai").start, src[2].start) || !close(at(e, "Tai").end, src[2].end)) throw Error("moved");
  },
  "idempotent: same text on same words changes nothing, however often"() {
    let w = ORIG;
    for (let k = 0; k < 6; k++) w = remapWords(text(w), w);
    w.forEach((x, i) => { if (!close(x.start, ORIG[i].start) || !close(x.end, ORIG[i].end)) throw Error(x.word); });
  },
  "no compounding: edit, edit again, revert → original timings come back"() {
    let w = remapWords("I start challenge this Monday and I feel great already really", ORIG);
    w = remapWords("I start challenge on this Monday and I feel great already really", w);
    w = remapWords(text(ORIG), w);
    const bad = w.filter((x, i) => !close(x.start, ORIG[i].start) && !["the"].includes(x.word));
    if (bad.length) throw Error(bad.map(x => x.word).join(","));
  },
  "splitting one word shares that word's time span"() {
    const e = remapWords("I start the chal lenge this Monday and I feel great already", ORIG);
    const c = at(ORIG, "challenge");
    if (!close(at(e, "chal").start, c.start) || !close(at(e, "lenge").end, c.end)) throw Error("span");
  },
  "typing a new word key by key stays stable and leaves the anchors alone"() {
    let w = ORIG, base = "I start the challenge this Monday and I feel great already";
    for (const t of ["I start the n challenge", "I start the ne challenge", "I start the new challenge"])
      w = remapWords(t + base.slice("I start the challenge".length), w);
    if (!close(at(w, "challenge").start, at(ORIG, "challenge").start)) throw Error("anchor moved while typing");
    if (at(w, "new").end - at(w, "new").start < 0.12) throw Error("new word collapsed while typing");
  },
  "curly vs straight apostrophe is the same word (I\u2019ll = I'll) — keeps its exact time"() {
    const src = say("I\u2019ll start next week"), e = remapWords("I'll start next Monday", src);
    if (!close(at(e, "I'll").start, src[0].start) || !close(at(e, "start").start, src[1].start)) throw Error("moved");
  },
  "a hand-set time survives later text edits"() {
    const w = ORIG.map(x => ({ ...x })); w[3] = { ...w[3], start: 1.05, end: 1.5, manual: true };   // "challenge" set by hand
    const e = remapWords("I start the big challenge this Monday and I feel great already", w);
    const c = at(e, "challenge");
    if (!close(c.start, 1.05) || !close(c.end, 1.5) || !c.manual) throw Error(`lost: ${c.start}-${c.end} manual=${c.manual}`);
  },
  "timings always run forward (no word ends before it starts or overlaps backwards)"() {
    for (const t of ["new I start the challenge", "I start the challenge this Monday and I feel great already again",
                     "I start the brand new exciting challenge this Monday", "completely different words here"]) {
      const e = remapWords(t, ORIG);
      e.forEach((x, i) => {
        if (x.end < x.start - 1e-9) throw Error(`${t}: ${x.word} ends before it starts`);
        if (i && x.start < e[i - 1].start - 1e-9) throw Error(`${t}: ${x.word} starts before ${e[i - 1].word}`);
      });
    }
  },
};

let failed = 0;
for (const [name, fn] of Object.entries(tests)) {
  try { fn(); console.log("  ✓", name); } catch (e) { failed++; console.log("  ✗", name, "—", e.message); }
}
console.log(`\n${Object.keys(tests).length - failed}/${Object.keys(tests).length} passed`);
process.exit(failed ? 1 : 0);
