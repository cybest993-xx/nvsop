import { expect, test } from '@playwright/test'

const SESSION = {
  user_id: '018f0000-0000-7000-8000-000000000001',
  login_name: 'wang.li',
  display_name: '王丽',
  expires_at: '2026-09-07T13:00:00Z',
  permissions: ['template.draft.view'],
}

test('SYS-22-07 — login is labelled, keyboard reachable, and visibly focused', async ({ page }) => {
  await page.route('**/api/v1/auth/session', async (route) => {
    await route.fulfill({
      status: 401,
      contentType: 'application/problem+json',
      body: JSON.stringify({ title: '请先登录', error_code: 'AUTHENTICATION_REQUIRED' }),
    })
  })

  await page.goto('/login')

  const loginName = page.getByRole('textbox', { name: '登录名' })
  const password = page.getByLabel('密码')
  await expect(loginName).toBeFocused()
  await page.keyboard.press('Tab')
  await expect(password).toBeFocused()
  await expect(password).toHaveCSS('outline-style', 'solid')
  await expect(password).toHaveCSS('outline-width', '2px')
  await expect(page.getByRole('button', { name: '登录' })).toBeVisible()
})

test('failed logout remains visibly signed in and reports the control-plane fault', async ({
  page,
}) => {
  await page.route('**/api/v1/auth/session', async (route) => {
    if (route.request().method() === 'DELETE') {
      await route.fulfill({
        status: 502,
        contentType: 'application/problem+json',
        body: JSON.stringify({
          type: 'about:blank',
          title: '请求未能完成，请稍后重试',
          status: 502,
          error_code: 'UPSTREAM_UNAVAILABLE',
        }),
      })
      return
    }
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(SESSION),
    })
  })

  await page.goto('/')
  await page.getByRole('button', { name: '退出' }).click()

  await expect(page.getByRole('alert')).toContainText('请求未能完成，请稍后重试')
  await expect(page.getByRole('banner').getByText('王丽')).toBeVisible()
  await expect(page).toHaveURL('/')
})

test('SYS-22-07 — protected layout fits the supported desktop width and names state', async ({
  page,
}) => {
  await page.route('**/api/v1/auth/session', async (route) => {
    await route.fulfill({
      status: 200,
      contentType: 'application/json',
      body: JSON.stringify(SESSION),
    })
  })

  await page.goto('/')

  const navigation = page.getByRole('navigation', { name: '主导航' })
  await expect(navigation).toBeVisible()
  await expect(navigation.getByText('工位与设备')).toHaveCount(0)
  await expect(navigation.getByRole('link', { name: 'SOP 模板' })).toBeVisible()
  await expect(page.getByRole('banner').getByText('王丽')).toBeVisible()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(
    true,
  )
})
