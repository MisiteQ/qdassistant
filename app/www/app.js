/* QDAssistant 签到助手 —— 前端交互
   零依赖原生 JS。所有状态变更请求都带 X-QD-Request 头（服务端 CSRF 闸）。

   数据层约定：所有接口数据统一进 cache，渲染函数只读 cache；
   loadXxx 负责「拉取 → 写缓存 → 渲染」。切页时先用缓存即时渲染
   （无白屏、无跳变），再后台拉最新；任何模块变更后 refreshAll
   全量刷新，联动不再依赖用户手动切页。 */

"use strict";

const state = {
  page: "overview",
  tab: "workbuddy",
  authRequired: false,
};

const cache = {
  status: null, // /api/status
  checkin: null, // /api/checkin/status
  accounts: null, // /api/accounts
  config: null, // /api/config
};

/* ---------------- 网络 ---------------- */

/* 接口路径按「当前文档基准」解析，不能写死绝对路径。
   直连 IP:端口时文档在根，基准为 /，请求落到 /api/xxx；
   经飞牛统一网关 / 域名访问时，文档挂在 /app/qdassistant/（<base> 已修正），
   此时绝对路径 /api/xxx 会跳出应用前缀打到网关根 —— 那不是本应用，
   真实设备上的表现就是面板一直转圈、浏览器提示「响应时间过长」。
   去掉前导斜杠后由文档基准解析，两种访问形态都能命中应用自己。 */
function apiUrl(path) {
  if (/^[a-z][a-z0-9+.-]*:\/\//i.test(path)) return path; // 已是完整 URL，原样返回
  return path.replace(/^\/+/, "");
}

async function api(path, options = {}) {
  const url = apiUrl(path);
  const init = { headers: { "X-QD-Request": "1" }, ...options };
  if (init.body && typeof init.body === "object") {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(init.body);
  }
  // fetch 默认没有超时：对端不返回就永远不 settle，界面会永久停在加载中。
  // 网关并发转发不可靠，超时必须显式加上（启动请求放宽到 25s）。
  const timeoutMs = init.timeoutMs || 15000;
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  let resp;
  try {
    resp = await fetch(url, { ...init, signal: ctrl.signal });
  } catch (err) {
    clearTimeout(timer);
    if (err && err.name === "AbortError") {
      throw new Error(`请求超时（${timeoutMs / 1000}s 未响应）：${url}`);
    }
    throw new Error(`网络请求失败：${url}（${err && err.message}）`);
  }
  clearTimeout(timer);
  if (resp.status === 401) {
    state.authRequired = true;
    showLogin();
    throw new Error("需要登录");
  }
  const data = await resp.json().catch(() => ({}));
  if (!resp.ok) throw new Error(data.error || `请求失败 (${resp.status})`);
  return data;
}

const get = (path) => api(path);
const post = (path, body) => api(path, { method: "POST", body: body || {} });

/* ---------------- 登录 ---------------- */

function showLogin() {
  document.getElementById("login-mask").classList.remove("hidden");
  document.getElementById("login-password").focus();
}

async function doLogin() {
  const password = document.getElementById("login-password").value;
  const errBox = document.getElementById("login-error");
  errBox.classList.add("hidden");
  try {
    const data = await post("/api/login", { password });
    if (data.token) {
      document.cookie = `qd_token=${data.token}; path=/; max-age=2592000; samesite=lax`;
    }
    document.getElementById("login-mask").classList.add("hidden");
    await refreshAll();
  } catch (err) {
    errBox.textContent = err.message;
    errBox.classList.remove("hidden");
  }
}

/* ---------------- 页面切换 ---------------- */

/* 每页的后台刷新清单：切页即拉，轮询也按当前页取用 */
const PAGE_LOADERS = {
  overview: [loadStatus, loadCheckinStatus, () => loadStats(false)],
  checkin: [loadStatus, loadCheckinStatus, loadAccounts, loadConfig],
  settings: [loadConfig, loadStatus, loadAccounts],
  about: [loadStatus, loadLogs],
};

function switchPage(page) {
  state.page = page;
  document.querySelectorAll(".page").forEach((el) => el.classList.add("hidden"));
  document.getElementById(`page-${page}`).classList.remove("hidden");
  document.querySelectorAll(".nav-item").forEach((el) => {
    el.classList.toggle("active", el.dataset.page === page);
  });
  // 先用缓存即时渲染（秒开），再后台拉最新数据
  renderPage(page);
  runPageLoaders(page);
}

/* 串行执行某页的后台刷新清单。
   飞牛统一网关的并发转发不可靠：并发发出的这几个接口会一起悬住，
   界面永远停在旧数据，且控制台没有任何报错。逐个 await 反而稳定。 */
function runPageLoaders(page) {
  const loaders = PAGE_LOADERS[page] || [];
  (async () => {
    for (const fn of loaders) {
      try {
        await fn();
      } catch (err) {
        /* 单接口失败不阻塞其余 */
      }
    }
  })();
}

function renderPage(page) {
  if (page === "overview") {
    renderOverview();
    renderStats();
  } else if (page === "checkin") {
    renderCheckinStatus();
    fillSettingsForm();
  } else if (page === "settings") {
    renderAccounts();
    fillSettingsForm();
  } else if (page === "about") {
    renderAbout();
    renderLogsFromCache();
  }
}

function switchTab(tab) {
  state.tab = tab;
  ["workbuddy", "trae"].forEach((name) => {
    document.getElementById(`tab-${name}`).classList.toggle("hidden", name !== tab);
  });
  document.querySelectorAll("#account-tabs .tab").forEach((el) => {
    el.classList.toggle("active", el.dataset.tab === tab);
  });
}

/* ---------------- 渲染：总览 ---------------- */

/* 关于面板：版本 / 数据目录 / 运行时长 / 账号数，全部以服务端状态为准 */
function renderAbout() {
  const status = cache.status;
  if (!status) return;
  document.getElementById("about-version").textContent = `v${status.version}`;
  document.getElementById("about-datadir").textContent = status.dataDir || "-";
  const up = Number(status.uptimeSeconds || 0);
  document.getElementById("about-uptime").textContent = up
    ? `${Math.floor(up / 86400)} 天 ${Math.floor((up % 86400) / 3600)} 小时 ${Math.floor((up % 3600) / 60)} 分`
    : "刚刚启动";
  const counts = status.counts || {};
  document.getElementById("about-accounts").textContent =
    `WorkBuddy ${counts.workbuddy || 0} 个 · Trae ${counts.trae || 0} 个`;
}

function renderOverview() {
  const status = cache.status;
  if (!status) return;
  document.getElementById("side-version").textContent = `v${status.version}`;
  document.getElementById("side-date").textContent = status.localDate;
  renderAbout();

  const pending = cache.checkin || {};
  const fill = (prefix, block) => {
    const total = (block && block.total) || 0;
    const wait = (block && block.pending ? block.pending.length : 0);
    document.getElementById(`${prefix}-total`).textContent = total;
    document.getElementById(`${prefix}-pending`).textContent = wait;
    document.getElementById(`${prefix}-done`).textContent = Math.max(0, total - wait);
  };
  fill("wb", pending.workbuddy);
  fill("trae", pending.trae);

  document.getElementById("wb-status-sub").textContent = status.workbuddyEnabled
    ? "自动签到已启用"
    : "自动签到已关闭";
  document.getElementById("trae-status-sub").textContent = status.traeEnabled
    ? "自动签到已启用"
    : "自动签到已关闭";

  const notice = document.getElementById("overview-notice");
  if (!status.autoEnabled) {
    notice.innerHTML =
      '<div class="notice info">后台自动签到已关闭，可在「签到」页中开启。</div>';
  } else {
    notice.innerHTML = "";
  }

  const sched = status.scheduler || {};
  const lines = [];
  ["workbuddy", "trae"].forEach((name) => {
    const item = sched[name];
    if (!item) {
      lines.push(`${name}：尚未执行过（服务启动 5 秒后跑第一轮）`);
      return;
    }
    const at = new Date(item._at || Date.now()).toLocaleString("zh-CN");
    const label = { workbuddy: "WorkBuddy", trae: "Trae" }[name];
    lines.push(`${label}：${item.status || "-"} · ${at}`);
  });
  const info = document.getElementById("scheduler-info");
  info.textContent = lines.join("\n");
  info.style.whiteSpace = "pre-line";
  renderSideMini();
}

/* 侧栏迷你进度：与「今日待签到」同源（cache.checkin），任何刷新都会同步 */
function renderSideMini() {
  const el = document.getElementById("side-mini");
  const data = cache.checkin;
  if (!el || !data) return;
  const fill = (platform, barId, textId) => {
    const block = data[platform] || { total: 0, pending: [] };
    const pending = (block.pending || []).length;
    const done = Math.max(0, (block.total || 0) - pending);
    const pct = block.total ? Math.round((done / block.total) * 100) : 0;
    const bar = document.getElementById(barId);
    if (bar) bar.style.width = pct + "%";
    const text = document.getElementById(textId);
    if (text) text.textContent = `${done}/${block.total || 0}`;
  };
  fill("workbuddy", "mini-wb-bar", "mini-wb-text");
  fill("trae", "mini-trae-bar", "mini-trae-text");
}

/* ---------------- 渲染：账号表 ---------------- */

function badge(text, kind) {
  return `<span class="badge ${kind}">${text}</span>`;
}

function resultBadge(value) {
  if (value === "success") return badge("已签到", "ok");
  if (value === "already") return badge("今日已签", "ok");
  if (value === "error" || value === "fail" || value === "failed") return badge("失败", "err");
  if (value === "skipped") return badge("已跳过", "muted");
  if (value === "inactive") return badge("未激活", "muted");
  return badge("未签到", "muted");
}

function renderAccounts() {
  if (!cache.accounts) return;
  renderWorkbuddyTable(cache.accounts.accounts);
  renderTraeTable(cache.accounts.accounts);
}

function renderWorkbuddyTable(accounts) {
  const rows = accounts.filter((a) => a.platform === "workbuddy");
  document.getElementById("wb-count").textContent = `共 ${rows.length} 个`;
  const box = document.getElementById("wb-table");
  if (!rows.length) {
    box.innerHTML = '<div class="empty">还没有 WorkBuddy 账号</div>';
    return;
  }
  const body = rows
    .map(
      (a) => `<tr>
        <td>${escapeHtml(a.name)}</td>
        <td>${badge(a.variant === "ai" ? "国际版" : "国内版", a.variant === "ai" ? "warn" : "info")}</td>
        <td class="mono">${escapeHtml(a.accessTokenMasked)}</td>
        <td>${a.hasRefreshToken ? badge("有", "ok") : badge("无", "muted")}</td>
        <td>${a.needsRelogin ? badge("需重新登录", "err") : a.supportsCheckin ? badge("支持", "ok") : badge("不支持", "muted")}</td>
        <td>${resultBadge(a.todayResult)}</td>
        <td>
          <button class="btn btn-sm" data-wb-checkin="${a.id}">签到</button>
          <button class="btn btn-sm" data-wb-refresh="${a.id}">刷新 token</button>
          <button class="btn btn-sm danger" data-wb-del="${a.id}">删除</button>
        </td>
      </tr>`
    )
    .join("");
  box.innerHTML = `<table><thead><tr>
      <th>账号</th><th>档位</th><th>token</th><th>refresh</th><th>状态</th><th>今日</th><th>操作</th>
    </tr></thead><tbody>${body}</tbody></table>`;

  box.querySelectorAll("[data-wb-checkin]").forEach((btn) =>
    btn.onclick = () => runCheckin("workbuddy", btn.dataset.wbCheckin, "wb-checkin-result")
  );
  box.querySelectorAll("[data-wb-refresh]").forEach((btn) =>
    btn.onclick = async () => {
      try {
        await post("/api/accounts/refresh-token", { id: btn.dataset.wbRefresh });
        await refreshAll();
      } catch (err) { alert(err.message); }
    }
  );
  box.querySelectorAll("[data-wb-del]").forEach((btn) =>
    btn.onclick = async () => {
      if (!confirm("确定删除该账号？")) return;
      await post("/api/accounts/delete", { platform: "workbuddy", id: btn.dataset.wbDel });
      await refreshAll();
    }
  );
}

function renderTraeTable(accounts) {
  const rows = accounts.filter((a) => a.platform === "trae");
  document.getElementById("trae-count").textContent = `共 ${rows.length} 个`;
  const box = document.getElementById("trae-table");
  if (!rows.length) {
    box.innerHTML = '<div class="empty">还没有 Trae 账号</div>';
    return;
  }
  const jwtBadge = (status) => {
    if (status === "ok") return badge("正常", "ok");
    if (status === "warn") return badge("即将过期", "warn");
    if (status === "expired") return badge("已过期", "err");
    return badge("未知", "muted");
  };
  const body = rows
    .map(
      (a) => `<tr>
        <td>${escapeHtml(a.name)}${a.email ? `<div style="color:var(--muted);font-size:12px">${escapeHtml(a.email)}</div>` : ""}</td>
        <td class="mono">${escapeHtml(a.userId || "")}</td>
        <td class="mono">${escapeHtml(a.jwtMasked)}</td>
        <td>${jwtBadge(a.jwtStatus)}${a.expHours !== null ? ` <span style="color:var(--muted)">${a.expHours}h</span>` : ""}</td>
        <td>${a.cooldown ? badge(`冷却中 · ${a.cooldown}`, "warn") : badge("正常", "ok")}</td>
        <td>${resultBadge(a.todayResult)}</td>
        <td>
          <button class="btn btn-sm" data-trae-checkin="${a.id}">签到</button>
          ${a.hasRefreshToken ? `<button class="btn btn-sm" data-trae-refresh="${a.id}">刷新 JWT</button>` : ""}
          <button class="btn btn-sm danger" data-trae-del="${a.id}">删除</button>
        </td>
      </tr>`
    )
    .join("");
  box.innerHTML = `<table><thead><tr>
      <th>名称</th><th>user id</th><th>JWT</th><th>有效期</th><th>冷却</th><th>今日</th><th>操作</th>
    </tr></thead><tbody>${body}</tbody></table>`;

  box.querySelectorAll("[data-trae-checkin]").forEach((btn) =>
    btn.onclick = () => runCheckin("trae", btn.dataset.traeCheckin, "trae-checkin-result")
  );
  box.querySelectorAll("[data-trae-refresh]").forEach((btn) =>
    btn.onclick = async () => {
      try {
        const data = await post("/api/accounts/refresh-jwt", { id: btn.dataset.traeRefresh });
        if (data.error) throw new Error(data.error);
        await refreshAll();
      } catch (err) { alert(err.message); }
    }
  );
  box.querySelectorAll("[data-trae-del]").forEach((btn) =>
    btn.onclick = async () => {
      if (!confirm("确定删除该账号？")) return;
      await post("/api/accounts/delete", { platform: "trae", id: btn.dataset.traeDel });
      await refreshAll();
    }
  );
}

/* ---------------- OAuth 登录 ---------------- */

let wbOAuthTimer = null;

document.getElementById("wb-oauth-btn").onclick = async () => {
  const box = document.getElementById("wb-oauth-box");
  const status = document.getElementById("wb-oauth-status");
  box.classList.remove("hidden");
  status.textContent = "正在申请授权链接…";
  try {
    const data = await post("/api/oauth/workbuddy/start", { variant: document.getElementById("wb-variant").value });
    status.innerHTML =
      `在任意设备的浏览器打开下面的链接完成授权（<b>${data.expiresIn / 60} 分钟内有效</b>）：` +
      `<div style="margin-top:6px"><a href="${data.verificationUri}" target="_blank" rel="noopener">${escapeHtml(data.verificationUri)}</a></div>` +
      `<div style="margin-top:6px;color:var(--muted)">授权完成后本页会自动完成，无需手动操作。</div>`;
    if (wbOAuthTimer) clearInterval(wbOAuthTimer);
    wbOAuthTimer = setInterval(async () => {
      try {
        const result = await post("/api/oauth/workbuddy/poll", { loginId: data.loginId });
        if (result.done) {
          clearInterval(wbOAuthTimer);
          wbOAuthTimer = null;
          if (result.error) {
            status.className = "notice err";
            status.textContent = `登录失败：${result.error}`;
          } else {
            status.className = "notice ok";
            status.textContent = `已登录：${(result.result || {}).name || ""}`;
            setTimeout(() => box.classList.add("hidden"), 2000);
            await refreshAll();
          }
        }
      } catch (err) {
        clearInterval(wbOAuthTimer);
        wbOAuthTimer = null;
        status.className = "notice err";
        status.textContent = `轮询失败：${err.message}`;
      }
    }, 3000);
  } catch (err) {
    status.className = "notice err";
    status.textContent = `发起失败：${err.message}`;
  }
};

/* Trae 授权的完整体验都在这一段：先把「回调页必然打不开」讲清楚，再给回填框
   加倒计时与即时校验——异地授权场景下用户唯一的困惑就是这一步。 */

const traeOAuth = { timer: null, checkTimer: null, deadline: 0 };

function fmtRemain(sec) {
  return `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}`;
}

function stopTraeCountdown() {
  if (traeOAuth.timer) {
    clearInterval(traeOAuth.timer);
    traeOAuth.timer = null;
  }
}

function startTraeCountdown(seconds) {
  stopTraeCountdown();
  const el = document.getElementById("trae-oauth-countdown");
  const submit = document.getElementById("trae-callback-submit");
  const msg = document.getElementById("trae-oauth-msg");
  traeOAuth.deadline = Date.now() + seconds * 1000;
  submit.disabled = false;
  const tick = () => {
    const remain = Math.max(0, Math.round((traeOAuth.deadline - Date.now()) / 1000));
    if (remain <= 0) {
      stopTraeCountdown();
      el.textContent = "AuthCode 已过期";
      el.classList.add("expired");
      submit.disabled = true;
      msg.innerHTML =
        '<div class="notice err">AuthCode 已过期（有效期 10 分钟）——请点「重新发起登录」重新授权。</div>';
      return;
    }
    el.textContent = `剩余 ${fmtRemain(remain)}`;
    el.classList.toggle("expired", remain <= 60);
  };
  tick();
  traeOAuth.timer = setInterval(tick, 1000);
}

async function runTraeCallbackCheck() {
  const hint = document.getElementById("trae-callback-hint");
  const url = document.getElementById("trae-callback").value.trim();
  if (!url) {
    hint.className = "hint";
    hint.textContent = "";
    return;
  }
  try {
    const res = await post("/api/oauth/trae/callback/check", { callbackUrl: url });
    const check = res.check || {};
    hint.className = "hint " + (check.ok ? "ok" : check.expired ? "err" : "warn");
    hint.textContent = check.reason || (check.ok ? "这条链接可用，点「完成登录」" : "这条链接看起来不完整");
  } catch (err) {
    // 校验只是体验增强：服务端没响应时不能挡住提交
    hint.className = "hint warn";
    hint.textContent = "校验未响应，可直接点「完成登录」试试";
  }
}

async function startTraeOAuth() {
  const box = document.getElementById("trae-oauth-box");
  const status = document.getElementById("trae-oauth-status");
  const msg = document.getElementById("trae-oauth-msg");
  box.classList.remove("hidden");
  msg.innerHTML = "";
  document.getElementById("trae-callback").value = "";
  document.getElementById("trae-callback-hint").className = "hint";
  document.getElementById("trae-callback-hint").textContent = "";
  status.textContent = "正在生成登录链接…";
  try {
    const data = await post("/api/oauth/trae/start", {});
    status.innerHTML =
      `1) 在浏览器打开下面的链接并完成授权：` +
      `<div style="margin-top:6px"><a href="${data.url}" target="_blank" rel="noopener">点击打开 Trae 授权页</a></div>` +
      `<div style="margin-top:6px">2) 授权后浏览器会跳转到 ${escapeHtml(data.redirectUri || "127.0.0.1:17388")} ——` +
      `<b>该页面打不开是正常现象</b>，不用管报错；</div>` +
      `<div style="margin-top:2px">3) <b>全选复制地址栏里的完整 URL</b>，粘贴到下方输入框。</div>`;
    status.className = "notice info";
    startTraeCountdown(data.authCodeTtl || 600);
  } catch (err) {
    status.className = "notice err";
    status.textContent = `生成失败：${err.message}`;
  }
}

/* 客户端凭据导入：NAS 上 OAuth 走不通时的主路径，直接解出 JWT 入库 */

document.getElementById("trae-storage-import").onclick = async () => {
  const msg = document.getElementById("trae-storage-msg");
  const box = document.getElementById("trae-storage");
  const nameInput = document.getElementById("trae-storage-name");
  const content = box.value.trim();
  msg.innerHTML = "";
  if (!content) {
    msg.innerHTML = '<div class="notice err">请先粘贴客户端全局存储文件（storage.json）的内容</div>';
    return;
  }
  try {
    const data = await post("/api/accounts/import-storage", { content, name: nameInput.value.trim() });
    const acc = data.account || {};
    const id = data.identity || {};
    const who = [id.name, id.email].filter(Boolean).map(escapeHtml).join(" · ") || escapeHtml(acc.name || "");
    const extra = data.hasRefreshToken ? "，可自动续期" : "";
    msg.innerHTML =
      `<div class="notice ok">已导入：<b>${who}</b>（user id ${escapeHtml(id.userId || acc.userId || "")}${extra}）</div>` +
      `<div class="notice info" style="font-size:12px">列表里的账号已按这个名字显示；多个账号可凭名称 / 邮箱区分。</div>`;
    box.value = "";
    nameInput.value = "";
    await refreshAll();
  } catch (err) {
    msg.innerHTML = `<div class="notice err">${escapeHtml(err.message)}</div>`;
  }
};

document.getElementById("trae-oauth-btn").onclick = () => {
  document.getElementById("trae-oauth-guide").classList.remove("hidden");
};
document.getElementById("trae-guide-cancel").onclick = () => {
  document.getElementById("trae-oauth-guide").classList.add("hidden");
};
document.getElementById("trae-guide-go").onclick = () => {
  document.getElementById("trae-oauth-guide").classList.add("hidden");
  startTraeOAuth();
};
document.getElementById("trae-oauth-restart").onclick = () => startTraeOAuth();

document.getElementById("trae-callback").addEventListener("input", () => {
  clearTimeout(traeOAuth.checkTimer);
  traeOAuth.checkTimer = setTimeout(runTraeCallbackCheck, 300);
});

document.getElementById("trae-callback-submit").onclick = async () => {
  const msg = document.getElementById("trae-oauth-msg");
  msg.innerHTML = "";
  const url = document.getElementById("trae-callback").value.trim();
  if (!url) {
    msg.innerHTML = '<div class="notice err">请先粘贴回调 URL</div>';
    return;
  }
  try {
    const data = await post("/api/oauth/trae/callback", { callbackUrl: url });
    if (data.ok === false) {
      // 过期 / 被拒都由服务端给出可读文案，这里只负责把重试入口摆出来
      msg.innerHTML = `<div class="notice err">${escapeHtml(data.error || "登录失败")}</div>`;
      if (data.expired) stopTraeCountdown();
      return;
    }
    stopTraeCountdown();
    msg.innerHTML = `<div class="notice ok">已登录：${escapeHtml(data.name || "")}${data.merged ? "（已更新既有账号）" : ""}</div>`;
    document.getElementById("trae-callback").value = "";
    setTimeout(() => document.getElementById("trae-oauth-box").classList.add("hidden"), 2500);
    await refreshAll();
  } catch (err) {
    msg.innerHTML = `<div class="notice err">${escapeHtml(err.message)}</div>`;
  }
};

/* ---------------- 渲染：统计（双平台对等） ----------------

   WorkBuddy 与 Trae 各有一套「积分统计 + Token 统计」，页面结构完全相同，
   只是数据源不同：WorkBuddy 的消耗维度是积分（官方请求用量接口），Trae 同时
   有积分与 Token（会话用量接口带 token 明细）。切平台只重渲染，不重新拉数据。 */

const statsState = {
  days: 30,
  scope: "workbuddy",
  data: { workbuddy: { credits: null, usage: null }, trae: { credits: null, usage: null } },
  at: 0,
};

const PLATFORM_LABEL = { workbuddy: "WorkBuddy", trae: "Trae" };
const MODEL_COLORS = ["#2f9e6e", "#4a9df8", "#9a6df0", "#e8a33d", "#e05f5f", "#3fbfbf", "#7c8bf5", "#c58bd6", "#8bbf5a", "#f08c5a"];

function fmtNum(n) {
  if (n === null || n === undefined || Number.isNaN(n)) return "-";
  if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + " 亿";
  if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(1) + "K";
  return Number(n).toLocaleString("zh-CN", { maximumFractionDigits: 2 });
}

function fmtTokens(n) {
  if (!n) return "0";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(n);
}

function modelColor(name, index) {
  let hash = 0;
  for (const ch of String(name)) hash = (hash * 31 + ch.charCodeAt(0)) & 0xffff;
  return MODEL_COLORS[(hash + index) % MODEL_COLORS.length];
}

async function loadStats(force) {
  const msg = document.getElementById("stats-msg");
  document.querySelectorAll("[data-days]").forEach((btn) => {
    btn.classList.toggle("active", Number(btn.dataset.days) === statsState.days);
  });
  if (msg) {
    msg.innerHTML = '<div class="notice info">正在拉取官方统计（多账号时可能需要几秒，结果有 10 分钟缓存）…</div>';
  }
  const days = statsState.days;
  const forceQ = force ? "&force=1" : "";
  // 串行取数：真机上飞牛统一网关的并发转发不可靠 —— 四个统计接口一起发出会
  // 集体悬住（面板停在「正在拉取」、控制台却干净）。逐个 await 反而稳定，
  // 且每拿到一项就渲染一项，体感更连续。
  const targets = [
    ["workbuddy", "credits", `/api/stats/workbuddy/credits${force ? "?force=1" : ""}`],
    ["workbuddy", "usage", `/api/stats/workbuddy/usage?days=${days}${forceQ}`],
    ["trae", "credits", `/api/stats/trae/credits${force ? "?force=1" : ""}`],
    ["trae", "usage", `/api/stats/trae/usage?days=${days}${forceQ}`],
  ];
  for (const [platform, kind, url] of targets) {
    try {
      statsState.data[platform][kind] = await get(url, { timeoutMs: 60000 });
    } catch (err) {
      statsState.data[platform][kind] = { ok: false, error: (err && err.message) || "请求失败" };
    }
    statsState.at = Date.now();
    renderStats();
  }
  if (msg) msg.innerHTML = "";
}

/* 平台徽章与模块标题统一在这里刷新，各模块不再自己拼文案 */
function renderStatsHeaders(scope) {
  const label = PLATFORM_LABEL[scope];
  const cls = scope === "trae" ? "plat trae" : "plat wb";
  ["plat-cards", "plat-chart", "plat-models", "plat-tokens", "plat-detail", "plat-packages"].forEach((id) => {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = label;
    el.className = cls;
  });
  document.getElementById("stats-chart-title").textContent =
    scope === "trae" ? "积分 / Token 消耗趋势" : "积分消耗趋势";
  document.getElementById("stats-models-title").textContent =
    scope === "trae" ? "模型 Token 构成" : "模型消耗构成";
  document.getElementById("stats-detail-title").textContent =
    scope === "trae" ? "会话用量 Top 10" : "最近请求明细";
  document.getElementById("stats-range-sub").textContent = `近 ${statsState.days} 天`;
}

function renderStats() {
  const scope = statsState.scope;
  const pack = statsState.data[scope] || {};
  const credits = pack.credits || { ok: false };
  const usage = pack.usage || { ok: false };
  renderStatsHeaders(scope);
  renderStatsKpi(scope, credits, usage);
  renderCreditCards(scope, credits);
  renderStatsChart(scope, usage);
  renderStatsAccounts(scope, usage);
  renderStatsModels(scope, usage);
  renderStatsTokens(scope, usage);
  renderStatsDetail(scope, usage);
  renderPackages(scope, credits);
  const at = statsState.at ? new Date(statsState.at).toLocaleTimeString("zh-CN") : "尚未更新";
  document.getElementById("stats-updated").textContent =
    `数据更新于 ${at} · 近 ${statsState.days} 天 · 官方接口，10 分钟缓存`;
}

function renderStatsKpi(scope, credits, usage) {
  const label = PLATFORM_LABEL[scope];
  document.getElementById("kpi-balance").textContent =
    credits.ok && credits.totalBalance !== undefined ? fmtNum(credits.totalBalance) : "-";
  document.getElementById("kpi-today").textContent = usage.ok ? fmtNum(usage.today) : "-";
  document.getElementById("kpi-week").textContent = usage.ok ? fmtNum(usage.week) : "-";
  document.getElementById("kpi-month").textContent = usage.ok ? fmtNum(usage.month) : "-";
  document.getElementById("kpi-balance-label").textContent = `${label} 剩余积分`;
  document.getElementById("kpi-today-label").textContent = "今日消耗";
  document.getElementById("kpi-week-label").textContent = "近 7 天消耗";
  document.getElementById("kpi-month-label").textContent = "本月消耗";
  // 失败原因必须显式说出来：否则「卡片空白」无法判断是没数据还是凭证过期
  const failures = (credits.accounts || []).filter((a) => !a.ok);
  const msg = document.getElementById("stats-msg");
  if (failures.length) {
    const detail = failures
      .slice(0, 2)
      .map((a) => `${a.name || "账号"}：${a.error || "统计失败"}`)
      .join("；");
    msg.innerHTML = `<div class="notice warn">${failures.length} 个${label}账号统计失败 —— ${escapeHtml(detail)}${failures.length > 2 ? " 等" : ""}</div>`;
  } else if (usage && usage.ok === false && usage.error) {
    msg.innerHTML = `<div class="notice warn">${escapeHtml(usage.error)}</div>`;
  }
}

/* 账号积分卡片：一账号一卡（余额 / 积分包数 / 近期到期 + 消耗进度）。两平台字段同构 */
function renderCreditCards(scope, credits) {
  const box = document.getElementById("stats-credit-cards");
  const label = PLATFORM_LABEL[scope];
  const accounts = credits.accounts || [];
  if (!accounts.length) {
    box.innerHTML = `<div class="empty">还没有 ${label} 账号</div>`;
    document.getElementById("acc-cards-sub").textContent = "";
    return;
  }
  const okCount = accounts.filter((a) => a.ok).length;
  document.getElementById("acc-cards-sub").textContent =
    `${okCount}/${accounts.length} 个账号 · 余额合计 ${fmtNum(credits.totalBalance)}`;
  const AVATAR_COLORS = ["#2f6feb", "#8b5cf0", "#e8952d", "#1fa05c", "#e05f5f", "#3fbfbf", "#7c8bf5", "#f08c5a"];
  const avatarColor = (name) => {
    let hash = 0;
    for (const ch of String(name)) hash = (hash * 31 + ch.charCodeAt(0)) & 0xffff;
    return AVATAR_COLORS[hash % AVATAR_COLORS.length];
  };
  box.innerHTML = accounts
    .map((a) => {
      const name = a.name || "未命名账号";
      const initial = name.trim().charAt(0).toUpperCase() || "?";
      const head =
        `<div class="acc-head">` +
        `<div class="acc-avatar" style="background:${avatarColor(name)}">${escapeHtml(initial)}</div>` +
        `<div style="min-width:0">` +
        `<div class="acc-name">${escapeHtml(name)} ${badge(a.variant === "ai" ? "国际版" : a.variant === "trae" ? "Trae" : "国内版", a.variant === "ai" ? "warn" : "info")}</div>` +
        `<div class="acc-uid">UID · ${escapeHtml(String(a.uid || a.id || "-")).slice(0, 40)}</div>` +
        `</div></div>`;
      if (!a.ok) {
        return `<div class="acc-card">${head}<div class="empty" style="padding:16px 0 6px">统计失败：${escapeHtml(a.error || "凭据可能过期")}</div></div>`;
      }
      const packages = (a.packages || []).filter((p) => (p.remaining || 0) > 0);
      const balance = a.balance === null || a.balance === undefined ? "-" : fmtNum(a.balance);
      const rows = packages
        .slice(0, 3)
        .map((p) => {
          const pct = p.total ? Math.min(100, Math.max(2, (p.remaining / p.total) * 100)) : 100;
          return (
            `<div class="pkg-row">` +
            `<div class="pkg-meta">` +
            `<span class="pkg-remaining">${fmtNum(p.remaining)} 积分</span>` +
            `<span class="pkg-name">${escapeHtml(p.name || "积分包")}</span>` +
            `<span class="pkg-expire">${escapeHtml(p.expireText || "-")}</span>` +
            `</div>` +
            `<div class="pkg-bar"><i style="width:${pct.toFixed(1)}%"></i></div>` +
            `</div>`
          );
        })
        .join("");
      return (
        `<div class="acc-card">` +
        head +
        `<div class="acc-balance-row"><span class="acc-balance">${balance}</span><span class="acc-pkg-count">${packages.length} 个积分包</span></div>` +
        (packages.length
          ? `<div class="acc-expire-head"><span>近期到期</span><span>剩余 / 到期时间</span></div>${rows}`
          : `<div class="empty" style="padding:10px 0 4px">没有剩余积分包</div>`) +
        `</div>`
      );
    })
    .join("");
}

function renderStatsAccounts(scope, usage) {
  const box = document.getElementById("stats-by-account");
  const label = PLATFORM_LABEL[scope];
  const rows = usage.ok ? usage.byAccount || [] : [];
  if (!rows.length) {
    box.innerHTML = `<div class="empty">暂无数据（还没有 ${label} 账号，或近 ${statsState.days} 天无消耗）</div>`;
    return;
  }
  const total = rows.reduce((sum, r) => sum + (r.credits || 0), 0) || 1;
  box.innerHTML = rows
    .map(
      (r) => `<div style="margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;font-size:13px">
          <span>${escapeHtml(r.name || "")}</span>
          <span style="color:var(--muted)">${fmtNum(r.credits)} 积分 · ${((r.credits / total) * 100).toFixed(1)}%</span>
        </div>
        <div class="bar"><i style="width:${Math.max(1, (r.credits / total) * 100).toFixed(1)}%"></i></div>
      </div>`
    )
    .join("");
}

/* 模型维度构成：WorkBuddy 是积分，Trae 是 Token */
function renderStatsModels(scope, usage) {
  const box = document.getElementById("stats-models");
  const metricKey = scope === "trae" ? "tokens" : "credits";
  const fmt = scope === "trae" ? fmtTokens : fmtNum;
  const rows = (usage.ok ? usage.models || [] : []).filter((r) => (r[metricKey] || 0) > 0);
  if (!rows.length) {
    box.innerHTML = '<div class="empty">暂无模型维度数据</div>';
    return;
  }
  const total = rows.reduce((sum, r) => sum + (r[metricKey] || 0), 0) || 1;
  box.innerHTML = rows
    .slice(0, 8)
    .map((r, index) => {
      const value = r[metricKey] || 0;
      const color = modelColor(r.model || "", index);
      return `<div style="margin-bottom:10px">
        <div style="display:flex;justify-content:space-between;font-size:13px">
          <span><span style="display:inline-block;width:9px;height:9px;border-radius:2px;background:${color};margin-right:6px"></span>${escapeHtml(r.model || "未知模型")}</span>
          <span style="color:var(--muted)">${fmt(value)}${scope === "trae" ? "" : " 积分"} · ${((value / total) * 100).toFixed(1)}%</span>
        </div>
        <div class="bar"><i style="width:${Math.max(1, (value / total) * 100).toFixed(1)}%;background:${color}"></i></div>
      </div>`;
    })
    .join("");
}

/* Token 统计：WorkBuddy 官方用量接口不返回 token 维度时如实说明，不做假数据 */
function renderStatsTokens(scope, usage) {
  const box = document.getElementById("stats-tokens");
  const totals = usage.ok ? usage.tokenTotals : null;
  if (!totals) {
    box.innerHTML =
      scope === "trae"
        ? '<div class="empty">暂无 Trae Token 数据（近 ' + statsState.days + ' 天没有会话记录）</div>'
        : '<div class="empty">WorkBuddy 官方请求用量接口只返回积分消耗，不提供 Token 维度 —— 这块仅 Trae 有数据</div>';
    return;
  }
  const today = usage.todayTokens || {};
  const items = [
    ["总 Token", fmtTokens(totals.total)],
    ["输入 Token", fmtTokens(totals.input)],
    ["输出 Token", fmtTokens(totals.output)],
    ["缓存读取", fmtTokens(totals.cacheRead)],
    [
      "缓存命中率",
      usage.cacheHitRate === null || usage.cacheHitRate === undefined
        ? "-"
        : (usage.cacheHitRate * 100).toFixed(1) + "%",
    ],
    ["今日 Token", fmtTokens((today.input || 0) + (today.output || 0))],
  ];
  box.innerHTML = `<div class="stat-row">${items
    .map(([label, value]) => `<div class="stat"><div class="num">${value}</div><div class="label">${label}</div></div>`)
    .join("")}</div>`;
}

/* 逐日堆叠柱状图（纯 SVG）：WorkBuddy 按积分、Trae 按 Token */
function renderStatsChart(scope, usage) {
  const box = document.getElementById("stats-chart");
  const legend = document.getElementById("stats-legend");
  const daily = usage.ok ? usage.daily || [] : [];
  const metricKey = scope === "trae" ? "tokens" : "credits";
  const fmt = scope === "trae" ? fmtTokens : fmtNum;
  const unit = scope === "trae" ? "Token" : "积分";
  if (!daily.length) {
    box.innerHTML = `<div class="empty">近 ${statsState.days} 天没有 ${PLATFORM_LABEL[scope]} 消耗记录</div>`;
    legend.textContent = "";
    return;
  }
  const dayValue = (day) => {
    const explicit = Number(day[metricKey]);
    if (Number.isFinite(explicit) && explicit >= 0) return explicit;
    return Object.values(day.models || {}).reduce((sum, v) => sum + (Number(v) || 0), 0);
  };
  // 全局模型调色板：出现过的模型按首次出现顺序分配颜色
  const palette = {};
  let next = 0;
  for (const day of daily) {
    for (const model of Object.keys(day.models || {})) {
      if (!(model in palette)) palette[model] = MODEL_COLORS[next++ % MODEL_COLORS.length];
    }
  }
  const W = 680, H = 240, PAD_L = 46, PAD_B = 22, PAD_T = 8;
  const maxVal = Math.max(...daily.map(dayValue), 0.0001);
  const innerW = W - PAD_L - 8, innerH = H - PAD_T - PAD_B;
  const slot = innerW / daily.length;
  const barW = Math.max(4, Math.min(26, slot * 0.6));
  const parts = [];
  // 网格与刻度
  for (let i = 0; i <= 4; i++) {
    const y = PAD_T + innerH - (innerH * i) / 4;
    const val = (maxVal * i) / 4;
    parts.push(`<line x1="${PAD_L}" y1="${y}" x2="${W - 8}" y2="${y}" stroke="var(--border)" stroke-width="1"/>`);
    parts.push(`<text x="${PAD_L - 6}" y="${y + 4}" font-size="10" fill="var(--muted)" text-anchor="end">${fmt(val)}</text>`);
  }
  daily.forEach((day, i) => {
    const x = PAD_L + slot * i + (slot - barW) / 2;
    let yCursor = PAD_T + innerH;
    const entries = Object.entries(day.models || {}).sort((a, b) => b[1] - a[1]);
    for (const [model, amount] of entries) {
      const h = (amount / maxVal) * innerH;
      yCursor -= h;
      parts.push(`<rect x="${x}" y="${yCursor}" width="${barW}" height="${Math.max(0.5, h)}" fill="${palette[model]}" rx="2"><title>${escapeHtml(model)}：${fmt(amount)} ${unit}</title></rect>`);
    }
    if (i % Math.ceil(daily.length / 12) === 0) {
      parts.push(`<text x="${x + barW / 2}" y="${H - 6}" font-size="10" fill="var(--muted)" text-anchor="middle">${day.date.slice(5)}</text>`);
    }
  });
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" style="width:100%;height:auto;display:block">${parts.join("")}</svg>`;
  legend.innerHTML =
    "模型：" +
    Object.keys(palette)
      .map((m) => `<span style="margin-right:10px"><span style="display:inline-block;width:10px;height:10px;border-radius:2px;background:${palette[m]};margin-right:4px"></span>${escapeHtml(m)}</span>`)
      .join("");
}

/* 明细表：WorkBuddy 看官方请求明细（积分维度），Trae 看会话用量（Token 维度） */
function renderStatsDetail(scope, usage) {
  const box = document.getElementById("stats-detail");
  const sub = document.getElementById("stats-detail-sub");
  if (!usage.ok) {
    box.innerHTML = `<div class="empty">${escapeHtml(usage.error || PLATFORM_LABEL[scope] + " 用量查询失败")}</div>`;
    sub.textContent = "";
    return;
  }
  if (scope === "trae") {
    const rows = usage.sessions || [];
    sub.textContent = `近 ${statsState.days} 天 · 共 ${usage.totalSessions || 0} 个会话`;
    if (!rows.length) {
      box.innerHTML = `<div class="empty">近 ${statsState.days} 天没有 Trae 会话记录</div>`;
      return;
    }
    box.innerHTML = `<table><thead><tr>
      <th>时间</th><th>账号</th><th>模型</th><th>输入</th><th>输出</th><th>缓存读</th><th>积分</th><th>会话说明</th>
    </tr></thead><tbody>${rows
      .map(
        (s) => `<tr>
        <td class="mono">${new Date(s.ts).toLocaleDateString("zh-CN")}</td>
        <td>${escapeHtml(s.account || "")}</td>
        <td>${escapeHtml(s.model || "")}</td>
        <td class="mono">${fmtTokens(s.input)}</td>
        <td class="mono">${fmtTokens(s.output)}</td>
        <td class="mono">${fmtTokens(s.cacheRead)}</td>
        <td class="mono">${fmtNum(s.credits)}</td>
        <td style="color:var(--muted);max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${escapeHtml(s.preview || "")}">${escapeHtml(s.preview || "-")}</td>
      </tr>`
      )
      .join("")}</tbody></table>`;
    return;
  }
  const rows = usage.requests || [];
  sub.textContent = `近 ${statsState.days} 天 · 共 ${usage.totalCalls || 0} 次请求 · 显示最近 ${rows.length} 条`;
  if (!rows.length) {
    box.innerHTML = `<div class="empty">近 ${statsState.days} 天没有官方请求记录</div>`;
    return;
  }
  box.innerHTML = `<table><thead><tr>
      <th>时间</th><th>账号</th><th>模型</th><th>客户端</th><th>积分</th>
    </tr></thead><tbody>${rows
      .slice(0, 50)
      .map(
        (r) => `<tr>
        <td class="mono">${new Date(r.ts).toLocaleString("zh-CN")}</td>
        <td>${escapeHtml(r.account || "")}</td>
        <td>${escapeHtml(r.model || "")}</td>
        <td style="color:var(--muted)">${escapeHtml(r.client || "-")}</td>
        <td class="mono">${fmtNum(r.credit)}</td>
      </tr>`
      )
      .join("")}</tbody></table>`;
}

/* 积分包到期：两平台字段同构（名称 / 类型 / 剩余 / 到期 + 剩余进度条） */
function renderPackages(scope, credits) {
  const box = document.getElementById("stats-packages");
  const label = PLATFORM_LABEL[scope];
  const rows = [];
  for (const acc of credits.accounts || []) {
    for (const pkg of acc.packages || []) {
      if ((pkg.remaining || 0) > 0) rows.push({ ...pkg, account: acc.name });
    }
  }
  if (!rows.length) {
    box.innerHTML = `<div class="empty">${credits.ok ? `没有剩余 ${label} 积分包` : escapeHtml(credits.error || "积分包查询失败")}</div>`;
    return;
  }
  rows.sort((a, b) => (a.expireTs || 9e15) - (b.expireTs || 9e15));
  box.innerHTML = `<table><thead><tr>
      <th>账号</th><th>积分包</th><th>类型</th><th>剩余 / 总额度</th><th>到期</th>
    </tr></thead><tbody>${rows
      .slice(0, 15)
      .map((r) => {
        const pct = r.total ? Math.min(100, Math.max(2, (r.remaining / r.total) * 100)) : 100;
        return `<tr>
        <td>${escapeHtml(r.account || "")}</td>
        <td>${escapeHtml(r.name || "")}</td>
        <td>${badge(r.kind || "-", r.kind === "付费" || r.kind === "Work" ? "info" : "muted")}</td>
        <td>
          <div class="pkg-inline"><span class="mono">${fmtNum(r.remaining)}</span><span style="color:var(--muted)"> / ${r.total ? fmtNum(r.total) : "-"}</span></div>
          <div class="pkg-bar"><i style="width:${pct.toFixed(1)}%"></i></div>
        </td>
        <td class="mono">${escapeHtml(r.expireText || "-")}</td>
      </tr>`;
      })
      .join("")}</tbody></table>`;
}

document.getElementById("stats-refresh").onclick = () => loadStats(true);
document.querySelectorAll("[data-days]").forEach((btn) => {
  btn.onclick = () => {
    statsState.days = Number(btn.dataset.days);
    loadStats(false);
  };
});
document.querySelectorAll("#stats-scope .tab").forEach((btn) => {
  btn.onclick = () => {
    statsState.scope = btn.dataset.scope;
    document.querySelectorAll("#stats-scope .tab").forEach((el) => {
      el.classList.toggle("active", el.dataset.scope === statsState.scope);
    });
    renderStats();
  };
});
// 先渲染一次骨架（平台徽章 / 空态），接口回来后再补数据
renderStats();

/* ---------------- 渲染：日志 ---------------- */

function renderLogs(checkinLogs, appLogLines) {
  const box = document.getElementById("logs-table");
  if (!checkinLogs.length) {
    box.innerHTML = '<div class="empty">暂无签到记录</div>';
  } else {
    const body = checkinLogs
      .map((entry) => {
        const time = new Date(entry.ts || Date.now()).toLocaleString("zh-CN");
        const platform = entry.platform === "trae" ? "Trae" : "WorkBuddy";
        return `<tr>
        <td class="mono">${time}</td>
        <td>${badge(platform, entry.platform === "trae" ? "info" : "muted")}</td>
        <td>${escapeHtml(entry.account || entry.email || entry.accountId || "")}</td>
        <td>${resultBadge(entry.status || entry.result)}</td>
        <td class="mono" style="color:var(--muted)">${escapeHtml(entry.detail || entry.error || "")}</td>
      </tr>`;
      })
      .join("");
    box.innerHTML = `<table><thead><tr>
      <th>时间</th><th>平台</th><th>账号</th><th>结果</th><th>备注</th>
    </tr></thead><tbody>${body}</tbody></table>`;
  }
  if (appLogLines) {
    document.getElementById("app-log-box").textContent = appLogLines.join("\n") || "（暂无）";
  }
}

function renderLogsFromCache() {
  if (cache.logs) renderLogs(cache.logs.checkin, cache.logs.app);
}

async function loadLogs() {
  // 串行取数：网关并发转发不可靠，并发请求会一起悬住（见 loadStats 注释）
  const logs = await get("/api/checkin/logs?limit=200");
  const appLogs = await get("/api/logs?limit=200");
  cache.logs = { checkin: logs.logs, app: appLogs.lines || [] };
  renderLogs(cache.logs.checkin, cache.logs.app);
}

/* ---------------- 签到 ---------------- */

/* 统一的任务执行：发起 → 轮询进度 → 返回结果。
   单平台与一键双平台共用这一条路径，保证签到体验只有一种。 */
async function startCheckinTask(platform, accountId) {
  const started = await post("/api/checkin", { platform, id: accountId });
  for (let i = 0; i < 180; i += 1) {
    await new Promise((r) => setTimeout(r, 1000));
    const task = await get(`/api/checkin/progress?taskId=${encodeURIComponent(started.taskId)}`);
    if (task.error) throw new Error(task.error);
    if (task.status === "done") return task.result || {};
  }
  throw new Error("签到超时");
}

/* 两平台结果结构不同（WB: accounts[] / Trae: rows+计数），
   先归一化成同一种行结构再渲染 —— 签到页从此只有一种效果。 */
function normalizeCheckinResult(platform, r) {
  const rows = [];
  let ok = 0, already = 0, failed = 0;
  if (platform === "workbuddy") {
    for (const x of r.accounts || []) {
      const st = x.result || "error";
      if (st === "success") ok += 1;
      else if (st === "already") already += 1;
      else failed += 1;
      rows.push({ name: x.email || "", status: st, message: x.error || "" });
    }
    if (!rows.length && r.status) {
      rows.push({ infoOnly: true, message: r.status });
    }
  } else {
    ok = r.ok || 0;
    already = r.already || 0;
    failed = r.failed || 0;
    for (const x of r.rows || []) {
      rows.push({ name: x.name || "", status: x.status || "", message: x.message || "" });
    }
  }
  return { rows, ok, already, failed };
}

function renderCheckinResult(boxId, platform, r) {
  const n = normalizeCheckinResult(platform, r);
  const tone = n.failed ? "err" : "ok";
  const info = n.rows
    .filter((x) => x.infoOnly)
    .map((x) => `<div class="notice info">${escapeHtml(x.message)}</div>`)
    .join("");
  const lines = n.rows
    .filter((x) => !x.infoOnly)
    .map(
      (x) =>
        `<div class="checkin-line"><span class="checkin-name">${escapeHtml(x.name || "-")}</span>` +
        `${resultBadge(x.status)}` +
        (x.message ? `<span class="checkin-msg">${escapeHtml(x.message)}</span>` : "") +
        `</div>`
    )
    .join("");
  document.getElementById(boxId).innerHTML =
    `<div class="notice ${tone}" style="margin-bottom:8px">成功 ${n.ok} · 已签 ${n.already} · 失败 ${n.failed}</div>` +
    info +
    (lines ? `<div class="checkin-lines">${lines}</div>` : "");
}

async function runCheckin(platform, accountId, resultBoxId) {
  const box = document.getElementById(resultBoxId);
  box.innerHTML = '<div class="notice info">签到进行中…</div>';
  try {
    const r = await startCheckinTask(platform, accountId);
    renderCheckinResult(resultBoxId, platform, r);
    await refreshAll();
  } catch (err) {
    box.innerHTML = `<div class="notice err">${escapeHtml(err.message)}</div>`;
  }
}

/* 一键双平台：两个平台依次触发（串行更稳，见下方注释），各自结果写回各自卡片，
   顶部给一条汇总。失败的平台不影响另一个。 */
document.getElementById("checkin-all-btn").onclick = async () => {
  const btn = document.getElementById("checkin-all-btn");
  const box = document.getElementById("checkin-all-result");
  const label = btn.textContent;
  btn.disabled = true;
  btn.textContent = "⏳ 签到进行中…";
  box.innerHTML = '<div class="notice info">已同时向 WorkBuddy 与 Trae 发起签到，请稍候…</div>';
  const safeRun = async (platform, boxId) => {
    try {
      const r = await startCheckinTask(platform);
      renderCheckinResult(boxId, platform, r);
      return normalizeCheckinResult(platform, r);
    } catch (err) {
      document.getElementById(boxId).innerHTML =
        `<div class="notice err">${escapeHtml(err.message)}</div>`;
      return { ok: 0, already: 0, failed: 1 };
    }
  };
  try {
    // 串行触发两个平台：签到本身是长请求，网关并发转发会让两个长请求一起
    // 悬住 —— 串行虽然总耗时相加，但每个平台都能拿到确定结果。
    const wb = await safeRun("workbuddy", "wb-checkin-result");
    const trae = await safeRun("trae", "trae-checkin-result");
    const failed = wb.failed + trae.failed;
    box.innerHTML =
      `<div class="notice ${failed ? "err" : "ok"}">🎉 双平台签到完成 —— ` +
      `成功 ${wb.ok + trae.ok} · 已签 ${wb.already + trae.already} · 失败 ${failed}</div>`;
    await refreshAll();
  } finally {
    btn.disabled = false;
    btn.textContent = label;
  }
};

/* 今日待签到：待签账号直接点名，配合账号表用同一个名字 */
function renderCheckinStatus() {
  const box = document.getElementById("checkin-status-box");
  const data = cache.checkin;
  if (!data) return;
  const accounts = (cache.accounts && cache.accounts.accounts) || [];
  const line = (platform, label, cls) => {
    const block = data[platform] || { total: 0, pending: [] };
    const pending = block.pending || [];
    const names = pending.map((id) => {
      const acc = accounts.find((x) => x.platform === platform && x.id === id);
      return acc ? acc.name : id || "?";
    });
    const chips = names.map((n) => `<span class="chip">${escapeHtml(n)}</span>`).join(" ");
    return (
      `<div class="checkin-line"><span class="plat ${cls}">${label}</span>` +
      `<span>共 ${block.total} 个账号，待签到 <b>${pending.length}</b> 个</span>` +
      (chips
        ? `<span class="checkin-msg">${chips}</span>`
        : (block.total ? '<span class="checkin-msg" style="color:var(--ok)">全部完成 ✓</span>' : "")) +
      `</div>`
    );
  };
  box.innerHTML = line("workbuddy", "WorkBuddy", "wb") + line("trae", "Trae", "trae");
  box.classList.remove("empty");
  box.style.whiteSpace = "";
}

/* ---------------- 数据加载 ---------------- */

async function loadStatus() {
  cache.status = await get("/api/status");
  state.authRequired = !!cache.status.authRequired;
  if (cache.status.authRequired && !document.cookie.includes("qd_token")) {
    showLogin();
    return;
  }
  renderOverview();
}

async function loadCheckinStatus() {
  cache.checkin = await get("/api/checkin/status");
  renderCheckinStatus();
  if (cache.status) renderOverview();
}

async function loadAccounts() {
  cache.accounts = await get("/api/accounts");
  renderAccounts();
  if (cache.checkin) renderCheckinStatus();
}

async function loadConfig() {
  cache.config = await get("/api/config");
}

/* 设置表单回填：只在进入设置页或保存后执行，
   后台刷新只更新缓存，绝不覆盖用户正在编辑的内容 */
function fillSettingsForm() {
  if (!cache.config) return;
  const cfg = cache.config.config;
  document.getElementById("cfg-enabled").checked = !!cfg.enabled;
  document.getElementById("cfg-start").value = cfg.checkin_start || "";
  document.getElementById("cfg-end").value = cfg.checkin_end || "";
  document.getElementById("cfg-interval").value = cfg.recovery_interval_minutes ?? 30;
  document.getElementById("cfg-lazy").value = (cfg.workbuddy || {}).lazy_refresh_hours ?? 24;
  document.getElementById("cfg-gap").value = (cfg.trae || {}).checkin_gap_secs ?? 3;
  document.getElementById("cfg-retry").value = (cfg.trae || {}).retry ?? 1;
  document.getElementById("cfg-host").value = cfg.host || "0.0.0.0";
  document.getElementById("cfg-port").value = cfg.port || 8790;
  document.getElementById("cfg-password").value = "";
  document.getElementById("cfg-clear-password").checked = false;
}

/* 全量刷新：任何模块变更后调用，联动到所有页面。
   串行执行 —— 飞牛统一网关的并发转发不可靠，并发请求会一起悬住（见 loadStats）。
   单个接口失败不阻塞其余，与原先 allSettled 的容错语义一致。
   注意：各 loadXxx 已自带渲染，这里不得再 renderPage —— 否则设置表单
   会被回填逻辑覆盖，用户正在编辑的内容直接丢掉。 */
async function refreshAll() {
  for (const fn of [loadStatus, loadCheckinStatus, loadAccounts, loadConfig]) {
    try {
      await fn();
    } catch (err) {
      /* 单接口失败不影响其余模块刷新 */
    }
  }
}

/* 首屏：只发一次请求取回全部初始数据。
   并发是这里最致命的坑 —— 真机上首屏并发发出的几个接口会集体悬住，
   页面永久停在加载态而控制台干净得可疑。故收敛为单请求，
   外部统计（积分 / 用量）不进首屏，由调用方在骨架出来之后单独补拉。 */
async function bootLoad() {
  const data = await get(`/api/bootstrap?days=${statsState.days}`, { timeoutMs: 25000 });
  cache.status = data.status || null;
  cache.checkin = data.checkin || null;
  cache.accounts = data.accounts || null;
  cache.config = data.config || null;
  const snap = data.stats || {};
  const pick = (platform, kind) => ((snap[platform] || {})[kind] || null);
  statsState.data.workbuddy = { credits: pick("workbuddy", "credits"), usage: pick("workbuddy", "usage") };
  statsState.data.trae = { credits: pick("trae", "credits"), usage: pick("trae", "usage") };
  statsState.at = Date.now();
  state.authRequired = !!(cache.status && cache.status.authRequired);
  if (state.authRequired && !document.cookie.includes("qd_token")) {
    showLogin();
    return;
  }
  renderPage(state.page);
}

/* ---------------- 工具 ---------------- */

function escapeHtml(text) {
  return String(text == null ? "" : text).replace(/[&<>"']/g, (ch) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[ch]));
}

/* ---------------- 事件绑定 ---------------- */

document.querySelectorAll(".nav-item").forEach((el) => {
  el.onclick = () => switchPage(el.dataset.page);
});

document.querySelectorAll("#account-tabs .tab").forEach((el) => {
  el.onclick = () => switchTab(el.dataset.tab);
});

document.getElementById("login-btn").onclick = doLogin;
document.getElementById("login-password").onkeydown = (e) => {
  if (e.key === "Enter") doLogin();
};

document.getElementById("wb-add").onclick = async () => {
  const msg = document.getElementById("wb-add-msg");
  msg.innerHTML = "";
  try {
    await post("/api/accounts", {
      platform: "workbuddy",
      access_token: document.getElementById("wb-token").value.trim(),
      refresh_token: document.getElementById("wb-refresh").value.trim(),
      nickname: document.getElementById("wb-nickname").value.trim(),
      email: document.getElementById("wb-email").value.trim(),
      uid: document.getElementById("wb-uid").value.trim(),
      variant: document.getElementById("wb-variant").value,
    });
    ["wb-token", "wb-refresh", "wb-nickname", "wb-email", "wb-uid"].forEach((id) => {
      document.getElementById(id).value = "";
    });
    msg.innerHTML = '<div class="notice ok">已添加</div>';
    await refreshAll();
  } catch (err) {
    msg.innerHTML = `<div class="notice err">${escapeHtml(err.message)}</div>`;
  }
};

document.getElementById("trae-add").onclick = async () => {
  const msg = document.getElementById("trae-add-msg");
  msg.innerHTML = "";
  try {
    await post("/api/accounts", {
      platform: "trae",
      jwt: document.getElementById("trae-jwt").value.trim(),
      name: document.getElementById("trae-name").value.trim(),
    });
    document.getElementById("trae-jwt").value = "";
    document.getElementById("trae-name").value = "";
    msg.innerHTML = '<div class="notice ok">已添加</div>';
    await refreshAll();
  } catch (err) {
    msg.innerHTML = `<div class="notice err">${escapeHtml(err.message)}</div>`;
  }
};

document.querySelectorAll("[data-checkin]").forEach((btn) => {
  btn.onclick = () => {
    const platform = btn.dataset.checkin;
    runCheckin(platform, null, `${platform === "trae" ? "trae" : "wb"}-checkin-result`);
  };
});

document.getElementById("refresh-app-log").onclick = loadLogs;

/* 配置保存：自动签到（账号页）与服务端（设置页）共用一份提交逻辑。
   表单元素始终在文档里（页面只是隐藏），所以一次提交携带完整配置，
   不会把另一页的字段重置成默认值。 */
async function saveConfig(msgId) {
  const msg = document.getElementById(msgId);
  if (!msg) return;
  msg.innerHTML = "";
  const val = (id) => {
    const el = document.getElementById(id);
    return el ? el.value.trim() : "";
  };
  const num = (id, fallback) => {
    const el = document.getElementById(id);
    const parsed = el ? Number(el.value) : NaN;
    return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
  };
  const zeroOk = (id, fallback) => {
    const el = document.getElementById(id);
    const parsed = el ? Number(el.value) : NaN;
    return Number.isFinite(parsed) && parsed >= 0 ? parsed : fallback;
  };
  try {
    await post("/api/config", {
      enabled: document.getElementById("cfg-enabled").checked,
      checkin_start: val("cfg-start"),
      checkin_end: val("cfg-end"),
      recovery_interval_minutes: num("cfg-interval", 30),
      workbuddy: { lazy_refresh_hours: num("cfg-lazy", 24) },
      trae: { checkin_gap_secs: zeroOk("cfg-gap", 3), retry: zeroOk("cfg-retry", 1) },
      host: val("cfg-host") || "0.0.0.0",
      port: num("cfg-port", 8790),
      access_password: document.getElementById("cfg-password").value,
      clear_password: document.getElementById("cfg-clear-password").checked,
    });
    msg.innerHTML = '<div class="notice ok">已保存</div>';
    await loadConfig();
    fillSettingsForm();
    await loadStatus();
  } catch (err) {
    msg.innerHTML = `<div class="notice err">${escapeHtml(err.message)}</div>`;
  }
}

document.getElementById("cfg-save").onclick = () => saveConfig("cfg-msg");
document.getElementById("cfg-save-server").onclick = () => saveConfig("server-msg");

/* ---------------- 配置导出 / 导入（账号搬家） ----------------

   目标场景：已有的 NAS 跑着一堆账号，换机时不想逐个重新登录。
   导出把账号与签到策略打成一个 JSON 文件带过去，导入按账号身份去重合并，
   同名账号就地更新，不会出现「同一个号两张表」。 */

document.getElementById("export-btn").onclick = async () => {
  const msg = document.getElementById("export-msg");
  msg.textContent = "正在导出…";
  try {
    const doc = await get("/api/export");
    const blob = new Blob([JSON.stringify(doc, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const stamp = String(doc.exportedAt || "").slice(0, 10).replace(/-/g, "") || "config";
    const link = document.createElement("a");
    link.href = url;
    link.download = `qdassistant-config-${stamp}.json`;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    setTimeout(() => URL.revokeObjectURL(url), 4000);
    const counts = doc.counts || {};
    msg.textContent = `已导出 WorkBuddy ${counts.workbuddy || 0} 个 · Trae ${counts.trae || 0} 个账号`;
  } catch (err) {
    msg.textContent = `导出失败：${err.message}`;
  }
};

document.getElementById("import-btn").onclick = () => {
  document.getElementById("import-box").classList.toggle("hidden");
  document.getElementById("import-msg").innerHTML = "";
};

document.getElementById("import-cancel").onclick = () => {
  document.getElementById("import-box").classList.add("hidden");
  document.getElementById("import-msg").innerHTML = "";
};

document.getElementById("import-file").onchange = async (e) => {
  const file = e.target.files && e.target.files[0];
  if (!file) return;
  try {
    document.getElementById("import-content").value = await file.text();
    document.getElementById("import-msg").innerHTML =
      '<div class="notice info">文件已读入，确认无误后点「开始导入」。</div>';
  } catch (err) {
    document.getElementById("import-msg").innerHTML =
      `<div class="notice err">读取文件失败：${escapeHtml(err.message)}</div>`;
  }
};

document.getElementById("import-confirm").onclick = async () => {
  const msg = document.getElementById("import-msg");
  const raw = document.getElementById("import-content").value.trim();
  msg.innerHTML = "";
  if (!raw) {
    msg.innerHTML = '<div class="notice err">请先粘贴导出文件内容，或选择一个 .json 文件</div>';
    return;
  }
  let doc;
  try {
    doc = JSON.parse(raw);
  } catch (err) {
    msg.innerHTML = '<div class="notice err">不是合法的 JSON —— 请确认整段复制完整，或直接选文件</div>';
    return;
  }
  try {
    const res = await post("/api/import", { document: doc });
    const added = res.added || {};
    const updated = res.updated || {};
    msg.innerHTML =
      `<div class="notice ok">导入完成 —— WorkBuddy 新增 ${added.workbuddy || 0} 个 / 更新 ${updated.workbuddy || 0} 个 · ` +
      `Trae 新增 ${added.trae || 0} 个 / 更新 ${updated.trae || 0} 个</div>` +
      `<div class="notice info" style="font-size:12px">同 uid（Trae 为 user id）的账号就地更新，不会重复添加；` +
      `监听地址 / 端口 / 访问口令不在导出范围内，需在新设备单独设置。</div>`;
    document.getElementById("import-content").value = "";
    document.getElementById("import-file").value = "";
    await refreshAll();
  } catch (err) {
    msg.innerHTML = `<div class="notice err">${escapeHtml(err.message)}</div>`;
  }
};

/* ---------------- 启动 ---------------- */

// 启动失败要可描述，不能只留一个转圈。请求阶段与渲染阶段分别兜底，
// 并把异常同时报到页面上的启动诊断面板。
function bootFail(err, stage) {
  const text = `${stage}：${err && err.message ? err.message : String(err)}`;
  const info = document.getElementById("scheduler-info");
  if (info) info.textContent = `加载失败：${text}`;
  const box = document.getElementById("boot-fail");
  const detail = document.getElementById("boot-fail-detail");
  if (box && detail) {
    detail.textContent = text;
    box.classList.remove("hidden");
  }
}

window.addEventListener("unhandledrejection", (e) => {
  if (!window.__qdBooted) bootFail(e.reason, "未处理的异步异常");
});
window.addEventListener("error", (e) => {
  if (!window.__qdBooted) bootFail(e.error || e.message, "脚本错误");
});

bootLoad()
  .then(() => {
    window.__qdBooted = true;
    loadStats(false).catch(() => {});
  })
  .catch((err) => {
    window.__qdBooted = true; // 已给出明确错误，不再触发看门狗
    bootFail(err, "初始化请求失败");
  });

// 轮询只刷当前页的数据：联动靠 refreshAll 与切页拉取兜底，这里保底新鲜度
setInterval(() => runPageLoaders(state.page), 30000);
