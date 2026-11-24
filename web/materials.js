// 教材バーとパネル。開閉は MATERIAL_VIEW イベントとして記録する（判定には使わない）。
"use strict";

// 教材用の簡易 Markdown 表示（見出し・段落・箇条書き・番号付き・表・コード・画像・太字・インラインコード）
function renderMarkdown(md) {
  const root = document.createDocumentFragment();
  const lines = md.replace(/<!--[\s\S]*?-->/g, "").split("\n");
  const inline = (text) => {
    const frag = document.createDocumentFragment();
    const re = /(`[^`\n]+`|\*\*[^*\n]+\*\*|!\[[^\]]*\]\([^)\s]+\))/g;
    let last = 0, m;
    while ((m = re.exec(text))) {
      if (m.index > last) frag.append(text.slice(last, m.index));
      const t = m[0];
      if (t.startsWith("`")) { const c = document.createElement("code"); c.textContent = t.slice(1, -1); frag.append(c); }
      else if (t.startsWith("**")) { const b = document.createElement("strong"); b.textContent = t.slice(2, -2); frag.append(b); }
      else {
        const mm = t.match(/^!\[([^\]]*)\]\(([^)\s]+)\)$/);
        const src = mm[2];
        if (/^\/materials\/[\w./-]+$/.test(src) && !src.includes("..")) {   // 教材フォルダ内の画像だけ
          const img = document.createElement("img"); img.alt = mm[1]; img.src = src; frag.append(img);
        } else frag.append(mm[1]);
      }
      last = m.index + t.length;
    }
    if (last < text.length) frag.append(text.slice(last));
    return frag;
  };
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    let m;
    if (/^```/.test(line)) {
      const lang = line.slice(3).trim().toLowerCase();
      const buf = [];
      i++;
      while (i < lines.length && !/^```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      const pre = document.createElement("pre");
      if (["c", "h", ""].includes(lang)) highlightInto(pre, buf.join("\n")); else pre.textContent = buf.join("\n");
      root.append(pre);
    } else if ((m = line.match(/^(#{1,4})\s+(.*)$/))) {
      const h = document.createElement(m[1].length <= 2 ? "h3" : "h4");
      h.append(inline(m[2]));
      root.append(h);
      i++;
    } else if (/^\s*([-*]|\d+\.)\s+/.test(line)) {
      const ordered = /^\s*\d+\./.test(line);
      const list = document.createElement(ordered ? "ol" : "ul");
      while (i < lines.length && /^\s*([-*]|\d+\.)\s+/.test(lines[i])) {
        const li = document.createElement("li");
        li.append(inline(lines[i].replace(/^\s*([-*]|\d+\.)\s+/, "")));
        list.append(li);
        i++;
      }
      root.append(list);
    } else if (/^\|/.test(line)) {
      const table = document.createElement("table");
      let first = true;
      while (i < lines.length && /^\|/.test(lines[i])) {
        const cells = lines[i].replace(/^\||\|\s*$/g, "").split("|").map((c) => c.trim());
        if (!cells.every((c) => /^:?-+:?$/.test(c))) {
          const tr = document.createElement("tr");
          cells.forEach((c) => { const td = document.createElement(first ? "th" : "td"); td.append(inline(c)); tr.append(td); });
          table.append(tr);
          first = false;
        }
        i++;
      }
      root.append(table);
    } else if (line.trim() === "") {
      i++;
    } else {
      const p = document.createElement("p");
      const buf = [];
      while (i < lines.length && lines[i].trim() !== "" && !/^(```|#{1,4}\s|\s*([-*]|\d+\.)\s|\|)/.test(lines[i])) buf.push(lines[i++]);
      buf.forEach((b, k) => { if (k) p.append(document.createElement("br")); p.append(inline(b)); });
      root.append(p);
    }
  }
  return root;
}

const Materials = {
  openId: null,
  key: "",
  render(list) {
    const bar = document.getElementById("materials-bar");
    bar.hidden = !list || !list.length;
    const key = (list || []).map((m) => m.id).join(",");
    if (key === this.key) return;
    this.key = key;
    const box = document.getElementById("materials-list");
    box.textContent = "";
    for (const m of list || []) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = m.title;
      b.dataset.id = m.id;
      b.setAttribute("aria-pressed", String(this.openId === m.id));
      b.addEventListener("click", () => (this.openId === m.id ? this.close() : this.open(m.id)));
      box.append(b);
    }
    if (this.openId && !(list || []).some((m) => m.id === this.openId)) this.close();
  },
  async open(id) {
    if (this.openId) this.close();
    let m;
    try { m = await api("/api/material?id=" + encodeURIComponent(id)); }
    catch (e) { toast("教材を開けませんでした"); return; }
    this.openId = id;
    document.getElementById("mp-title").textContent = m.title;
    const body = document.getElementById("mp-body");
    body.textContent = "";
    body.append(renderMarkdown(m.content));
    body.scrollTop = 0;
    document.getElementById("materials-panel").hidden = false;
    this.mark();
    enqueue("MATERIAL_VIEW", { material_id: id, action: "open" });
  },
  close() {
    if (!this.openId) return;
    enqueue("MATERIAL_VIEW", { material_id: this.openId, action: "close" });
    this.openId = null;
    document.getElementById("materials-panel").hidden = true;
    this.mark();
  },
  mark() {
    document.querySelectorAll("#materials-list button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.id === this.openId)));
  },
};
document.getElementById("mp-close").addEventListener("click", () => Materials.close());
// ページを閉じる・読み直すときに開いたままなら閉じたと記録する（端末に保存され、次に開いたときに送られる）
window.addEventListener("pagehide", () => Materials.close());
