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
- **外部 ASR**：硅基流动 OpenAI 兼容 /v1/audio/transcriptions，默认模型 XingChenAGI/XingChenASR-V3.2-Ultra（免费）；ffmpeg 转 16k 单声道 mp3，50MB/1 小时限制，错误降级为文本。本机没有 ffmpeg/ffprobe 时会自动并发镜像下载安装（BtbN win64 包，NCD 同款 7 个镜像竞速，失败清理由日志记录）。

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
| bilibili_video__comment_tree(identifier,root_rpid,limit) | 读评论区树（顶级评论 + 楼中楼；root_rpid 可只展开某根评论） |
| bilibili_video__read_bili_messages(limit,include_at) | 读「回复我的」+「@我的」通知，归一化后给 agent 自己判断；只读 |
| bilibili_video__comment_video(identifier,text,root,parent) | 以当前登录账号给视频发评论/楼中楼回复（写操作，确认意图后调用） |
| bilibili_video__like_video(identifier) | 给视频点赞（写操作） |
| bilibili_video__like_comment(identifier,rpid) | 给视频评论点赞（写操作） |
| bilibili_user__user_info(uid) | B站用户资料 |
| bilibili_user__user_videos(uid,page) | 用户投稿列表 |
| bilibili_user__follow_user(uid,action) | 关注/取消关注B站用户（公开写操作） |
| bilibili_user__linked_qq_profile(uid) | 按B站UID查关联的QQ主页：QQ号、备注、印象、user_summary/user_profile 档案；无关联明确提示 |
| bilibili_link__link_account / unlink_account / get_link | 账号关联 |
| bilibili_memory__save/read/list/delete_memory | B站长期记忆 |
| bilibili_diary__publish_bilibili_diary | 动态日记（preview 可只出草稿） |

> 工具结果都是 JSON 文本；identifier 支持 BV 号、av 号、完整视频链接；b23.tv 短链暂不支持。

## 命令

- /login bilibili：扫码登录（次级管理员）
- /bili status：查看登录/轮询状态（次级管理员）
- /bili dynamic <文本>：发布一条 B站纯文本动态（次级管理员）

## 直播间交互（与监听插件独立）

直播间交互是 B站模块自带能力，**不依赖 streaming_parser 的监听订阅**，两者可分别开关：

| 命令 | 权限 | 说明 |
|---|---|---|
| /直播互动 <房间号|链接> 开|关 | 次级管理员 | 启用/停用某房间弹幕交互并持久化 |
| /直播互动AI <房间号> 开|关 | 次级管理员 | 开关该房间自动回复（群聊式意愿判断，@/叫名字必回） |
| /直播互动列表 | 所有人 | 查看已启用房间、真实房间号、自动回复状态 |
| /直播发言 <房间号|链接> <文本> | 次级管理员 | 用当前登录B站账号手动发一条弹幕 |

- 使用当前 B站登录态（/login bilibili 的 cookie）；未登录时只能记录房间，发弹幕/自动回复不可用。
- 自动回复：和群聊共用 WillingService 意愿判断（被 @/叫名字按 at_mention_guaranteed_reply 必回，其余按概率/问句/系数）；排除自己、同房间 20s 冷却 + 每分钟最多 3 条、回复截断 40 字符。无 willing 服务时退回旧的 live_interaction_at_only @过滤。
- 状态文件：data/bilibili/live_interact.json，重启自动恢复。
- 直播监听（开播/下播通知）在 streaming_parser 插件里，和这里完全分开。

定时弹幕与 agent 操作（bilibili_live 技能工具）：
- bilibili_live__status / send_danmaku / read_history（默认 500 条，按 [HH:MM:SS] 显示，间隔超 5 分钟插时间线）
- bilibili_live__snapshot / transcribe：按需截直播画面一帧、按需截 10–30 秒音频做 ASR（非实时字幕）
- bilibili_live__enable / disable
- bilibili_live__schedule / list_schedules / delete_schedule：本地时间 HH:MM，recurrence=once/daily/weekly；到点且房间在播才生成并发送，同一时段不重复；下播跳过。
- 所有自动回复、定时弹幕、手动发弹幕都先检查开播状态，下播不互动；只在开播阶段参与。
- 自动回复、定时弹幕、跨聊天直播投递触发时，会先抓一帧当前画面并让视觉模型生成画面描述，作为回复上下文（没有视觉模型时跳过截图）。
- 跨聊天可通过 target_kind=bilibili + live:<room_id> 把任务派给直播间（同样仅开播时发送）。

## UP投稿更新轮询（调研/预备，未实装）

已补 bilibili_user__follow_user 的 B站账号关注/取关；后台“关注 UP 并轮询新视频”尚未接线。
调研与预备设计见 Reference/_research/bilibili-update-polling.md，纯 diff/状态代码在
app/src/neobot_app/bilibili/updates.py（当前未被 runtime 引用），暂不启动轮询/通知。

## 直播监听

B站直播监听已内嵌为官方内置插件 streaming_parser（默认开启，命令见 docs/04-功能文档/B站直播监听.md）。
B站视频搜索/看视频走上面的 agent 工具（QQ 侧触发），直播插件负责开播/下播通知，两者独立。

## 直播媒体能力（2026-10-20）

- 直播拉流：getRoomPlayInfo 匿名拿 FLV/HLS 地址，缓存到过期前 5 分钟。
- 直播画面：bilibili_live__snapshot 按需截一帧 JPEG（ffmpeg，640 宽，存 data/live_frames），可附带视觉模型描述；自动回复/定时弹幕/跨聊天会各抓一帧注入上下文。
- 直播 ASR：bilibili_live__transcribe 按需截 10–30 秒音频转 16k mono mp3 后走现有 ASR；不是实时字幕，也没有连续转写。
- 直播历史：每房间最近 500 条，带时间戳；间隔超过 5 分钟插入时间线。
- 仍只处理 DANMU_MSG；礼物/SC/进场/舰长等事件未接入。

## 扩展能力（2026-10-20）

### 直播事件

直播弹幕 WSS 现支持 DANMU_MSG、SEND_GIFT、SEND_GIFT_V2(protobuf)、GUARD_BUY、USER_TOAST_MSG_V2、SUPER_CHAT_MESSAGE、SUPER_CHAT_MESSAGE_DELETE、INTERACT_WORD_V2、开播/下播/看过人数等事件；镜像消息在解析层丢弃，礼物/SC/上舰/互动有去重键。认证使用 protover=3 + Brotli，op=8 校验，host 轮换，认证后立即补心跳。

- bilibili_live__recent_events(room_id,count) 读最近 100 条礼物/SC/上舰/进房事件。
- 可选 QQ 推送：配置 [bilibili] live_event_notify_targets（group:群号/private:QQ号，默认空）和 live_event_notify_kinds（默认 gift/super_chat/guard）；只推这些事件，不刷弹幕。
- bilibili_live__record_clip(room_id,seconds) 按需录一小段 FLV（默认 30 秒，最大 300 秒）。
- ASR/截帧/定时弹幕/跨聊天仍只在开播阶段生效。

### 评论分页与类型

- get_comments 使用 /x/v2/reply/wbi/main + pagination_str 游标；comment_type 支持 video(1)、dynamic(17)、article/opus(12)、bangumi(11)。
- video_comments / comment_tree 支持 comment_type、limit、cursor；comment_tree 支持动态/专栏/opus 的楼中楼。
- 已回复评论状态持久化（data/bilibili/comment_replies.json）；重启不重复回复。

### 视频画面理解

- bilibili_video_frames skill：video_frames 按 every 秒抽关键帧（最多 32 帧），生成 4x4 接触印相纸，可选视觉模型描述；返回本地路径，可用 read_image 细看。
- 与 watch_video 的字幕/ASR 互补，解决“只有字幕、没有画面”的缺口。

### B站只读扩展

bilibili_read skill 工具：dynamic_detail、user_dynamics、article、bangumi、user_card、user_live_status、video_stream、video_danmaku、expand_b23。覆盖动态详情/用户动态、专栏 info/正文/cards、番剧 info/stream、用户名片、按 UID 直播状态、DASH/FLV 视频流、XML 视频弹幕、b23 短链展开；全部只读。

### 视频互动

bilibili_video 新增 coin_video / share_video / favorite_video / favorite_folders（写操作会以当前登录账号公开操作，需收藏夹 ID）。

## 风险

- **评论已回复持久化**：评论监控把已处理评论记入 data/bilibili/comment_replies.json，重启后继续去重，避免反复回复同一评论。

- 前端控制台扫码页已实现：面板打开 /bilibili/login 扫码（需面板登录；远程访问受 allow_remote_manage 限制）。
- 扫码成功、发评论/私信/动态、搜索/看视频/查用户、直播间弹幕收发与点赞均只经 mock + 匿名接口验证，未用真实账号全面实测；写操作可能触发B站风控。ASR 路径取决于 SiliconFlow key 与视频是否有公开字幕。
- 评论回复已记录 token 用量；@ 通知已支持（get_at_feed，与「回复我的」按 oid+parent_id 去重）。
- B站接口为 Web 协议，存在风控/合规风险；默认关闭自动回复是安全边界。

## 配置

见 docs/05-配置参考.md 的 [bilibili] 与 [asr] 段。
