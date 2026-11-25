// JobPilot 简历编辑器：内嵌在预览页和导出的 HTML 里，同一份代码两处用（02 决定 21）。
// - 在应用里（save_url 不为空）：自动保存到服务器，每次保存新增一个版本；
//   版本冲突或网络失败时，修改存成本机草稿，不会丢。
// - 离线打开导出文件：修改保存在浏览器本地（localStorage），下次打开自动恢复。
// 页面上的文字只是显示；保存时按 data-* 标记把文字收回成结构化内容，引用和检查结论由服务器决定。
(() => {
  "use strict";

  const data = JSON.parse(document.getElementById("jobpilot-resume").textContent);
  const page = document.querySelector("main.page");
  const online = Boolean(data.save_url);
  const SAVE_DELAY_MS = 1000;
  let versionId = data.version_id;
  let editing = false;
  let saveTimer = null;

  // ---------- 本机存储（隐私模式等情况下可能不可用） ----------

  const storage = {
    get(key) {
      try {
        return JSON.parse(localStorage.getItem(key));
      } catch {
        return null;
      }
    },
    set(key, value) {
      try {
        localStorage.setItem(key, JSON.stringify(value));
        return true;
      } catch {
        return false;
      }
    },
    remove(key) {
      try {
        localStorage.removeItem(key);
      } catch {
        /* 不可用时没有可删的 */
      }
    },
  };
  const OFFLINE_KEY = `jobpilot:${data.storage_key}`;
  const DRAFT_KEY = `jobpilot-draft:${data.storage_key}`;

  // ---------- 页面文字 → 带 **加粗** 标记的文字 ----------

  function marked(node) {
    if (node.nodeType === Node.TEXT_NODE) return node.textContent;
    if (node.nodeType !== Node.ELEMENT_NODE) return "";
    if (node.classList.contains("mark")) return ""; // 检查标记不是简历内容
    const inner = [...node.childNodes].map(marked).join("");
    // 只认明确的加粗标签（加粗按钮生成的就是它们）；标题本身的粗体是样式，不是内容。
    const bold = node.tagName === "STRONG" || node.tagName === "B";
    return bold && inner.trim() ? `**${inner}**` : inner;
  }

  function textOf(element) {
    return element ? marked(element).replace(/\s+/g, " ").replace(/\*\*\*\*/g, "").trim() : "";
  }

  // ---------- 收集内容 ----------

  function collect() {
    const content = structuredClone(data.content);
    content.header = [...page.querySelectorAll('[data-part="header"]')]
      .map(textOf)
      .filter(Boolean);
    for (const section of content.sections) {
      for (const entry of section.entries) {
        const box = page.querySelector(`.entry[data-entry-id="${CSS.escape(entry.entry_id)}"]`);
        if (!box) continue;
        entry.title = textOf(box.querySelector('[data-part="title"]')) || entry.title;
        const time = box.querySelector('[data-part="time"]');
        if (time) entry.time = textOf(time) || null;
        entry.lines = [...box.querySelectorAll('[data-part="line"]')].map(textOf).filter(Boolean);
        const shown = new Map(
          [...box.querySelectorAll("li[data-bullet-id]")].map((li) => [
            li.dataset.bulletId,
            textOf(li.querySelector('[data-part="text"]')),
          ]),
        );
        // 页面上没显示的条目（导出时略过的待处理条目）原样保留；删空的条目去掉。
        entry.bullets = entry.bullets
          .filter((b) => !shown.has(b.bullet_id) || shown.get(b.bullet_id) !== "")
          .map((b) => (shown.has(b.bullet_id) ? { ...b, text: shown.get(b.bullet_id) } : b));
      }
    }
    content.style = currentStyle();
    return content;
  }

  function currentStyle() {
    const style = getComputedStyle(page);
    return {
      font: page.style.getPropertyValue("--font").trim(),
      size_pt: parseFloat(style.getPropertyValue("--size")) || 10.5,
      leading: parseFloat(style.getPropertyValue("--leading")) || 1.5,
    };
  }

  // ---------- 保存 ----------

  // 去掉编辑状态的副本：保存到本地或下载时用，打开后不会处在「可编辑却没在编辑」的状态。
  function withoutEditing(node) {
    const copy = node.cloneNode(true);
    copy.querySelectorAll("[contenteditable]").forEach((el) => el.removeAttribute("contenteditable"));
    copy.querySelector(".toolbar")?.remove();
    copy.querySelector("body")?.classList.remove("editing");
    return copy;
  }

  function scheduleSave() {
    clearTimeout(saveTimer);
    setStatus("有未保存的修改");
    saveTimer = setTimeout(save, SAVE_DELAY_MS);
  }

  async function save() {
    clearTimeout(saveTimer);
    if (!online) {
      const ok = storage.set(OFFLINE_KEY, {
        html: withoutEditing(page).innerHTML,
        style: page.getAttribute("style"),
      });
      setStatus(ok ? "已保存在本机浏览器" : "浏览器不允许本地保存，请用「保存 HTML」");
      return;
    }
    const content = collect();
    setStatus("保存中…");
    try {
      const response = await fetch(data.save_url, {
        method: "PUT",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_version_id: versionId, content }),
      });
      if (response.ok) {
        const saved = await response.json();
        versionId = saved.version_id;
        data.content = saved.content;
        refreshMarks(saved.content);
        storage.remove(DRAFT_KEY);
        setStatus(`已保存 · 第 ${saved.version} 版`);
        return;
      }
      storage.set(DRAFT_KEY, content);
      setStatus(
        response.status === 409
          ? "简历已在别处保存过：你的修改已存为本机草稿，请刷新后再改"
          : "保存失败：修改已存为本机草稿",
      );
    } catch {
      storage.set(DRAFT_KEY, content);
      setStatus("网络异常：修改已存为本机草稿");
    }
  }

  // 保存后服务器重新检查了改过的条目，按新结论更新标记。
  function refreshMarks(content) {
    const bullets = content.sections.flatMap((s) => s.entries.flatMap((e) => e.bullets));
    for (const bullet of bullets) {
      const li = page.querySelector(`li[data-bullet-id="${CSS.escape(bullet.bullet_id)}"]`);
      if (!li) continue;
      li.querySelector(".mark")?.remove();
      const issues = bullet.check.issues.map((i) => i.message).join("；");
      const label =
        bullet.origin === "pending" ? "待处理" : issues ? "请核对" : null;
      if (!label) continue;
      const mark = document.createElement("span");
      mark.className = "mark mark-issue";
      mark.textContent = label;
      mark.title = issues;
      mark.contentEditable = "false";
      li.append(mark);
    }
  }

  // ---------- 编辑开关 ----------

  function editableParts() {
    return page.querySelectorAll("[data-part]");
  }

  function setEditing(on) {
    editing = on;
    for (const part of editableParts()) part.contentEditable = on ? "true" : "false";
    document.body.classList.toggle("editing", on);
    buttons.edit.textContent = on ? "完成编辑" : "编辑";
    if (!on) save();
  }

  page.addEventListener("input", () => {
    updateFit();
    if (editing) scheduleSave();
  });
  page.addEventListener("keydown", (event) => {
    // 每段文字各自一段：回车不新建段落（服务器不认识新段落）。
    if (event.key === "Enter" && event.target.closest("[data-part]")) event.preventDefault();
  });
  page.addEventListener("paste", (event) => {
    // 只粘贴纯文字，不带来别处的格式和标签。
    if (!event.target.closest("[data-part]")) return;
    event.preventDefault();
    const text = event.clipboardData.getData("text/plain").replace(/\s+/g, " ");
    document.execCommand("insertText", false, text);
  });

  // ---------- 工具栏 ----------

  const toolbar = document.createElement("div");
  toolbar.className = "toolbar no-print";
  toolbar.setAttribute("role", "toolbar");
  toolbar.setAttribute("aria-label", "简历编辑");
  const buttons = {};
  const statusText = document.createElement("span");
  statusText.className = "toolbar-status";
  statusText.setAttribute("role", "status");
  const fitText = document.createElement("span");
  fitText.className = "toolbar-fit";

  function addButton(name, label, onClick) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.addEventListener("click", onClick);
    toolbar.append(button);
    buttons[name] = button;
    return button;
  }

  function setStatus(message) {
    statusText.textContent = message;
  }

  function downloadHtml() {
    const copy = withoutEditing(document.documentElement);
    const blob = new Blob([`<!doctype html>\n${copy.outerHTML}`], { type: "text/html" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `${document.title}.html`;
    link.click();
    URL.revokeObjectURL(link.href);
  }

  const css = document.createElement("style");
  css.textContent = `
    .toolbar { position: sticky; top: 0; z-index: 10; display: flex; flex-wrap: wrap;
      gap: 6px; align-items: center; padding: 8px 12px; background: #fff;
      border-bottom: 1px solid var(--rule); font: 13px var(--font); }
    .toolbar button, .toolbar select { font: inherit; padding: 3px 8px; }
    .toolbar-status { color: var(--muted); margin-left: auto; }
    .toolbar-fit { color: var(--muted); }
    body.paged .page { background-image: repeating-linear-gradient(to bottom,
      transparent 0, transparent calc(297mm - 1px), #cf222e calc(297mm - 1px), #cf222e 297mm); }
    body.editing [data-part]:hover { outline: 1px dashed var(--rule); }
    body.editing [data-part]:focus { outline: 1px solid #0969da; }
  `;
  document.head.append(css);
  document.body.prepend(toolbar);

  // ---------- 版式：字体、字号、加粗、撤回、页面模式 ----------

  const FONTS = [
    ["默认字体", ""],
    ["苹方", '"PingFang SC", sans-serif'],
    ["微软雅黑", '"Microsoft YaHei", sans-serif'],
    ["思源黑体", '"Source Han Sans SC", "Noto Sans CJK SC", sans-serif'],
    ["宋体", '"Songti SC", SimSun, serif'],
    ["楷体", '"Kaiti SC", KaiTi, serif'],
  ];
  const SIZES = [9, 9.5, 10, 10.5, 11, 11.5, 12];
  const VIEW_KEY = "jobpilot-view";
  const selects = {};

  function addSelect(label, options, value, onChange) {
    const select = document.createElement("select");
    select.setAttribute("aria-label", label);
    for (const [text, optionValue] of options) {
      const option = document.createElement("option");
      option.textContent = text;
      option.value = optionValue;
      select.append(option);
    }
    select.value = value;
    select.addEventListener("change", () => onChange(select.value));
    toolbar.append(select);
    return select;
  }

  function setStyle(name, value) {
    if (value === "") page.style.removeProperty(name);
    else page.style.setProperty(name, value);
    updateFit();
    scheduleSave();
  }

  function addFormatting() {
    const style = currentStyle();
    addSelect("字体", FONTS, style.font, (font) => setStyle("--font", font));
    const sizes = SIZES.map((size) => [`${size} pt`, String(size)]);
    if (!SIZES.includes(style.size_pt)) sizes.unshift([`${style.size_pt} pt`, String(style.size_pt)]);
    selects.size = addSelect("字号", sizes, String(style.size_pt), (size) =>
      setStyle("--size", `${size}pt`),
    );
    // 加粗、撤回、前进用浏览器自带的编辑命令，作用于正在编辑的文字。
    addButton("bold", "加粗", () => editing && document.execCommand("bold"));
    addButton("undo", "撤回", () => editing && document.execCommand("undo"));
    addButton("redo", "前进", () => editing && document.execCommand("redo"));
    addButton("paged", "A4 分页", () => setPaged(!document.body.classList.contains("paged")));
    addButton("fit", "自动一页", fitOnePage);
  }
  function setPaged(on) {
    document.body.classList.toggle("paged", on);
    buttons.paged.textContent = on ? "连续显示" : "A4 分页";
    storage.set(VIEW_KEY, { paged: on }); // 只是看的方式，不属于简历内容
  }

  // ---------- 自动一页 ----------

  const A4_PX = (() => {
    const probe = document.createElement("div");
    probe.style.cssText = "position:absolute;visibility:hidden;height:297mm";
    document.body.append(probe);
    const height = probe.getBoundingClientRect().height;
    probe.remove();
    return height;
  })();

  function contentHeight() {
    // 页面本身至少一页高，量内容时先去掉最小高度。
    const minHeight = page.style.minHeight;
    page.style.minHeight = "0";
    const height = page.scrollHeight;
    page.style.minHeight = minHeight;
    return height;
  }

  function updateFit() {
    const ratio = contentHeight() / A4_PX;
    fitText.textContent =
      ratio <= 1
        ? `一页内 · 占用 ${Math.round(ratio * 100)}%`
        : `超出 ${Math.ceil(ratio) - 1} 页`;
    return ratio <= 1;
  }

  function fitOnePage() {
    // 先缩字号，到 9 pt 后再缩行距，最低 8 pt、1.25 倍；还放不下就停在最小值并如实显示。
    let { size_pt: size, leading } = currentStyle();
    while (!updateFit() && (size > 8 || leading > 1.25)) {
      if (size > 9 || leading <= 1.25) size = Math.max(8, size - 0.25);
      else leading = Math.max(1.25, Math.round((leading - 0.05) * 100) / 100);
      page.style.setProperty("--size", `${size}pt`);
      page.style.setProperty("--leading", String(leading));
    }
    if (selects.size) {
      if (![...selects.size.options].some((o) => o.value === String(size))) {
        selects.size.add(new Option(`${size} pt`, String(size)), 0);
      }
      selects.size.value = String(size);
    }
    scheduleSave();
  }

  // ---------- 重置 ----------

  async function reset() {
    if (!confirm("回到系统生成的版本？现在的修改会作为历史版本保留。")) return;
    if (!online) {
      storage.remove(OFFLINE_KEY); // 离线文件回到导出时的样子
      window.location.reload();
      return;
    }
    // 由服务器把系统生成的第一版复制成一个新版本。这一版里删掉的条目，第一版里还有，
    // 读出第一版再当普通编辑保存会被拒绝。
    const response = await fetch(data.reset_url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_version_id: versionId }),
    });
    if (response.ok) window.location.reload();
    else setStatus(response.status === 409 ? "简历已在别处保存过，请刷新后再重置" : "重置失败");
  }

  function buildToolbar() {
    addButton("edit", "编辑", () => setEditing(!editing));
    addFormatting();
    if (online && data.export_url) {
      addButton("export", "导出 HTML", () => {
        window.location.href = data.export_url;
      });
    } else {
      addButton("download", "保存 HTML", downloadHtml);
    }
    addButton("print", "打印 / 导出 PDF", () => window.print());
    addButton("reset", "重置", reset);
    toolbar.append(fitText, statusText);
  }

  // ---------- 打开时 ----------

  buildToolbar();

  if (!online) {
    const saved = storage.get(OFFLINE_KEY);
    if (saved?.html) {
      page.innerHTML = saved.html;
      if (saved.style) page.setAttribute("style", saved.style);
      setStatus("已恢复本机保存的修改");
    }
  } else if (storage.get(DRAFT_KEY)) {
    setStatus("有一份未保存到服务器的本机草稿");
    addButton("draft", "用本机草稿覆盖", async () => {
      if (!confirm("用本机草稿覆盖服务器上的当前版本？当前版本仍会保留在历史里。")) return;
      data.content = storage.get(DRAFT_KEY);
      const response = await fetch(data.save_url, {
        method: "PUT",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_version_id: versionId, content: data.content }),
      });
      if (response.ok) {
        storage.remove(DRAFT_KEY);
        window.location.reload();
      } else {
        setStatus("草稿没能保存，请刷新后再试");
      }
    });
  }

  if (storage.get(VIEW_KEY)?.paged) setPaged(true);
  updateFit();

  // 测试和其他脚本可以调用（例如应用外壳在关闭前立即保存）。
  window.jobpilotEditor = { collect, save, setEditing, fitOnePage };
})();
