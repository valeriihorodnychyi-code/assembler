// 📅 Date finder — offline. Run from the repo root:   node tests/test_dates.js
// Extracts findDates() verbatim from web/index.html (between the @@findDates markers).
const fs = require("fs"), path = require("path");
const html = fs.readFileSync(path.join(__dirname, "..", "web", "index.html"), "utf8");
const a = html.indexOf("// @@findDates:start"), b = html.indexOf("// @@findDates:end");
if (a < 0 || b < 0) { console.error("✗ @@findDates markers not found"); process.exit(1); }
const findDates = new Function(html.slice(a, b) + "\nreturn findDates;")();
const say = s => s.split(" ").map((w, i) => ({ word: w, start: i * 0.4, end: i * 0.4 + 0.3 }));
const got = s => findDates(say(s)).map(d => d.text + "|" + d.key);
const eq = (s, want) => { const g = got(s); if (JSON.stringify(g) !== JSON.stringify(want)) throw Error(`${s}\n   got  ${JSON.stringify(g)}\n   want ${JSON.stringify(want)}`); };
const tests = {
  "month + day forms"() {
    eq("Join us on November 16th for the launch.", ["November 16th|11-16"]);
    eq("Starting November sixteen, everything changes", ["November sixteen|11-16"]);
    eq("from Nov. 16 only", ["Nov. 16|11-16"]);
    eq("on November twenty-first or November twenty first", ["November twenty-first|11-21", "November twenty first|11-21"]);
  },
  "day + month, weekday, year"() {
    eq("the 16th of November is coming", ["the 16th of November|11-16"]);
    eq("Sunday, November 16, 2026 we start", ["Sunday, November 16, 2026|11-16"]);
    eq("on 16 November.", ["16 November|11-16"]);
  },
  "numeric"() { eq("deadline 11/16 sharp", ["11/16|11-16"]); },
  "no false positives"() {
    eq("You may feel better in March", []);
    eq("we march on and may 2 people win", ["may 2|5-2"]);   // a real (if odd) date — shown, the designer unticks it
    eq("wait a second, the first time I tried", []);
    eq("I lost 16 pounds in November", []);
  },
  "four times in a long video → one key"() {
    const r = findDates(say("November 16 is the day. Remember November 16th! See you November sixteenth. Last call November 16."));
    if (r.length !== 4 || new Set(r.map(d => d.key)).size !== 1) throw Error(JSON.stringify(r));
  },
};
let ok = 0;
for (const [n, f] of Object.entries(tests)) { try { f(); ok++; console.log("✓", n); } catch (e) { console.log("✗", n, "\n  ", e.message); } }
console.log(`${ok}/${Object.keys(tests).length} passed`); if (ok !== Object.keys(tests).length) process.exit(1);
