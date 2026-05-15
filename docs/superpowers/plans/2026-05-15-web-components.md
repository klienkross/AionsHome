# Web Components 迁移实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 创建 `components.js`，包含 7 个原生 Web Components，然后渐进式迁移子页面和 chat.html 侧栏按钮。

**Architecture:** 单文件 `components.js` 定义所有组件，每个组件用 Shadow DOM 隔离样式、通过 CSS 变量读取全局主题。子页面用 `<script src>` 引入，与现有 `common.css` / `common.js` 并行。

**Tech Stack:** 原生 Web Components（Custom Elements v1 + Shadow DOM），无框架无构建工具。

---

## 文件清单

| 操作 | 路径 | 职责 |
|------|------|------|
| 创建 | `aion-chat/static/components.js` | 7 个 Web Component 的定义与注册 |
| 修改 | `aion-chat/static/worldbook.html` | 试点迁移：用 `<aion-subpage>` + `<aion-topbar>` 替换手写布局 |
| 修改 | `aion-chat/static/settings.html` | 迁移子页面 |
| 修改 | `aion-chat/static/schedule.html` | 迁移子页面 |
| 修改 | `aion-chat/static/memory.html` | 迁移子页面 |
| 修改 | `aion-chat/static/fund.html` | 迁移子页面 + `<aion-toggle>` 替换 `.switch` |
| 修改 | `aion-chat/static/gift.html` | 迁移子页面 |
| 修改 | `aion-chat/static/activity-logs.html` | 迁移子页面 |
| 修改 | `aion-chat/static/monitor-logs.html` | 迁移子页面 |
| 修改 | `aion-chat/static/reading.html` | 迁移子页面 |
| 修改 | `aion-chat/static/location.html` | 迁移子页面 |
| 修改 | `aion-chat/static/chat.html` | sidebar-header / sidebar-footer 按钮替换为 `<aion-icon-btn>` / `<aion-sidebar-btn>` |

---

### Task 1: 创建 components.js — aion-dots + aion-toggle

最简单的两个无依赖组件，先建立文件骨架。

**Files:**
- 创建: `aion-chat/static/components.js`

- [ ] **Step 1: 创建 components.js，实现 AionDots**

```js
/* ── Aion Web Components ── */

class AionDots extends HTMLElement {
  static get observedAttributes() { return ['color']; }

  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: inline-flex; gap: 3px; align-items: center; }
        span {
          width: 5px; height: 5px; border-radius: 50%;
          background: var(--dot-color, var(--accent, #ff8359));
          animation: bounce 1.4s infinite ease-in-out both;
        }
        span:nth-child(2) { animation-delay: 0.16s; }
        span:nth-child(3) { animation-delay: 0.32s; }
        @keyframes bounce {
          0%, 80%, 100% { transform: scale(0.4); opacity: 0.4; }
          40% { transform: scale(1); opacity: 1; }
        }
      </style>
      <span></span><span></span><span></span>
    `;
  }

  attributeChangedCallback(name, _, val) {
    if (name === 'color') this.shadowRoot.host.style.setProperty('--dot-color', val);
  }
}
```

- [ ] **Step 2: 在同一文件中实现 AionToggle**

在 `AionDots` class 之后追加：

```js
class AionToggle extends HTMLElement {
  static get observedAttributes() { return ['label', 'checked']; }

  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: flex; align-items: center; justify-content: space-between; cursor: pointer; }
        .label { font-size: 14px; font-weight: 600; color: var(--text, #4a3b32); }
        .track {
          position: relative; width: 44px; height: 24px; flex-shrink: 0;
          background: var(--surface2, #fff0e6); border: 1px solid var(--border, #f0e4dd);
          border-radius: 12px; transition: 0.2s;
        }
        .track.on { background: var(--accent, #ff8359); border-color: var(--accent, #ff8359); }
        .thumb {
          position: absolute; top: 2px; left: 2px;
          width: 18px; height: 18px; border-radius: 50%;
          background: var(--text3, #b0a39a); transition: 0.2s;
        }
        .track.on .thumb { transform: translateX(20px); background: #fff; }
      </style>
      <span class="label"></span>
      <div class="track"><div class="thumb"></div></div>
    `;
    this.shadowRoot.querySelector('.track').addEventListener('click', () => this._toggle());
    this.shadowRoot.querySelector('.label').addEventListener('click', () => this._toggle());
  }

  get checked() { return this.hasAttribute('checked'); }
  set checked(v) { v ? this.setAttribute('checked', '') : this.removeAttribute('checked'); }

  attributeChangedCallback(name, _, val) {
    if (name === 'label') this.shadowRoot.querySelector('.label').textContent = val;
    if (name === 'checked') this._render();
  }

  connectedCallback() { this._render(); }

  _render() {
    const track = this.shadowRoot.querySelector('.track');
    this.checked ? track.classList.add('on') : track.classList.remove('on');
  }

  _toggle() {
    this.checked = !this.checked;
    this.dispatchEvent(new CustomEvent('change', { detail: { checked: this.checked } }));
  }
}
```

- [ ] **Step 3: 在文件末尾添加注册代码**

```js
customElements.define('aion-dots', AionDots);
customElements.define('aion-toggle', AionToggle);
```

- [ ] **Step 4: 在浏览器中验证**

在 `worldbook.html`（最简单的子页面）的 `</head>` 前临时加一行 `<script src="/static/components.js"></script>`，页面 `<body>` 底部加测试标签：

```html
<aion-dots></aion-dots>
<aion-dots color="#1976d2"></aion-dots>
<aion-toggle label="测试开关"></aion-toggle>
```

启动服务器，打开页面确认三个圆点动画正常、开关可点击切换。确认后删除测试标签。

- [ ] **Step 5: 提交**

```
git add aion-chat/static/components.js
git commit -m "feat: components.js 骨架 — aion-dots + aion-toggle"
```

---

### Task 2: 实现 aion-topbar + aion-subpage

**Files:**
- 修改: `aion-chat/static/components.js`

- [ ] **Step 1: 在 AionToggle 之后实现 AionTopbar**

```js
class AionTopbar extends HTMLElement {
  static get observedAttributes() { return ['title', 'back']; }

  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: flex; align-items: center; gap: 10px;
          padding: 12px 16px;
          border-bottom: 1px solid var(--border, #f0e4dd);
          background: rgba(255,249,245,0.85);
          backdrop-filter: blur(20px) saturate(1.6);
          -webkit-backdrop-filter: blur(20px) saturate(1.6);
          flex-shrink: 0; z-index: 10;
        }
        .back-btn {
          background: none; border: none; color: var(--accent, #ff8359);
          font-size: 20px; cursor: pointer; padding: 4px 6px; flex-shrink: 0;
          display: flex; align-items: center;
        }
        h2 {
          flex: 1; margin: 0; font-size: 17px; font-weight: 600;
          overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
          color: var(--text, #4a3b32);
        }
        .actions { display: flex; align-items: center; gap: 6px; }
      </style>
      <button class="back-btn">⬅</button>
      <h2></h2>
      <div class="actions"><slot name="actions"></slot></div>
    `;
    this.shadowRoot.querySelector('.back-btn').addEventListener('click', () => {
      location.href = this.getAttribute('back') || '/';
    });
  }

  attributeChangedCallback(name, _, val) {
    if (name === 'title') this.shadowRoot.querySelector('h2').textContent = val;
  }

  connectedCallback() {
    this.shadowRoot.querySelector('h2').textContent = this.getAttribute('title') || '';
  }
}
```

- [ ] **Step 2: 实现 AionSubpage**

```js
class AionSubpage extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: flex; flex-direction: column;
          height: 100dvh; max-width: 600px; margin: 0 auto;
        }
        :host-context(html.aion-app:not(.aion-iframe)) {
          padding-top: 34px;
        }
        :host-context(html.aion-app:not(.aion-iframe))::before {
          content: ""; position: fixed; top: 0; left: 0; right: 0;
          height: 34px; z-index: 999; pointer-events: none;
          background: var(--aion-safe-bg, var(--bg, #fff9f5));
        }
        .content {
          flex: 1; overflow-y: auto; padding: 16px;
        }
        ::slotted(aion-topbar) { flex-shrink: 0; }
      </style>
      <slot name="topbar"></slot>
      <slot></slot>
    `;
  }
}
```

**注意：** `<aion-subpage>` 使用两个 slot：一个 named slot `topbar` 用于放 `<aion-topbar>`，一个 default slot 用于页面内容。但实际上更简单的做法是全部用 default slot，让 topbar 作为子元素自然排列——因为 topbar 自身已经是 `flex-shrink: 0`。所以这里只用 default slot：

```js
class AionSubpage extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: flex; flex-direction: column;
          height: 100dvh; max-width: 600px; margin: 0 auto;
        }
        :host-context(html.aion-app:not(.aion-iframe)) {
          padding-top: 34px;
        }
        :host-context(html.aion-app:not(.aion-iframe))::before {
          content: ""; position: fixed; top: 0; left: 0; right: 0;
          height: 34px; z-index: 999; pointer-events: none;
          background: var(--aion-safe-bg, var(--bg, #fff9f5));
        }
        .page-body { flex: 1; overflow-y: auto; padding: 16px; }
      </style>
      <slot name="topbar"></slot>
      <div class="page-body"><slot></slot></div>
    `;
  }
}
```

最终采用 named slot `topbar` 放在滚动区外，default slot 放在 `.page-body` 滚动区内。这样 topbar 固定在顶部，内容可滚动。

- [ ] **Step 3: 注册两个新组件**

在文件末尾的注册区追加：

```js
customElements.define('aion-topbar', AionTopbar);
customElements.define('aion-subpage', AionSubpage);
```

- [ ] **Step 4: 提交**

```
git add aion-chat/static/components.js
git commit -m "feat: aion-topbar + aion-subpage 组件"
```

---

### Task 3: 实现 aion-modal

**Files:**
- 修改: `aion-chat/static/components.js`

- [ ] **Step 1: 在 AionSubpage 之后实现 AionModal**

```js
class AionModal extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: none; }
        :host([visible]) { display: block; }
        .overlay {
          position: fixed; inset: 0;
          background: rgba(0,0,0,0.25); z-index: 1000;
          display: flex; align-items: center; justify-content: center;
          animation: overlayIn 0.2s ease;
        }
        @keyframes overlayIn { from { opacity: 0; } to { opacity: 1; } }
        .popup {
          position: relative;
          background: linear-gradient(145deg, #fffaf6, #fff3eb);
          border: 1px solid rgba(255,131,89,0.18);
          border-radius: 16px; padding: 22px 24px 20px;
          min-width: 260px; max-width: 380px; width: 85vw;
          box-shadow: 0 12px 40px rgba(0,0,0,0.12), 0 2px 8px rgba(255,131,89,0.08);
          animation: cardIn 0.3s cubic-bezier(0.34, 1.56, 0.64, 1);
        }
        @keyframes cardIn {
          from { opacity: 0; transform: scale(0.85) translateY(10px); }
          to { opacity: 1; transform: scale(1) translateY(0); }
        }
        .label-slot {
          font-size: 12px; color: #b0a39a;
          margin-bottom: 12px; letter-spacing: 1px;
        }
        .body-slot {
          font-size: 15px; color: #4a3b32;
          line-height: 1.7; white-space: pre-wrap; word-break: break-word;
        }
        .close-btn {
          position: absolute; top: 12px; right: 14px;
          background: none; border: none; color: #b0a39a;
          font-size: 18px; cursor: pointer; padding: 4px 6px;
          border-radius: 6px; line-height: 1;
          transition: color 0.15s, background 0.15s;
        }
        .close-btn:hover { color: #4a3b32; background: rgba(0,0,0,0.05); }
      </style>
      <div class="overlay">
        <div class="popup">
          <button class="close-btn">✕</button>
          <div class="label-slot"><slot name="label"></slot></div>
          <div class="body-slot"><slot name="body"></slot></div>
        </div>
      </div>
    `;
    this.shadowRoot.querySelector('.close-btn').addEventListener('click', () => this.close());
    this.shadowRoot.querySelector('.overlay').addEventListener('click', (e) => {
      if (e.target === e.currentTarget) this.close();
    });
  }

  open() { this.setAttribute('visible', ''); }

  close() {
    this.removeAttribute('visible');
    this.dispatchEvent(new Event('close'));
  }
}
```

- [ ] **Step 2: 注册组件**

```js
customElements.define('aion-modal', AionModal);
```

- [ ] **Step 3: 提交**

```
git add aion-chat/static/components.js
git commit -m "feat: aion-modal 弹窗组件"
```

---

### Task 4: 实现 aion-sidebar-btn + aion-icon-btn

**Files:**
- 修改: `aion-chat/static/components.js`

- [ ] **Step 1: 实现 AionSidebarBtn**

```js
class AionSidebarBtn extends HTMLElement {
  static get observedAttributes() { return ['icon']; }

  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        button {
          width: 100%; padding: 10px; border: none; border-radius: 8px;
          background: var(--surface2, #fff0e6); color: var(--text, #4a3b32);
          font-size: 14px; cursor: pointer;
          display: flex; align-items: center; justify-content: center; gap: 6px;
          font-family: inherit;
        }
        button:hover { background: var(--border, #f0e4dd); }
        .icon { flex-shrink: 0; }
      </style>
      <button><span class="icon"></span><slot></slot></button>
    `;
    this.shadowRoot.querySelector('button').addEventListener('click', () => {
      this.click();
    });
  }

  attributeChangedCallback(name, _, val) {
    if (name === 'icon') this.shadowRoot.querySelector('.icon').textContent = val;
  }

  connectedCallback() {
    const icon = this.getAttribute('icon');
    if (icon) this.shadowRoot.querySelector('.icon').textContent = icon;
  }
}
```

- [ ] **Step 2: 实现 AionIconBtn**

```js
class AionIconBtn extends HTMLElement {
  static get observedAttributes() { return ['icon']; }

  constructor() {
    super();
    this.attachShadow({ mode: 'open' });
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: inline-block; }
        button {
          width: 40px; height: 40px; border: none; border-radius: 8px;
          background: var(--surface2, #fff0e6); color: var(--text, #4a3b32);
          font-size: 18px; cursor: pointer;
          display: flex; align-items: center; justify-content: center;
          font-family: inherit; padding: 0;
        }
        button:hover { background: var(--border, #f0e4dd); }
      </style>
      <button></button>
    `;
    this.shadowRoot.querySelector('button').addEventListener('click', () => {
      this.click();
    });
  }

  attributeChangedCallback(name, _, val) {
    if (name === 'icon') this.shadowRoot.querySelector('button').textContent = val;
  }

  connectedCallback() {
    const icon = this.getAttribute('icon');
    if (icon) this.shadowRoot.querySelector('button').textContent = icon;
  }
}
```

- [ ] **Step 3: 注册两个组件**

```js
customElements.define('aion-sidebar-btn', AionSidebarBtn);
customElements.define('aion-icon-btn', AionIconBtn);
```

- [ ] **Step 4: 提交**

```
git add aion-chat/static/components.js
git commit -m "feat: aion-sidebar-btn + aion-icon-btn 侧栏按钮组件"
```

---

### Task 5: 试点迁移 worldbook.html

用组件替换手写布局，验证效果完全一致。

**Files:**
- 修改: `aion-chat/static/worldbook.html`

- [ ] **Step 1: 迁移 worldbook.html**

将现有代码：

```html
<link rel="stylesheet" href="/static/common.css">
<link rel="manifest" href="/manifest.json">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#ff8359">
</head>
<body>
<div class="sub-page">
  <div class="top-bar">
    <button class="back-btn" onclick="location.href='/'">⬅</button>
    <h2>📖 世界书</h2>
  </div>
  <div class="page-content">
```

替换为：

```html
<link rel="stylesheet" href="/static/common.css">
<script src="/static/components.js"></script>
<link rel="manifest" href="/manifest.json">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="theme-color" content="#ff8359">
</head>
<body>
<aion-subpage>
  <aion-topbar slot="topbar" title="📖 世界书"></aion-topbar>
```

将结尾的：

```html
  </div>
</div>
```

（`page-content` 和 `sub-page` 的闭合标签）替换为：

```html
</aion-subpage>
```

- [ ] **Step 2: 启动服务器验证**

打开 worldbook 页面，检查：
1. 顶栏样式与原版一致（毛玻璃背景、标题字体、返回按钮颜色）
2. 返回按钮点击跳转到 `/`
3. 内容区可滚动
4. 表单元素样式正常（它们仍使用 common.css 的 `.form-input` 等）
5. 保存功能正常

- [ ] **Step 3: 提交**

```
git add aion-chat/static/worldbook.html
git commit -m "feat: worldbook.html 迁移到 Web Components"
```

---

### Task 6: 迁移 settings.html

**Files:**
- 修改: `aion-chat/static/settings.html`

- [ ] **Step 1: 迁移**

在 `<head>` 中 `<link rel="stylesheet" href="/static/common.css">` 之后加一行：
```html
<script src="/static/components.js"></script>
```

将 `<body>` 中：
```html
<div class="sub-page">
  <div class="top-bar">
    <button class="back-btn" onclick="location.href='/'">⬅</button>
    <h2>⚙ 设置</h2>
  </div>
  <div class="page-content">
```

替换为：
```html
<aion-subpage>
  <aion-topbar slot="topbar" title="⚙ 设置"></aion-topbar>
```

将对应的闭合 `</div></div>` 替换为 `</aion-subpage>`。

- [ ] **Step 2: 验证并提交**

打开 settings 页面确认布局和功能正常。

```
git add aion-chat/static/settings.html
git commit -m "feat: settings.html 迁移到 Web Components"
```

---

### Task 7: 迁移 schedule.html

**Files:**
- 修改: `aion-chat/static/schedule.html`

- [ ] **Step 1: 迁移**

与 Task 6 相同模式——加 `<script src="/static/components.js"></script>`，将 `sub-page` > `top-bar` > `page-content` 替换为 `<aion-subpage>` + `<aion-topbar slot="topbar" title="📅 日程管理">`。

- [ ] **Step 2: 验证并提交**

```
git add aion-chat/static/schedule.html
git commit -m "feat: schedule.html 迁移到 Web Components"
```

---

### Task 8: 迁移 memory.html

**Files:**
- 修改: `aion-chat/static/memory.html`

- [ ] **Step 1: 迁移**

同模式。注意 memory.html 的 topbar 右侧有额外按钮，需要检查是否有 actions slot 的需求。如果有，使用：

```html
<aion-topbar slot="topbar" title="🧠 记忆库">
  <button slot="actions" class="btn-digest" ...>摘要</button>
</aion-topbar>
```

- [ ] **Step 2: 验证并提交**

```
git add aion-chat/static/memory.html
git commit -m "feat: memory.html 迁移到 Web Components"
```

---

### Task 9: 迁移 fund.html（含 aion-toggle）

**Files:**
- 修改: `aion-chat/static/fund.html`

- [ ] **Step 1: 迁移布局**

同模式替换 `sub-page` > `top-bar` > `page-content`。标题: `"💰 奥罗斯财团"`（从页面现有 top-bar 获取）。

- [ ] **Step 2: 替换 toggle 开关**

将 fund.html 中的：

```html
<div class="fund-toggle">
  <div>
    <span class="label">基金监控</span>
    <span class="status" id="toggleStatus">每日 14:45 自动分析</span>
  </div>
  <label class="switch">
    <input type="checkbox" id="enableToggle" checked onchange="toggleEnabled()">
    <span class="slider"></span>
  </label>
</div>
```

替换为：

```html
<div class="fund-toggle">
  <div>
    <span class="label">基金监控</span>
    <span class="status" id="toggleStatus">每日 14:45 自动分析</span>
  </div>
  <aion-toggle id="enableToggle" checked></aion-toggle>
</div>
```

同时更新 `toggleEnabled()` 函数，从原来的读取 checkbox 改为读取 `aion-toggle`：

```js
// 原来：$("enableToggle").checked
// 改为：$("enableToggle").checked（aion-toggle 也暴露了 .checked 属性，无需改代码）
```

但需要将 `onchange` 改为事件监听（因为 Web Component 的 `change` 是 CustomEvent）：

```js
$("enableToggle").addEventListener('change', () => toggleEnabled());
```

并从 `toggleEnabled()` 中将读取 checked 的方式保持为 `$("enableToggle").checked`——组件已经暴露了这个属性。

删除 fund.html `<style>` 中的 `.switch` 相关 CSS（第 20-32 行），因为已被 `<aion-toggle>` 替代。

- [ ] **Step 3: 验证并提交**

确认开关可切换、状态正确。

```
git add aion-chat/static/fund.html
git commit -m "feat: fund.html 迁移到 Web Components + aion-toggle"
```

---

### Task 10: 迁移剩余子页面（gift, activity-logs, monitor-logs, reading, location）

**Files:**
- 修改: `aion-chat/static/gift.html`
- 修改: `aion-chat/static/activity-logs.html`
- 修改: `aion-chat/static/monitor-logs.html`
- 修改: `aion-chat/static/reading.html`
- 修改: `aion-chat/static/location.html`

- [ ] **Step 1: 逐个迁移**

每个页面同模式：加 `<script src="/static/components.js"></script>`，替换 `sub-page` + `top-bar` + `page-content` 为 `<aion-subpage>` + `<aion-topbar>`。

注意 gift.html 的特殊情况：它有自定义深色背景 `body { background: #1a1412; }` 和自定义 top-bar 样式覆盖。迁移时需要确认 `<aion-topbar>` 在深色背景下的外观——可能需要在 gift.html 里对 `aion-topbar` 添加 CSS 变量覆盖：

```css
aion-topbar {
  --text: #e8ddd4;
  --accent: #ff8359;
  --border: rgba(255,131,89,0.15);
}
```

每个页面迁移完后逐一在浏览器中验证。

- [ ] **Step 2: 批量提交**

```
git add aion-chat/static/gift.html aion-chat/static/activity-logs.html aion-chat/static/monitor-logs.html aion-chat/static/reading.html aion-chat/static/location.html
git commit -m "feat: 剩余子页面迁移到 Web Components"
```

---

### Task 11: 迁移 chat.html 侧栏按钮

**Files:**
- 修改: `aion-chat/static/chat.html`

- [ ] **Step 1: 在 chat.html 的 `<head>` 中引入 components.js**

在 chat.html 的 `<style>` 标签之前（或 `</head>` 之前）加：

```html
<script src="/static/components.js"></script>
```

- [ ] **Step 2: 替换 sidebar-header 按钮**

将 chat.html 第 760-764 行的：

```html
<div class="sidebar-header">
  <button onclick="newConversation()">+ 新对话</button>
  <button class="settings-btn" onclick="openFileManager()">📁</button>
  <button class="settings-btn" onclick="openSubPage('/settings')">⚙</button>
</div>
```

替换为：

```html
<div class="sidebar-header">
  <button onclick="newConversation()">+ 新对话</button>
  <aion-icon-btn icon="📁" onclick="openFileManager()"></aion-icon-btn>
  <aion-icon-btn icon="⚙" onclick="openSubPage('/settings')"></aion-icon-btn>
</div>
```

同时删除 chat.html `<style>` 中的 `.sidebar-header .settings-btn` 规则（第 53 行），因为已被组件替代。

- [ ] **Step 3: 替换 sidebar-footer 按钮**

将第 766-771 行的：

```html
<div class="sidebar-footer">
  <button id="sysLogBtn" onclick="openSystemLog()">📋 系统日志</button>
  <button onclick="openWhisper()">💗 密语时刻</button>
  <button onclick="openWalletPanel()">💰 钱包</button>
  <button onclick="openSubPage('/')">🏠 返回主页</button>
</div>
```

替换为：

```html
<div class="sidebar-footer">
  <aion-sidebar-btn id="sysLogBtn" icon="📋" onclick="openSystemLog()">系统日志</aion-sidebar-btn>
  <aion-sidebar-btn icon="💗" onclick="openWhisper()">密语时刻</aion-sidebar-btn>
  <aion-sidebar-btn icon="💰" onclick="openWalletPanel()">钱包</aion-sidebar-btn>
  <aion-sidebar-btn icon="🏠" onclick="openSubPage('/')">返回主页</aion-sidebar-btn>
</div>
```

删除 chat.html `<style>` 中的 `.sidebar-footer button` 规则（第 300-301 行），因为已被组件替代。

- [ ] **Step 4: 验证**

打开 chat 页面，检查：
1. sidebar-header 的 📁 ⚙ 按钮样式和点击行为正常
2. sidebar-footer 的四个按钮样式和点击行为正常
3. 移动端侧栏打开/关闭正常
4. 其他 chat 功能不受影响

- [ ] **Step 5: 提交**

```
git add aion-chat/static/chat.html
git commit -m "feat: chat.html 侧栏按钮迁移到 Web Components"
```

---

### Task 12: 最终验证 + 清理

- [ ] **Step 1: 全面验证**

逐一打开所有迁移的页面，确认无视觉回归：
- worldbook, settings, schedule, memory, fund, gift, activity-logs, monitor-logs, reading, location
- chat.html 侧栏

- [ ] **Step 2: 提交**

如果验证过程中有修复，单独提交修复。

```
git commit -m "fix: Web Components 迁移最终调整"
```
