import type { DatasetUploadInstructions } from '@/api/controlPlane'

export class ObjectUploadError extends Error {
  readonly status: number

  constructor(status: number) {
    super(`对象存储上传失败（HTTP ${status}）`)
    this.name = 'ObjectUploadError'
    this.status = status
  }
}

export type UploadProgressHandler = (percentage: number) => void

/**
 * 将浏览器选择的一个文件直接上传到对象存储。
 *
 * 这不是控制面请求：URL 和表单字段来自短期上传说明，浏览器不会把会话 Cookie 或 CSRF
 * 令牌转发给 MinIO。
 */
export function uploadVideoObject(
  instructions: DatasetUploadInstructions,
  file: File,
  onProgress: UploadProgressHandler,
): Promise<void> {
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest()
    const method = instructions.method.toUpperCase()
    if (method !== 'POST') {
      reject(new ObjectUploadError(0))
      return
    }

    request.open(method, instructions.url, true)
    request.withCredentials = false
    for (const [name, value] of Object.entries(instructions.headers)) {
      request.setRequestHeader(name, value)
    }

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
      reject(new ObjectUploadError(request.status))
    })
    request.addEventListener('error', () => reject(new ObjectUploadError(0)))
    request.addEventListener('abort', () => reject(new ObjectUploadError(0)))

    const form = new FormData()
    for (const [name, value] of Object.entries(instructions.fields)) {
      form.append(name, value)
    }
    form.append('file', file, file.name)
    request.send(form)
  })
}
