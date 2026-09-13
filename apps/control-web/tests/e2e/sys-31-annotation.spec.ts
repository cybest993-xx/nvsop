import { expect, test } from '@playwright/test'
import { readFileSync } from 'node:fs'

test('SYS-31-18 — real gateway loads and submits the NVIDIA React annotation UI', async ({
  page,
}) => {
  const baseURL = process.env.NVSOP_SYS31_E2E_URL
  const loginName = process.env.NVSOP_SYS31_E2E_LOGIN_NAME
  const passwordFile = process.env.NVSOP_SYS31_E2E_PASSWORD_FILE
  if (process.env.NVSOP_SYS31_ALLOW_MUTATION !== '1' || !baseURL || !loginName || !passwordFile) {
    test.skip(true, '需要显式允许写入的真实 Nginx/HTTPS 标注部署')
    return
  }

  const parsedURL = new URL(baseURL)
  expect(parsedURL.protocol).toBe('https:')
  const password = readFileSync(passwordFile, 'utf8').trim()
  await page.goto(`${baseURL}/login`)
  await page.getByLabel('登录名').fill(loginName)
  await page.getByLabel('密码').fill(password)
  await page.getByRole('button', { name: '登录' }).click()
  await expect(page.getByRole('banner')).toBeVisible()

  await page.goto(`${baseURL}/training-datasets`)
  await expect(page.getByRole('heading', { name: '训练数据集' })).toBeVisible()
  const enterAnnotation = page.getByRole('button', { name: '进入标注' }).first()
  await expect(enterAnnotation).toBeVisible()
  await enterAnnotation.click()
  const launchAnnotation = page.getByRole('link', { name: '进入 NVIDIA React 标注界面' })
  await expect(launchAnnotation).toBeVisible({ timeout: 180_000 })
  await launchAnnotation.click()

  await expect(page.getByRole('heading', { name: '动作标注' })).toBeVisible()
  const completionSlider = page.getByRole('slider', { name: '事件 1 完成时间' })
  await expect(completionSlider).toBeVisible()
  await completionSlider.focus()
  await expect(completionSlider).toBeFocused()
  await expect(page.getByRole('spinbutton', { name: '事件 1 完成时间（秒）' })).toBeVisible()
  const video = page.locator('.action-timestamp-editor video').first()
  await expect(video).toBeVisible()
  await expect
    .poll(async () => video.evaluate((element) => (element as HTMLVideoElement).readyState))
    .toBeGreaterThanOrEqual(1)

  const splitRequests: string[] = []
  page.on('request', (request) => {
    if (
      request.method() === 'POST' &&
      request.url().includes('/api/annotation/api/v1/videos/') &&
      request.url().endsWith('/split')
    ) {
      const body = request.postData()
      if (body !== null) splitRequests.push(body)
    }
  })

  await page.getByRole('button', { name: 'Submit Timestamp Data' }).click()
  await expect.poll(() => splitRequests.length).toBe(1)
  const firstSplit = splitRequests.at(0)
  if (firstSplit === undefined) throw new Error('标注请求未发送')
  const split = JSON.parse(firstSplit) as {
    timestamps: Array<{ start: number; end: number; actionIndex: number }>
    twoOperatorMode: boolean
  }
  expect(split).toEqual({
    timestamps: [
      {
        start: 0,
        end: expect.any(Number),
        actionIndex: 0,
        actionDescription: expect.any(String),
      },
    ],
    twoOperatorMode: false,
  })
  const firstTimestamp = split.timestamps.at(0)
  if (firstTimestamp === undefined) throw new Error('标注请求缺少时间段')
  expect(firstTimestamp.end).toBeGreaterThan(0)
  await expect(
    page.getByText(/Timestamps submitted and \d+ clips created successfully!/),
  ).toBeVisible({ timeout: 180_000 })
})
