# Web Components 迁移设计

## 目标

用原生 Web Components 封装子页面公共 UI 组件，实现：
- 新功能开发时可复用，不重复写样板代码
- 减少 AI 辅助开发时的 token 消耗
- 渐进式迁移，不破坏现有页面

## 约束

- 不引入构建工具、npm、框架（Lit 等）
- 不引入 ES modules，保持 `<script src>` 方式
- 第一期主要做子页面（settings, schedule, worldbook, memory, fund, gift 等）
- chat.html 只组件化侧栏按钮（上游合并痛点），不动消息列表/输入框等核心区域
- 不封装表单元素（`form-label`, `form-input`），它们留在 `common.css`

## 文件结构

新增一个文件：`aion-chat/static/components.js`

引入顺序（子页面 `<head>` 中）：

```html
<link rel="stylesheet" href="/static/common.css">
<script src="/static/components.js"></script>
```

职责划分：
- `common.css` — 全局 CSS 变量（`:root`）、基础 reset、表单样式
- `components.js` — Web Components 定义与注册
- `common.js` — 工具函数（`$()`, `api()`, `showToast()`, `connectCommonWS()` 等）

三者互不干扰。

## 组件清单

### `<aion-subpage>`

整个子页面的布局壳。

- 内部结构：flexbox 列布局，`max-width: 600px` 居中，内容区可滚动
- 自动处理 AionApp WebView 的 safe area padding

```html
<aion-subpage>
  <aion-topbar title="📅 日程管理"></aion-topbar>
  <!-- 页面内容 -->
</aion-subpage>
```

### `<aion-topbar>`

顶部导航栏：返回按钮 + 标题 + 可选右侧 slot。

**属性：**
- `title` — 标题文字
- `back` — 返回 URL，默认 `"/"`

**Slot：**
- `actions` — 右侧自定义内容

```html
<aion-topbar title="⚙ 设置"></aion-topbar>

<aion-topbar title="记忆库">
  <button slot="actions" class="btn-digest">摘要</button>
</aion-topbar>

<aion-topbar title="设置" back="/chat"></aion-topbar>
```

### `<aion-modal>`

居中弹窗（overlay + popup 卡片）。

**方法：**
- `open()` — 打开弹窗
- `close()` — 关闭弹窗

**Slot：**
- `label` — 顶部小标签
- `body` — 内容区

**事件：**
- `close` — 弹窗关闭时派发

点击遮罩层或关闭按钮自动关闭。

```html
<aion-modal id="hwModal">
  <span slot="label">💌 心语</span>
  <p slot="body">心语内容...</p>
</aion-modal>

<script>
  document.getElementById('hwModal').open();
</script>
```

### `<aion-toggle>`

开关组件。

**属性：**
- `label` — 文字
- `checked` — 布尔，当前状态

**事件：**
- `change` — `detail.checked` 为当前状态

```html
<aion-toggle id="autoTrade" label="自动交易" checked></aion-toggle>

<script>
  document.getElementById('autoTrade')
    .addEventListener('change', e => console.log(e.detail.checked));
</script>
```

### `<aion-dots>`

弹跳小圆点加载动画。

**属性：**
- `color` — 圆点颜色，默认 `var(--accent)`

```html
<aion-dots></aion-dots>
<aion-dots color="#1976d2"></aion-dots>
```

### `<aion-sidebar-btn>`

侧栏全宽按钮（用于 chat.html sidebar-footer）。

**属性：**
- `icon` — emoji 图标

按钮文字通过默认 slot 传入。onclick 等事件直接写在标签上。

```html
<aion-sidebar-btn icon="📋" onclick="openSystemLog()">系统日志</aion-sidebar-btn>
<aion-sidebar-btn icon="💗" onclick="openWhisper()">密语时刻</aion-sidebar-btn>
```

### `<aion-icon-btn>`

方形图标小按钮（用于 chat.html sidebar-header 的 📁 ⚙ 等）。

**属性：**
- `icon` — emoji 图标

```html
<aion-icon-btn icon="📁" onclick="openFileManager()"></aion-icon-btn>
<aion-icon-btn icon="⚙" onclick="openSubPage('/settings')"></aion-icon-btn>
```

## 技术方案

### Shadow DOM

所有组件使用 Shadow DOM（`mode: 'open'`）：
- 样式隔离，组件内外互不干扰
- 通过 CSS 变量（`var(--accent)` 等）读取全局主题色——CSS 变量天然穿透 Shadow DOM

### 组件注册

`components.js` 内部每个组件一个 class，文件末尾统一 `customElements.define()`。

### 迁移策略

渐进式，不一刀切：

1. 写好 `components.js`，所有组件可用
2. 选 `worldbook.html` 作为试点页面迁移
3. 验证后逐页迁移其他子页面
4. `common.css` 里的旧样式（`.top-bar`, `.sub-page` 等）暂时保留，等全部迁移完再清理
