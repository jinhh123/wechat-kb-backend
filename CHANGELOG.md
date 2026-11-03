# 更新记录

## 2026-11-03

- 知识卡片改为点击弹窗浏览全文，Markdown 图片直接显示、支持灯箱放大。
- 新增卡片多选合并；合并后原卡片保留并写入 `is_merged: true`。
- 分类拖拽后调用后端，把 `category` 写回 Markdown 的 YAML frontmatter。
- 新增“粘贴链接”按钮和后端 `/api/knowledge/link`，公众号链接可直接归档。
- 详情弹窗新增“添加到日程”，复用前端日历的 localStorage 日程数据。
- 微信原文仍不支持深链，保留“复制定位信息”（标题 + 日期）手动搜索方案。
- 后端 API 新增 `/api/knowledge/category`、`/api/knowledge/link`、`/api/knowledge/merge`、`/images/*`。
- 使用说明以 `README.md` 为准，已同步本轮功能；Word/PDF 摘要文档如需要可再生成。
