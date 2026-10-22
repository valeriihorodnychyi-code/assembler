// Phrase re-voice selection — offline test. Run from the repo root:  node tests/test_revoice_select.js
// Extracts rvPairs / rvSpan / changedSpans / selectionSpan verbatim from web/index.html.
const fs=require("fs");const h=fs.readFileSync("web/index.html","utf8");
const a=h.indexOf("const rvNorm="),b=h.indexOf("function setWordRange");
let realWords,_wRng,ORIG;const video={duration:12},videoLoaded=true;
const origWordsNow=()=>ORIG;
eval(h.slice(a,b).replace("const rvNorm","var rvNorm"));
const say=s=>s.split(" ").map((w,i)=>({word:w,start:+(i*.4).toFixed(2),end:+(i*.4+.3).toFixed(2)}));
ORIG=say("Another year saying I'll start next week. Then nothing");
// edited week -> Monday
realWords=ORIG.map(w=>({...w}));realWords[6]={...realWords[6],word:"Monday."};
// 1) single change span
let c=changedSpans();console.log("changed:",c.map(s=>[s.old,s.text,s.lo,s.hi]));
// 2) select "I'll start next Monday." (3..7)
_wRng=[3,7];let s=selectionSpan();console.log("sel:",s.old,"|",s.text,"| slot",s.lo,s.hi,"| os/oe",s.os,s.oe);
if(s.old!=="I'll start next week."||s.text!=="I'll start next Monday.")throw "sel text";
if(!(s.lo===1.1&&s.hi===2.8))throw "slot "+s.lo+" "+s.hi;
// 3) selection cuts through an edited group: "next big Monday" replacing "next week"
realWords=say("Another year saying I'll start soon big Monday. Then nothing");
_wRng=[0,6];s=selectionSpan();console.log("grow:",s.ns,s.ne,"|",s.old,"→",s.text);
if(s.ne!==8)throw "should grow over the edited group";
// 4) unchanged range re-voice (new intonation)
realWords=ORIG.map(w=>({...w}));_wRng=[0,3];s=selectionSpan();if(s.old!==s.text||s.os!==0||s.oe!==3)throw "plain";
// 5) curly apostrophe from the recogniser vs a typed one — NOT a change
ORIG=say("Another year saying I\u2019ll start next week.");realWords=say("Another year saying I'll start next week.");
if(changedSpans().length)throw "apostrophe counted as a change";
console.log("ALL OK");
