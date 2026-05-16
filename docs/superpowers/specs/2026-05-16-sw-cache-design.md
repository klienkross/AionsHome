# SW 缓存方案设计

## 目的

通过 Service Worker 缓存静态资源（JS/CSS/HTML/图片/音频），减少 tailnet 网络延迟对 app 加载速度的影响。

## 策略

**运行时动态缓存（Runtime Caching）**

- 不维护预缓存列表，SW 拦截请求时按路径判断是否缓存
- 首次访问走网络并存入缓存，后续访问直接返回缓存
- 支持手动强制刷新（设置页按钮）

## 路由规则

| 路径模式 | 策略 | 说明 |
|----------|------|------|
| `/static/*` | Cache First | JS/CSS/HTML 页面资源 |
| `/public/*` | Cache First | 图片、音频、图标 |
| `/manifest.json` | Cache First | PWA 清单 |
| `/api/*` | Network Only | 动态数据，不缓存 |
| `?nocache` 参数 | Network Only | 逃生口，跳过缓存 |

## 强制刷新

- 触发方式：settings.html 加「清除缓存」按钮
- 流程：页面 postMessage → SW 删除 CacheStorage → SW 通知所有 client → 页面 reload

## 文件改动

1. **`aion-chat/static/sw.js`** — 重写，加入 cache-first 逻辑 + message 监听
2. **`aion-chat/static/common.js`** — 加 SW 注册 + reload 监听
3. **`aion-chat/static/settings.html`** — 加「清除缓存」按钮

## 缓存版本

`CACHE_NAME = 'aion-v1'`，修改此值会在 SW activate 时自动清除旧缓存。

## 兼容性

- 不改 APK，老版本 WebView 加载同一 URL 即可生效
- SW 注册失败时静默降级，等同当前行为（每次走网络）
