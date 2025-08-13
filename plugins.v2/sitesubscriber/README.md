## 站点资源订阅：脚本逻辑

### 简介
站点资源订阅插件用于定时或手动刷新各站点的最新资源，识别媒体信息后，按用户配置的属性过滤与规则组过滤进行筛选，并根据动作选择：自动订阅、直接下载或加入待办列表等待确认。支持可选的独立通知（Telegram）。

### 1. 初始化与配置
- 读取配置项：
  - enabled、cron、address（站点列表）、include/exclude、quality/resolution/effect、filter_groups、downloader
  - notify、independent_notify、independent_notify_config（仅 Telegram）、onlyonce、clear、save_path、size_range（GB）
- 加载历史 `_history`。
- 处理一次性运行与清理：
  - onlyonce：保存配置后立即单次执行 `check()`；随后复位为 False。
  - clear：记录 `_clearflag`，执行后清空历史并复位。
- 调度：
  - 配置了 `cron` 则使用 `CronTrigger`；否则启用 30 分钟的间隔任务。

### 2. 任务入口：check()
- 若 `_clearflag` 为真，清空历史 `_history` 并复位。
- 构建属性过滤参数（忽略空值和“全部”）：`include/exclude/quality/resolution/effect`。
- 遍历配置的各个站点 `address`：调用 `search_by_title(title="", sites=[site_id])` 拉取候选上下文列表。
- 对每个上下文调用 `_process_torrent()` 进行处理。

### 3. 处理种子：_process_torrent(context, site_id, filter_params, torrent_helper)
依次执行以下步骤：
1) 属性过滤：
   - 使用 `TorrentHelper.filter_torrent(torrent_info, filter_params)` 进行初筛（标题、质量、分辨率、特效等）。

2) 元信息识别：
   - 从标题识别季号（支持 `S01`/`第1季`/`Season 1` 等），写入 `MetaInfo.begin_season`。
   - 调用 `SearchChain.recognize_media(meta)` 获取 `MediaInfo`；未识别到则跳过。

3) 规则组过滤（可选）：
   - 若设置了 `filter_groups`，调用 `searchchain.filter_torrents(rule_groups, [torrent], mediainfo)` 进一步筛选。

4) 去重与条件限制：
   - 种子大小：`size_range` 用 GB 表示，转换为字节后与 `torrent_info.size` 对比，超限则跳过。
   - 媒体存在性：
     - 电影：`media_exists_check` 返回存在即跳过。
     - 电视剧：只有当 `meta.episode_list` 非空时，才按“子集判断”该季是否已齐（避免空集误判存在）。
   - 订阅去重：若 `subscribechain.exists(mediainfo, meta)` 为真，跳过。

5) 动作分支：
   - auto_subscribe：调用 `add_subscribe()` 自动创建订阅。
   - download：调用 `download_torrent()` 直接下载。
   - manual_subscribe：写入待办历史，等待前端确认。
     - 历史唯一键：`tmdb_id + Sxx`（剧集带季号，默认 `S00`），用以精准定位待办项。
     - 保存可序列化字段：
       - `meta`: 仅包含 `name/year/type/season`
       - `mediainfo` 与 `torrent_info` 使用各自的 `to_dict()`
       - 其它：`title/poster/type/time/status/action/site_id/key`
     - 通知：若启用通知，优先尝试独立通知（Telegram），否则走系统通知。

### 4. 前端页面：get_page()
- 读取 `_history` 中 `status=pending` 的待办项，生成卡片列表。
- 每个卡片包含：海报、标题、年份/季、类型、时间等；并提供“订阅/下载”与“忽略”按钮。
- 按钮事件携带唯一 `key`，调用 `confirm_item` 或 `ignore_item`。

### 5. API
- confirm_item(key, apikey)
  - 校验 apikey。
  - 用 `key` 定位待办项（只处理 `pending`）。
  - 依据 `action` 执行：`download` 或 `manual_subscribe`（若未在订阅中则添加）。
  - 状态改为 `confirmed` 并保存历史。

- ignore_item(key, apikey)
  - 校验 apikey。
  - 用 `key` 定位待办项（只处理 `pending`）。
  - 状态改为 `ignored` 并保存历史。

### 6. 其它关键点
- 独立通知（Telegram）：
  - 配置项包含 `token/chat_id/proxy`；可选代理从全局 `settings.PROXY` 读取。
  - 优先发送图片（backdrop 或海报），否则发送文本。

- 历史唯一键与日志标题：
  - `_get_history_key(mediainfo, meta)`：电影使用 `tmdb_id`，剧集使用 `tmdb_id_Sxx`。
  - `_get_log_title(mediainfo_dict, meta_dict)`：用于统一日志展示，如 `Title (Year) Sxx`。

- 季号解析：
  - `_get_season_from_title(title)`：支持 `S01`（可在开头）、`第1季`、`Season 1` 三类常见格式。

- 配置校验：
  - `__validate_and_fix_config(config)`：校验 `size_range`（支持单值或区间），非法时重置并通知。

- 调度管理：
  - `stop_service()`：移除任务、关闭调度器，释放资源。

### 7. 版本说明
- 当前版本：1.0（未发布版本号按 1.0 保持）。



