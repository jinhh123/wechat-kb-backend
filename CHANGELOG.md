# 更新记录

## 2026-11-03

- 知识卡片改为点击弹窗浏览全文，Markdown 图片直接显示、支持灯箱放大。
- 新增卡片多选合并；原卡片保留，写入 `is_merged: true` 和 `merged_into`；合并失败自动回滚。
- 已合并的卡片再次参与合并时，后端返回 409 并拒绝。
- 分类拖拽后调用后端，把 `category` 写回 Markdown 的 YAML frontmatter；写回失败回滚并显示“未同步”。
- 新增“一键迁移旧分类到文件”按钮，把浏览器残留分类批量写回 Markdown。
- 知识库顶部搜索支持 300ms 防抖，匹配标题、正文、分类、标签。
- 新增“粘贴链接”按钮和后端 `/api/knowledge/link`，同 URL 自动去重。
- 详情弹窗新增“添加到日程”，使用 `YYYY-MM-DD` 标准日期，localStorage 只存 200 字摘要。
- 本地 API 写操作加 `LOCAL_API_TOKEN` 校验，CORS 限制为本机前端。
- 微信原文仍不支持深链，保留“复制定位信息”（标题 + 日期）手动搜索方案。
- 后端 API 新增 `/api/knowledge/category`、`/api/knowledge/link`、`/api/knowledge/merge`、`/images/*`。
- 使用说明以 `README.md` 为准，已同步本轮功能；Word/PDF 摘要文档如需要可再生成。
