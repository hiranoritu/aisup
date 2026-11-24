// C コードの色分け（表示専用）。エディタの入力・イベント記録には関与しない。
// textarea の文字を透明にし、同じ位置に色付きの <pre> を重ねる方式。
"use strict";

const C_KEYWORDS = new Set(("if else for while do switch case default return break continue goto sizeof " +
  "typedef struct union enum static extern const volatile register inline restrict").split(" "));
const C_TYPES = new Set(("int char float double void long short unsigned signed bool size_t FILE " +
  "int8_t int16_t int32_t int64_t uint8_t uint16_t uint32_t uint64_t").split(" "));

// 先頭から順に照合する（sticky）。どれにも当たらない文字は1文字ずつ素通し。
const C_RULES = [
  ["comment", /\/\*[\s\S]*?(?:\*\/|$)/y],
  ["comment", /\/\/[^\n]*/y],
  ["preproc", /#[ \t]*[a-zA-Z_]+/y],
  ["string", /"(?:\\[\s\S]|[^"\\\n])*"?/y],
  ["string", /'(?:\\[\s\S]|[^'\\\n])*'?/y],
  ["number", /(?:0[xX][0-9a-fA-F]+|\d+\.?\d*(?:[eE][+-]?\d+)?|\.\d+)[uUlLfF]*/y],
  ["ident", /[A-Za-z_]\w*/y],
  ["ptr", /->|&&|\*|&/y],   // && は論理演算なので ptr にしない（下で戻す）
  ["space", /\s+/y],
];

function lastSignificant(toks) {
  for (let k = toks.length - 1; k >= 0; k--) {
    if (toks[k].cls !== "comment" && toks[k].text.trim()) return toks[k];
  }
  return null;
}

function tokenizeC(src) {
  const out = [];
  let i = 0;
  let lineStart = true;    // 行頭（空白を除く）なら # は前処理指令
  while (i < src.length) {
    let hit = null;
    for (const [cls, re] of C_RULES) {
      if (cls === "preproc" && !lineStart) continue;
      re.lastIndex = i;
      const m = re.exec(src);
      if (m && m[0].length) { hit = [cls, m[0]]; break; }
    }
    if (!hit) hit = ["plain", src[i]];
    let [cls, text] = hit;
    if (cls === "ident") {
      if (C_KEYWORDS.has(text)) cls = "kw";
      else if (C_TYPES.has(text)) cls = "type";
      else {
        const rest = src.slice(i + text.length).match(/^\s*\(/);
        cls = rest ? "fn" : "plain";
      }
    } else if (cls === "ptr" && text !== "->") {
      // 直前が値（変数・数値・文字列・閉じ括弧）なら掛け算／ビット積なので色を付けない
      const prev = lastSignificant(out);
      const binary = prev && (prev.operand || prev.text === ")" || prev.text === "]");
      if (text === "&&" || binary) cls = "plain";
    } else if (cls === "space") cls = "plain";
    const operand = hit[0] === "number" || hit[0] === "string" || (hit[0] === "ident" && cls === "plain");
    out.push({ text, cls, operand });
    i += text.length;
    if (text.includes("\n")) lineStart = /\n[ \t]*$/.test(text) || text.endsWith("\n");
    else if (cls !== "plain" || text.trim()) lineStart = false;
  }
  // #include の後ろの <stdio.h> を前処理指令の色に含める
  for (let k = 0; k < out.length; k++) {
    if (out[k].cls === "preproc") {
      let j = k + 1;
      while (j < out.length && !out[j].text.includes("\n")) { if (out[j].cls !== "comment") out[j].cls = "preproc"; j++; }
    }
  }
  return out;
}

function tokenSpan(t) {
  if (t.cls === "plain") return document.createTextNode(t.text);
  const s = document.createElement("span");
  s.className = "tk-" + t.cls;
  s.textContent = t.text;
  return s;
}

// コード全体を色付きの要素列にする
function highlightInto(el, src) {
  el.textContent = "";
  for (const t of tokenizeC(src)) el.append(tokenSpan(t));
}

// 行ごとに分けた要素列（行番号付き表示用）。複数行のコメントも正しく色付けする。
function highlightLines(src) {
  const lines = [[]];
  for (const t of tokenizeC(src)) {
    const parts = t.text.split("\n");
    parts.forEach((p, idx) => {
      if (idx > 0) lines.push([]);
      if (p) lines[lines.length - 1].push(tokenSpan({ text: p, cls: t.cls }));
    });
  }
  return lines;
}

// textarea に色付き表示を重ねる。戻り値の refresh() を値を書き換えたら呼ぶ。
function attachHighlight(ta) {
  const wrap = document.createElement("div");
  wrap.className = "hl-wrap";
  const pre = document.createElement("pre");
  pre.className = "hl-layer";
  pre.setAttribute("aria-hidden", "true");
  ta.parentNode.insertBefore(wrap, ta);
  wrap.append(pre, ta);
  ta.classList.add("hl-input");
  let pending = false;
  const sync = () => { pre.scrollTop = ta.scrollTop; pre.scrollLeft = ta.scrollLeft; };
  const refresh = () => {
    pending = false;
    highlightInto(pre, ta.value + "\n");   // 末尾の改行でも高さを合わせる
    sync();
  };
  const schedule = () => { if (!pending) { pending = true; requestAnimationFrame(refresh); } };
  ta.addEventListener("input", schedule);
  ta.addEventListener("scroll", sync);
  refresh();
  return { refresh, wrap };
}
