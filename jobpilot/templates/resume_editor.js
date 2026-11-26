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
  let version = data.version;
  let fontCode = page.dataset.fontCode || data.content?.style?.font || "default";
  let editing = false;
  let saveTimer = null;
  let saving = null; // 进行中的保存；它没结束之前不发下一次
  let dirty = false; // 页面上有还没存到服务器的修改
  let evidenceState = { show: false, bullets: {}, changedIds: [] };

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
    if (node.classList.contains("mark") || node.classList.contains("evidence-badge")) return ""; // 检查标记与依据编号不是简历内容
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
      font: fontCode,
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
    copy.querySelectorAll(".bullet-action, .evidence-badge").forEach((el) => el.remove());
    copy.querySelectorAll("li.is-changed").forEach((el) => el.classList.remove("is-changed"));
    copy.querySelector("body")?.classList.remove("editing");
    return copy;
  }

  function scheduleSave() {
    dirty = true;
    clearTimeout(saveTimer);
    setStatus("有未保存的修改");
    saveTimer = setTimeout(save, SAVE_DELAY_MS);
  }

  function save() {
    return startSave();
  }

  // 发起一次保存并返回它的结果。上一次还没返回时就等那一次：连着发两次会带着同一个
  // 旧版本号，后到的那次被服务器当成「在别处保存过」（409）。期间的修改由 dirty 记着，
  // 上一次存完后接着存。
  function startSave(content) {
    clearTimeout(saveTimer);
    if (!saving) saving = write(content ?? collect()).finally(() => { saving = null; });
    return saving;
  }

  // 存完手上的修改再返回，重置、改用事实原文这类要先落地的动作用它；没有要存的就算成功。
  async function flush() {
    clearTimeout(saveTimer);
    const pending = saving ? await saving : null;
    if (pending && !pending.ok) return pending; // 上一次没存上，再发一次也是同样的结果
    return dirty ? save() : { ok: true };
  }

  // 一次保存，返回 { ok, reason }。存不上的修改留成本机草稿，刷新后可以覆盖回来。
  async function write(content) {
    dirty = false; // 页面上的文字已经收进 content，从这一刻起再改的算下一次
    if (!online) {
      const ok = storage.set(OFFLINE_KEY, {
        html: withoutEditing(page).innerHTML,
        style: page.getAttribute("style"),
        fontCode,
      });
      setStatus(ok ? "已保存在本机浏览器" : "浏览器不允许本地保存，请用「保存 HTML」");
      return { ok };
    }
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
        version = saved.version;
        data.content = saved.content;
        refreshMarks(saved.content);
        storage.remove(DRAFT_KEY);
        setStatus(`已保存 · 第 ${saved.version} 版`);
        if (window.parent !== window) {
          window.parent.postMessage(
            { type: "jobpilot:saved", version: saved.version, version_id: saved.version_id },
            window.location.origin,
          );
        }
        if (dirty) scheduleSave(); // 保存期间又有修改：接着存
        return { ok: true };
      }
      return keepDraft(content, response.status === 409
        ? "简历已在别处保存过：你的修改已存为本机草稿，请刷新后再改"
        : "保存失败：修改已存为本机草稿");
    } catch {
      return keepDraft(content, "网络异常：修改已存为本机草稿");
    }
  }

  function keepDraft(content, reason) {
    // 保存期间又改过的，草稿里留页面上最新的文字。
    storage.set(DRAFT_KEY, dirty ? collect() : content);
    setStatus(reason);
    return { ok: false, reason };
  }

  // 保存后服务器重新检查了改过的条目，按新结论更新标记。
  function renderRichText(element, text) {
    element.textContent = "";
    const pattern = /\*\*(.+?)\*\*/g;
    let pos = 0;
    let match;
    while ((match = pattern.exec(text)) !== null) {
      if (match.index > pos) {
        element.append(document.createTextNode(text.slice(pos, match.index)));
      }
      const strong = document.createElement("strong");
      strong.textContent = match[1];
      element.append(strong);
      pos = pattern.lastIndex;
    }
    if (pos < text.length) {
      element.append(document.createTextNode(text.slice(pos)));
    }
  }

  function refreshMarks(content) {
    const bullets = content.sections.flatMap((s) => s.entries.flatMap((e) => e.bullets));
    for (const bullet of bullets) {
      const li = page.querySelector(`li[data-bullet-id="${CSS.escape(bullet.bullet_id)}"]`);
      if (!li) continue;
      li.classList.toggle("pending", bullet.origin === "pending");
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
      const action = li.querySelector(".bullet-action");
      if (action) action.before(mark);
      else li.append(mark);
    }
    renderEvidence();
  }

  function renderEvidence() {
    page.querySelectorAll(".evidence-badge").forEach((el) => el.remove());
    page.querySelectorAll("li.is-changed").forEach((el) => el.classList.remove("is-changed"));
    if (!online) return;
    const changed = new Set(evidenceState.changedIds);
    for (const li of page.querySelectorAll("li[data-bullet-id]")) {
      const bulletId = li.dataset.bulletId;
      if (changed.has(bulletId)) li.classList.add("is-changed");
      if (!evidenceState.show) continue;
      const items = evidenceState.bullets[bulletId];
      if (!Array.isArray(items) || items.length === 0) continue;
      const anchor = li.querySelector(".mark") || li.querySelector(".bullet-action");
      for (const item of items) {
        const badge = document.createElement("span");
        badge.className = "evidence-badge no-print";
        badge.textContent = item.code;
        badge.tabIndex = 0;
        badge.contentEditable = "false";
        if (item.text) badge.dataset.tip = `${item.code} ${item.text}`;
        if (anchor) anchor.before(badge);
        else li.append(badge);
      }
    }
  }

  function applyServerVersion(saved, statusMessage) {
    versionId = saved.version_id;
    version = saved.version;
    page.dataset.resumeVersion = String(saved.version);
    data.content = saved.content;
    const bullets = saved.content.sections.flatMap((s) => s.entries.flatMap((e) => e.bullets));
    for (const bullet of bullets) {
      const li = page.querySelector(`li[data-bullet-id="${CSS.escape(bullet.bullet_id)}"]`);
      const textSpan = li?.querySelector('[data-part="text"]');
      if (textSpan) renderRichText(textSpan, bullet.text);
    }
    refreshMarks(saved.content);
    updateFit();
    storage.remove(DRAFT_KEY);
    setStatus(statusMessage);
  }

  // ---------- 编辑开关与按钮激活态 ----------

  function editableParts() {
    return page.querySelectorAll("[data-part]");
  }

  function setPressed(button, active) {
    if (!button) return;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  }

  function syncBoldState() {
    const boldActive = Boolean(
      editing &&
        typeof document.queryCommandState === "function" &&
        document.queryCommandState("bold"),
    );
    setPressed(buttons.bold, boldActive);
  }

  function setEditing(on) {
    editing = on;
    for (const part of editableParts()) part.contentEditable = on ? "true" : "false";
    document.body.classList.toggle("editing", on);
    buttons.edit.textContent = on ? "完成编辑" : "编辑";
    setPressed(buttons.edit, on);
    syncBoldState();
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
  document.addEventListener("selectionchange", syncBoldState);
  page.addEventListener("keyup", syncBoldState);
  page.addEventListener("mouseup", syncBoldState);

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

  function addGroup() {
    const group = document.createElement("div");
    group.className = "toolbar-group";
    toolbar.append(group);
    return group;
  }

  function addButton(parent, name, label, onClick, toggle = false) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    if (toggle) button.setAttribute("aria-pressed", "false");
    button.addEventListener("click", onClick);
    parent.append(button);
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
    .toolbar {
      --tb-bg: #FFFFFF; /* theme.token.colorBgContainer */
      --tb-hover: #F2F2F0; /* theme.token.colorFillTertiary */
      --tb-border: #E6E6E2; /* theme.token.colorBorder */
      --tb-divider: #EFEFEC; /* theme.token.colorBorderSecondary */
      --tb-ink: #1F1F1F; /* theme.token.colorText */
      --tb-muted: #5F5F5B; /* theme.token.colorTextSecondary */
      --tb-active-bg: #E4EAF0; /* theme.token.colorPrimaryBg */
      --tb-active-border: #C9D4DF; /* theme.token.colorPrimaryBorder */
      --tb-active-ink: #3B5268; /* theme.token.colorPrimaryText */
      --tb-focus: #50687F; /* theme.token.colorPrimary */
      --tb-radius: 6px; /* theme.token.borderRadiusSM */
      position: sticky; top: 0; z-index: 10; display: flex; flex-wrap: wrap;
      gap: 6px 10px; align-items: center; padding: 8px 12px;
      background: var(--tb-bg); border-bottom: 1px solid var(--tb-divider);
      color: var(--tb-ink); font: 14px var(--font);
    }
    .toolbar-group { display: inline-flex; flex-wrap: wrap; gap: 4px; align-items: center; }
    .toolbar-group + .toolbar-group { padding-left: 10px; border-left: 1px solid var(--tb-divider); }
    .toolbar button, .toolbar select {
      font: inherit; font-size: 14px; line-height: 1.4; color: var(--tb-ink);
      background: var(--tb-bg); border: 1px solid var(--tb-border);
      border-radius: var(--tb-radius); padding: 4px 10px; cursor: pointer;
      transition: background-color 0.15s ease, border-color 0.15s ease;
    }
    .toolbar button:hover, .toolbar select:hover { background: var(--tb-hover); }
    .toolbar button:focus-visible, .toolbar select:focus-visible {
      outline: 2px solid var(--tb-active-border); outline-offset: 1px;
    }
    .toolbar button.is-active {
      background: var(--tb-active-bg); border-color: var(--tb-active-border);
      color: var(--tb-active-ink); font-weight: 500;
    }
    .toolbar-fit, .toolbar-status {
      font-size: 13px; color: var(--tb-muted); font-variant-numeric: tabular-nums;
    }
    .toolbar-fit { margin-left: auto; }
    body.paged .page { background-image: repeating-linear-gradient(to bottom,
      transparent 0, transparent calc(297mm - 1px), #cf222e calc(297mm - 1px), #cf222e 297mm); }
    body.editing [data-part]:hover { outline: 1px dashed var(--tb-border); }
    body.editing [data-part]:focus { outline: 1px solid var(--tb-focus); }
    li[data-bullet-id] { position: relative; }
    .bullet-action {
      position: absolute; right: 0; top: 0; z-index: 1;
      font: inherit; font-size: .78em; color: var(--tb-muted); background: var(--tb-bg);
      border: 1px solid var(--tb-border); border-radius: var(--tb-radius); padding: 1px 6px;
      box-shadow: 0 1px 4px rgba(0, 0, 0, .08); cursor: pointer;
      opacity: 0; pointer-events: none; transition: opacity 0.15s ease;
    }
    li[data-bullet-id]:hover .bullet-action,
    li[data-bullet-id]:focus-within .bullet-action,
    .bullet-action:focus-visible {
      opacity: 1; pointer-events: auto;
    }
    .bullet-action:hover { color: var(--tb-ink); background: var(--tb-hover); }
  `;
  document.head.append(css);
  document.body.prepend(toolbar);

  // ---------- 版式：字体、字号、加粗、撤回、页面模式 ----------

  const FONTS = (data.fonts || []).map((item) => [item.label, item.code, item.css]);
  const FONT_CSS_BY_CODE = new Map(FONTS.map(([, code, cssText]) => [code, cssText]));
  const SIZES = [9, 9.5, 10, 10.5, 11, 11.5, 12];
  const VIEW_KEY = "jobpilot-view";
  const selects = {};

  function addSelect(parent, label, options, value, onChange) {
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
    parent.append(select);
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
    const typoGroup = addGroup();
    selects.font = addSelect(
      typoGroup,
      "字体",
      FONTS.map(([label, code]) => [label, code]),
      FONT_CSS_BY_CODE.has(style.font) ? style.font : "default",
      (code) => {
        fontCode = code;
        page.dataset.fontCode = code;
        setStyle("--font", FONT_CSS_BY_CODE.get(code) || "");
      },
    );
    const sizes = SIZES.map((size) => [`${size} pt`, String(size)]);
    if (!SIZES.includes(style.size_pt)) sizes.unshift([`${style.size_pt} pt`, String(style.size_pt)]);
    selects.size = addSelect(typoGroup, "字号", sizes, String(style.size_pt), (size) =>
      setStyle("--size", `${size}pt`),
    );
    // 加粗、撤回、前进用浏览器自带的编辑命令，作用于正在编辑的文字。
    addButton(
      typoGroup,
      "bold",
      "加粗",
      () => {
        if (!editing) return;
        document.execCommand("bold");
        syncBoldState();
      },
      true,
    );

    const historyGroup = addGroup();
    addButton(historyGroup, "undo", "撤回", () => editing && document.execCommand("undo"));
    addButton(historyGroup, "redo", "前进", () => editing && document.execCommand("redo"));

    const layoutGroup = addGroup();
    addButton(
      layoutGroup,
      "paged",
      "A4 分页",
      () => setPaged(!document.body.classList.contains("paged")),
      true,
    );
    addButton(layoutGroup, "fit", "自动一页", fitOnePage);
  }
  function setPaged(on) {
    document.body.classList.toggle("paged", on);
    buttons.paged.textContent = on ? "连续显示" : "A4 分页";
    setPressed(buttons.paged, on);
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
    const percent = Math.round(ratio * 100);
    const overflowPages = ratio <= 1 ? 0 : Math.ceil(ratio) - 1;
    fitText.textContent =
      ratio <= 1 ? `一页内 · 占用 ${percent}%` : `超出 ${overflowPages} 页`;
    if (online && window.parent !== window) {
      window.parent.postMessage(
        {
          type: "jobpilot:fit",
          fit: ratio <= 1,
          percent,
          overflowPages,
          text: fitText.textContent,
        },
        window.location.origin,
      );
    }
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
    // 先把手上的修改存成一个版本（确认框里说了会保留），也等上一次保存返回，带上最新的版本号。
    const saved = await flush();
    if (!saved.ok) {
      setStatus(`没有重置，有修改没能保存。${saved.reason}`);
      return;
    }
    const response = await fetch(data.reset_url, {
      method: "POST",
      credentials: "same-origin",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ base_version_id: versionId }),
    });
    if (response.ok) window.location.reload();
    else setStatus(response.status === 409 ? "简历已在别处保存过，请刷新后再重置" : "重置失败");
  }

  async function useFacts(bulletId) {
    setStatus("保存并改用事实原文…");
    const saved = await flush();
    if (!saved.ok) {
      setStatus(`没有改用事实原文，有修改没能保存。${saved.reason}`);
      return;
    }
    try {
      const url = data.use_facts_url.replace("{bullet_id}", encodeURIComponent(bulletId));
      const response = await fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ base_version_id: versionId }),
      });
      if (response.ok) {
        const updated = await response.json();
        applyServerVersion(updated, `已改用事实原文 · 第 ${updated.version} 版`);
        return;
      }
      if (response.status === 409) {
        setStatus("简历已在别处保存过，请刷新后再改用事实原文");
        return;
      }
      if (response.status === 422) {
        const err = await response.json().catch(() => null);
        setStatus(typeof err?.detail === "string" ? err.detail : "引用的事实已失效，无法改用事实原文");
        return;
      }
      setStatus("改用事实原文失败");
    } catch {
      setStatus("网络异常：没能改用事实原文");
    }
  }

  function addBulletActions() {
    if (!online || !data.use_facts_url) return;
    for (const li of page.querySelectorAll("li[data-bullet-id]")) {
      if (li.querySelector(".bullet-action")) continue;
      const bulletId = li.dataset.bulletId;
      const button = document.createElement("button");
      button.type = "button";
      button.className = "bullet-action no-print";
      button.dataset.action = "use-facts";
      button.textContent = "改用事实原文";
      button.contentEditable = "false";
      button.addEventListener("click", () => {
        void useFacts(bulletId);
      });
      li.append(button);
    }
  }

  function buildToolbar() {
    const editGroup = addGroup();
    addButton(editGroup, "edit", "编辑", () => setEditing(!editing), true);
    addFormatting();
    // 在应用里预览时统一走外层面板顶部导出，离线导出文件才自带保存与打印。
    if (!online) {
      const offlineGroup = addGroup();
      addButton(offlineGroup, "download", "保存 HTML", downloadHtml);
      addButton(offlineGroup, "print", "打印 / 导出 PDF", () => window.print());
    }
    const resetGroup = addGroup();
    addButton(resetGroup, "reset", "重置", reset);
    toolbar.append(fitText, statusText);
  }

  // ---------- 打开时 ----------

  buildToolbar();
  addBulletActions();

  if (!online) {
    const saved = storage.get(OFFLINE_KEY);
    if (saved?.html) {
      page.innerHTML = saved.html;
      if (saved.style) page.setAttribute("style", saved.style);
      if (saved.fontCode && FONT_CSS_BY_CODE.has(saved.fontCode)) {
        fontCode = saved.fontCode;
        page.dataset.fontCode = saved.fontCode;
        if (selects.font) selects.font.value = saved.fontCode;
      }
      setStatus("已恢复本机保存的修改");
    }
  } else if (storage.get(DRAFT_KEY)) {
    setStatus("有一份未保存到服务器的本机草稿");
    const draftGroup = addGroup();
    addButton(draftGroup, "draft", "用本机草稿覆盖", async () => {
      if (!confirm("用本机草稿覆盖服务器上的当前版本？当前版本仍会保留在历史里。")) return;
      if (saving) await saving; // 和自动保存串行：上一次没返回之前不发这一次
      data.content = storage.get(DRAFT_KEY);
      dirty = false; // 要存的就是草稿，页面上的文字不用再存一遍
      const saved = await startSave(data.content);
      if (saved.ok) {
        window.location.reload();
      } else setStatus("草稿没能保存，请刷新后再试");
    });
  }

  if (storage.get(VIEW_KEY)?.paged) setPaged(true);
  updateFit();

  if (online) {
    window.addEventListener("message", (event) => {
      if (event.origin !== window.location.origin) return;
      const msg = event.data;
      if (!msg || typeof msg !== "object") return;
      if (msg.type === "jobpilot:evidence") {
        evidenceState = {
          show: Boolean(msg.show),
          bullets: msg.bullets && typeof msg.bullets === "object" ? msg.bullets : {},
          changedIds: Array.isArray(msg.changedIds) ? msg.changedIds : [],
        };
        renderEvidence();
      } else if (msg.type === "jobpilot:print") {
        window.print();
      }
    });
    if (window.parent !== window) {
      window.parent.postMessage({ type: "jobpilot:ready" }, window.location.origin);
    }
  }

  // 测试和其他脚本可以调用（例如应用外壳在关闭前立即保存）。
  window.jobpilotEditor = { collect, save, setEditing, fitOnePage };
})();
