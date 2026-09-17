# 数据结构与 API 契约

本文件是 [技术设计](./technical-design.md) 的配套实现契约。路径统一为 `/api/v1`；以下字段为最低要求，可添加内部字段，不得改变已确认语义。

## 1. 通用约定

- ID 使用 UUID 文本，aria2 GID 单独存储。时间为 UTC ISO 8601，数据库统一格式；大小为 byte，速率为 byte/s，持续时间为秒。
- `null` 表示未知，不用 0 代替未知大小、速度或剩余时间。百分比范围 0–100；未知为 null。
- JSON 请求用 Pydantic 严格校验，拒绝额外危险参数。分页默认 `limit=30`，最大 100，使用 opaque cursor；目录排序稳定追加 ID/路径打破并列。
- 成功返回资源对象或 `{items, next_cursor}`；删除成功 204，后台作业 202；参数错误 422，未登录 401，无权限/CSRF 403，不存在 404，状态/版本冲突 409，过载 429。
- HTTP 200 的任务资源可以是 failed，业务执行失败与请求失败区分。
- 创建任务/批量提交支持 `Idempotency-Key`，24 小时内同用户同 key 同 payload 返回首次结果，不再建任务；不同 payload 返回 409。
- 修改排序/设置携带 `revision`；过期版本返回 409 并提示刷新。所有并发启动和动作在后端串行仲裁。
- 错误格式如下，`details` 必须脱敏，不能返回 stderr 全文或 Cookie 内容。

```json
{
  "error": {
    "code": "DEPENDENCY_MISSING",
    "message": "A required tool is unavailable",
    "details": {"dependency": "ffmpeg"},
    "request_id": "9c62461f-7796-47af-baf3-50593aab79e7"
  }
}
```

错误码至少包含：AUTH_REQUIRED、INVALID_CREDENTIALS、CSRF_FAILED、VALIDATION_ERROR、STATE_CONFLICT、REVISION_CONFLICT、DUPLICATE_SOURCE、PATH_INVALID、FILE_MISSING、DEPENDENCY_MISSING、COOKIE_IN_USE、COOKIE_UNAVAILABLE、SOURCE_AUTH_REQUIRED、SOURCE_UNAVAILABLE、FORMAT_UNAVAILABLE、NETWORK_ERROR、DISK_LOW、MEMORY_LOW、PROCESS_FAILED、INSTALL_NOT_CONFIGURED、MAINTENANCE_ACTIVE。

## 2. 数据库

使用迁移维护 schema，不允许每次启动删除重建数据库。枚举存稳定字符串，应用校验并配合 CHECK 约束；JSON 用 TEXT 保存经过校验的对象。表之间只保存逻辑引用 ID，不创建数据库外键约束；引用有效性、删除时的清理或阻止由应用层负责。唯一约束和必要索引仍通过迁移定义。

### 2.1 账号与设置

| 表 | 字段与约束 |
| --- | --- |
| users | id、username UNIQUE、password_hash、created_at、updated_at；仅允许一个管理员 |
| sessions | id、user_id FK、token_hash UNIQUE、csrf_token_hash、created_at、expires_at；数据库不存原始 session token |
| settings | key PK、value_json、revision、updated_at；仅存非敏感可编辑设置 |
| runtime_control | id=1、dispatch_suspended、blocked_reason、revision、updated_at；重启保留磁盘/维护阻断 |
| idempotency_keys | user_id、key、request_hash、response_json、created_at、expires_at；联合唯一 |

会话鉴权读取可使用短时内存缓存，但注销和改密必须立即失效。密码只存哈希。安装路径、Cookie 根目录、公开 URL、密钥等启动配置不允许通过通用 settings 任意修改。

默认可编辑设置：

```json
{
  "max_active_tasks": 1,
  "download_limit_bps": 0,
  "upload_limit_bps": 0,
  "disk_min_free_bytes": 1073741824,
  "default_video_height": 1080,
  "default_subtitle_languages": ["zh", "en"],
  "allow_auto_subtitles": true
}
```

自动重试次数/间隔、重型槽、解析时限、内存门槛、缓存容量首版作为受控后端配置，不把每个技术参数都暴露到界面。

### 2.2 Cookie

| 表 | 字段与约束 |
| --- | --- |
| cookie_profiles | id、name、site_key、domain_scope_json、secret_file_key、content_version、entry_count、last_result_code、last_used_at、created_at、updated_at |
| site_cookie_defaults | site_key PK、cookie_profile_id FK；一个站点最多一个默认配置 |

Cookie 明文只在私有文件，`secret_file_key` 是服务端生成的文件键，不是用户可指定路径。替换通过同目录临时文件 + 原子 rename 完成，失败保留旧版；数据库版本与文件不一致时可恢复。数据库和私有文件操作均要处理崩溃中断，不在响应中返回存储路径。

### 2.3 解析、合集和调度

| 表 | 字段与约束 |
| --- | --- |
| analysis_jobs | id、status、urls_json、cookie_profile_id nullable、cookie_version、site_key、error_code、summary_json、created_at、expires_at、updated_at |
| analysis_items | id、analysis_job_id FK、ordinal、source_url、source_media_id、title、duration_seconds nullable、thumbnail_key nullable、formats_json nullable、subtitles_json、is_collection_entry；UNIQUE(job_id, ordinal) |
| torrent_uploads | id、blob_key、infohash、summary_json、created_at、expires_at；未引用上传 24h 后清理，已建任务转入 task_sources 管理 |
| task_groups | id、title、source_url nullable、site_key nullable、created_at；用于批次/合集分组 |
| work_queue | id、work_type、resource_id、position INTEGER UNIQUE、available_at、created_at；UNIQUE(work_type, resource_id) |

work_type 至少含 download、analysis、metadata、subtitle_retry、media_probe。一个任务最多有一个等待工作项；调度器按 position 查找可运行项，跳过未来 available_at 或依赖不满足项。领取工作项、写 attempt 和更新任务状态同一事务完成。运行中的项从等待队列移除；重试追加尾部并设置时间。

analysis 状态为 queued/running/completed/partial/failed/cancelled/expired；每个输入项独立记录成功或失败。tool_job 状态为 queued/waiting/running/completed/failed/cancelled/interrupted。非下载作业也必须保存 started_at、updated_at 和进程身份，重启可恢复/中止而不留下永远 running 的行。排序 position 更新使用同一事务内临时不冲突的区间后重编号，避免 SQLite UNIQUE 的逐行检查导致交换位置失败。

元数据完成后用户确认选择，再插入 download 项。预解析是独立短期作业，不显示在下载历史，但显示在全局队列。解析结果保留 24 小时；清理前已创建任务必须已有自己的来源/策略快照。合集只存必要摘要，不存无限嵌套的 yt-dlp 原始 JSON。

### 2.4 下载任务与引擎

| 表 | 字段与约束 |
| --- | --- |
| tasks | id、kind（general/video）、source_type（http/magnet/torrent/video）、source_url nullable、source_fingerprint、source_media_id nullable、title、site_key nullable、group_id nullable、status、phase、pending_action nullable、blocked_reason nullable、download_subdir、task_directory_key、cookie_profile_id nullable、cookie_name_snapshot nullable、selection_json、options_json、progress_json、retry_cycle、attempt_count、next_retry_at nullable、error_code nullable、error_summary nullable、warnings_json、revision、created_at、updated_at、started_at nullable、finished_at nullable |
| task_attempts | id、task_id FK、cycle、ordinal、engine、status、engine_ref nullable、process_identity_json nullable、tool_version、cookie_version nullable、started_at、finished_at nullable、exit_code nullable、error_code nullable、log_key |
| aria2_bindings | id、task_id FK、attempt_id FK、gid UNIQUE、role（metadata/content）、parent_gid nullable、last_engine_state、created_at |
| task_sources | task_id PK/FK、torrent_blob_key nullable、magnet_infohash nullable、source_snapshot_json；原始种子私有保存用于恢复 |

source_fingerprint 不 UNIQUE，因为允许明确重复下载。HTTP 去掉 URL fragment、规范 host/scheme，但保留查询参数和大小写敏感路径；磁力用 infohash；视频优先 site + media ID + 输出模式，预解析前用规范页面 URL。不要删除签名 URL 参数进行实际请求。重复检测只提示，不能擅自合并已有任务。

options_json 最低包含视频画质策略/音频模式/字幕规则，以及创建时采用的参数快照。selected file IDs 使用 aria2 的文件 index 而不是前端数组位置，校验当前元数据版本，防止选错文件。进度字段不与 options 混存。

主要索引：tasks(status, created_at)、tasks(kind, status, updated_at)、tasks(finished_at DESC, id)、tasks(source_fingerprint)、task_attempts(task_id, started_at)、work_queue(position)、sessions(expires_at)。

### 2.5 文件、操作与工具作业

| 表 | 字段与约束 |
| --- | --- |
| files | id、task_id nullable FK ON DELETE SET NULL、relative_path UNIQUE、display_name、size_bytes、mtime_ns、kind（video/audio/subtitle/other）、mime_type、media_info_json nullable、availability（present/missing/deleting）、is_complete、created_at、updated_at |
| subtitle_tracks | id、media_file_id FK、original_file_id nullable FK、vtt_file_id nullable FK、language、label、is_auto、status、error_code nullable |
| file_operations | id、kind、status、target_snapshot_json、results_json、created_at、finished_at nullable；删除等有副作用操作的恢复记录 |
| tool_jobs | id、tool、action、status、previous_version nullable、target_version nullable、error_code nullable、log_key、created_at、started_at nullable、finished_at nullable |

files 只索引完整产物；目录浏览另外读取受控磁盘目录，不要求所有文件必须有任务记录。外部放入的普通文件可在浏览时懒注册 ID；播放前获取元数据走排队探测，不能访问即启动无限 ffprobe。文件修改时间/大小变化时使旧媒体元数据失效。

删除任务不能级联删除文件索引和实际文件。删除媒体时关联字幕的范围在确认预览中给出；不误删同目录其他文件。运行中路径禁止删除；文件不存在视为已删除但记录结果。

## 3. REST 接口清单

### 3.1 账号与系统

| 方法与路径 | 请求/行为 | 返回 |
| --- | --- | --- |
| POST /auth/login | username、password；检查 Origin、限速 | user、csrf_token；Set-Cookie |
| GET /auth/session | 当前会话 | user、csrf_token、expires_at |
| POST /auth/logout | 撤销会话，CSRF | 204、清 cookie |
| PATCH /auth/password | current_password、new_password | 204，全部会话失效，需重新登录 |
| GET /system/summary | 资源和引擎汇总 | 速度、磁盘、内存、运行数、队列数、阻断原因、server_time |
| GET /settings | 读设置 | editable、read_only、revision |
| PATCH /settings | 可编辑字段、revision | 新设置、即时/延后生效说明 |
| POST /queue/resume-dispatch | 显式解除保护阻断，再检测资源 | runtime_control；仍不自动继续已暂停/停止任务 |

登录之外不提供设置账号 API。健康检查 `/healthz` 只暴露存活，不暴露路径/版本；依赖不可用不应让登录和设置页不可访问。启动未初始化账号时提供只读提示，账号仍由服务器 CLI 创建。

### 3.2 新建、解析与队列

| 方法与路径 | 请求/行为 | 返回 |
| --- | --- | --- |
| POST /sources/check-duplicates | sources、mode | 每项匹配任务概要 |
| POST /tasks/general | sources[]、download_subdir、allow_duplicates | 201，创建的任务列表；一批校验失败则不创建 |
| POST /torrents | multipart file，最大 10 MiB | torrent_id、解析摘要、可分页文件列表引用，24h 过期 |
| GET /torrents/{id}/files | cursor、limit、搜索 | 文件 index/path/size |
| POST /tasks/torrent | torrent_id、selected_indices、download_subdir、allow_duplicates | 201，任务 |
| POST /analyses | urls[]、cookie_profile_id nullable | 202，analysis_job |
| GET /analyses/{id} | 查询状态与概要 | job；条目另外分页 |
| GET /analyses/{id}/items | cursor、limit | 条目摘要 |
| GET /analyses/{id}/items/{itemId} | 单项已解析详情；未解析则表明待获取 | formats、subtitles、metadata |
| POST /analyses/{id}/cancel | 取消作业、终止其进程 | 202 或当前终态 |
| POST /tasks/video | analysis_id、item_ids、输出策略、cookie_profile_id nullable、download_subdir、allow_duplicates | 201，group 和任务列表 |
| GET /queue | 所有类型等待项分页 | items、revision、阻断原因 |
| POST /queue/{workId}/move | direction（up/down/top）、revision | 新 revision |

合集选中项的精确格式按执行时提取，统一选择画质上限。需要查看合集单项清晰度时，可针对该项 source_url 新建单视频 analysis，不在 GET 接口中隐式启动重型进程。

解析时和创建任务时若 Cookie ID 不同或版本已变，显示需要重新解析/重新验证的提示；任务的实际执行以创建时所选配置 ID 的最新内容为准。分析摘要不等于授权缓存。

所有新建任务提交后不等待真实下载开始。大批次入库须在一个短事务中完成；因验证失败整批未创建时返回逐项错误路径。解析本身允许某个链接失败而其他链接成功，前端显示各项状态。

视频提交示例：

```json
{
  "analysis_id": "bc5cbcb6-34c7-4769-8937-8c8b50742052",
  "item_ids": ["041b437b-b4c8-4235-812c-f43457495651"],
  "cookie_profile_id": null,
  "mode": "video",
  "format_policy": {"type": "height_cap", "max_height": 1080, "prefer_compatible": true},
  "subtitles": {"enabled": true, "languages": ["zh", "en"], "auto_fallback": true},
  "download_subdir": "videos",
  "allow_duplicates": false
}
```

### 3.3 任务操作

| 方法与路径 | 请求/行为 | 返回 |
| --- | --- | --- |
| GET /tasks | kind、status、query、group_id、cursor、limit | 未完成任务（可含失败） |
| GET /history | kind、result、site、时间范围、cursor | 终态任务 |
| GET /tasks/{id} | 查询 | 完整任务、允许操作、文件概要 |
| GET /tasks/{id}/logs | cursor、limit<=200 | 有界脱敏日志 |
| GET /tasks/{id}/files | 文件/BT 候选列表，分页 | 文件状态与选中情况 |
| PUT /tasks/{id}/selection | selected_indices、metadata_revision；仅 awaiting_selection | task，进入 queued |
| POST /tasks/{id}/pause | 普通任务 | 202，状态或 pending_action |
| POST /tasks/{id}/resume | 普通 paused -> queued | 202，task |
| POST /tasks/{id}/stop | 视频 -> stopped | 202，状态或 pending_action |
| POST /tasks/{id}/cancel | 取消但保留文件 | 202，task |
| POST /tasks/{id}/retry | 可选 cookie_profile_id；停止/失败/取消任务 | 202，task；新重试周期 |
| PATCH /tasks/{id}/cookie | cookie_profile_id nullable、revision；仅未运行视频任务 | task，更新后续执行配置；运行中返回 409 |
| POST /tasks/{id}/redownload | 成功任务，使用原配置新建 | 201，新 task |
| POST /tasks/{id}/retry-subtitles | 仅重新取字幕 | 202，work_item |
| POST /tasks/batch-actions | ids<=100、action | 每项结果，部分失败不回滚已完成动作 |
| POST /tasks/{id}/deletion-preview | 删除范围预览 | preview_id、files、total_bytes、expires_at |
| DELETE /tasks/{id} | 默认只删终态记录；删除文件需 preview_id + delete_files=true | 仅记录 204；带文件 202 operation |

删除前预览有效期 5 分钟，执行重新校验文件版本/占用，变更返回 409。运行/等待任务先取消并等引擎退出，再允许删除；不能把 DELETE 偷换成“终止并立即递归删除”。

任务响应示例（字段节选）：

```json
{
  "id": "2d8f90ad-0d21-4a7b-9196-f9d7b396695a",
  "kind": "video",
  "title": "Example video",
  "status": "downloading",
  "phase": "video_stream",
  "pending_action": null,
  "blocked_reason": null,
  "progress": {
    "scope": "current_stream",
    "percent": 42.5,
    "downloaded_bytes": 44564480,
    "total_bytes": 104857600,
    "total_is_estimate": false,
    "speed_bps": 524288,
    "eta_seconds": 115
  },
  "warnings": [],
  "allowed_actions": ["stop", "cancel"],
  "revision": 8
}
```

前端依据 allowed_actions 展示按钮，后端仍必须独立验证状态。当前流进度旁注明“视频流/音频流”，后处理显示独立阶段。

### 3.4 文件与媒体

| 方法与路径 | 请求/行为 | 返回 |
| --- | --- | --- |
| GET /files | 受控相对 directory、query、cursor | 目录项、文件 ID、媒体概要 |
| POST /directories | 受控 parent、单级 name | 新目录，禁止符号链接/穿越 |
| GET /files/{id} | 文件详情 | 路径显示值、媒体信息、tracks、可播放性提示 |
| POST /files/{id}/probe | 仅缺少有效媒体信息时排队，幂等 | 202 work_item 或缓存结果 |
| GET/HEAD /files/{id}/content | 鉴权、Range，disposition=inline/attachment | 文件流、206/416 等 |
| GET /files/{id}/subtitles/{trackId} | 校验字幕属于该文件 | text/vtt |
| GET /thumbnails/{key} | 鉴权、本地有界缓存 | 图片 |
| POST /files/deletion-preview | file_ids 或受控目录键 | 待删除清单、preview_id |
| POST /files/delete | preview_id、confirmed=true | 202 operation |
| GET /operations/{id} | 文件操作进度 | status、逐项结果 |

不提供通用任意 URL 代理。不将下载根目录挂成公开 StaticFiles。播放器请求与文件下载复用鉴权端点；下载 disposition 不能通过 CRLF 文件名注入响应头。字幕同源鉴权，避免依赖前端无法给 `<track>` 设置的自定义 Authorization 头。

### 3.5 Cookie 和依赖

| 方法与路径 | 请求/行为 | 返回 |
| --- | --- | --- |
| GET /cookies | 配置列表 | 元数据，无秘密 |
| POST /cookies | multipart name、site_key、domains、file 或 text（二选一） | 201 元数据 |
| PATCH /cookies/{id} | name/domain_scope 等；改范围检查引用 | 元数据 |
| PUT /cookies/{id}/content | 替换文件或文本 | 更新后的版本/条目数量 |
| PUT /cookies/defaults/{site} | cookie_profile_id nullable | 默认映射 |
| DELETE /cookies/{id} | 检查等待/运行引用 | 204 或 409 COOKIE_IN_USE |
| GET /dependencies | 缓存检测结果 | aria2、yt-dlp、ffmpeg、ffprobe、Deno、EJS 能力 |
| POST /dependencies/check | 有界本机检查，无默认站点联网测试 | 202 tool_job |
| POST /dependencies/{tool}/install | tool 白名单；串行维护 | 202 tool_job |
| POST /dependencies/yt-dlp/update | 安装受支持新版本到暂存环境 | 202 tool_job |
| POST /dependencies/yt-dlp/rollback | 上一已验证版本，维护期 | 202 tool_job |
| GET /tool-jobs/{id} | 状态和脱敏日志分页 | job |
| POST /tool-jobs/{id}/cancel | 仅 queued/waiting；运行中安装不强杀包管理器 | job 或 409 |

安装入口的逻辑工具枚举是 aria2、ffmpeg、video-support（yt-dlp/EJS/Deno 一组，可内部拆步）。ffprobe 不单独装；check 的结果逐项展示。安装/更新流程先设置维护阻断，等待已有依赖使用者结束；等待时可取消作业并恢复原有调度控制。失败后恢复维护前的阻断状态，不能顺便解除 disk_low。

## 4. SSE 协议

接口：`GET /events`，`Content-Type: text/event-stream`，`Cache-Control: no-cache`；使用同源会话，不把 token 放 URL。每连接队列上限 100，慢消费者断开；最多保留 1,000 条小事件用于短时重放，不存全部日志和文件列表。

连接过程：服务端注册订阅后发送 snapshot（系统汇总、队列 revision、当前活跃任务，数量有界），随后增量事件。前端同步刷新当前页 REST 列表，按照 resource revision 忽略旧数据；历史分页资源无需塞进 snapshot。

事件 ID 为 `<boot_uuid>:<monotonic_sequence>`。同一 boot 且 ID 仍在缓冲区可重放；重启、丢失或缓冲区过期发送 `resync_required`，前端重新取快照和当前列表。断线不影响后台任务。

| event | data 内容 |
| --- | --- |
| snapshot | 系统概要、有限活跃 tasks、queue_revision |
| task.updated | task_id、revision、status、phase、progress、allowed_actions、warnings |
| task.removed | task_id |
| queue.updated | revision；前端按需重新请求队列 |
| analysis.updated | analysis_id、status、已解析/失败计数 |
| system.updated | 速率、磁盘、内存、阻断原因 |
| dependency.updated | tool、status、version、capabilities |
| tool_job.updated | job_id、status、当前步骤 |
| operation.updated | operation_id、status、完成/失败数量 |
| resync_required | reason |

```text
id: 025fb54a:193
event: task.updated
data: {"task_id":"2d8f90ad-0d21-4a7b-9196-f9d7b396695a","revision":9,"status":"postprocessing","phase":"merge","progress":{"percent":null}}

: heartbeat

```

心跳间隔 15 秒，普通进度最多每任务每秒一次，终态/错误立即推送。全量日志由 REST 分页获取。SSE 掉线采用带抖动的指数退避（最多 30 秒）；鉴权到期返回登录页并关闭连接，不无限重连。后端在会话撤销/过期时主动断开已建立 SSE。

## 5. 状态一致性测试重点

1. 两个同时到来的创建/继续请求在并发 1 时只能产生一个运行任务。
2. aria2 启动成功、数据库后续写入失败时，恢复能找到相同 GID，不重复添加。
3. 视频进程退出与 stop 同时发生，不能同时出现 completed 和 stopped 两套终态。
4. 删除记录后文件可浏览；删除文件失败时不能从 UI 假装消失。
5. Cookie 替换不影响运行快照；等待任务执行拿到新版本；删除引用配置被拒绝。
6. SSE 重连、服务重启、事件乱序后 UI 与 REST 快照一致。
7. 限速、并发和排序修改冲突时返回 409，不悄悄覆盖别人/另一个浏览器标签的修改。
8. 自动重试延迟释放名额，用户停止/取消能撤销后续重试工作项。
