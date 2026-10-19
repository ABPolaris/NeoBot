# B站与跨平台

> 状态：首版已实现（2026-10-19）。B站功能默认开启，但未登录时服务休眠、等同关闭。

## 功能

- **扫码登录**：`/login bilibili` 发送二维码图片，B站 App 扫码确认后 cookie 落到 data/bilibili/cookies.json；二维码 180 秒有效。
- **评论/@ 回复轮询**：每 1800 秒（30 分钟）检查一次「回复我的」；默认关闭（config.bilibili.comment_reply_enabled=false），避免不可控回复与 API 费用。只处理回复 bot 的新评论，每轮最多 3 条，同一会话 60 秒冷却，失败指数退避。
- **私信回复轮询**：同构，默认关闭。
- **B站专用提示词**：评论/私信/跨聊天任务/日记各有独立提示词，包含 B站语境（短评、楼中楼、无 QQ 上下文）与「不懂装懂」禁令；不套用 QQ 群聊提示词。
- **QQ-B站账号关联**：通过 agent 工具 link/unlink/get_link 把 B站 UID 关联到 QQ 号；首次关联把 bilibili_user/<uid> 档案合并进 user_profile/<qq>，回复时按关联回查档案。
- **B站动态日记**：默认关闭；开启后可按每日时间生成聊天/档案摘要并发一条 B站纯文本动态，不读写 QQ 空间。agent 工具 publish_bilibili_diary 支持 preview。
- **跨聊天通道**：cross_chat 增加 bilibili 目标，会话 ID 为 private:<uid> 或 comment:<oid>:<root>:<parent>。
- **外部 ASR**：硅基流动 OpenAI 兼容 /v1/audio/transcriptions，默认模型 XingChenAGI/XingChenASR-V3.2-Ultra（免费）；ffmpeg 转 16k 单声道 mp3，50MB/1 小时限制，错误降级为文本。

## 命令

- /login bilibili：扫码登录（次级管理员）
- /bili status：查看登录/轮询状态（次级管理员）
- /bili dynamic <文本>：发布一条 B站纯文本动态（次级管理员）

## 直播监听

B站直播监听已内嵌为官方内置插件 streaming_parser（默认开启，命令见 docs/04-功能文档/B站直播监听.md）。
不接入 B站视频搜索/看视频功能（用户指示暂不接，后续再设计）。

## 风险

- 前端控制台扫码页已实现：面板打开 /bilibili/login 扫码（需面板登录；远程访问受 allow_remote_manage 限制）。
- 扫码成功、发评论/私信/动态均只经 mock + 匿名接口验证，未用真实账号实测。
- 评论回复已记录 token 用量；@ 通知已支持（get_at_feed，与「回复我的」按 oid+parent_id 去重）。
- B站接口为 Web 协议，存在风控/合规风险；默认关闭自动回复是安全边界。

## 配置

见 docs/05-配置参考.md 的 [bilibili] 与 [asr] 段。
