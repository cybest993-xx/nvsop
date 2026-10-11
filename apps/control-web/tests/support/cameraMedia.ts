import type { CameraMediaView } from '@/api/controlPlane'

/**
 * 相机媒体元数据的共享夹具。
 *
 * `camera_id` 与 `media_path` 由 `cameraId` 派生，`camera_name` 由调用方给定，其余字段可用
 * `overrides` 覆盖。集成测试与 e2e 测试共用这一份 21 字段定义，避免手工拷贝后漂移。
 */
export function cameraMedia(
  cameraId: string,
  cameraName: string,
  overrides: Partial<CameraMediaView> = {},
): CameraMediaView {
  return {
    backend_id: 'backend-1',
    camera_address: '10.0.8.21',
    camera_id: cameraId,
    camera_name: cameraName,
    camera_revision: 2,
    camera_status: 'active',
    credentials_configured: false,
    host_id: 'host-1',
    host_name: '推理机 A',
    host_status: 'active',
    main_stream_path: '/Streaming/Channels/101',
    media_path: cameraId,
    media_path_mode: 'passthrough',
    mediamtx_address: 'https://media.example.test:8889',
    mediamtx_playback_address: 'https://media.example.test:9996',
    recording_mode: 'continuous',
    recording_window_seconds: 3600,
    station_id: 'station-1',
    station_name: '一号装配工位',
    station_status: 'active',
    sub_stream_path: '/Streaming/Channels/102',
    ...overrides,
  }
}
