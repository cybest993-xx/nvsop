// 由 scripts/generate_reason_codes.py 从 packages/contracts/openapi.json 生成。
// 请勿手改。原因码定义权在判定核心 edge_runtime.judgment.reasons.ReasonCode。

export const REASON_CODE_HINTS: Readonly<Record<string, string>> = {
  ACTION_ID_UNKNOWN: '动作编号不在模板中',
  CHUNK_BACKLOG_EXCEEDED: '观测积压超限',
  DEADLINE_EXCEEDED: '步骤超时',
  INFERENCE_BACKEND_UNREACHABLE: '推理后端不可达',
  INFERENCE_HOST_DOWN: '推理机失联',
  INFERENCE_TIMEOUT: '推理超时',
  IO_SIGNAL_LOST: '外部信号不可达',
  IO_TIME_UNALIGNED: '外部信号时间未对齐',
  MISSED_STEP: '缺少必做步骤',
  OUT_OF_ORDER: '步骤顺序错误',
  RUN_INTERRUPTED: '运行被中断。配置切换、进程重启、本地暂停或显式终止。',
  STORAGE_UNAVAILABLE: '边缘存储不可用或超过安全水位',
  STREAM_LOST: '观测流中断',
  TIMESTAMP_DISCONTINUITY: '时间轴不连续',
  WRONG_STEP: '出现了错误步骤',
}
