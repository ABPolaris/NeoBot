# B站与跨平台

> 状态：已实现（2026-10-20 增补搜索/视频/用户工具与回复池）。B站功能默认开启，但未登录时服务休眠、等同关闭。

## 功能

- **扫码登录**：`/login bilibili` 发送二维码图片，B站 App 扫码确认后 cookie 落到 data/bilibili/cookies.json；二维码 180 秒有效。
- **评论/@ 回复轮询**：每 1800 秒（30 分钟）检查一次「回复我的 + @我的」；默认关闭（config.bilibili.comment_reply_enabled=false），避免不可控回复与 API 费用。每轮最多回复 50 条；未回完/发送失败/冷却中的事件进入回复池，后续轮次继续；同一会话 60 秒冷却，指数退避。旧通知不再因启动时间被丢弃。
- **私信回复轮询**：同构；每轮最多 50 个会话，未轮到的进入待处理池，默认关闭。
- **B站专用提示词**：评论/私信/跨聊天任务/日记各有独立提示词，包含 B站语境（短评、楼中楼、无 QQ 上下文）与「不懂装懂」禁令；不套用 QQ 群聊提示词。活人感增强规则 QQ 与 B站两侧都启用（QQ 走 prompts.toml 的 human_feel，B站走 BILI_STYLE_RULES）。
- **QQ-B站账号关联**：通过 agent 工具 link/unlink/get_link 把 B站 UID 关联到 QQ 号；首次关联把 bilibili_user/<uid> 档案合并进 user_profile/<qq>，回复时按关联回查档案。
- **B站动态日记**：默认关闭；开启后可按每日时间生成聊天/档案摘要并发一条 B站纯文本动态，不读写 QQ 空间。agent 工具 publish_bilibili_diary 支持 preview。
- **跨聊天通道**：cross_chat 增加 bilibili 目标，会话 ID 为 private:<uid> 或 comment:<oid>:<root>:<parent>。
- **B站回复的视频链接增强**：评论/私信正文或评论树里出现 BV 号/bilibili.com 视频链接时，回复引擎自动调用 watch_video 取标题+字幕/ASR 摘要（最多 3000 字符），作为「相关视频」上下文交给模型；超过 3000 字符的完整转写自动存入 bilibili_video 档案，QQ 侧 agent 可用 read_video_transcript 按需读取；失败静默跳过，不阻断回复。
- **外部 ASR**：硅基流动 OpenAI 兼容 /v1/audio/transcriptions，默认模型 XingChenAGI/XingChenASR-V3.2-Ultra（免费）；ffmpeg 转 16k 单声道 mp3，50MB/1 小时限制，错误降级为文本。

## Agent 工具（QQ 侧触发）

搜索/视频/用户三组技能注册进 SkillManager，QQ 侧 agent 可直接调用：

| 工具（SkillManager 前缀） | 说明 |
|---|---|
| bilibili_search__search_video(keyword,page,order) | 搜视频：标题、BV号、UP主、播放、链接、简介；order 支持综合/播放/最新/弹幕/收藏 |
| bilibili_search__search_user(keyword,page) | 搜用户：UID、昵称、签名、粉丝、空间链接 |
| bilibili_search__search_article(keyword,page) | 搜专栏：CV号、标题、作者、阅读、链接 |
| bilibili_video__video_info(identifier) | 视频资料（BV/av/URL） |
| bilibili_video__watch_video(identifier,max_chars,include_comments) | 看视频：站点字幕优先，无字幕走 ASR 转写；默认最多 3000 字符，返回 source=site/asr，可选热门评论；超长全文自动存档案 |
| bilibili_video__read_video_transcript(identifier) | 按 BV 号读取 watch_video 存档的完整转写（转写超过 3000 字符时才有），用于按需读取 |
| bilibili_video__video_comments(identifier,page,order) | 拉视频评论（热门/最新） |
| bilibili_user__user_info(uid) | B站用户资料 |
| bilibili_user__user_videos(uid,page) | 用户投稿列表 |
| bilibili_user__linked_qq_profile(uid) | 按B站UID查关联的QQ主页：QQ号、备注、印象、user_summary/user_profile 档案；无关联明确提示 |
| bilibili_link__link_account / unlink_account / get_link | 账号关联 |
| bilibili_memory__save/read/list/delete_memory | B站长期记忆 |
| bilibili_diary__publish_bilibili_diary | 动态日记（preview 可只出草稿） |

> 工具结果都是 JSON 文本；identifier 支持 BV 号、av 号、完整视频链接；b23.tv 短链暂不支持。

## 命令

- /login bilibili：扫码登录（次级管理员）
- /bili status：查看登录/轮询状态（次级管理员）
- /bili dynamic <文本>：发布一条 B站纯文本动态（次级管理员）

## 直播监听

B站直播监听已内嵌为官方内置插件 streaming_parser（默认开启，命令见 docs/04-功能文档/B站直播监听.md）。
B站视频搜索/看视频走上面的 agent 工具（QQ 侧触发），直播插件负责开播/下播通知，两者独立。

## 风险

- 前端控制台扫码页已实现：面板打开 /bilibili/login 扫码（需面板登录；远程访问受 allow_remote_manage 限制）。
- 扫码成功、发评论/私信/动态、搜索/看视频/查用户均只经 mock + 匿名接口验证，未用真实账号全面实测；ASR 路径取决于 SiliconFlow key 与视频是否有公开字幕。
- 评论回复已记录 token 用量；@ 通知已支持（get_at_feed，与「回复我的」按 oid+parent_id 去重）。
- B站接口为 Web 协议，存在风控/合规风险；默认关闭自动回复是安全边界。

## 配置

见 docs/05-配置参考.md 的 [bilibili] 与 [asr] 段。
