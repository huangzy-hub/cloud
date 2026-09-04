(() => {
  "use strict";

  const SOURCES = {
    SSD: { title: "本地磁盘 (SSD)", short: "SSD" },
    USB: { title: "可移动磁盘 (USB)", short: "USB" },
    USB2: { title: "可移动磁盘 (USB2)", short: "USB2" },
  };
  const CHUNK_SIZE = 10 * 1024 * 1024;
  const state = {
    view: "home",
    source: null,
    path: "/",
    entries: [],
    selected: null,
    sort: { key: "name", direction: 1 },
    filter: "",
    history: [],
    historyIndex: -1,
    loading: false,
  };

  const $ = (selector) => document.querySelector(selector);
  const $$ = (selector) => [...document.querySelectorAll(selector)];
  const elements = {
    homeView: $("#homeView"), filesView: $("#filesView"), fileList: $("#fileList"),
    loading: $("#loadingState"), empty: $("#emptyState"), breadcrumbs: $("#breadcrumbs"),
    folderTitle: $("#folderTitle"), folderMeta: $("#folderMeta"), sourceLabel: $("#sourceLabel"),
    status: $("#statusText"), selection: $("#selectionText"), search: $("#searchInput"),
    back: $("#backButton"), forward: $("#forwardButton"), up: $("#upButton"),
    refresh: $("#refreshButton"), download: $("#downloadButton"), rename: $("#renameButton"),
    delete: $("#deleteButton"), upload: $("#uploadButton"), newFolder: $("#newFolderButton"),
    fileInput: $("#fileInput"), dropZone: $("#dropZone"), transferPanel: $("#transferPanel"),
    transferList: $("#transferList"), promptDialog: $("#promptDialog"),
    confirmDialog: $("#confirmDialog"), content: $("#content"),
  };

  function apiUrl(endpoint, params = {}) {
    const query = new URLSearchParams();
    Object.entries(params).forEach(([key, value]) => {
      if (value !== undefined && value !== null && value !== "") query.set(key, value);
    });
    return `/api/${endpoint}${query.size ? `?${query}` : ""}`;
  }

  async function apiFetch(endpoint, params = {}, options = {}) {
    const response = await fetch(apiUrl(endpoint, params), { credentials: "same-origin", ...options });
    if (response.redirected && new URL(response.url).pathname === "/login") {
      location.href = `/login?next=${encodeURIComponent(location.pathname + location.hash)}`;
      throw new Error("登录已过期");
    }
    if (!response.ok) {
      let message = `${response.status} ${response.statusText}`;
      try {
        const body = await response.json();
        message = body.message || message;
      } catch (_) { /* response was not JSON */ }
      throw new Error(message);
    }
    return response;
  }

  async function jsonFetch(endpoint, params, options) {
    return (await apiFetch(endpoint, params, options)).json();
  }

  function cleanPath(path, directory = true) {
    const parts = String(path || "/").split("/").filter(Boolean);
    const result = `/${parts.join("/")}`;
    return result === "/" ? result : `${result}${directory ? "/" : ""}`;
  }

  function joinPath(base, name, directory = false) {
    return cleanPath(`${cleanPath(base)}${name}`, directory);
  }

  function parentPath(path) {
    const parts = cleanPath(path).split("/").filter(Boolean);
    parts.pop();
    return cleanPath(`/${parts.join("/")}`);
  }

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>'"]/g, (char) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;",
    })[char]);
  }

  function formatBytes(bytes) {
    const value = Number(bytes) || 0;
    if (value === 0) return "0 字节";
    const units = ["字节", "KB", "MB", "GB", "TB"];
    const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1);
    const number = value / (1024 ** index);
    return `${number.toLocaleString("zh-CN", { maximumFractionDigits: index > 1 ? 1 : 0 })} ${units[index]}`;
  }

  function formatDate(value) {
    if (!value) return "";
    const date = new Date(value);
    if (Number.isNaN(date.getTime())) return "";
    return new Intl.DateTimeFormat("zh-CN", {
      year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
    }).format(date);
  }

  function entryIsDirectory(entry) {
    return entry.type === "directory" || entry.isDir === true;
  }

  function entryPath(entry) {
    return joinPath(state.path, entry.name, entryIsDirectory(entry));
  }

  function extension(name) {
    const value = name.includes(".") ? name.split(".").pop().toLowerCase() : "";
    return value;
  }

  function typeInfo(entry) {
    if (entryIsDirectory(entry)) return { label: "文件夹", iconClass: "folder", badge: "" };
    const ext = extension(entry.name);
    const mime = String(entry.type || "").toLowerCase();
    if (mime.startsWith("image/") || ["jpg", "jpeg", "png", "gif", "webp", "svg"].includes(ext)) return { label: `${ext.toUpperCase()} 图像`, iconClass: "image", badge: "IMG" };
    if (mime.startsWith("video/") || ["mp4", "mkv", "avi", "mov", "webm"].includes(ext)) return { label: `${ext.toUpperCase()} 视频`, iconClass: "video", badge: "▶" };
    if (["zip", "rar", "7z", "gz", "tar"].includes(ext)) return { label: `${ext.toUpperCase()} 压缩文件`, iconClass: "archive", badge: "ZIP" };
    if (["js", "ts", "py", "go", "c", "cpp", "h", "java", "json", "yaml", "yml", "sh"].includes(ext)) return { label: `${ext.toUpperCase()} 文件`, iconClass: "code", badge: "</>" };
    if (["doc", "docx"].includes(ext)) return { label: "Microsoft Word 文档", iconClass: "", badge: "W" };
    if (["xls", "xlsx"].includes(ext)) return { label: "Microsoft Excel 工作表", iconClass: "image", badge: "X" };
    if (ext === "pdf") return { label: "PDF 文档", iconClass: "video", badge: "PDF" };
    return { label: ext ? `${ext.toUpperCase()} 文件` : "文件", iconClass: "", badge: ext.slice(0, 3).toUpperCase() || "" };
  }

  function toast(message, kind = "") {
    const node = document.createElement("div");
    node.className = `toast ${kind}`;
    node.textContent = message;
    $("#toastStack").append(node);
    setTimeout(() => node.remove(), 4300);
  }

  function setOnline(online, message) {
    const container = $(".connection-state");
    container.classList.toggle("online", online);
    container.classList.toggle("offline", !online);
    $("#connectionText").textContent = message || (online ? "已连接到 RK3576" : "连接已中断");
  }

  async function loadCapacities() {
    try {
      const info = await jsonFetch("settings/sources");
      Object.keys(SOURCES).forEach((source) => updateDriveCard(source, info[source]));
      setOnline(true);
    } catch (error) {
      Object.keys(SOURCES).forEach((source) => updateDriveCard(source, null));
      setOnline(false);
      toast(`容量读取失败：${error.message}`, "error");
    }
  }

  function updateDriveCard(source, info) {
    const card = $(`.drive-card[data-source="${source}"]`);
    if (!card) return;
    const label = card.querySelector(".capacity-label");
    const bar = card.querySelector(".capacity-track i");
    const total = Number(info?.total) || 0;
    const used = Number(info?.usedAlt ?? info?.used) || 0;
    if (!total) {
      label.textContent = "容量暂不可用";
      bar.style.width = "0%";
      return;
    }
    const free = Math.max(0, total - used);
    const percent = Math.min(100, (used / total) * 100);
    label.textContent = `${formatBytes(free)} 可用，共 ${formatBytes(total)}`;
    bar.style.width = `${percent}%`;
    bar.className = percent >= 90 ? "danger" : percent >= 75 ? "warning" : "";
  }

  function showHome(push = true) {
    state.view = "home";
    state.source = null;
    state.path = "/";
    state.entries = [];
    state.selected = null;
    elements.homeView.hidden = false;
    elements.filesView.hidden = true;
    elements.search.placeholder = "在此电脑中搜索";
    updateSelection();
    renderBreadcrumbs();
    activateSidebar("home", "/");
    elements.status.textContent = "3 个驱动器";
    if (push) pushHistory({ view: "home" });
    loadCapacities();
  }

  async function openFolder(source, path = "/", push = true) {
    if (!SOURCES[source]) return;
    state.view = "files";
    state.source = source;
    state.path = cleanPath(path);
    state.selected = null;
    elements.homeView.hidden = true;
    elements.filesView.hidden = false;
    elements.loading.hidden = false;
    elements.empty.hidden = true;
    elements.fileList.innerHTML = "";
    elements.sourceLabel.textContent = source;
    elements.folderTitle.textContent = state.path === "/" ? SOURCES[source].title : decodeURIComponent(state.path.split("/").filter(Boolean).at(-1));
    elements.folderMeta.textContent = "正在加载…";
    elements.search.placeholder = `在 ${elements.folderTitle.textContent} 中搜索`;
    renderBreadcrumbs();
    activateSidebar(source, state.path);
    updateSelection();
    updateNavigationButtons();
    if (push) pushHistory({ view: "files", source, path: state.path });

    state.loading = true;
    try {
      const data = await jsonFetch("resources", { source, path: state.path, skipExtendedAttrs: "true" });
      const folders = Array.isArray(data.folders) ? data.folders : [];
      const files = Array.isArray(data.files) ? data.files : [];
      state.entries = [...folders, ...files];
      state.loading = false;
      renderEntries();
      setOnline(true);
    } catch (error) {
      state.entries = [];
      state.loading = false;
      renderEntries();
      setOnline(false);
      toast(`无法打开文件夹：${error.message}`, "error");
      elements.folderMeta.textContent = "读取失败";
    } finally {
      state.loading = false;
      elements.loading.hidden = true;
    }
  }

  function renderEntries() {
    const filter = state.filter.trim().toLocaleLowerCase("zh-CN");
    const entries = state.entries.filter((entry) => !filter || entry.name.toLocaleLowerCase("zh-CN").includes(filter));
    const { key, direction } = state.sort;
    entries.sort((left, right) => {
      const leftDir = entryIsDirectory(left), rightDir = entryIsDirectory(right);
      if (leftDir !== rightDir) return leftDir ? -1 : 1;
      let a = left[key] ?? "", b = right[key] ?? "";
      if (key === "type") { a = typeInfo(left).label; b = typeInfo(right).label; }
      if (key === "size") { a = Number(a) || 0; b = Number(b) || 0; }
      if (key === "modified") { a = new Date(a).getTime() || 0; b = new Date(b).getTime() || 0; }
      return (typeof a === "number" ? a - b : String(a).localeCompare(String(b), "zh-CN", { numeric: true, sensitivity: "base" })) * direction;
    });

    elements.fileList.innerHTML = entries.map((entry) => {
      const info = typeInfo(entry);
      const selected = state.selected?.name === entry.name;
      return `<tr data-name="${escapeHtml(entry.name)}" class="${selected ? "selected" : ""}">
        <td><div class="name-cell"><span class="file-icon ${info.iconClass}">${escapeHtml(info.badge)}</span><span class="file-name">${escapeHtml(entry.name)}</span></div></td>
        <td>${escapeHtml(formatDate(entry.modified))}</td><td>${escapeHtml(info.label)}</td>
        <td>${entry.size === undefined || entry.size === null ? "正在计算…" : escapeHtml(formatBytes(entry.size))}</td></tr>`;
    }).join("");

    elements.empty.hidden = state.loading || entries.length > 0;
    const folderCount = entries.filter(entryIsDirectory).length;
    const fileCount = entries.length - folderCount;
    elements.folderMeta.textContent = `${folderCount} 个文件夹，${fileCount} 个文件`;
    elements.status.textContent = filter ? `找到 ${entries.length} 个项目` : `${entries.length} 个项目`;
    bindRows();
  }

  function bindRows() {
    $$("#fileList tr").forEach((row) => {
      row.addEventListener("click", () => selectEntry(row.dataset.name));
      row.addEventListener("dblclick", () => openEntry(row.dataset.name));
    });
  }

  function selectEntry(name) {
    state.selected = state.entries.find((entry) => entry.name === name) || null;
    renderEntries();
    updateSelection();
  }

  function updateSelection() {
    const has = Boolean(state.selected);
    elements.download.disabled = !has;
    elements.rename.disabled = !has;
    elements.delete.disabled = !has;
    elements.newFolder.disabled = state.view !== "files";
    elements.upload.disabled = state.view !== "files";
    elements.up.disabled = state.view === "home";
    elements.selection.textContent = has ? `已选择 1 个项目  ${formatBytes(state.selected.size)}` : "";
  }

  function openEntry(name) {
    const entry = state.entries.find((item) => item.name === name);
    if (!entry) return;
    if (entryIsDirectory(entry)) openFolder(state.source, entryPath(entry));
    else downloadEntry(entry);
  }

  function downloadEntry(entry = state.selected) {
    if (!entry) return;
    const params = { source: state.source, file: entryPath(entry) };
    if (entryIsDirectory(entry)) params.algo = "zip";
    location.href = apiUrl("resources/download", params);
  }

  function renderBreadcrumbs() {
    if (state.view === "home") {
      elements.breadcrumbs.innerHTML = '<button class="crumb" data-home="true"><span class="crumb-icon">▰</span>主页</button>';
      return;
    }
    const parts = state.path.split("/").filter(Boolean);
    let accumulated = "/";
    const crumbs = [`<button class="crumb" data-home="true"><span class="crumb-icon">▰</span>主页</button>`, `<button class="crumb" data-source="${state.source}" data-path="/">${escapeHtml(SOURCES[state.source].title)}</button>`];
    parts.forEach((part) => {
      accumulated = joinPath(accumulated, part, true);
      crumbs.push(`<button class="crumb" data-source="${state.source}" data-path="${escapeHtml(accumulated)}">${escapeHtml(decodeURIComponent(part))}</button>`);
    });
    elements.breadcrumbs.innerHTML = crumbs.join("");
  }

  function activateSidebar(target, path) {
    $$(".side-item").forEach((item) => item.classList.remove("active"));
    if (target === "home") $(".side-item[data-target='home']").classList.add("active");
    else {
      const selector = `.side-item.nested[data-source="${target}"]`;
      ($(selector) || $(`.side-item[data-source="${target}"]`))?.classList.add("active");
    }
  }

  function pushHistory(locationState) {
    state.history = state.history.slice(0, state.historyIndex + 1);
    state.history.push(locationState);
    state.historyIndex = state.history.length - 1;
    updateNavigationButtons();
    updateHash();
  }

  function updateHash() {
    const hash = state.view === "home" ? "#/home" : `#/${encodeURIComponent(state.source)}${state.path.split("/").map(encodeURIComponent).join("/")}`;
    history.replaceState(null, "", hash);
  }

  function updateNavigationButtons() {
    elements.back.disabled = state.historyIndex <= 0;
    elements.forward.disabled = state.historyIndex >= state.history.length - 1;
    elements.up.disabled = state.view === "home";
  }

  function travel(delta) {
    const index = state.historyIndex + delta;
    if (index < 0 || index >= state.history.length) return;
    state.historyIndex = index;
    const destination = state.history[index];
    destination.view === "home" ? showHome(false) : openFolder(destination.source, destination.path, false);
    updateNavigationButtons();
    updateHash();
  }

  async function createFolder() {
    if (state.view !== "files") return;
    const name = await promptValue("新建文件夹", "请输入文件夹名称", "新建文件夹");
    if (!name) return;
    if (/[\\/:*?"<>|]/.test(name)) return toast("名称不能包含 \\ / : * ? \" < > |", "error");
    try {
      await apiFetch("resources", { source: state.source, path: joinPath(state.path, name, true), override: "false", isDir: "true" }, { method: "POST", body: new Blob([]) });
      toast(`已创建“${name}”`, "success");
      await openFolder(state.source, state.path, false);
    } catch (error) { toast(`创建失败：${error.message}`, "error"); }
  }

  async function renameSelected() {
    if (!state.selected) return;
    const original = state.selected.name;
    const name = await promptValue("重命名", "输入新的名称", original);
    if (!name || name === original) return;
    if (/[\\/:*?"<>|]/.test(name)) return toast("名称包含无效字符", "error");
    const fromPath = entryPath(state.selected);
    const toPath = joinPath(state.path, name, entryIsDirectory(state.selected));
    const payload = { items: [{ fromSource: state.source, fromPath, toSource: state.source, toPath }], action: "rename", overwrite: false, rename: false };
    try {
      await apiFetch("resources", {}, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      toast("重命名完成", "success");
      await openFolder(state.source, state.path, false);
    } catch (error) { toast(`重命名失败：${error.message}`, "error"); }
  }

  async function deleteSelected() {
    if (!state.selected) return;
    const confirmed = await confirmAction("永久删除此项目？", `“${state.selected.name}”将被直接删除，无法从回收站恢复。`);
    if (!confirmed) return;
    try {
      await apiFetch("resources", { source: state.source, path: entryPath(state.selected) }, { method: "DELETE" });
      toast("删除完成", "success");
      await openFolder(state.source, state.path, false);
      loadCapacities();
    } catch (error) { toast(`删除失败：${error.message}`, "error"); }
  }

  function promptValue(title, description, initial = "") {
    $("#promptTitle").textContent = title;
    $("#promptDescription").textContent = description;
    $("#promptInput").value = initial;
    elements.promptDialog.showModal();
    requestAnimationFrame(() => { $("#promptInput").focus(); $("#promptInput").select(); });
    return new Promise((resolve) => elements.promptDialog.addEventListener("close", () => resolve(elements.promptDialog.returnValue === "confirm" ? $("#promptInput").value.trim() : ""), { once: true }));
  }

  function confirmAction(title, description) {
    $("#confirmTitle").textContent = title;
    $("#confirmDescription").textContent = description;
    elements.confirmDialog.showModal();
    return new Promise((resolve) => elements.confirmDialog.addEventListener("close", () => resolve(elements.confirmDialog.returnValue === "confirm"), { once: true }));
  }

  function addTransfer(file) {
    elements.transferPanel.hidden = false;
    const item = document.createElement("div");
    item.className = "transfer-item";
    item.innerHTML = `<div class="transfer-name"><span>${escapeHtml(file.name)}</span><span>等待中</span></div><div class="transfer-progress"><i></i></div>`;
    elements.transferList.prepend(item);
    return {
      update(percent, text) { item.querySelector("i").style.width = `${percent}%`; item.querySelector(".transfer-name span:last-child").textContent = text || `${Math.round(percent)}%`; },
      error(text) { item.classList.add("error"); item.querySelector(".transfer-name span:last-child").textContent = text; },
    };
  }

  async function uploadFiles(files) {
    if (state.view !== "files" || !files.length) return;
    const source = state.source, path = state.path;
    for (const file of files) {
      const transfer = addTransfer(file);
      try {
        let offset = 0;
        while (offset < file.size || (file.size === 0 && offset === 0)) {
          const chunk = file.slice(offset, Math.min(offset + CHUNK_SIZE, file.size));
          const response = await fetch(apiUrl("resources", { source, path: joinPath(path, file.name, false), override: "false" }), {
            method: "POST", credentials: "same-origin", body: chunk,
            headers: { "X-File-Chunk-Offset": String(offset), "X-File-Total-Size": String(file.size) },
          });
          if (!response.ok) throw new Error(response.status === 409 ? "同名文件已存在" : `${response.status} ${response.statusText}`);
          offset += chunk.size;
          const percent = file.size ? (offset / file.size) * 100 : 100;
          transfer.update(percent, `${Math.min(100, Math.round(percent))}%`);
          if (file.size === 0) break;
        }
        transfer.update(100, "已完成");
      } catch (error) {
        transfer.error("失败");
        toast(`${file.name} 上传失败：${error.message}`, "error");
      }
    }
    if (state.source === source && state.path === path) await openFolder(source, path, false);
    loadCapacities();
  }

  function bindEvents() {
    $$(".drive-card, .quick-card, .side-item[data-source]").forEach((item) => item.addEventListener("dblclick", () => openFolder(item.dataset.source, item.dataset.path || "/")));
    $$(".drive-card, .quick-card, .side-item[data-source]").forEach((item) => item.addEventListener("click", () => openFolder(item.dataset.source, item.dataset.path || "/")));
    $(".side-item[data-target='home']").addEventListener("click", () => showHome());
    $("#networkItem").addEventListener("click", () => toast("RK3576 已通过 Headscale/Tailscale 安全网络连接"));
    elements.breadcrumbs.addEventListener("click", (event) => {
      const crumb = event.target.closest(".crumb");
      if (!crumb) return;
      crumb.dataset.home ? showHome() : openFolder(crumb.dataset.source, crumb.dataset.path);
    });
    elements.back.addEventListener("click", () => travel(-1));
    elements.forward.addEventListener("click", () => travel(1));
    elements.up.addEventListener("click", () => state.view === "files" && (state.path === "/" ? showHome() : openFolder(state.source, parentPath(state.path))));
    elements.refresh.addEventListener("click", () => state.view === "home" ? loadCapacities() : openFolder(state.source, state.path, false));
    elements.newFolder.addEventListener("click", createFolder);
    elements.upload.addEventListener("click", () => elements.fileInput.click());
    elements.fileInput.addEventListener("change", () => { uploadFiles([...elements.fileInput.files]); elements.fileInput.value = ""; });
    elements.download.addEventListener("click", () => downloadEntry());
    elements.rename.addEventListener("click", renameSelected);
    elements.delete.addEventListener("click", deleteSelected);
    $("#closeTransfers").addEventListener("click", () => { elements.transferPanel.hidden = true; });
    elements.search.addEventListener("input", () => { state.filter = elements.search.value; if (state.view === "files") renderEntries(); });
    $$(".file-table th[data-sort]").forEach((header) => header.addEventListener("click", () => {
      const key = header.dataset.sort;
      state.sort.direction = state.sort.key === key ? -state.sort.direction : 1;
      state.sort.key = key;
      renderEntries();
    }));
    elements.dropZone.addEventListener("dragover", (event) => { event.preventDefault(); elements.dropZone.classList.add("dragging"); });
    elements.dropZone.addEventListener("dragleave", (event) => { if (!elements.dropZone.contains(event.relatedTarget)) elements.dropZone.classList.remove("dragging"); });
    elements.dropZone.addEventListener("drop", (event) => { event.preventDefault(); elements.dropZone.classList.remove("dragging"); uploadFiles([...event.dataTransfer.files]); });
    elements.content.addEventListener("click", (event) => {
      if (state.view === "files" && !event.target.closest("tr") && !event.target.closest("button")) { state.selected = null; renderEntries(); updateSelection(); }
    });
    document.addEventListener("keydown", (event) => {
      if (event.key === "F2" && state.selected) { event.preventDefault(); renameSelected(); }
      if (event.key === "Delete" && state.selected) { event.preventDefault(); deleteSelected(); }
      if (event.key === "F5") { event.preventDefault(); elements.refresh.click(); }
      if (event.key === "Enter" && state.selected && document.activeElement?.tagName !== "INPUT") openEntry(state.selected.name);
      if (event.altKey && event.key === "ArrowUp") { event.preventDefault(); elements.up.click(); }
      if (event.altKey && event.key === "ArrowLeft") { event.preventDefault(); travel(-1); }
      if (event.altKey && event.key === "ArrowRight") { event.preventDefault(); travel(1); }
    });
  }

  function startFromHash() {
    const raw = location.hash.replace(/^#\/?/, "");
    if (!raw || raw === "home") return showHome();
    const parts = raw.split("/");
    const source = decodeURIComponent(parts.shift());
    const path = cleanPath(`/${parts.map(decodeURIComponent).join("/")}`);
    return SOURCES[source] ? openFolder(source, path) : showHome();
  }

  bindEvents();
  startFromHash();
})();
