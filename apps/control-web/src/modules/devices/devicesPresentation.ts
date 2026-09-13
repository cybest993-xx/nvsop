import type { DeviceStatus } from '@/api/controlPlane'

export function connectorTypeLabel(type: string): string {
  switch (type) {
    case 'hikvision_isapi':
      return '海康 ISAPI'
    case 'board_card':
      return '板卡连接器'
    default:
      return `未知连接器类型（${type}）`
  }
}

export interface ReachabilityPresentation {
  label: string
  tag: 'success' | 'danger' | 'warning'
}

export function reachabilityPresentation(value: string): ReachabilityPresentation {
  switch (value) {
    case 'reachable':
      return { label: '可达', tag: 'success' }
    case 'unreachable':
      return { label: '不可达', tag: 'danger' }
    case 'unverified':
      return { label: '未验证', tag: 'warning' }
    default:
      return { label: `未知连接状态（${value}）`, tag: 'warning' }
  }
}

export interface StatusPresentation {
  label: string
  tag: 'success' | 'info' | 'warning'
  action: { next: DeviceStatus; label: string; successMessage: string } | null
}

export function statusPresentation(status: string): StatusPresentation {
  switch (status) {
    case 'active':
      return {
        label: '在用',
        tag: 'success',
        action: { next: 'deactivated', label: '停用', successMessage: '连接器已停用' },
      }
    case 'deactivated':
      return {
        label: '已停用',
        tag: 'info',
        action: { next: 'active', label: '恢复', successMessage: '连接器已恢复' },
      }
    default:
      return { label: `未知状态（${status}）`, tag: 'warning', action: null }
  }
}
