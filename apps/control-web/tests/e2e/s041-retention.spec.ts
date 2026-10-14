import { expect, test } from '@playwright/test'

import type {
  RetentionPolicyView,
  StationRetentionUpdate,
  StationRetentionView,
} from '../../src/api/controlPlane'

// 合成 HTTP 夹具只证明真实浏览器中的表单与传输；PG/CAS 另由 owner 集成用例证明。
const STATION_ID = '018f0000-0000-7000-8000-000000000002'
const POLICY: RetentionPolicyView = {
  evidence_pass: { mode: 'duration', seconds: 86400 },
  evidence_fail: { mode: 'permanent', seconds: null },
  evidence_indeterminate: { mode: 'duration', seconds: 86400 },
  record_detail: { mode: 'duration', seconds: 86400 },
  record_compression_age: { mode: 'duration', seconds: 3600 },
  record_aggregate_age: { mode: 'duration', seconds: 172800 },
  disposal: { mode: 'permanent', seconds: null },
  local_reported: { mode: 'duration', seconds: 259200 },
}

test('S041 — browser shows resolved groups, saves a complete group and reports a conflict', async ({
  page,
}) => {
  let station: StationRetentionView = {
    station_id: STATION_ID,
    revision: 0,
    mode: 'follow_global',
    default: POLICY,
    overrides: null,
    effective: POLICY,
  }
  const writes: { body: StationRetentionUpdate; revision: string | undefined }[] = []
  const unexpected: string[] = []
  let conflict = false

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    if (path === '/api/v1/auth/session') {
      await route.fulfill({
        json: {
          user_id: '018f0000-0000-7000-8000-000000000001',
          login_name: 'synthetic.operator',
          display_name: '合成操作员',
          expires_at: '2099-01-01T00:00:00Z',
          permissions: ['retention.policy.view', 'retention.policy.edit'],
        },
      })
    } else if (path === '/api/v1/retention/policy') {
      await route.fulfill({ json: { policy: POLICY, revision: 0 } })
    } else if (path === `/api/v1/retention/stations/${STATION_ID}`) {
      if (request.method() === 'PUT') {
        const body = request.postDataJSON() as StationRetentionUpdate
        writes.push({ body, revision: request.headers()['if-match'] })
        if (conflict) {
          await route.fulfill({
            status: 409,
            contentType: 'application/problem+json',
            body: JSON.stringify({
              status: 409,
              title: '保留策略已被他人修改，请刷新后重试',
              error_code: 'RETENTION_POLICY_CONFLICT',
            }),
          })
          return
        }
        station = {
          ...station,
          revision: station.revision + 1,
          mode: body.mode,
          overrides: body.overrides ?? null,
          effective: body.overrides ?? POLICY,
        }
      }
      await route.fulfill({ json: station })
    } else {
      unexpected.push(path)
      await route.fulfill({ status: 404, json: { detail: 'unexpected synthetic route' } })
    }
  })

  await page.goto('/devices')
  const panel = page.getByRole('region', { name: '保留策略', exact: true })
  await expect(page.getByRole('link', { name: '工位与设备', exact: true })).toBeVisible()
  await panel.getByRole('textbox', { name: '工位 ID', exact: true }).fill(STATION_ID)
  await panel.getByRole('button', { name: '读取工位保留组', exact: true }).click()
  const defaults = panel.getByRole('region', { name: '全局默认组', exact: true })
  const effective = panel.getByRole('region', { name: `工位 ${STATION_ID} 的覆盖与生效组` })
  await expect(defaults).toContainText('尚未设置')
  await expect(defaults.getByText('永久', { exact: true })).toHaveCount(2)
  await expect(effective).toContainText('整组等于全局默认组')

  await panel.getByRole('combobox', { name: '工位保留来源', exact: true }).press('ArrowDown')
  await page.getByRole('option', { name: '整组自定义', exact: true }).click()
  await expect(effective).toContainText('当前模式：整组跟随全局')
  await panel
    .getByRole('combobox', { name: '证据：通过类 保留方式', exact: true })
    .press('ArrowDown')
  await page.getByRole('option', { name: '显式不保留', exact: true }).click()
  await panel.getByRole('button', { name: '保存工位保留组', exact: true }).click()

  await expect.poll(() => writes.length).toBe(1)
  expect(writes[0]).toEqual({
    revision: '0',
    body: {
      mode: 'custom',
      overrides: { ...POLICY, evidence_pass: { mode: 'discard', seconds: null } },
    },
  })
  await expect(effective).toContainText('当前模式：整组自定义')
  await expect(effective).toContainText('显式不保留')
  await expect(defaults).not.toContainText('显式不保留')

  conflict = true
  await panel.getByRole('button', { name: '保存工位保留组', exact: true }).click()
  await expect(panel.getByRole('alert')).toContainText('保留策略已被他人修改')
  expect(writes[1]?.revision).toBe('1')
  expect(station.revision).toBe(1)
  expect(unexpected).toEqual([])
})
