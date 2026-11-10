/**
 * Video analysis settings strip (P4 panel, per the P4 design draft v2) -- a hot-loaded desktop plugin; never touches the core asar.
 *
 * Location = contribution point composer.top: rendered inside the composer
 * apps/desktop/src/app/chat/composer/contrib.ts: top = "banner strip above the
 * input"). Collapsed to one line by default (shows the effective-parameter summary); click to expand the four parameter columns.
 *
 * Data flows only through the backend HTTP bridge ctx.rest -> /api/plugins/
 * video-xray (= plugin dir dashboard/api.py); parameter names map 1:1 onto the
 * engine SCHEMA_ANALYZE (verified 11-06: no "deep" preset; subtitle injection is called subtitles; segmented progress is ALWAYS saved).
 *
 * Pure ESM, no compilation: UI may only use jsx()/jsxs() (no JSX compiler); the
 * import surface is limited to @hermes/plugin-sdk / react / react/jsx-runtime.
 */
import {
  Badge,
  Button,
  COMPOSER_AREAS,
  Checkbox,
  EmptyState,
  Input,
  ScrollArea,
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
  Separator,
  Textarea,
  cn,
  host,
  queryClient,
  useQuery
} from '@hermes/plugin-sdk'
import { useEffect, useRef, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const ID = 'video-xray'
// capture ctx at register(); components use it directly (render injects no params)
let api = null

// presets: value = real engine key (the enum in __init__.py SCHEMA_ANALYZE); label = i18n key
const PRESETS = [
  ['default', 'preset.default'],
  ['quick8', 'preset.quick8'],
  ['single64', 'preset.single64'],
  ['single32', 'preset.single32'],
  ['segment24fps', 'preset.segment24fps'],
  ['segment30fps', 'preset.segment30fps']
]
const PRESET_LABEL = Object.fromEntries(PRESETS)
// Observation granularity = engine max_frames: single mode = total frames of
// the one request; segment mode = frames per segment (total = per-seg x 4 requests).
const FRAMES = [
  ['16', 'frames.f16'],
  ['8', 'frames.f8'],
  ['32', 'frames.f32'],
  ['64', 'frames.f64']
]
const FRAME_LABEL = Object.fromEntries(FRAMES)
// ASR engine dropdown: auto = backend picks the best engine this machine has; off = video only
const ASRS = [
  ['auto', 'asr.auto'],
  ['r2t2', 'asr.r2t2'],
  ['whisper', 'asr.whisper'],
  ['cloud', 'asr.cloud'],
  ['off', 'asr.off']
]
const ASR_LABEL = Object.fromEntries(ASRS)
// H3 prompt export: segmented = format B (bucketed by real ffmpeg cut points) / single = format A (one continuous shot)
const H3MODES = [
  ['shots', 'h3.shots'],
  ['single', 'h3.single']
]
// shot-cut threshold (ffmpeg scene score): higher = prefer fewer, coarser cuts
const THRESHOLDS = [
  ['0.2', 'thresh.t02'],
  ['0.3', 'thresh.t03'],
  ['0.4', 'thresh.t04']
]
// status labels resolve at render time via t('panel.status.<status>'); the whitelist keeps unknown backend statuses from leaking raw keys
const STATUS_KEYS = new Set(['pending', 'running', 'done', 'cancelled', 'error', 'interrupted'])
const statusLabel = s => (STATUS_KEYS.has(s) ? t('panel.status.' + s) : String(s))
// Badge's real variant enum (app CVA): there is no 'secondary'; mapped per status
const BADGE_VARIANT = {
  done: 'success',
  error: 'destructive',
  cancelled: 'warn',
  interrupted: 'warn'
}
const LIVE = new Set(['pending', 'running'])
const RESUMABLE = new Set(['cancelled', 'error', 'interrupted'])
const VIDEO_EXT = ['mp4', 'mkv', 'mov', 'avi', 'webm', 'm4v']
const DEF_FORM = {
  path: '',
  preset: 'default',
  frames: '16',
  start: '0',
  duration: '',
  transcribe: 'auto',
  subtitles: '',
  auto_send: true,
  h3mode: 'shots',
  thresh: '0.3'
}

// ── i18n: official plugin i18n (ctx.i18n); follows the app active language; missing en falls back automatically ──
// t('panel.xxx') is evaluated at render time, never frozen at module load, so language switches apply instantly.
const B = {
  zh: { panel: {
    preset: { default: '分段观察＋汇总（默认）', quick8: '快速概览（8帧单请求）', single64: '单次全帧观察', single32: '单次观察·32帧', segment24fps: '全程分段·24FPS', segment30fps: '全程分段·30FPS' },
    frames: { f16: '16帧/段（默认）', f8: '8帧/段·省额度', f32: '32帧/段', f64: '64帧/段·看得最细' },
    h3: { shots: 'H3·分段（按真实镜头）', single: 'H3·单镜（一镜连写）', short_shots: '分段', short_single: '单镜', engine_llm: '模型整理', engine_rule: '逐字拼接' },
    thresh: { t02: '镜头阈值·敏感 0.2', t03: '镜头阈值·稳妥 0.3', t04: '镜头阈值·保守 0.4' },
    status: { pending: '排队中', running: '分析中', done: '已完成', cancelled: '已取消', error: '出错', interrupted: '已中断' },
    notify: {
      panel_not_mounted: '视频面板没装载，附件没自动分析（Ctrl+Shift+R 重载窗口）',
      already_analyzing: '已经在分析这条了：{{0}}',
      auto_started: '附件视频已按面板这套参数开始分析：{{0}}',
      auto_failed: '附件自动分析没起来：{{0}}'
    },
    asr: {
      auto: '自动（择优）',
      engine_cpu: '转录 CPU 上限',
      engine_cpu_25: '25%（最稳）', engine_cpu_50: '50%', engine_cpu_75: '75%', engine_cpu_100: '100%（吃满）',
      engine_cpu_tip: '限制转录进程最多吃多少 CPU，防止整机卡顿；只有设备选 CPU 时才生效，云端和关闭不占本地 CPU',
      r2t2: 'R2T2（本地 · 中文）',
      audio_track: '音轨',
      whisper: 'Whisper（本地 · 多语）',
      cloud: '云端 ASR（地址+密钥填在下面）', off: '关闭'
    },
    hint: {
        auto: 'ℹ️ 一个都不勾就不转录，只按画面出报告。要台词就在下面选一个已装好的引擎',
        r2t2: '✅ R2T2 本地引擎：简体中文带标点，断句最准（CPU 运行，默认限 50% CPU 防卡顿）',
        whisper: '✅ Whisper 本地引擎（faster-whisper）：CPU int8，默认限 50% CPU；多语种含中英；首次会自动从 hf-mirror 下载模型（约 460MB）',
        cloud: 'ℹ️ 云端 ASR（OpenAI 兼容转写端点）：不用本地模型 —— 在下方「云端ASR」行填地址 + API 密钥，点「测试连通」会实测转写并自动读取模型列表，保存后左边的圈才会亮',
        off: 'ℹ️ 不转录：只靠画面观察（贴了字幕也不会有台词）',
        missing: '这台机器没装这个引擎：点下方「本地引擎装填…」选它的目录再按「保存」，验证通过就能勾上；Whisper 首次分析会自动下载模型，不用装填',
        none: '这台机器还没装任何转录引擎：点下方「本地引擎装填…」选目录后按「保存」（后端会真实加载模型验证，通过才登记变绿）；云端 ASR 只需在下方填地址+密钥；不装也能跑，只是没有台词',
        load_ok: '✅ 已装载 ',
        cpu_tip: '限制转录进程最多用多少 CPU，防止整机卡顿；云端 ASR 不受此项影响',
        cpu_na_tip: '此上限只约束本地引擎；当前选的云端 / 关闭不用本地 CPU，所以这里置灰'
    },
    live: {
      analyzing: '正在分析 {{0}}', segs_used: '段{{0}}/{{1}} · 已用{{2}}', transcribing: '（转录中）',
      cancel: '取消', background: '后台',
      backgroundHint: '把分析挂到后台并收起此卡；作业照常继续跑，随时打开任务列表看结果'
    },
    detail: {
      load_failed: '⚠ 读不到这条作业：{{0}}', from_resume: ' · 由续跑而来', segs: ' 段',
      cancel: '取消', cancelling: '取消中…', resume: '续跑', exporting: '导出中', export: '导出…',
      copying: '反推中', copy_h3: '复制 H3', reveal: '打开文件夹', collapse: '收起',
      truncated: '⚠ 总结结尾被切断（预算在 engine/pipeline.py 的 _summary_budget）',
      error_prefix: '错误：{{0}}', no_summary_live: '（还没出总结）', no_summary: '（没有总结）',
      lines_title: '台词（{{0}} 条）', notes_title: '逐段观察记录（{{0}} 段）', gallery_title: '模型看到的画面（{{0}} 帧）', gallery_loading: '放大帧加载中…',
      h3_missing: '没生成', h3_copied: 'H3 提示词已复制：{{0}}｜{{1}} 个镜头｜{{2}}',
      h3_failed: 'H3 反推失败：{{0}}',
      export_title: '导出分析结果（同目录会同时写一个同名的 _H3提示词.txt）',
      write_failed: '写盘失败', exported: '已导出：{{0}} ＋ {{1}}', export_failed: '导出失败：{{0}}',
      export_ok: '已导出 {{0}} ＋ {{1}}', export_ok_btn: '✓ 已导出',
      h3_ok: 'H3 提示词已复制（{{0}} · {{1}} 个镜头）', h3_ok_btn: '✓ 已复制',
      export_stem: '分析'
    },
    list: {
      load_failed: '⚠ 读不到作业（{{0}}）', empty_title: '还没有分析作业',
      empty_desc: '展开设置条选好视频点「立即分析」，或把视频拖进输入框',
      pick_aria: '勾选 {{0}}', has_lines: ' · 有台词', resumed: ' · 续跑', resume: '续跑'
    },
    strip: {
      title: '视频分析工作台', brand: '雅澈引擎 · {{0}} 预设',
      need_path: '先选视频文件（点「分析视频…」）',
      started: '视频分析已发起：{{0}}',
      running: '分析运行中',
      pick_title: '选择要分析的视频', filter_video: '视频',
      refused: '{{0}} 条没删掉：{{1}}', refused_reason: '后端拒绝',
      deleted_n: '已删除 {{0}} 条任务',
      cleared_all: '已删除 {{0}} 条已结束任务', cleared_skip: '（{{0}} 条在跑，已跳过）', cleared_bad: '（{{0}} 条删不掉）',
      done_line: '✅ 已完成：{{0}} · 报告已在对话里',
      whole: '整个视频', dur: '{{0}}秒', t_on: '转录开', t_off: '转录关',
      auto_note: '⚡ 附件已按这套参数起作业：{{0}}',
      select_all: '全选', deleting: '删除中', del_n: '删除({{0}})', del: '删除',
      clearing: '清理中', confirm_all: '确认删除全部?', clear_all: '删除全部任务',
      collapse_tasks: '收起任务', show_tasks: '查看保存的任务',
      label_preset: '处理方式', label_dur: '分析时长（秒，空=整个视频）', ph_whole: '整个视频',
      label_start: '从第几秒开始', label_frames: '观察粒度',
      label_h3: 'H3 提示词形态（导出时用）', label_thresh: '镜头切换阈值（分段边界）',
      keep_progress: '保存分段进度（总是开）', auto_transcribe: '语音转录文字引擎（台词从哪来）',
      load_engine: '选择本地语音转录文字模型目录', loading_engine: '识别目录中…',
      pick_dir: '选择本地转录引擎目录',
      pick_dir_tip: '选 R2T2 运行时根目录、或装了 faster-whisper 的 venv 根目录，然后按「保存」——后端会真实启动引擎、加载模型试转 0.2 秒静音，通过才登记（重启后依然有效）',
      cpu_title: '转录CPU上限',
      cloud_title: '云端ASR',
      ph_pick_dir: '先点左边按钮选引擎目录（或直接粘路径），再按「保存」验证',
      ph_url: '云端地址（如 https://api.openai.com）',
      ph_model: '模型（点「测试连通」后自动读取）',
      ph_key: 'API密钥（公开服务可留空）',
      key_keep: '已存····{{0}}（留空=不改）',
      test_conn: '测试连通',
      testing: '测试中…',
      saving: '保存中…',
      save_btn: '保存',
      verifying: '真机加载验证中…',
      save_local_tip: '后端会把这台引擎真正启动、加载模型并试转 0.2 秒静音：通过才登记（左边圈变绿），失败会把原因写清楚；验证进程用完即退，不留显存/CPU',
      cloud_model_tip: '点「测试连通」会自动拉取云端模型列表填到这里',
      hint_load_ok: '✅ {{0}} 已登记：真机加载实测通过（左边圈已变绿）',
      hint_missing: '还没有任何转录引擎：上面选目录装填 R2T2/whisper，或点「云端语音自定义」接云端——都没有时台词会是空白',
      cpu_saved: '✅ 转录 CPU 上限已保存：{{0}}%',
      cloud_models_ok: '✅ 地址通了，读到 {{0}} 个模型（转写端点未实测）',
      cloud_test_ok: '✅ 连通测试通过：转写端点实测可用（模型 {{0}}）——按右边「保存」才登记生效',
      cloud_saved: '✅ 云端 ASR 已保存并通过真实转写测试：',
      cloud_open: '云端语音转录文字自定义接口设置',
      cloud_done: '已登记：{{0}}',
      cloud_unset: '未登记——点左边按钮填上云端地址即可',
      dlg_title: '云端 ASR 配置',
      dlg_desc: '接一个 OpenAI 兼容的语音转写服务（硅基流动 / 阿里百炼 / OpenRouter 等）：地址、模型、密钥填好，先测后存，全程不用碰 yaml。',
      back: '返回',
      params_btn: '参数',
      params_tip_r2t2: 'R2T2 调参：转写设备、量化精度、识别语言、热词、音轨、声道、字幕偏移——只有本机登记验证绿圈、且单选着 R2T2 时才可点',
      params_tip_whisper: 'Whisper 调参：模型大小、设备、识别语言、初始提示、音轨、声道、字幕偏移——只有本机登记验证绿圈、且单选着 Whisper 时才可点',
      params_locked: '点不了：这个引擎要么还没登记验证（圈没绿），要么当前选的不是它——先在下方「本地引擎」选目录保存登记，再单选它',
      params_need_video: '点不了：请先上传/选择要分析的视频文件',
      need_video_hint: '请先上传/选择要分析的视频（不必发送），其余参数随视频解锁',
      run_locked_engine: '点不动：转写引擎还没登记——在下方「本地引擎」选好目录点「保存」（圈变绿），或把转录选成「关闭」',
      path_ro_tip: '路径只能通过「分析视频…」选入（禁手工输入/粘贴，防打错、防串片）',
      dlg_r2t2: 'R2T2 调参', dlg_whisper: 'Whisper 调参',
      dlg_desc_r2t2: '保存前会先真实加载这组参数做验证：机器上没有 CUDA 却选了 CUDA、Q4 模型文件还没下载，都会被拒绝并原样写出引擎的报错——加载得了才保存生效。',
      dlg_desc_whisper: '保存前同样真实加载验证：换 medium/large 第一次没缓存会联网下载（几十秒到几分钟）；CUDA 不可用会被拒绝。',
      p_device: '转写设备',
    p_device_tip: '语音转字幕（转录）用哪个设备。它只管转录这一小步——通常只占整个分析的几秒；画面观察和最终总结由您的推理引擎自己决定用什么设备跑（就是常驻显存里的那个模型），不受此项控制。转录模型较小，CPU 与 GPU 的速度差异通常很小，选 CPU 还能给引擎省显存。', p_quant: '量化精度', p_size: '模型大小', p_lang: '识别语言',
      p_hot: '热词（提高专有名词命中）', p_hot_ph: '例：容器 微服务 Kubernetes（空格或逗号分隔）',
      p_hot_whisper: '初始提示（近似热词）', p_hot_tip_whisper: 'faster-whisper 没有真正的热词加权：这段文字作为初始提示，引导模型偏向这些词',
      p_track: '音轨', p_track_probe: '正在数这条视频的音轨…', p_track_probe_fail: '音轨探测失败——填 0 走默认音轨',
      p_track_none: '这条视频没有音轨（无声）', p_track_of: '这条视频共 {{0}} 条音轨',
      p_channel: '声道', p_offset: '字幕对齐偏移（秒，正值字幕向后挪）',
      p_ch0: '默认（自动混音）', p_ch1: '仅左声道', p_ch2: '仅右声道',
      p_dev_cpu: 'CPU（不占显存）', p_dev_cuda: 'CUDA（GPU，快）', p_dev_auto: '自动（有显卡用显卡）',
      p_q8: 'Q8_0 · 更准（约 1.9G 显存/内存）', p_q4: 'Q4_K_M · 省四成（需先下载模型文件）',
      p_s_tiny: 'tiny · 最快最糙（~0.5G）', p_s_base: 'base · 快（~0.7G）', p_s_small: 'small · 均衡（~1G，推荐）',
      p_s_medium: 'medium · 较准（~3G）', p_s_large: 'large-v3 · 最准（~10G）',
      p_auto_lang: '自动检测',
      p_testing: '真实加载验证中…（可能要几十秒）', p_saving: '先验证再保存中…',
      p_test_ok: '✅ 验证通过：引擎能用这组参数真实加载（还没保存）',
      p_saved: '✅ 已保存：引擎真实加载验证通过，即刻生效',
      p_audio_note: '（音轨/声道/偏移也一并保存了）',
      auto_send: '附件发送即分析',
      auto_send_tip: '点发送时，输入框里的视频附件自动按这套参数起分析作业（消息照发，不拦）',
      label_subs: '字幕 / 台词线索（粘贴后自动跳过转录）',
      ph_subs: '可粘贴 SRT/VTT/时标行（可选）',
      ph_path: '视频文件绝对路径（或点右侧「分析视频…」）',
      browse: '分析视频…', starting: '发起中…', analyze_now: '立即分析',
      probe_dead: '语言模型未就绪：{{0}} 无响应（{{1}}）。先确认桌面端模型已加载完，再重试', probe_err: '引擎体检请求失败：{{0}}', probe_ok: '引擎就绪：{{0}}（走 {{1}}）', auto_skipped: '雅澈引擎未启动分析：{{0}}（消息照常发送）'
    }
  } },
  en: { panel: {
    preset: { default: 'Segmented observation + summary (default)', quick8: 'Quick overview (8 frames, single request)', single64: 'Single-pass full-frame observation', single32: 'Single pass · 32 frames', segment24fps: 'Full-video segments · 24 FPS', segment30fps: 'Full-video segments · 30 FPS' },
    frames: { f16: '16 frames/segment (default)', f8: '8 frames/segment · low cost', f32: '32 frames/segment', f64: '64 frames/segment · finest detail' },
    h3: { shots: 'H3 · multi-shot (by real cuts)', single: 'H3 · single-shot (one continuous take)', short_shots: 'multi-shot', short_single: 'single-shot', engine_llm: 'LLM-polished', engine_rule: 'verbatim' },
    thresh: { t02: 'Cut threshold · sensitive 0.2', t03: 'Cut threshold · balanced 0.3', t04: 'Cut threshold · conservative 0.4' },
    status: { pending: 'Queued', running: 'Analyzing', done: 'Done', cancelled: 'Cancelled', error: 'Failed', interrupted: 'Interrupted' },
    notify: {
      panel_not_mounted: 'Video panel is not loaded; attachments were not auto-analyzed (press Ctrl+Shift+R to reload)',
      already_analyzing: 'Already analyzing this video: {{0}}',
      auto_started: 'Attached video started analyzing with the panel settings: {{0}}',
      auto_failed: 'Attachment auto-analysis failed to start: {{0}}'
    },
    asr: {
      auto: 'Auto (best available)',
      engine_cpu: 'Transcription CPU cap',
      engine_cpu_25: '25% (safest)', engine_cpu_50: '50%', engine_cpu_75: '75%', engine_cpu_100: '100% (full)',
      engine_cpu_tip: 'Caps how much CPU the transcription process may take so the machine never stalls; only applies when device = CPU (cloud / off use no local CPU)',
      r2t2: 'R2T2 (local · zh)',
      audio_track: 'Audio track',
      whisper: 'Whisper (local · any lang)',
      cloud: 'Cloud ASR (URL + key below)', off: 'Off'
    },
    hint: {
        auto: 'ℹ️ More than one transcription engine is installed — run without picking and R2T2 (best dialogue quality) wins · segment progress auto-saved; interrupted jobs can resume',
        r2t2: '✅ R2T2 local engine: simplified Chinese with punctuation, best segmentation (CPU-bound, capped at 50% CPU by default)',
        whisper: '✅ Whisper local engine (faster-whisper): CPU int8, capped at 50% CPU by default; multilingual; first analysis auto-downloads the model (~460 MB)',
        cloud: 'ℹ️ Cloud ASR (OpenAI-compatible endpoint): no local model — fill the Cloud ASR row below with URL + API key, press Connect test to probe transcription live and auto-read the model list; the circle lights up only after Save',
        off: 'ℹ️ No transcription: visual observation only (even pasted subtitles stay unused)',
        missing: 'This engine is not installed here: click 「Load local engine…」 below, pick its folder and press Save — it becomes tickable once the live load check passes',
        none: 'No transcription engine is installed here yet: click 「Load local engine…」 below, pick the folder and press Save (the backend really loads the model to verify — only a pass registers it); or fill the Cloud ASR row below. You can still run without one — visuals only, no dialogue',
        load_ok: '✅ Loaded ',
        cpu_tip: 'Caps how much CPU the transcription process may take so the machine never stalls; cloud ASR ignores this cap',
        cpu_na_tip: 'This cap only applies to local engines; the selected cloud / off engine uses no local CPU, so this is greyed out'
    },
    live: {
      analyzing: 'Analyzing {{0}}', segs_used: 'seg {{0}}/{{1}} · {{2}} elapsed', transcribing: '(transcribing)',
      cancel: 'Cancel', background: 'Background',
      backgroundHint: 'Keep the analysis running in the background and collapse this card; open the task list anytime to see the result'
    },
    detail: {
      load_failed: '⚠ Cannot load this job: {{0}}', from_resume: ' · resumed run', segs: ' segments',
      cancel: 'Cancel', cancelling: 'Cancelling…', resume: 'Resume', exporting: 'Exporting…', export: 'Export…',
      copying: 'Generating…', copy_h3: 'Copy H3', reveal: 'Open folder', collapse: 'Collapse',
      truncated: '⚠ Summary was cut off at the end (budget: _summary_budget in engine/pipeline.py)',
      error_prefix: 'Error: {{0}}', no_summary_live: '(no summary yet)', no_summary: '(no summary)',
      lines_title: 'Dialogue ({{0}} lines)', notes_title: 'Per-segment observation notes ({{0}} segments)', gallery_title: 'Frames the model saw ({{0}})', gallery_loading: 'loading full frame…',
      h3_missing: 'no result', h3_copied: 'H3 prompt copied: {{0}} · {{1}} shots · {{2}}',
      h3_failed: 'H3 generation failed: {{0}}',
      export_title: 'Export analysis report (a matching _H3_prompt.txt is written in the same folder)',
      write_failed: 'write failed', exported: 'Exported: {{0}} + {{1}}', export_failed: 'Export failed: {{0}}',
      export_ok: 'Exported {{0}} + {{1}}', export_ok_btn: '✓ Exported',
      h3_ok: 'H3 prompt copied ({{0}} · {{1}} shots)', h3_ok_btn: '✓ Copied',
      export_stem: 'analysis'
    },
    list: {
      load_failed: '⚠ Cannot load jobs ({{0}})', empty_title: 'No analysis jobs yet',
      empty_desc: 'Expand the settings strip, pick a video and hit "Analyze now", or drop a video into the input box',
      pick_aria: 'Select {{0}}', has_lines: ' · has dialogue', resumed: ' · resumed', resume: 'Resume'
    },
    strip: {
      title: 'Video analysis workbench', brand: 'Yache engine · {{0}} presets',
      need_path: 'Pick a video file first (use "Analyze a video…")',
      started: 'Analysis started: {{0}}',
      running: 'Analysis running',
      pick_title: 'Choose a video to analyze', filter_video: 'Video',
      refused: '{{0}} job(s) not deleted: {{1}}', refused_reason: 'rejected by backend',
      deleted_n: 'Deleted {{0}} job(s)',
      cleared_all: 'Cleared {{0}} finished job(s)', cleared_skip: ' ({{0}} still running, skipped)', cleared_bad: ' ({{0}} could not be deleted)',
      done_line: '✅ Done: {{0}} · the report is in the chat',
      whole: 'full video', dur: '{{0}}s', t_on: 'transcription on', t_off: 'transcription off',
      auto_note: '⚡ Attachment started with these settings: {{0}}',
      select_all: 'Select all', deleting: 'Deleting…', del_n: 'Delete ({{0}})', del: 'Delete',
      clearing: 'Clearing…', confirm_all: 'Confirm delete all?', clear_all: 'Clear all jobs',
      collapse_tasks: 'Collapse jobs', show_tasks: 'Saved jobs',
      label_preset: 'Processing mode', label_dur: 'Duration (seconds, empty = full video)', ph_whole: 'Full video',
      label_start: 'Start at second', label_frames: 'Observation granularity',
      label_h3: 'H3 prompt style (used on export)', label_thresh: 'Scene-cut threshold (segment bounds)',
      keep_progress: 'Keep segment progress (always on)', auto_transcribe: 'Speech-to-text engine (dialogue source)',
      load_engine: 'Choose local speech-to-text model folder…', loading_engine: 'Checking folder…',
      pick_dir: 'Choose a local transcription engine folder',
      pick_dir_tip: 'Pick an R2T2 runtime root or a venv root that has faster-whisper installed, then press Save — the backend really boots that engine, loads its model and transcribes 0.2 s of silence; only a pass registers it (survives restarts)',
      cpu_title: 'Transcription CPU cap',
      cloud_title: 'Cloud ASR',
      ph_pick_dir: 'Pick an engine folder with the button (or paste a path), then press Save to verify',
      ph_url: 'Cloud URL (e.g. https://api.openai.com)',
      ph_model: 'Model (auto-filled after Connect test)',
      ph_key: 'API key (empty for open endpoints)',
      key_keep: 'saved ····{{0}} (empty = keep)',
      test_conn: 'Connect test',
      testing: 'Testing…',
      saving: 'Saving…',
      save_btn: 'Save',
      verifying: 'Live load check…',
      save_local_tip: 'The backend really starts this engine, loads its model and transcribes 0.2 s of silence: only a pass registers it (circle turns green); failures explain themselves. The probe worker exits right after — no VRAM/CPU left behind.',
      cloud_model_tip: 'Press Connect test to pull the cloud model list into this dropdown',
      hint_load_ok: '✅ {{0}} registered: real engine load verified (the circle on the left is green)',
      hint_missing: 'No transcription engine yet: load R2T2/whisper above, or open Cloud ASR setup for a cloud service — without any, subtitles stay empty',
      cpu_saved: '✅ Transcribe CPU cap saved: {{0}}%',
      cloud_models_ok: '✅ URL reachable, read {{0}} models (transcription endpoint not exercised)',
      cloud_test_ok: '✅ Connection verified: the transcription endpoint really works (model {{0}}) — press Save to register it',
      cloud_saved: '✅ Cloud ASR saved and verified with a real transcription: ',
      cloud_open: 'Cloud speech-to-text custom endpoint setup',
      cloud_done: 'Registered: {{0}}',
      cloud_unset: 'Not registered — press the button to fill in your cloud URL',
      dlg_title: 'Cloud ASR Setup',
      dlg_desc: 'Point at any OpenAI-compatible speech-to-text service (SiliconFlow / DashScope / OpenRouter ...): fill URL, model and key, test first, then save — no yaml required.',
      back: 'Back',
      params_btn: 'Params',
      params_tip_r2t2: 'R2T2 tuning: device, quantization, language, hotwords, audio track, channel, subtitle offset — clickable only while R2T2 is verified green AND selected',
      params_tip_whisper: 'Whisper tuning: model size, device, language, initial prompt, audio track, channel, subtitle offset — clickable only while Whisper is verified green AND selected',
      params_locked: 'Locked: this engine is not verified green yet (or another engine is selected) — register it below first, then select it',
      params_need_video: 'Locked: attach the video file to analyse first',
      need_video_hint: 'Attach a video file first (no need to send) — the other controls unlock once one is attached',
      run_locked_engine: 'Locked: the transcription engine is not registered yet — pick its folder under "Local engines" and press Save (the dot turns green), or switch transcription to Off',
      path_ro_tip: 'The path can only come from "Analyze a video…" (typing/pasting is blocked — no typos, no mismatched files)',
      dlg_r2t2: 'R2T2 settings', dlg_whisper: 'Whisper settings',
      dlg_desc_r2t2: 'Saving first loads the engine for real with these settings: choosing CUDA without CUDA hardware, or Q4 before its model file is downloaded, is rejected with the engine error shown verbatim.',
      dlg_desc_whisper: 'Saving also loads for real: first-time medium/large downloads may take minutes; CUDA is refused when unavailable.',
      p_device: 'Device',
    p_device_tip: 'Which device runs the speech-to-text step. It only governs transcription — a few seconds of a whole analysis; frame observation and the final summary run wherever your inference engine itself runs (the model already resident in VRAM), independent of this setting. The transcription model is small, so CPU vs GPU usually makes little speed difference, and CPU also saves VRAM for the engine.', p_quant: 'Quantization', p_size: 'Model size', p_lang: 'Language',
      p_hot: 'Hotwords (bias proper nouns)', p_hot_ph: 'e.g. container microservice Kubernetes (space or comma separated)',
      p_hot_whisper: 'Initial prompt (hotword approximation)', p_hot_tip_whisper: 'faster-whisper has no true hotword biasing: this text seeds the model toward those words',
      p_track: 'Audio track', p_track_probe: 'Counting audio tracks…', p_track_probe_fail: 'Track probe failed — 0 uses the default track',
      p_track_none: 'This video has no audio track', p_track_of: '{{0}} audio track(s) in this video',
      p_channel: 'Channel', p_offset: 'Subtitle offset (seconds, positive shifts later)',
      p_ch0: 'Default (auto mix)', p_ch1: 'Left channel only', p_ch2: 'Right channel only',
      p_dev_cpu: 'CPU (no VRAM)', p_dev_cuda: 'CUDA (GPU, fast)', p_dev_auto: 'Auto (GPU when present)',
      p_q8: 'Q8_0 · most accurate (~1.9 GB)', p_q4: 'Q4_K_M · ~40% smaller (download the file first)',
      p_s_tiny: 'tiny · fastest (0.5 GB)', p_s_base: 'base · fast (0.7 GB)', p_s_small: 'small · balanced (1 GB, recommended)',
      p_s_medium: 'medium · better (3 GB)', p_s_large: 'large-v3 · best (10 GB)',
      p_auto_lang: 'Auto-detect',
      p_testing: 'Real load test… (may take a while)', p_saving: 'Testing before saving…',
      p_test_ok: '✅ Test passed: the engine loads with these settings (not saved yet)',
      p_saved: '✅ Saved: real engine load verified, effective now',
      p_audio_note: ' (track / channel / offset saved too)',
      auto_send: 'Analyze attached videos on send',
      auto_send_tip: 'When you press send, video attachments in the input box start analysis with these settings (the message still sends)',
      label_subs: 'Subtitles / dialogue hints (paste to skip transcription)',
      ph_subs: 'Paste SRT/VTT/timestamped lines (optional)',
      ph_path: 'Absolute video path (or use "Analyze a video…" on the right)',
      browse: 'Analyze a video…', starting: 'Starting…', analyze_now: 'Analyze now',
      probe_dead: 'LLM not ready: {{0}} is not answering ({{1}}). Load the model in the desktop app first, then retry', probe_err: 'Engine probe request failed: {{0}}', probe_ok: 'Engine ready: {{0}} (via {{1}})', auto_skipped: 'Yache engine did not start: {{0}} (message sent as usual)'
    }
  } }
}

function t(key) {
  const args = Array.prototype.slice.call(arguments, 1).map(a => String(a))
  try {
    if (api && api.i18n && api.i18n.t) {
      const s = api.i18n.t(key, ...args)
      // host returns string leaves verbatim (its interpolation contract is
      // function leaves, not {{N}} placeholders); apply positional args here
      if (typeof s === 'string' && s.includes('{{')) {
        return s.replace(/\{\{(\d+)\}\}/g, (_, i) => String(args[Number(i)] ?? ''))
      }
      return s
    }
  } catch {
    /* older hosts have no ctx.i18n; fall through to the local fallback below */
  }
  let walk = B.zh
  for (const k of String(key).split('.')) {
    walk = walk && typeof walk === 'object' ? walk[k] : undefined
    if (walk == null) break
  }
  const s = typeof walk === 'string' ? walk : key
  return s.replace(/\{\{(\d+)\}\}/g, (_, i) => String(args[Number(i)] ?? ''))
}

// form persistence: ctx.storage = sync { get(key, fallback) / set(key, value) } (verified against source)
function loadForm() {
  try {
    const f = { ...DEF_FORM, ...(api.storage.get('form', {}) || {}) }
    // drafts saved before 0.1 stored a boolean switch: true -> auto, false -> off
    if (typeof f.transcribe === 'boolean') f.transcribe = f.transcribe ? 'auto' : 'off'
    // 视频路径每次装载都置空：路径只在本会话有效——重启后文件可能已被挪走/删掉，
    // 沿用上次的旧路径反而制造 BUG（主人 2026-11-09 定）；其余参数照旧持久化
    f.path = ''
    return f
  } catch {
    return { ...DEF_FORM }
  }
}
function saveForm(form) {
  try {
    api.storage.set('form', form)
  } catch {
    /* failing to persist is harmless */
  }
}

// params -> request body. The panel and the attachment middleware share this one function, so the two can never drift apart.
function buildBody(f) {
  const out = {
    path: String(f.path || '').trim(),
    preset: f.preset,
    transcribe: asrResolved(ASR_LABEL[f.transcribe] ? f.transcribe : 'auto', ASR_AVAIL),
    max_frames: Number(f.frames) || 16
  }
  if (String(f.start ?? '').trim()) out.start = Number(f.start) || 0
  if (String(f.duration ?? '').trim()) out.duration = Number(f.duration) || 0
  const sub = String(f.subtitles || '').trim()
  if (sub) out.subtitles = sub
  return out
}

// ── analyze-on-send ──────────────────────────────────────────────────────
// The SDK exposes pending composer attachments as no subscribable store; the plugin
// only sees draft.attachments at the moment of sending (see composer/hooks/
// use-middleware-submit.ts, which stuffs attachments into the draft). So mirror
// it in reverse: every green-bar change hangs the latest copy here, so the middleware always reads exactly what was just dialed in.
const panel = { form: null }
// The same send may run twice (resend, chain re-run): dedupe by attachment fingerprint; never start double jobs.
const fired = new Set()
// When a job fires, report back to the green bar and mark it in the summary (both components share one module instance; one wire is enough).
const bus = { last: null, subs: new Set() }
function publish(v) {
  bus.last = v
  bus.subs.forEach(sink => sink(v))
}

const VIDEO_RE = /\.(mp4|mov|mkv|webm|m4v|avi|flv|ts|mpe?g)$/i

function videoAttachments(draft) {
  return (draft?.attachments ?? []).filter(a => {
    if (!a || a.kind !== 'file') return false
    const p = String(a.path ?? '')
    // not yet materialized to a path (uploading / pure paste): the engine cannot read it; do not force-start a job
    if (!p || a.uploadState === 'uploading') return false
    return (
      String(a.mime ?? '').toLowerCase().startsWith('video/') ||
      VIDEO_RE.test(p) ||
      VIDEO_RE.test(String(a.label ?? ''))
    )
  })
}

// 主人 2026-11-09 定：发起分析前必须先认出当前聊天模型+端口并确认真的活着。
// 桌面端切模型只改网关内存镜像，config.yaml 可能还指着刚死的旧端口（47831
// 事故：聊天窗口活得好好的，分析却全打在死端口上）。probe 与分析线程同一
// 套解析（网关运行时镜像），这里绿的就是分析真正会用的那个引擎。
let LLM_LIVE = null
async function llmPreflight() {
  try {
    const r = await api.rest('/llm/probe', { timeoutMs: 15000 })
    if (r && r.ok) { LLM_LIVE = r; return null }
    return t('panel.strip.probe_dead', String((r && (r.model || r.provider)) || '?'),
             String((r && r.error) || ''))
  } catch (e) {
    return t('panel.strip.probe_err', friendly(e))
  }
}

async function autoAnalyzeOnSend(draft) {
  const vids = videoAttachments(draft)
  const f = panel.form
  if (!vids.length) return draft
  if (!f) {
    // panel unmounted -> params unavailable. a silent skip is the worst trap: the user thinks analysis runs but nothing started
    host.notify({ kind: 'warning', message: t('panel.notify.panel_not_mounted') })
    return draft
  }
  if (f.auto_send === false) return draft
  // 与「立即分析」按钮同一道引擎体检。fail-open：引擎没活就照常放行消息 +
  // 提醒原因，绝不因插件问题吞掉主人的发送（官方中间件契约）
  const pf = await llmPreflight()
  if (pf) {
    host.notify({ kind: 'warning', message: t('panel.strip.auto_skipped', pf) })
    return draft
  }
  for (const a of vids) {
    const key = a.occurrenceId ?? a.id ?? a.path
    if (fired.has(key)) continue
    fired.add(key)
    // 统一路径来源：镜像进工作台路径框（等价「分析视频…」选片），参数弹窗跟着能探轨
    try { if (panel.setPath) panel.setPath(a.path) } catch (e) { /* 面板没挂载就算了 */ }
    try {
      // double-run guard: an active job already exists for the same path (resend, or a hot reload swapped the handler instance and cleared the
      // fingerprint table) -- do not start another; two jobs fighting over one 3090 slow both down
      const live = await api.rest('/jobs', { timeoutMs: 5000 })
      const dup = (live?.jobs ?? []).find((x) => (x?.path || '').toLowerCase() === a.path.toLowerCase()
        && (x?.status === 'running' || x?.status === 'pending'))
      if (dup) {
        publish({ name: baseName(a.path), id: dup.id, at: Date.now() })
        host.notify({ kind: 'info', message: t('panel.notify.already_analyzing', baseName(a.path)) })
        continue
      }
      // reuse the panel's parameter set; only swap the path for the attachment's own path
      const b = buildBody({ ...f, path: a.path })
      const r = await api.rest('/analyze', {
        method: 'POST',
        body: b,
        timeoutMs: 15000
      })
      publish({ name: baseName(a.path), id: r?.id ?? null, at: Date.now() })
      queryClient.invalidateQueries({ queryKey: ['video-xray'] })
      host.notify({
        kind: 'info',
        message: t('panel.notify.auto_started', baseName(a.path))
      })
    } catch (e) {
      fired.delete(key)   // if it failed to start, hand the fingerprint back; a resend can retry
      host.notify({ kind: 'warning', message: t('panel.notify.auto_failed', friendly(e)) })
    }
  }
  return draft   // always pass the draft through unchanged: a plugin error never cancels the user's send
}

function fmtElapsed(sec) {
  const s = Math.max(0, Math.round(sec || 0))
  return s >= 60 ? `${Math.floor(s / 60)}m${String(s % 60).padStart(2, '0')}s` : `${s}s`
}
// HTTP errors carry an "HTTP 400: " prefix in the message; the panel keeps only the human-readable sentence the backend wrote
function friendly(e) {
  return String(e?.message ?? e).replace(/^HTTP \d+:\s*/, '').slice(0, 200)
}

function baseName(p) {
  return String(p || '').split(/[\\/]/).pop() || '?'
}
function pips(done, total) {
  const n = Math.min(12, Math.max(0, total || 0))
  if (!n) return ''
  const d = Math.min(n, Math.max(0, done || 0))
  return '▓'.repeat(d) + '░'.repeat(n - d)
}

/** one column in the 4-column grid: small label + control. */
function Labeled(props) {
  return jsxs('label', {
    className: 'flex min-w-0 flex-col gap-1',
    children: [
      jsx('span', {
        className: 'text-[0.8125rem] font-medium text-(--ui-text-primary)',
        children: props.label
      }),
      props.child
    ]
  })
}

function SelectField(props) {
  return jsx(Labeled, {
    label: props.label,
    child: jsxs(Select, {
      value: props.value,
      onValueChange: props.onChange,
      disabled: props.disabled,
      children: [
        jsx(SelectTrigger, { className: 'h-7 w-full text-[0.75rem]', children: jsx(SelectValue, {}) }),
        jsx(SelectContent, {
          children: props.options.map(([v, k]) =>
            jsx(SelectItem, { value: v, children: t('panel.' + k) }, v)
          )
        })
      ]
    })
  })
}

/** design draft 2's engine status line at the bottom (only report what is real: R2T2 in place, segmented progress always saved). */
// 最近一次 /health 探测到的引擎实装情况（模块级缓存）。buildBody 是模块级
// 函数看不到 hook 数据，'auto' 草稿值就靠这份缓存解释成用户看得见的选择。
let ASR_AVAIL = null

// 面板端解释「到底算哪个引擎」：'auto' 只代表用户还没点。本机只装了一个→
// 高亮并采用它；一个没装→'off'（只影响显示，后端也会优雅跳过）；装了两个
// →不预选（summary 行显示「未选定」），后端按 R2T2 优先兜底。
// 用户亲手点过的选择永远作数，插件不暗箱改。
function asrResolved(transcribe, avail) {
  if (transcribe && transcribe !== 'auto') return transcribe
  if (!avail) return 'auto'
  const on = ['r2t2', 'whisper', 'cloud'].filter(e => !!avail[e])
  return on.length === 1 ? on[0] : (on.length === 0 ? 'off' : 'auto')
}

// 「立即分析」的引擎前提，单一真源（按钮灰显和提示文案都只读这一个函数，
// 别各处自己拼条件）：明确「关闭转录」=永远就绪；明确选具体引擎=体检必须
// 说它装了；'auto'（多引擎可选/体检未回）=后端自会优雅兜底降级，同样算就绪。
function asrReady(transcribe, avail) {
  const k = asrResolved(transcribe, avail)
  if (k === 'off' || k === 'auto') return true
  return avail ? !!avail[k] : true
}

/** 明确选引擎的勾选圈：只有本机真装了的引擎可选；没装的灰掉（hover 说
 * 怎么装），「关闭」永远可选。配合「本地引擎装填…→保存」两步，小白全程
 * 不碰 config.yaml。R2T2/Whisper 两行还带「参数」按钮：只有该引擎验证
 * 绿圈（可选）且当前单选着它才可点——其余情形灰掉并在 hover 说明原因；
 * 点开的是 ParamsDialog 调参弹窗（仿云端 ASR 弹窗，保存前真实加载）。 */
const LANG_ZH = { Arabic: '阿拉伯语', ASSamese: '阿萨姆语', Chinese: '中文', Croatian: '克罗地亚语', Czech: '捷克语', Danish: '丹麦语', Dutch: '荷兰语', English: '英语', Filipino: '菲律宾语', Finnish: '芬兰语', French: '法语', German: '德语', Greek: '希腊语', Hebrew: '希伯来语', Hindi: '印地语', Icelandic: '冰岛语', Indonesian: '印尼语', Italian: '意大利语', Japanese: '日语', Kazakh: '哈萨克语', Korean: '韩语', Malay: '马来语', Norwegian: '挪威语', Persian: '波斯语', Polish: '波兰语', Portuguese: '葡萄牙语', Romanian: '罗马尼亚语', Russian: '俄语', Spanish: '西班牙语', Swedish: '瑞典语', Thai: '泰语', Turkish: '土耳其语', Ukrainian: '乌克兰语', Vietnamese: '越南语', zh: '中文', en: '英语', ja: '日语', ko: '韩语', fr: '法语', de: '德语', es: '西班牙语', ru: '俄语', pt: '葡萄牙语', it: '意大利语', th: '泰语', vi: '越南语' }
const R2T2_LANG_FALLBACK = ['Arabic', 'ASSamese', 'Chinese', 'Croatian', 'Czech', 'Danish', 'Dutch', 'English', 'Filipino', 'Finnish', 'French', 'German', 'Greek', 'Hebrew', 'Hindi', 'Icelandic', 'Indonesian', 'Italian', 'Japanese', 'Kazakh', 'Korean', 'Malay', 'Norwegian', 'Persian', 'Polish', 'Portuguese', 'Romanian', 'Russian', 'Spanish', 'Swedish', 'Thai', 'Turkish', 'Ukrainian', 'Vietnamese']
const WHISPER_LANG_CODES = ['zh', 'en', 'ja', 'ko', 'fr', 'de', 'es', 'ru', 'pt', 'it', 'th', 'vi']
// R2T2 吃英文全名，Whisper 吃短码——自动检测只有 Whisper 家有，两边列表不同。
const langLabel = (uiZh, n) => (uiZh && LANG_ZH[n]) ? LANG_ZH[n] + '（' + n + '）' : n

function AsrRadios(props) {
  const { form, set, avail, settings } = props
  const resolved = asrResolved(form.transcribe, avail)
  // 视频没上传 = 引擎选择/参数按钮整块灰掉（主人 2026-11-09 定：先看片再调参，
  // 不然 R2T2 弹窗里的音轨下拉无从探测，参数读出来是错的）
  const vd = !String(form.path || '').trim()
  const [dlg, setDlg] = useState('')
  const row = e => {
    const has = e === 'off' || !avail || !!avail[e]
    // 「参数」按钮的铁律：绿圈可选 && 当前单选的就是它，否则灰。
    const tunable = e === 'r2t2' || e === 'whisper'
    const cfgOn = tunable && has && resolved === e
    return jsx('label', {
      className: cn('flex h-5 min-w-0 items-center gap-1.5', has ? 'cursor-pointer' : 'cursor-not-allowed opacity-50'),
      title: vd ? t('panel.strip.need_video_hint') : has ? t('panel.hint.' + e) : t('panel.hint.missing'),
      children: jsxs('span', {
        className: 'flex min-w-0 flex-1 items-center gap-1.5',
        children: [
          jsx('input', {
            type: 'radio',
            name: 'video-xray-asr',
            checked: resolved === e,
            disabled: vd || !has,
            onChange: () => set({ transcribe: e }),
            className: 'h-3.5 w-3.5 shrink-0 cursor-pointer',
          style: { accentColor: 'var(--ui-green)', opacity: has ? 1 : 0.45 }
          }),
          jsx('span', {
            className: cn('truncate text-[0.75rem] font-medium text-(--ui-text-primary)', (has && !vd) ? '' : 'opacity-40'),
            children: t('panel.asr.' + e)
          }),
          tunable ? jsx(Button, {
            size: 'xs',
            variant: 'ghost',
            type: 'button',
            disabled: vd || !cfgOn,
            title: vd ? t('panel.strip.params_need_video') : cfgOn ? t('panel.strip.params_tip_' + e) : t('panel.strip.params_locked'),
            className: 'ml-auto h-5 shrink-0 px-1.5 text-[0.6875rem]',
            onClick: ev => { ev.preventDefault(); ev.stopPropagation(); if (cfgOn) setDlg(e) },
            onMouseDown: ev => ev.stopPropagation(),
            children: t('panel.strip.params_btn')
          }) : null
        ]
      })
    }, e)
  }
  return jsxs('div', {
    className: 'flex min-w-0 flex-col',
    children: [
      ['r2t2', 'whisper', 'cloud', 'off'].map(row),
      dlg ? jsx(ParamsDialog, { eng: dlg, settings, videoPath: form.path, onClose: () => setDlg('') }) : null
    ]
  })
}

/** 本地引擎调参弹窗（R2T2 / Whisper 各一张），姿势抄云端 ASR 弹窗：
 *  「保存」前后端先拿这组参数真实加载引擎（/asr/params/save 带草稿覆盖
 *  重探针），CUDA 缺硬件、Q4 模型缺文件都被拒并把引擎原话写回界面。
 *  下方共享行（音轨/声道/字幕偏移）两家通用——它们活在 ffmpeg 和字幕
 *  数学里，不在任一引擎内部，一套控件接住用户手里各式各样的片子。 */
function ParamsDialog(props) {
  const { eng, settings, videoPath, onClose } = props
  const isR = eng === 'r2t2'
  const sp = (settings && settings.params && settings.params[eng]) || {}
  const au = (settings && settings.audio) || {}
  const uiZh = /[一-鿿]/.test(t('panel.asr.r2t2'))
  const [dev, setDev] = useState(sp.device || (isR ? 'cpu' : 'auto'))
  const [quant, setQuant] = useState(sp.quantization || 'Q8_0')
  const [size, setSize] = useState(sp.model_size || 'small')
  const [lang, setLang] = useState(sp.language || (isR ? 'Chinese' : 'auto'))
  const [hot, setHot] = useState(isR ? (sp.hotwords || '') : (sp.hotwords || sp.initial_prompt || ''))
  const [track, setTrack] = useState(String(au.stream != null ? au.stream : 0))
  const [chan, setChan] = useState(String(au.channel != null ? au.channel : 0))
  const [offv, setOffv] = useState(String(au.offset != null ? au.offset : 0))
  // CPU 上限%：从前挂在外面行3，主人 2026-11-09 定挪进来，跟随设备=CPU 才出现
  const [cap, setCap] = useState(String(settings && settings.cpu_percent != null ? settings.cpu_percent : 50))
  const [nTracks, setNTracks] = useState(null) // null=探测中 -1=失败 N=条数
  const [pBusy, setPBusy] = useState('')
  const [pMsg, setPMsg] = useState(null)
  // 路径框里这条视频有几条音轨？换视频即重数——3 条轨的专业样本点亮
  // 0..2 三个选项；没音轨/探测失败都有实话说。
  useEffect(() => {
    if (!videoPath) { setNTracks(null); return }
    let dead = false
    setNTracks(null)
    api.rest('/asr/tracks?video=' + encodeURIComponent(videoPath), { timeoutMs: 15000 })
      .then(r => { if (!dead) setNTracks(r && typeof r.tracks === 'number' ? r.tracks : -1) })
      .catch(() => { if (!dead) setNTracks(-1) })
    return () => { dead = true }
  }, [videoPath])
  const draftParams = () => isR
    ? { device: dev, quantization: quant, language: lang, hotwords: hot }
    : { model_size: size, device: dev, language: lang, hotwords: hot }
  const audioBody = () => ({ stream: Number(track) || 0, channel: Number(chan) || 0, offset: Number(offv) || 0 })
  async function testParams() {
    if (pBusy !== '') return
    setPBusy('test'); setPMsg(null)
    try {
      await api.rest('/asr/params/test', { method: 'POST', body: { engine: eng, params: draftParams() }, timeoutMs: 600000 })
      setPMsg({ ok: true, text: t('panel.strip.p_test_ok') })
    } catch (e) { setPMsg({ ok: false, text: friendly(e) }) }
    setPBusy('')
  }
  async function saveParams() {
    if (pBusy !== '') return
    setPBusy('save'); setPMsg(null)
    try {
      await api.rest('/asr/params/save', { method: 'POST', body: { engine: eng, params: draftParams() }, timeoutMs: 600000 })
      let note = ''
      try {
        await api.rest('/asr/audio/save', { method: 'POST', body: audioBody(), timeoutMs: 15000 })
        note = t('panel.strip.p_audio_note')
      } catch (e) { note = ' / ' + friendly(e) }
      // CPU 上限是公共设置，跟引擎参数一起落账（失败不拦保存；健康轮询会追平）
      try {
        await api.rest('/asr/settings', { method: 'POST', body: { cpu_percent: Number(cap) }, timeoutMs: 5000 })
      } catch (e) { /* 存不上不碍事 */ }
      queryClient.invalidateQueries({ queryKey: ['video-xray', 'health'] })
      setPMsg({ ok: true, text: t('panel.strip.p_saved') + note })
    } catch (e) { setPMsg({ ok: false, text: friendly(e) }) }
    setPBusy('')
  }
  const Sel = (label, val, setV, opts, tip) => jsxs('label', {
    className: 'flex min-w-0 flex-col gap-1',
    children: [
      jsx('span', { className: 'text-[0.6875rem]', style: { color: 'var(--ui-text-secondary)' }, children: label }),
      jsxs(Select, {
        value: val,
        onValueChange: setV,
        children: jsxs('span', {
          className: 'contents',
          children: [
            jsx(SelectTrigger, { size: 'xs', className: 'h-8 w-full justify-start text-[0.8rem]', title: tip || label, children: jsx(SelectValue, {}) }),
            jsx(SelectContent, { children: opts.map(o => jsx(SelectItem, { value: o[0], className: 'text-[0.8rem]', children: o[1] }, o[0])) })
          ]
        })
      })
    ]
  })
  const trackOpts = (() => {
    if (nTracks === null && videoPath) return [['0', t('panel.strip.p_track_probe')]]
    if (nTracks === -1) return [['0', t('panel.strip.p_track_probe_fail')]]
    if (nTracks === 0) return [['0', t('panel.strip.p_track_none')]]
    const n = Math.max(1, Math.min(nTracks || 1, 16))
    const out = []
    for (let i = 0; i < n; i++) out.push([String(i), t('panel.asr.audio_track') + ' ' + i])
    return out
  })()
  const langOpts = isR
    ? ((settings && settings.options && settings.options.r2t2_languages) || R2T2_LANG_FALLBACK).map(n => [n, langLabel(uiZh, n)])
    : [['auto', t('panel.strip.p_auto_lang')]].concat(WHISPER_LANG_CODES.map(c => [c, langLabel(uiZh, c)]))
  return jsx('div', {
    className: 'fixed inset-0 z-50 flex items-center justify-center p-4',
    style: { background: 'rgba(0,0,0,0.55)' },
    onMouseDown: onClose, // 点遮罩=返回；内层吞掉点击
    children: jsxs('div', {
      onMouseDown: e => e.stopPropagation(),
      className: 'flex max-w-[92vw] flex-col gap-2.5 rounded-xl border p-4',
      style: { width: 520 },
      style: { background: 'var(--ui-bg-elevated)', borderColor: 'var(--ui-stroke-secondary)', boxShadow: '0 12px 40px rgba(0,0,0,0.5)' },
      children: [
        jsx('div', { className: 'text-[0.9rem] font-semibold', style: { color: 'var(--ui-text-primary)' }, children: t('panel.strip.dlg_' + eng) }),
        jsx('div', { className: 'text-[0.6875rem]', style: { color: 'var(--ui-text-secondary)' }, children: t('panel.strip.dlg_desc_' + eng) }),
        jsxs('div', { className: 'grid grid-cols-2 gap-2.5', children: [
          Sel(t('panel.strip.p_device'), dev, setDev,
            isR ? [['cpu', t('panel.strip.p_dev_cpu')], ['cuda', t('panel.strip.p_dev_cuda')]]
                : [['auto', t('panel.strip.p_dev_auto')], ['cpu', t('panel.strip.p_dev_cpu')], ['cuda', t('panel.strip.p_dev_cuda')]], t('panel.strip.p_device_tip')),
          isR
            ? Sel(t('panel.strip.p_quant'), quant, setQuant, [['Q8_0', t('panel.strip.p_q8')], ['Q4_K_M', t('panel.strip.p_q4')]])
            : Sel(t('panel.strip.p_size'), size, setSize, [['tiny', t('panel.strip.p_s_tiny')], ['base', t('panel.strip.p_s_base')], ['small', t('panel.strip.p_s_small')], ['medium', t('panel.strip.p_s_medium')], ['large-v3', t('panel.strip.p_s_large')]]),
          Sel(t('panel.strip.p_lang'), lang, setLang, langOpts),
          Sel(t('panel.strip.p_channel'), chan, setChan, [['0', t('panel.strip.p_ch0')], ['1', t('panel.strip.p_ch1')], ['2', t('panel.strip.p_ch2')]]),
          Sel(t('panel.strip.p_track') + (nTracks > 0 ? ' · ' + t('panel.strip.p_track_of', String(nTracks)) : ''), track, setTrack, trackOpts,
            nTracks === -1 ? t('panel.strip.p_track_probe_fail') : t('panel.strip.p_track')),
          dev === 'cpu' ? Sel(t('panel.asr.engine_cpu'), cap, setCap, [['25', t('panel.asr.engine_cpu_25')], ['50', t('panel.asr.engine_cpu_50')], ['75', t('panel.asr.engine_cpu_75')], ['100', t('panel.asr.engine_cpu_100')]], t('panel.asr.engine_cpu_tip')) : null,
          jsx(Labeled, {
            label: t('panel.strip.p_offset'),
            child: jsx(Input, { value: offv, onChange: e => setOffv(e.target.value), type: 'number', step: '0.5', className: 'h-8 w-full text-[0.8rem]', title: t('panel.strip.p_offset') })
          })
        ] }),
        jsx(Labeled, {
          label: isR ? t('panel.strip.p_hot') : t('panel.strip.p_hot_whisper'),
          child: jsx(Input, {
            value: hot,
            onChange: e => setHot(e.target.value),
            placeholder: t('panel.strip.p_hot_ph'),
            className: 'h-8 w-full text-[0.8rem]',
            title: isR ? t('panel.strip.p_hot') : t('panel.strip.p_hot_tip_whisper')
          })
        }),
        pMsg
          ? jsx('div', { className: 'break-words text-[0.75rem]', style: { color: pMsg.ok ? 'var(--ui-green)' : 'var(--ui-red)' }, children: pMsg.text })
          : null,
        jsxs('div', { className: 'mt-1 flex items-center gap-2', children: [
          jsx(Button, {
            size: 'sm',
            variant: 'outline',
            type: 'button',
            disabled: pBusy !== '',
            onClick: testParams,
            children: pBusy === 'test' ? t('panel.strip.p_testing') : t('panel.strip.test_conn')
          }),
          jsx(Button, {
            size: 'sm',
            type: 'button',
            disabled: pBusy !== '',
            onClick: saveParams,
            children: pBusy === 'save' ? t('panel.strip.p_saving') : t('panel.strip.save_btn')
          }),
          jsx('span', { className: 'flex-1' }),
          jsx(Button, {
            size: 'sm',
            variant: 'outline',
            type: 'button',
            disabled: pBusy !== '',
            onClick: onClose,
            children: t('panel.strip.back')
          })
        ] })
      ]
    })
  })
}

/** 转录引擎区的底部三行——新用户的首发仪式，全程不碰 config.yaml。
 *  行1 本地引擎：点「本地引擎装填…」选目录（原生对话框）→ 路径回填进
 *  输入框 → 按「保存」：后端真启动该引擎、加载模型、试转 0.2 秒静音——
 *  通过才写登记并让左边的圈变绿；失败用引擎自己的话写清原因，什么都不
 *  登记。验证是短命工人进程，用完即退，显存/CPU 立刻归还（Job Object 是
 *  孤儿兜底）。行2-3 云端 ASR 新手通道：地址 + 模型（「测试连通」自动从
 *  /v1/models 读取）+ 密钥，实测转写通过后「保存」才落账。CPU 上限已搬进
 *  本地引擎的参数弹窗（跟设备=CPU 走），这里只剩云端配置按钮和状态行。 */
function AsrFooter(props) {
  const { avail, settings, sel } = props
  const [msg, setMsg] = useState(null)
  const [busy, setBusy] = useState(false)
  const [draft, setDraft] = useState('')
  const [cUrl, setCUrl] = useState('')
  const [cModel, setCModel] = useState('')
  const [cKey, setCKey] = useState('')
  const [cModels, setCModels] = useState([])
  const [cBusy, setCBusy] = useState('')
  const [keyTail, setKeyTail] = useState('')
  const [seeded, setSeeded] = useState(false)
  const c = settings && settings.cloud
  // 云端输入框从后端预填一次（面板存过的 > config.yaml/env 里现配的）
  useEffect(() => {
    if (!seeded && c && (c.base_url || c.model)) {
      setCUrl(c.base_url || '')
      setCModel(c.model || '')
      setKeyTail(c.key_tail || '')
      setSeeded(true)
    }
  }, [seeded, c && c.base_url, c && c.model])
  // 2026-11-09 主人指定：云端一行塞三个输入框挤成小方块还溢出屏幕，改成
  // 「云端语音自定义…」按钮开配置框（框里 测试连通/保存/返回 三键）。
  const [cloudOpen, setCloudOpen] = useState(false)
  useEffect(() => {
    if (!cloudOpen) return
    const onKey = (ev) => { if (ev.key === 'Escape') closeCloud() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [cloudOpen])
  function closeCloud() {
    setCloudOpen(false)
    setCUrl((c && c.base_url) || '')
    setCModel((c && c.model) || '')
    setCKey('')
    setMsg(null)
  }
  const noEngine = !avail || (!avail.r2t2 && !avail.whisper && !avail.cloud)
  async function pickEngine() {
    let dir = null
    try {
      // 和选视频文件同一个原生选择框，只是 mode 换成选文件夹——小白只需要
      // 认识 R2T2/whisper 的目录长什么样，剩下的校验交给后端逐条讲明白。
      dir = await api.os.pickOpenPath({ title: t('panel.strip.pick_dir'), directories: true })  // Electron 真参数名：mode 会被无视弹成选文件(2026-11-09 实锤)
    } catch {
      return // 用户按了取消
    }
    const p = typeof dir === 'string' ? dir : (dir && dir.path) || null
    if (!p) return
    setDraft(p)
    setMsg(null)
  }
  async function saveLocal() {
    if (!draft.trim() || busy) return
    setBusy(true)
    setMsg(null)
    try {
      // 后端真加载模型验证；whisper 首跑可能还要下权重（~460MB），额度给足
      const r = await api.rest('/asr/register', { method: 'POST', body: { path: draft.trim() }, timeoutMs: 600000 })
      setMsg({ ok: true, text: t('panel.hint.load_ok') + (r.engine || '') + ' · ' + (r.path || draft) })
      setDraft('')
    } catch (e) {
      // 400 的 detail 就是后端逐条写好的「为什么这个目录不对」，直接给小白看
      setMsg({ ok: false, text: friendly(e) })
    }
    queryClient.invalidateQueries({ queryKey: ['video-xray', 'health'] })
    setBusy(false)
  }
  async function testCloud() {
    if (cBusy !== '' || !cUrl.trim()) return
    setCBusy('test')
    setMsg(null)
    try {
      const r = await api.rest('/asr/cloud/test', { method: 'POST', body: { base_url: cUrl, api_key: cKey, model: cModel }, timeoutMs: 90000 })
      if (Array.isArray(r.models) && r.models.length) setCModels(r.models)
      if (r.model_used && !cModel) setCModel(r.model_used)
      setMsg(r.ok
        ? { ok: true, text: t('panel.strip.cloud_test_ok', r.model_used || '') + (r.reason ? '（' + r.reason + '）' : '') }
        : { ok: false, text: friendly(r.reason || 'cloud test failed') })
    } catch (e) {
      setMsg({ ok: false, text: friendly(e) })
    }
    setCBusy('')
  }
  async function saveCloud() {
    if (cBusy !== '') return
    setCBusy('save')
    setMsg(null)
    try {
      const r = await api.rest('/asr/cloud/save', { method: 'POST', body: { base_url: cUrl, api_key: cKey, model: cModel }, timeoutMs: 180000 })
      setMsg({ ok: true, text: t('panel.strip.cloud_saved') + (r.model || cModel) })
      setCKey('') // 密钥不再回显，只留尾 4 位当记号
      setKeyTail(r.key_tail || '')
      queryClient.invalidateQueries({ queryKey: ['video-xray', 'health'] })
    } catch (e) {
      setMsg({ ok: false, text: friendly(e) })
    }
    setCBusy('')
  }
  return jsxs('div', {
    className: 'flex min-w-0 flex-col gap-1',
    style: { gridColumn: '1 / -1' },
    children: [
      // 行1：本地引擎 —— 选目录 → 路径回填 → 「保存」触发真机加载验证
      jsxs('div', {
        className: 'flex min-w-0 items-center gap-1.5',
        children: [
          jsx(Button, {
            size: 'xs',
            variant: 'outline',
            type: 'button',
            disabled: busy || cBusy !== '',
            title: t('panel.strip.pick_dir_tip'),
            onClick: pickEngine,
            className: 'shrink-0 whitespace-nowrap',
            children: t('panel.strip.load_engine')
          }),
          jsx(Input, {
            value: draft,
            onChange: e => setDraft(e.target.value),
            placeholder: t('panel.strip.ph_pick_dir'),
            className: 'h-6 min-w-0 flex-1 text-[0.6875rem]'
          }),
          jsx(Button, {
            size: 'xs',
            type: 'button',
            disabled: busy || cBusy !== '' || !draft.trim(),
            title: t('panel.strip.save_local_tip'),
            onClick: saveLocal,
            children: busy ? t('panel.strip.verifying') : t('panel.strip.save_btn')
          })
        ]
      }),
      // 行2：云端入口按钮+状态摘要；CPU 上限放这行最右（下拉不再被屏幕裁；
      // 测试/保存/返回都在配置框里，框里一行一个字段）。
      jsxs('div', {
        className: 'flex min-w-0 items-center gap-1.5',
        children: [
          jsx('span', {
            className: 'shrink-0 text-[0.6875rem] text-(--ui-accent)',
            children: t('panel.strip.cloud_title')
          }),
          jsx(Button, {
            size: 'xs',
            variant: 'outline',
            type: 'button',
            onClick: () => setCloudOpen(true),
            className: 'shrink-0 whitespace-nowrap',
            children: t('panel.strip.cloud_open')
          }),
          jsx('span', {
            className: 'min-w-0 flex-1 truncate text-[0.6875rem]',
            style: { color: c && c.base_url && c.model ? 'var(--ui-accent)' : 'var(--ui-text-secondary)' },
            children: (c && c.base_url && c.model) ? t('panel.strip.cloud_done', c.model || c.base_url) : t('panel.strip.cloud_unset')
          }),
        ]
      }),
      cloudOpen ? jsx(CloudDialog, {
        cUrl, setCUrl, cModel, setCModel, cModels, setCModels,
        cKey, setCKey, keyTail, msg, cBusy,
        onTest: testCloud, onSave: saveCloud, onClose: closeCloud
      }) : null,
      // 一条状态行：最近一次操作的结果；真·一个引擎都没有时才给短引导
      msg
        ? jsx('div', {
            className: 'break-words text-[0.75rem]',
            style: { color: msg.ok ? 'var(--ui-green)' : 'var(--ui-red)' },
            children: msg.text
          })
        : (noEngine ? jsx('div', {
            className: 'break-words text-[0.6875rem]',
            style: { color: 'var(--ui-orange)' },
            children: t('panel.hint.none')
          }) : null)
    ]
  })
}
function CloudDialog(props) {
  const { cUrl, setCUrl, cModel, setCModel, cModels, setCModels, cKey, setCKey, keyTail, msg, cBusy, onTest, onSave, onClose } = props
  // 宿主没导出 Modal（2026-11-09 dump 运行版全部 677 个 export 实锤），
  // 自绘 fixed 遮罩；样式全走内联变量，跟本文件其余部分同一姿势。
  return jsx('div', {
    className: 'fixed inset-0 z-50 flex items-center justify-center p-4',
    style: { background: 'rgba(0,0,0,0.55)' },
    onMouseDown: onClose, // 点遮罩=返回；内层吞掉点击
    children: jsxs('div', {
      onMouseDown: e => e.stopPropagation(),
      className: 'flex max-w-[92vw] flex-col gap-2.5 rounded-xl border p-4',
      style: { width: 440 },
      style: { background: 'var(--ui-bg-elevated)', borderColor: 'var(--ui-stroke-secondary)', boxShadow: '0 12px 40px rgba(0,0,0,0.5)' },
      children: [
        jsx('div', { className: 'text-[0.9rem] font-semibold', style: { color: 'var(--ui-text-primary)' }, children: t('panel.strip.dlg_title') }),
        jsx('div', { className: 'text-[0.6875rem]', style: { color: 'var(--ui-text-secondary)' }, children: t('panel.strip.dlg_desc') }),
        jsx(Input, {
          autoFocus: true,
          value: cUrl,
          onChange: e => setCUrl(e.target.value),
          placeholder: t('panel.strip.ph_url'),
          className: 'h-8 w-full text-[0.8rem]'
        }),
        jsxs(Select, {
          value: cModel,
          onValueChange: setCModel,
          disabled: !cModels.length,
          children: jsxs('span', {
            className: 'contents',
            children: [
              jsx(SelectTrigger, { size: 'xs', className: 'h-8 w-full justify-start text-[0.8rem]', title: t('panel.strip.cloud_model_tip'), children: jsx(SelectValue, { placeholder: t('panel.strip.ph_model') }) }),
              cModels.length ? jsx(SelectContent, { children: cModels.map(m => jsx(SelectItem, { value: m, children: m }, m)) }) : null
            ]
          })
        }),
        jsx(Input, {
          type: 'password',
          value: cKey,
          onChange: e => setCKey(e.target.value),
          placeholder: keyTail ? t('panel.strip.key_keep', keyTail) : t('panel.strip.ph_key'),
          className: 'h-8 w-full text-[0.8rem]'
        }),
        msg
          ? jsx('div', { className: 'break-words text-[0.75rem]', style: { color: msg.ok ? 'var(--ui-green)' : 'var(--ui-red)' }, children: msg.text })
          : jsx('div', { className: 'text-[0.625rem]', style: { color: 'var(--ui-text-secondary)' }, children: t('panel.strip.cloud_model_tip') }),
        jsxs('div', { className: 'mt-1 flex items-center gap-2', children: [
          jsx(Button, {
            size: 'sm',
            variant: 'outline',
            type: 'button',
            disabled: cBusy !== '' || !cUrl.trim(),
            onClick: onTest,
            children: cBusy === 'test' ? t('panel.strip.testing') : t('panel.strip.test_conn')
          }),
          jsx(Button, {
            size: 'sm',
            type: 'button',
            disabled: cBusy !== '' || !cUrl.trim() || !cModel.trim(),
            onClick: onSave,
            children: cBusy === 'save' ? t('panel.strip.saving') : t('panel.strip.save_btn')
          }),
          jsx('span', { className: 'flex-1' }),
          jsx(Button, {
            size: 'sm',
            variant: 'outline',
            type: 'button',
            onClick: onClose,
            children: t('panel.strip.back')
          })
        ] })
      ]
    })
  })
}
function LiveCard(props) {
  const j = props.job
  const transcribing = j.status === 'running' && j.done >= j.total
  return jsxs('div', {
    className: 'flex flex-col gap-1.5',
    children: [
      jsxs('div', {
        className: 'flex items-center gap-2 text-[0.75rem]',
        children: [
          jsx('span', { className: 'shrink-0', children: '🎬' }),
          jsx('span', {
            className: 'min-w-0 flex-1 truncate',
            children: t('panel.live.analyzing', baseName(j.path))
          }),
          jsx('span', {
            className: 'shrink-0 font-mono text-(--ui-text-tertiary)',
            children: pips(j.done, j.total)
          }),
          jsx('span', {
            className: 'shrink-0 text-(--ui-text-tertiary)',
            children: t('panel.live.segs_used', j.done, j.total, fmtElapsed(j.elapsed_s))
          }),
          transcribing
            ? jsx('span', {
                className: 'shrink-0 text-(--ui-accent)',
                children: t('panel.live.transcribing')
              })
            : null
        ]
      }),
      jsxs('div', {
        className: 'flex items-center gap-2',
        children: [
          jsx(Button, {
            size: 'sm',
            type: 'button',
            disabled: true,
            children: j.status === 'pending' ? t('panel.status.pending') : t('panel.status.running')
          }),
          jsx(Button, {
            size: 'sm',
            variant: 'ghost',
            type: 'button',
            onClick: () => props.onCancel(j.id),
            disabled: !!props.cancelling,
            children: props.cancelling ? t('panel.detail.cancelling') : t('panel.live.cancel')
          }),
          jsx(Button, {
            size: 'sm',
            variant: 'ghost',
            type: 'button',
            title: t('panel.live.backgroundHint'),
            onClick: props.onBackground,
            children: t('panel.live.background')
          })
        ]
      })
    ]
  })
}

/** design draft 7: job detail (summary + transcript timeline + per-segment notes + cancel/resume/open folder). */
/** Gallery strip: the exact frames the model saw, re-extracted on demand.
 *  Thumbnails ride the manifest request (one round-trip); a click fetches
 *  the full-size frame through the same rest bridge. Fail-soft: no frames
 *  or dead backend = the strip is simply absent, never an error wall. */
function GalleryFrames(props) {
  const [big, setBig] = useState(null)
  const { data } = useQuery({
    queryKey: ['video-xray', 'gallery', props.id],
    queryFn: () => api.rest('/jobs/gallery?id=' + encodeURIComponent(props.id)),
    enabled: !!props.id, staleTime: 60000
  })
  const frames = data && Array.isArray(data.frames) ? data.frames : []
  const { data: full } = useQuery({
    queryKey: ['video-xray', 'gallery-full', props.id, big && big.seg, big && big.i],
    queryFn: () => api.rest('/jobs/gallery?id=' + encodeURIComponent(props.id)
      + '&seg=' + big.seg + '&frame=' + big.i),
    enabled: !!big
  })
  if (!frames.length) return null
  return jsx('div', { className: 'flex flex-col gap-1', children: [
    jsx(Separator, {}),
    jsx('div', { className: 'text-[0.625rem] text-(--ui-accent)',
      children: t('panel.detail.gallery_title', frames.length) }),
    // The design says "strip": thumbs tile left-to-right and wrap, not one
    // per row — a 32-frame job must be scannable in one screenful.
    jsx('div', { className: 'flex flex-wrap items-start gap-1.5', children:
      frames.map((f) => jsxs('button', {
        className: 'flex max-w-fit items-center gap-1.5 rounded-md bg-(--ui-surface-background) p-1 pr-2 text-left hover:bg-(--ui-row-hover-background)',
        onClick: () => setBig(big && big.i === f.i && big.seg === f.seg ? null : { seg: f.seg, i: f.i }),
        children: [
          f.data_url ? jsx('img', { src: f.data_url, alt: '',
            className: 'h-12 w-auto rounded-sm' }) : null,
          jsx('span', { className: 'font-mono text-[0.5625rem] text-(--ui-text-tertiary)',
            children: String(f.ts) + 's' })
        ]
      }, f.seg + '-' + f.i)) }),
    big ? (full && full.data_url
      ? jsx('img', { src: full.data_url, alt: '', className: 'mt-1 w-full rounded-md' })
      : jsx('div', { className: 'mt-1 text-[0.625rem] text-(--ui-text-tertiary)',
          children: t('panel.detail.gallery_loading') }))
      : null
  ] })
}


function JobDetail(props) {
  const [busy, setBusy] = useState('')
  // persistent feedback right under the action buttons: {kind:'ok'|'bad', src:'export'|'h3', text, ms}
  const [flash, setFlash] = useState(null)
  const flashTimer = useRef(null)
  const flashNow = f => {
    if (flashTimer.current) clearTimeout(flashTimer.current)
    setFlash(f)
    flashTimer.current = f.ms ? setTimeout(() => setFlash(null), f.ms) : null
  }
  useEffect(() => () => { if (flashTimer.current) clearTimeout(flashTimer.current) }, [])
  const baseNameOf = p => String(p).split(/[\\/]/).pop()
  const { data, error } = useQuery({
    queryKey: ['video-xray', 'job', props.id],
    queryFn: () => api.rest(`/jobs/${encodeURIComponent(props.id)}`),
    enabled: !!props.id,
    refetchInterval: q => (LIVE.has(q.state.data?.status) ? 1500 : false)
  })
  if (!props.id) return null
  if (error && !data) {
    return jsx('div', {
      className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
      children: t('panel.detail.load_failed', String(error?.message ?? error).slice(0, 120))
    })
  }
  const j = data ?? {}
  const live = LIVE.has(j.status)
  const transcript = String(j.transcript ?? '').trim()
  const notes = Array.isArray(j.notes) ? j.notes : []
  const done = j.status === 'done'
  // export/copy use the green bar's current H3 settings; if the panel is unmounted, fall back to persisted params
  const h3opts = () => {
    const f = panel.form || loadForm()
    return { mode: f.h3mode || 'shots', thresh: Number(f.thresh) || 0.3 }
  }
  // copy the H3 prompt to the clipboard (paste straight into a ComfyUI node input)
  async function copyH3() {
    setBusy('h3')
    try {
      const r = await api.rest(`/jobs/${encodeURIComponent(j.id)}/h3`, {
        method: 'POST',
        body: h3opts(),
        timeoutMs: 120000
      })
      if (!r?.ok) throw new Error(String(r?.error ?? t('panel.detail.h3_missing')))
      await api.os?.writeClipboard?.(String(r.h3 ?? ''))
      flashNow({
        kind: 'ok', src: 'h3', ms: 6000,
        text: t('panel.detail.h3_ok',
          t(r.mode === 'single' ? 'panel.h3.short_single' : 'panel.h3.short_shots'),
          r.shots) + (r.warning ? `｜${r.warning}` : '')
      })
      host.notify({
        kind: 'info',
        message:
          t('panel.detail.h3_copied',
            t(r.mode === 'single' ? 'panel.h3.short_single' : 'panel.h3.short_shots'),
            r.shots,
            t(r.engine === 'llm' ? 'panel.h3.engine_llm' : 'panel.h3.engine_rule')) +
          (r.warning ? `｜${r.warning}` : '')
      })
    } catch (e) {
      flashNow({ kind: 'bad', src: 'h3', ms: 0, text: t('panel.detail.h3_failed', friendly(e)) })
      host.notify({ kind: 'warning', message: t('panel.detail.h3_failed', friendly(e)) })
    } finally {
      setBusy('')
    }
  }
  // export: one native save-as dialog (the user picks the location); the gateway writes .md + a same-named _H3 prompt .txt at the chosen path
  async function exportJob() {
    setBusy('export')
    try {
      const stem = String(j.name || baseName(j.path) || j.id).replace(/\.[A-Za-z0-9]+$/, '')
      const got = await api.os?.pickSavePath?.({
        defaultPath: `${stem}_${t('panel.detail.export_stem')}.md`,
        title: t('panel.detail.export_title'),
        filters: [{ name: 'Markdown', extensions: ['md'] }]
      })
      const path = typeof got === 'string' ? got : got?.path
      if (!path) return
      const r = await api.rest(`/jobs/${encodeURIComponent(j.id)}/export`, {
        method: 'POST',
        body: { path, ...h3opts() },
        timeoutMs: 120000
      })
      if (!r?.ok) throw new Error(String(r?.error ?? t('panel.detail.write_failed')))
      flashNow({
        kind: 'ok', src: 'export', ms: 6000,
        text: t('panel.detail.export_ok', baseNameOf(r.md_path), baseNameOf(r.txt_path)) + (r.warning ? `｜${r.warning}` : '')
      })
      host.notify({
        kind: 'info',
        message: t('panel.detail.exported', r.md_path, r.txt_path) + (r.warning ? `｜${r.warning}` : '')
      })
    } catch (e) {
      flashNow({ kind: 'bad', src: 'export', ms: 0, text: t('panel.detail.export_failed', friendly(e)) })
      host.notify({ kind: 'warning', message: t('panel.detail.export_failed', friendly(e)) })
    } finally {
      setBusy('')
    }
  }
  return jsxs('div', {
    className: 'flex flex-col gap-2',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx('span', {
            className: 'min-w-0 max-w-[40%] truncate text-[0.75rem]',
            children: baseName(j.path)
          }),
          jsx(Badge, {
            variant: BADGE_VARIANT[j.status] ?? 'default',
            className: 'text-[0.625rem]',
            children: statusLabel(j.status)
          }),
          jsx('span', {
            className: 'text-[0.625rem] text-(--ui-text-tertiary)',
            children: `${j.done ?? 0}/${j.total ?? '?'}${t('panel.detail.segs')}${
              j.elapsed_s ? ` · ${fmtElapsed(j.elapsed_s)}` : ''
            }${j.resume_of ? t('panel.detail.from_resume') : ''}`
          }),
          jsx('span', { className: 'flex-1' }),
          live
            ? jsx(Button, {
                size: 'sm',
                variant: 'ghost',
                type: 'button',
                onClick: () => props.onCancel(j.id),
                disabled: !!props.cancelling,
                children: props.cancelling ? t('panel.detail.cancelling') : t('panel.detail.cancel')
              })
            : j.status === 'cancelled'
              ? jsx(Button, {
                  size: 'sm',
                  variant: 'ghost',
                  type: 'button',
                  disabled: true,
                  children: t('panel.detail.cancel')
                })
              : null,
          RESUMABLE.has(j.status)
            ? jsx(Button, {
                size: 'sm',
                variant: 'ghost',
                type: 'button',
                className: 'text-amber-400',
                onClick: () => props.onResume(j.id),
                children: t('panel.detail.resume')
              })
            : null,
          done && j.summary
            ? jsx(Button, {
                size: 'sm',
                variant: 'outline',
                type: 'button',
                style: { minWidth: '6rem' },
                className: 'justify-center',
                disabled: busy !== '',
                onClick: exportJob,
                children: busy === 'export'
                  ? t('panel.detail.exporting')
                  : flash && flash.kind === 'ok' && flash.src === 'export'
                    ? t('panel.detail.export_ok_btn')
                    : t('panel.detail.export')
              })
            : null,
          done && notes.length
            ? jsx(Button, {
                size: 'sm',
                variant: 'ghost',
                type: 'button',
                style: { minWidth: '6rem' },
                className: 'justify-center',
                disabled: busy !== '',
                onClick: copyH3,
                children: busy === 'h3'
                  ? t('panel.detail.copying')
                  : flash && flash.kind === 'ok' && flash.src === 'h3'
                    ? t('panel.detail.h3_ok_btn')
                    : t('panel.detail.copy_h3')
              })
            : null,
          j.path
            ? jsx(Button, {
                size: 'sm',
                variant: 'ghost',
                type: 'button',
                onClick: () => api.os?.revealPath?.(String(j.path)),
                children: t('panel.detail.reveal')
              })
            : null,
          jsx(Button, {
            size: 'sm',
            variant: 'ghost',
            type: 'button',
            onClick: () => props.onClose(),
            children: t('panel.detail.collapse')
          })
        ]
      }),
      flash
        ? jsx('div', {
            className: 'text-right text-[0.6875rem]',
            style: { color: flash.kind === 'ok' ? 'var(--ui-green)' : 'var(--ui-red)' },
            children: (flash.kind === 'ok' ? '✅ ' : '❌ ') + flash.text
          })
        : null,
      j.summary_truncated
        ? jsx('div', {
            className: 'text-[0.625rem] text-(--ui-accent)',
            children: t('panel.detail.truncated')
          })
        : null,
      j.error
        ? jsx('div', {
            className: 'text-[0.625rem] text-(--ui-accent)',
            children: t('panel.detail.error_prefix', String(j.error).slice(0, 240))
          })
        : null,
      jsx(Separator, {}),
      jsx(ScrollArea, {
        className: 'max-h-[30vh]',
        children: jsxs('div', {
          className: 'flex flex-col gap-2 pr-1',
          children: [
            jsx('div', {
              className: 'whitespace-pre-wrap text-[0.75rem] leading-relaxed',
              children: j.summary || (live ? t('panel.detail.no_summary_live') : t('panel.detail.no_summary'))
            }),
            transcript
              ? jsxs('div', {
                  className: 'flex flex-col gap-1',
                  children: [
                    jsx(Separator, {}),
                    jsx('div', {
                      className: 'text-[0.625rem] text-(--ui-accent)',
                      children: t('panel.detail.lines_title', transcript.split('\n').filter(Boolean).length)
                    }),
                    ...transcript
                      .split('\n')
                      .filter(Boolean)
                      .map((line, i) => {
                        const m = line.match(/^\s*(\d{1,2}:\d{2})\s+(.*)$/)
                        return jsxs(
                          'div',
                          {
                            className: 'flex gap-2 text-[0.75rem]',
                            children: m
                              ? [
                                  jsx('span', {
                                    className: 'shrink-0 font-mono text-(--ui-accent)',
                                    children: m[1]
                                  }),
                                  jsx('span', { children: m[2] })
                                ]
                              : [jsx('span', { children: line })]
                          },
                          i
                        )
                      })
                  ]
                })
              : null,
            notes.length
              ? jsxs('details', {
                  className: 'flex flex-col gap-1',
                  children: [
                    jsx('summary', {
                      className: 'cursor-pointer text-[0.625rem] text-(--ui-accent)',
                      children: t('panel.detail.notes_title', notes.length)
                    }),
                    ...notes.map((n, i) =>
                      jsx('div', {
                        className: 'whitespace-pre-wrap rounded-md bg-(--ui-surface-background) p-2 text-[0.6875rem] leading-relaxed',
                        children: String(n)
                      }, i)
                    )
                  ]
                })
              : null,
              done ? jsx(GalleryFrames, { id: j.id }) : null,
          ]
        })
      })
    ]
  })
}

/** design draft 7: job list (expands right above the composer; no permanent UI footprint). */
function JobList(props) {
  // checkboxes are drawn here, but 'which rows are ticked' belongs to Strip: the button lives on the settings strip and the ticks live in list rows,
  // so both must read one shared state, or the number on the button drifts from the real ticks.
  const { data, error } = useQuery({
    queryKey: ['video-xray', 'jobs'],
    queryFn: () => api.rest('/jobs?limit=25'),
    refetchInterval: q =>
      (q.state.data?.jobs ?? []).some(j => LIVE.has(j.status)) ? 1500 : 4000
  })
  if (error && !data) {
    return jsx('div', {
      className: 'text-[0.6875rem] text-(--ui-text-tertiary)',
      children: t('panel.list.load_failed', String(error?.message ?? error).slice(0, 120))
    })
  }
  const jobs = data?.jobs ?? []
  if (!jobs.length) {
    return jsx(EmptyState, {
      className: 'py-4',
      title: t('panel.list.empty_title'),
      description: t('panel.list.empty_desc')
    })
  }

  return jsx(ScrollArea, {
    className: 'max-h-[26vh]',
    style: { maxHeight: '26vh' },
    children: jsxs(
      'div',
      {
        className: 'flex flex-col gap-0.5 pr-1',
        children: jobs.map(j =>
          jsxs(
            'div',
            {
              className: cn(
                'flex items-center gap-2 rounded-md px-2 py-1 text-[0.75rem] transition-colors',
                j.id === props.selected
                  ? 'bg-(--ui-selection-background)'
                  : 'hover:bg-(--ui-row-hover-background)'
              ),
              children: [
                jsx(Checkbox, {
                  // ticked = pending delete; running jobs may also be ticked -- the backend refuses on delete and surfaces the reason
                  checked: props.picked.has(j.id),
                  onCheckedChange: () => props.onTogglePick(j.id),
                  'aria-label': t('panel.list.pick_aria', baseName(j.path))
                }),
                jsxs('button', {
                  type: 'button',
                  onClick: () => props.onSelect(j.id),
                  className: 'flex min-w-0 flex-1 items-center gap-2 text-left',
                  children: [
                    jsx('span', {
                      className: 'min-w-0 flex-1 truncate',
                      children: baseName(j.path)
                    }),
                    jsx(Badge, {
                      variant: BADGE_VARIANT[j.status] ?? 'default',
                      className: 'shrink-0 text-[0.625rem]',
                      children: statusLabel(j.status)
                    }),
                    jsx('span', {
                      className: 'shrink-0 text-[0.625rem] text-(--ui-accent)',
                      children: `${j.done ?? 0}/${j.total ?? '?'}${t('panel.detail.segs')}${
                        j.elapsed_s ? ` · ${fmtElapsed(j.elapsed_s)}` : ''
                      }${j.transcript_used ? t('panel.list.has_lines') : ''}${j.resume_of ? t('panel.list.resumed') : ''}`
                    })
                  ]
                }),
                RESUMABLE.has(j.status)
                  ? jsx(Button, {
                      size: 'xs',
                      variant: 'ghost',
                      type: 'button',
                      className: 'text-amber-400',
                      onClick: () => props.onResume(j.id),
                      children: t('panel.list.resume')
                    })
                  : null
              ]
            },
            j.id
          )
        )
      }
    )
  })
}

function Strip() {
  const [form, setForm] = useState(loadForm)
  // rebuild instantly on language switch: a Strip re-render re-evaluates t() in every child component
  const [, bumpLocale] = useState(0)
  useEffect(() => api.i18n?.onLocaleChange?.(() => bumpLocale(x => x + 1)), [])
  // design draft 1: collapsed by default; click the title to expand the parameters
  const [open, setOpen] = useState(false)
  const [tasks, setTasks] = useState(false)
  const [sel, setSel] = useState(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState(null)
  // delete-trio state: picked rows + current delete op (delBusy) + the confirm step of 'delete all'.
  // Must live in Strip: the button is on the settings strip, the checkboxes in list rows; only a shared source stays consistent.
  const [picked, setPicked] = useState(() => new Set())
  const [delBusy, setDelBusy] = useState('')   // '' | 'picked' | 'all'
  const [armedAll, setArmedAll] = useState(false)

  // mirror params to the middleware: form is the single home of the parameter set and this useEffect is its only exit,
  // so there is no path where "the preset was dialed but analyze-on-send missed it".
  useEffect(() => {
    panel.form = form
  }, [form])

  // when the middleware starts a job with these params, echo the file name into the summary
  const [autoNote, setAutoNote] = useState(bus.last)
  useEffect(() => {
    const sink = v => setAutoNote(v)
    bus.subs.add(sink)
    return () => bus.subs.delete(sink)
  }, [])

  // one list query feeds three places: header summary/count, live job card, job list (React Query dedupes)
  const list = useQuery({
    queryKey: ['video-xray', 'jobs'],
    queryFn: () => api.rest('/jobs?limit=25'),
    refetchInterval: q =>
      (q.state.data?.jobs ?? []).some(j => LIVE.has(j.status)) ? 1500 : 4000
  })
  // Engine probe: /health reports which local transcription engines exist +
  // registered folders + CPU cap; drives the tick circles and CPU dropdown.
  const health = useQuery({
    queryKey: ['video-xray', 'health'],
    queryFn: () => api.rest('/health'),
    staleTime: 5000,
    refetchInterval: 6000
  })
  if (health.data?.asr) ASR_AVAIL = health.data.asr
  const jobs = list.data?.jobs ?? []
  const live = jobs.find(j => LIVE.has(j.status))
  const newest = jobs[0]
  // Cancel feedback: clicking cancel immediately flips that button into a
  // "cancelling" state so the click visibly landed. It clears once the ledger
  // projection shows the job left the live set (the engine honours the cancel
  // within ~1s); a 10s fuse guards against a wedged backend wedging the
  // button forever.
  const [cancelId, setCancelId] = useState('')
  useEffect(() => {
    if (!cancelId) return
    const j = jobs.find(x => x.id === cancelId)
    if (!j || !LIVE.has(j.status)) setCancelId('')
  }, [jobs, cancelId])
  useEffect(() => {
    if (!cancelId) return
    const fuse = setTimeout(() => setCancelId(''), 10000)
    return () => clearTimeout(fuse)
  }, [cancelId])
  // "Background" stashes the whole strip into one line: the live-job card,
  // task list and detail all fold away. The card reappears on its own the
  // moment the strip is opened again (params or task list), so the flag
  // needs no explicit reset path.
  const [liveHidden, setLiveHidden] = useState(false)
  const set = patch => setForm(f => ({ ...f, ...patch }))
  // 中间件捡到粘贴/拖进输入框的视频（+号的视频选项已拿掉，这是剩下的入口），把
  // 路径镜像进工作台：全插件视频路径只有这一个真源，参数弹窗的音轨下拉跟着对。
  panel.setPath = p => set({ path: String(p || '') })

  // params -> request body go through module-level buildBody: the panel and analyze-on-send share one assembly

  async function start() {
    setErr(null)
    const b = buildBody(form)
    if (!b.path) {
      setErr(t('panel.strip.need_path'))
      return
    }
    setBusy(true)
    // 引擎体检：模型+端口活着才准发起作业（缺这一步 = 往死端口上撞大运）
    const pf = await llmPreflight()
    if (pf) { setErr(pf); setBusy(false); return }
    host.notify({ kind: 'info', message: t('panel.strip.probe_ok', LLM_LIVE?.model ?? '?', LLM_LIVE?.provider ?? '') })
    try {
      const r = await api.rest('/analyze', { method: 'POST', body: b, timeoutMs: 20000 })
      saveForm(form)
      setSel(r?.id ?? null)
      setLiveHidden(false)
      setOpen(false)
      host.notify({ kind: 'info', message: t('panel.strip.started', r?.id ?? '') })
      queryClient.invalidateQueries({ queryKey: ['video-xray'] })
    } catch (e) {
      // the backend 400 detail is already human-readable (parameter validation info)
      setErr(String(e?.message ?? e).replace(/^HTTP \d+:\s*/, '').slice(0, 200))
    }
    setBusy(false)
  }

  async function pick() {
    try {
      const r = await api.os?.pickOpenPath?.({
        title: t('panel.strip.pick_title'),
        filters: [{ name: t('panel.strip.filter_video'), extensions: VIDEO_EXT }]
      })
      const p = typeof r === 'string' ? r : (r?.path ?? r?.filePath)
      if (p) set({ path: String(p) })
    } catch {
      /* selection cancelled */
    }
  }

  async function act(kind, id) {
    setErr(null)
    try {
      const r = await api.rest(`/jobs/${encodeURIComponent(id)}/${kind}`, {
        method: 'POST',
        timeoutMs: 20000
      })
      if (kind === 'cancel') setCancelId(id)
      if (kind === 'resume' && r?.id) setSel(r.id)
      queryClient.invalidateQueries({ queryKey: ['video-xray'] })
    } catch (e) {
      setErr(String(e?.message ?? e).replace(/^HTTP \d+:\s*/, '').slice(0, 200))
    }
  }

  // 'delete' removes exactly the ticked rows. Deletability is the backend's call: running jobs stay untouched and it returns
  // the human-readable refusal reasons in 'refused'; we only display them -- no duplicated state logic in the frontend.
  async function deletePicked() {
    const ids = [...picked]
    if (!ids.length || delBusy) return
    setErr(null)
    setDelBusy('picked')
    try {
      const r = await api.rest('/jobs/delete', { method: 'POST', body: { ids }, timeoutMs: 20000 })
      const deleted = r?.deleted ?? []
      const refused = r?.refused ?? []
      setPicked(s => {
        const next = new Set(s)
        deleted.forEach(id => next.delete(id))
        return next
      })
      if (deleted.includes(sel)) setSel(null)   // detail open on a deleted row -> collapse along; don't sit on a vanished ledger
      if (refused.length) {
        setErr(t('panel.strip.refused', refused.length, refused[0].reason ?? t('panel.strip.refused_reason')))
      } else {
        host.notify({ kind: 'info', message: t('panel.strip.deleted_n', deleted.length) })
      }
    } catch (e) {
      setErr(friendly(e))
    }
    setDelBusy('')
    queryClient.invalidateQueries({ queryKey: ['video-xray'] })
  }

  // 'delete all jobs' clears finished ones only; running/queued are skipped by the backend and listed in the receipt
  async function clearAll() {
    if (delBusy) return
    setErr(null)
    setDelBusy('all')
    setArmedAll(false)
    try {
      const r = await api.rest('/jobs/clear', { method: 'POST', timeoutMs: 20000 })
      setPicked(new Set())
      setSel(null)
      const skip = (r?.skipped_running ?? []).length
      const bad = (r?.failed ?? []).length
      host.notify({
        kind: bad ? 'warning' : 'info',
        message:
          t('panel.strip.cleared_all', r?.deleted ?? 0) +
          (skip ? t('panel.strip.cleared_skip', skip) : '') +
          (bad ? t('panel.strip.cleared_bad', bad) : '')
      })
    } catch (e) {
      setErr(friendly(e))
    }
    setDelBusy('')
    queryClient.invalidateQueries({ queryKey: ['video-xray'] })
  }

  // tail of design draft 5: when the latest finished job is shown and the UI is quiet, offer one gray hint line
  const doneLine =
    !live && newest?.status === 'done' && !tasks && !open
      ? t('panel.strip.done_line', baseName(newest.path))
      : null
  // While a job is running the collapsed single line speaks for itself:
  // gold "Analyzing · name 2/4" instead of the parameter summary.
  const runningLine = live
    ? t('panel.strip.running') + ' · ' + baseName(live.path || '') + ' ' + live.done + '/' + live.total
    : null

  const summary = [
    PRESET_LABEL[form.preset] ? t('panel.' + PRESET_LABEL[form.preset]) : form.preset,
    FRAME_LABEL[form.frames] ? t('panel.' + FRAME_LABEL[form.frames]) : form.frames,
    String(form.duration).trim() ? t('panel.strip.dur', form.duration) : t('panel.strip.whole'),
    t('panel.asr.' + asrResolved(form.transcribe, health.data?.asr)),
    autoNote
      ? t('panel.strip.auto_note', autoNote.name)
      : null
  ].filter(Boolean).join(' · ')
  // 主人 2026-11-09 定的两道门槛：①视频没进来=工作台参数区全灰（本地引擎登记/
  // 云端配置豁免，否则永远上传不了视频的死锁）②转录引擎没登记绿=「立即分析」点不动
  const vd = !String(form.path || '').trim()
  const readyEng = asrReady(form.transcribe, health.data && health.data.asr)

  return jsxs('div', {
    className: 'flex flex-col gap-1.5 border-b border-(--ui-stroke-secondary) pb-2',
    children: [
      // (1) collapsed title row: triangle + title + effective-parameter summary | engine line + job entry
      jsxs('div', {
        className: 'flex items-center gap-2 text-[0.75rem]',
        children: [
          jsxs('button', {
            type: 'button',
            onClick: () => setOpen(o => !o),
            className: 'flex min-w-0 flex-1 items-center gap-2 text-left',
            children: [
              jsx('span', {
                className: 'shrink-0 text-(--ui-accent)',
                children: open ? '▼' : '▶'
              }),
              jsx('span', { className: 'shrink-0', children: t('panel.strip.title') }),
              // 视频没进来时提示紧跟标题空几格亮金黄显示（主人 2026-11-09 点名要显眼），
              // 展开/收起两种状态都跟着标题走
              vd
                ? jsx('span', { className: 'ml-4 min-w-0 shrink truncate', style: { color: '#f6c945', fontWeight: 600 }, children: t('panel.strip.need_video_hint') })
                : null,
              open
                ? null
                : runningLine
                  ? jsx('span', {
                      className: 'min-w-0 flex-1 truncate text-[0.6875rem] font-semibold',
                      style: { color: '#f6c945' },
                      children: runningLine
                    })
                  : jsx('span', {
                      className: 'min-w-0 flex-1 truncate text-(--ui-accent)',
                      children: summary
                    })
            ]
          }),
          doneLine
            ? jsx('span', {
                className: 'min-w-0 shrink truncate text-[0.6875rem] text-(--ui-accent)',
                children: doneLine
              })
            : null,
          jsx('span', {
            className: 'shrink-0 text-[0.625rem] text-(--ui-accent)',
            children: t('panel.strip.brand', PRESETS.length)
          }),
          // the user-requested delete trio, right before 'collapse jobs' (design draft position)
          jsx(Button, {
            size: 'xs',
            variant: 'ghost',
            type: 'button',
            // select-all also expands the list, otherwise a row of ticks stays invisible
            onClick: () => {
              setTasks(true)
              setPicked(new Set(jobs.map(j => j.id)))
            },
            disabled: !jobs.length || delBusy !== '',
            children: t('panel.strip.select_all')
          }),
          jsx(Button, {
            size: 'xs',
            variant: 'destructive',
            type: 'button',
            disabled: !picked.size || delBusy !== '',
            onClick: deletePicked,
            children: delBusy === 'picked'
              ? t('panel.strip.deleting')
              : picked.size ? t('panel.strip.del_n', picked.size) : t('panel.strip.del')
          }),
          jsx(Button, {
            size: 'xs',
            // delete-all is irreversible: the first click only arms 'confirm', the second click really deletes
            variant: armedAll ? 'destructive' : 'ghost',
            type: 'button',
            disabled: delBusy !== '' || !jobs.some(j => !LIVE.has(j.status)),
            onClick: () => (armedAll ? clearAll() : setArmedAll(true)),
            children: delBusy === 'all'
              ? t('panel.strip.clearing')
              : armedAll ? t('panel.strip.confirm_all') : t('panel.strip.clear_all')
          }),
          jsx(Button, {
            size: 'xs',
            variant: 'ghost',
            type: 'button',
            onClick: () => setTasks(t => !t),
            children: tasks ? t('panel.strip.collapse_tasks') : `${t('panel.strip.show_tasks')}${jobs.length ? `(${jobs.length})` : ''}`
          })
        ]
      }),
      // (5) live job card (pinned on top while running)
      live && !(liveHidden && !open && !tasks)
        ? jsx(LiveCard, {
            job: live,
            onCancel: id => act('cancel', id),
            onBackground: () => {
              setLiveHidden(true)
              setTasks(false)
              setSel(null)
              setOpen(false)
            },
            cancelling: cancelId === live.id
          })
        : null,
      // (2) four-column parameter area (expanded and not running)
      open && !live
        ? jsxs('div', {
            className: 'grid grid-cols-4 gap-2',
            children: [
              jsx(SelectField, {
                label: t('panel.strip.label_preset'),
                value: form.preset,
                onChange: v => set({ preset: v }),
                options: PRESETS,
                disabled: vd
              }),
              jsx(Labeled, {
                label: t('panel.strip.label_dur'),
                child: jsx(Input, {
                  disabled: vd,
                  value: form.duration,
                  onChange: e => set({ duration: e.target.value }),
                  placeholder: t('panel.strip.ph_whole'),
                  className: 'h-7 text-[0.75rem]'
                })
              }),
              jsx(Labeled, {
                label: t('panel.strip.label_start'),
                child: jsx(Input, {
                  disabled: vd,
                  value: form.start,
                  onChange: e => set({ start: e.target.value }),
                  placeholder: '0',
                  className: 'h-7 text-[0.75rem]'
                })
              }),
              jsx(SelectField, {
                label: t('panel.strip.label_frames'),
                value: form.frames,
                onChange: v => set({ frames: v }),
                options: FRAMES,
                disabled: vd
              }),
              jsx(SelectField, {
                label: t('panel.strip.label_h3'),
                value: form.h3mode,
                onChange: v => set({ h3mode: v }),
                options: H3MODES,
                disabled: vd
              }),
              jsx(SelectField, {
                label: t('panel.strip.label_thresh'),
                value: form.thresh,
                onChange: v => set({ thresh: v }),
                options: THRESHOLDS,
                disabled: vd
              }),
              jsxs('label', {
                className: 'flex items-center gap-2 text-[0.75rem]',
                children: [
                  jsx(Checkbox, { checked: true, disabled: true, id: 'va-keep-progress' }),
                  jsx('span', {
                    className: 'text-(--ui-accent)',
                    children: t('panel.strip.keep_progress')
                  })
                ]
              }),
              jsx(Labeled, {
                label: t('panel.strip.auto_transcribe'),
                child: jsx(AsrRadios, { form, set, avail: health.data?.asr, settings: health.data?.asr_settings })
              }),
              jsx(AsrFooter, {
                avail: health.data?.asr,
                settings: health.data?.asr_settings,
                sel: form.transcribe
              }),
              jsx('div', {
                style: { gridColumn: '1 / -1' },
                children: jsx(Labeled, {
                  label: t('panel.strip.label_subs'),
                  child: jsx(Textarea, {
                    disabled: vd,
                    value: form.subtitles,
                    onChange: e => set({ subtitles: e.target.value }),
                    rows: 2,
                    placeholder: t('panel.strip.ph_subs'),
                    className: 'border-dashed text-[0.75rem]',
                    style: { resize: 'none' }
                  })
                })
              }),
              // design draft 4: auto-reading attachments needs the core frontend (plugins cannot see attachments),
              // so the native file picker stands in for now; the chosen absolute path auto-fills the input
              jsxs('div', {
                className: 'flex items-center gap-2',
                    style: { gridColumn: '1 / -1' },
                children: [
                  jsx(Input, {
                    // 主人 2026-11-09 定：路径只走「分析视频…」一道门——手工输入/粘贴全禁，
                    // 防打错字、防粘到不相干的旧路径引发分析报错
                    readOnly: true,
                    value: form.path,
                    onChange: e => set({ path: e.target.value }),
                    placeholder: t('panel.strip.ph_path'),
                    title: t('panel.strip.path_ro_tip'),
                    className: 'h-7 flex-1 text-[0.75rem]'
                  }),
                  jsx(Button, {
                    size: 'sm',
                    variant: 'ghost',
                    type: 'button',
                    onClick: pick,
                    children: t('panel.strip.browse')
                  }),
                  jsx(Button, {
                    size: 'sm',
                    type: 'button',
                    // 前提不齐就灰着：在跑 / 没视频 / 引擎没登记（hover 说清缺哪条）
                    disabled: busy || vd || !readyEng,
                    title: vd ? t('panel.strip.need_video_hint') : (!readyEng ? t('panel.strip.run_locked_engine') : ''),
                    onClick: start,
                    children: busy ? t('panel.strip.starting') : t('panel.strip.analyze_now')
                  })
                ]
              }),
              // analyze-on-send: a video pasted via the + button starts a job with the panel's parameters the moment send is clicked
              jsxs('label', {
                className: 'flex items-center gap-2 text-[0.75rem]',
                title: t('panel.strip.auto_send_tip'),
                children: [
                  jsx(Checkbox, {
                    checked: form.auto_send !== false,
                    disabled: vd,
                    onCheckedChange: v => set({ auto_send: v === true }),
                    id: 'va-auto-send'
                  }),
                  jsx('span', { children: t('panel.strip.auto_send') })
                ]
              }),
              err
                ? jsx('div', {
                    className: 'text-[0.625rem] text-(--ui-accent)',
                    style: { gridColumn: '1 / -1' },
                    children: `⚠ ${err}`
                  })
                : null
            ]
          })
        : null,
      // (7) job list + selected job detail
      tasks
        ? jsxs('div', {
            className: 'flex flex-col gap-2 border-t border-(--ui-stroke-secondary) pt-2',
            children: [
              jsx(JobList, {
                selected: sel,
                picked,
                onSelect: id => setSel(s => (s === id ? null : id)),
                onTogglePick: id =>
                  setPicked(s => {
                    const next = new Set(s)
                    if (next.has(id)) next.delete(id)
                    else next.add(id)
                    return next
                  }),
                onResume: id => act('resume', id)
              }),
              sel
                ? jsx(JobDetail, {
                    id: sel,
                    onCancel: id => act('cancel', id),
                    onResume: id => act('resume', id),
                    cancelling: cancelId === sel,
                    onClose: () => setSel(null)
                  })
                : null
            ]
          })
        : null,
      // even when the parameter area is collapsed, a failed start must stay visible
      err && !(open && !live)
        ? jsx('div', {
            className: 'text-[0.625rem] text-(--ui-accent)',
            children: `⚠ ${err}`
          })
        : null
    ]
  })
}

export default {
  id: ID,
  name: 'Video XRay',
  description: 'Video analysis strip above the composer: presets, transcription, subtitle injection, job resume',
  register(ctx) {
    api = ctx
    // the middleware must be able to start a job before the panel mounts (first frame, contribution not loaded):
    // seed persisted params first; once the panel mounts, real form state overwrites them (see the panel.form = form assignment).
    if (!panel.form) panel.form = loadForm()
    ctx.i18n?.register?.(B)
    // composer.top = the banner strip inside the composer card, above the input line (source: composer/contrib.ts)
    ctx.register({
      id: 'top',
      area: COMPOSER_AREAS.top,
      title: t('panel.strip.title'),
      order: 10,
      render: () => jsx(Strip, {})
    })
    // analyze-on-send: composer.middleware receives draft.attachments when send is clicked
    // (see composer/hooks/use-middleware-submit.ts); the bundled hermes-bots plugin
    // hangs its mention middleware the exact same way (plugins/hermes-bots/plugin.tsx)
    ctx.register({
      id: 'auto-send',
      area: COMPOSER_AREAS.middleware,
      title: t('panel.strip.auto_send'),
      order: 20,
      data: { handler: autoAnalyzeOnSend }
    })
  }
}