# 前端与渲染约定

## 现状：零构建

卡片的"前端"就是 `templates/` 下的 6 个 **纯 HTML + 内联 CSS** 模板：

``
templates/
├── card_sakura.html / card_neon.html / card_minimal.html               # 开播与下播卡片
├── overview_sakura.html / overview_neon.html / overview_minimal.html   # 开播状态总览
└── fonts/FusionPixel8px-zh_hans.woff2 (+ OFL 许可)                      # 随包分发的中文字体
``

渲染路径：Python 侧把数据填进 `{{占位符}}`（`render.py`），交给宿主 `ctx.screenshots`（Chromium + CDP）出 PNG，再 `ctx.send_image` 发出。

模板里不允许出现：`<script>`、`@import`、任何 `http(s)://` 外链。原因是截图端口用 CDP `setDocumentContent` 把 HTML 写进 `about:blank`，**`file://` 与相对路径资源都加载不到**，所以：

- CSS 必须内联在模板的 `<style>` 里；
- 图片必须由渲染端转成 data URI 再填进去（头像、封面都是这么处理的）；
- 字体以 `FontFace` 交给截图端口，由端口生成 data URI 的 `@font-face`。

这一层没有 Node、没有任何构建步骤，改模板就是改 HTML，部署侧自然也不需要前端工具链。

## 将来要引入真正的构建时

如果以后需要 React/Vite 这类工程（例如做一个可交互的配置页），沿用 NeoBot 官方插件约定：

| 目录 | 内容 | 入库 | 分发 |
|---|---|---|---|
| `frontend/` | 源码，只有开发者需要 | 入库 | **不**分发 |
| `web/` | 构建产物（`index.html` + `assets/index-<hash>.{js,css}`） | **必须入库** | 分发 |

构建基线：`base: './'`（支持面板子路径部署）、`build.outDir = '../web'`、`assetsDir = 'assets'`、`sourcemap: false`。

**产物与源码必须在同一个 commit 内更新**，且 `node_modules/` 永不入库（NeoBot 面板安装插件走 GitHub 归档 zip，有压缩包 64MB / 解压 256MB / 条目 5000 的硬上限）。

## 服务端形态（未接入）

若要把插件的页面挂到网页面板上，需要声明 `dependencies = ["dashboard>=1.0.0"]` 并实现 `DashboardWebExtension` 协议，在 `on_load` 里用能力 `web.register_extension` 注册，静态资源用 `StaticAssetDirectory` 暴露。当前版本未接入面板。

