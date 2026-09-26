import type { DatasetUploadInstructions } from '@/api/controlPlane'

const CSRF_COOKIE = 'sop_csrf'
const CSRF_HEADER = 'x-csrf-token'

export class ObjectUploadError extends Error {
  readonly status: number

  constructor(status: number, detail?: string) {
    super(detail ? `视频上传失败（HTTP ${status}）：${detail}` : `视频上传失败（HTTP ${status}）`)
    this.name = 'ObjectUploadError'
    this.status = status
  }
}

export type UploadProgressHandler = (percentage: number) => void

function csrfToken(): string | null {
  const match = document.cookie.split('; ').find((entry) => entry.startsWith(`${CSRF_COOKIE}=`))
  return match ? decodeURIComponent(match.slice(CSRF_COOKIE.length + 1)) : null
}

/**
 * 把浏览器选择的一个文件流式上传到中心正式入口。
 *
 * URL 与请求头来自短期上传说明，指向同一控制面；请求携带会话 Cookie 和 CSRF 双提交头，
 * 浏览器不再把视频直接发往独立对象存储，也不会把视频内容读进页面内存。
 */
export function uploadVideoObject(
  instructions: DatasetUploadInstructions,
  file: File,
  onProgress: UploadProgressHandler,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const method = instructions.method.toUpperCase()
    if (method !== 'PUT') {
      reject(new ObjectUploadError(0))
      return
    }

    const request = new XMLHttpRequest()
    const url = new URL(instructions.url, window.location.origin)
    request.open(method, url.toString(), true)
    request.withCredentials = true

    const headers = new Headers(instructions.headers)
    const token = csrfToken()
    if (token !== null) {
      headers.set(CSRF_HEADER, token)
    }
    headers.forEach((value, name) => request.setRequestHeader(name, value))

    request.upload.addEventListener('progress', (event) => {
      if (!event.lengthComputable) {
        return
      }
      const percentage = Math.min(100, Math.round((event.loaded / event.total) * 100))
      onProgress(percentage)
    })
    request.addEventListener('load', () => {
      if (request.status >= 200 && request.status < 300) {
        onProgress(100)
        resolve()
        return
      }
      reject(new ObjectUploadError(request.status, request.responseText))
    })
    request.addEventListener('error', () => reject(new ObjectUploadError(0)))
    request.addEventListener('abort', () => reject(new ObjectUploadError(0)))

    request.send(file)
  })
}
