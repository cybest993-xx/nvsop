import { expect, test } from '@playwright/test'

import type {
  RetentionImpactEstimateView,
  RetentionPolicyView,
  StationRetentionUpdate,
  StationRetentionView,
} from '../../src/api/controlPlane'

// 合成 HTTP 夹具只证明真实浏览器中的表单、估算展示与传输；PG/owner 计数另由集成用例证明。
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

const IMPACT: RetentionImpactEstimateView = {
  baseline_revision: 0,
  proposed: POLICY,
  station_id: null,
  complete: true,
  incomplete_reasons: [],
  fields: [
    {
      field: 'evidence_pass',
      owner: 'evidence' as const,
      category: 'pass' as const,
      removes_objects: true,
      unavailable_reason: null,
      savings_unknown: false,
      bytes_basis: null,
      source_configurations: [],
      exposed_from: '2026-07-16T00:00:00Z',
      exposed_to: '2026-09-14T00:00:00Z',
      exposed: {
        objects: 3,
        known_bytes: 2048,
        unknown_size_objects: 0,
        earliest: '2026-08-01T00:00:00Z',
        latest: '2026-09-01T00:00:00Z',
      },
      protected: {
        objects: 1,
        known_bytes: 1024,
        unknown_size_objects: 0,
        earliest: '2026-08-05T00:00:00Z',
        latest: '2026-08-05T00:00:00Z',
      },
    },
  ],
  reference_time: '2026-10-14T00:00:00Z',
  estimate_token: 'token-1',
}

test('S042 — browser shows the impact estimate, then confirms or cancels a shortening', async ({
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
  const writes: {
    body: StationRetentionUpdate
    revision: string | undefined
    token: string | undefined
  }[] = []
  const unexpected: string[] = []
  let conflict = false
  let incomplete = false
  let estimateCalls = 0

  await page.route('**/api/v1/**', async (route) => {
    const request = route.request()
    const path = new URL(request.url()).pathname
    const method = request.method()
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
    } else if (
      path === `/api/v1/retention/stations/${STATION_ID}/impact-estimate` &&
      method === 'POST'
    ) {
      estimateCalls += 1
      await route.fulfill({
        json: {
          ...IMPACT,
          baseline_revision: station.revision,
          complete: !incomplete,
          incomplete_reasons: incomplete ? ['local_reported:edge_estimate_refused'] : [],
          fields: incomplete
            ? [
                {
                  ...IMPACT.fields[0]!,
                  field: 'local_reported',
                  owner: 'edge',
                  exposed: null,
                  protected: null,
                  unavailable_reason: 'edge_estimate_refused',
                  bytes_basis: 'serialized_rows',
                  source_configurations: [
                    {
                      host_id: 'source-fixture',
                      configuration_revision: 7,
                      configuration_sha256: 'a'.repeat(64),
                    },
                  ],
                },
              ]
            : IMPACT.fields,
        },
      })
    } else if (path === `/api/v1/retention/stations/${STATION_ID}`) {
      if (method === 'PUT') {
        const body = request.postDataJSON() as StationRetentionUpdate
        writes.push({
          body,
          revision: request.headers()['if-match'],
          token: request.headers()['x-retention-estimate'],
        })
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

  // 原生 select：combobox 与 option 语义来自浏览器本身，Playwright 用 selectOption 驱动
  // （原生下拉的 option 不参与页面点击）。
  await panel
    .getByRole('combobox', { name: '工位保留来源', exact: true })
    .selectOption({ label: '整组自定义' })
  await expect(effective).toContainText('当前模式：整组跟随全局')
  await panel
    .getByRole('combobox', { name: '证据：通过类 保留方式', exact: true })
    .selectOption({ label: '显式不保留' })

  // 取消：展示影响估算后取消，既不提交也不改变数据。
  await panel.getByRole('button', { name: '查看影响估算', exact: true }).click()
  const impact = panel.locator('[aria-label="工位保留影响估算"]')
  await expect(impact).toContainText('新暴露 3 个对象')
  await expect(impact).toContainText('引用保护跳过 1 个')
  await expect(impact).toContainText('证据：通过类')
  await panel.getByRole('button', { name: '取消', exact: true }).click()
  await expect(impact).toHaveCount(0)
  expect(writes).toHaveLength(0)

  // 确认：带回估算令牌后才提交，并只改变后续回收依据。
  await panel.getByRole('button', { name: '查看影响估算', exact: true }).click()
  await panel.getByRole('button', { name: '确认保存工位保留组', exact: true }).click()

  await expect.poll(() => writes.length).toBe(1)
  expect(writes[0]).toEqual({
    revision: '0',
    token: 'token-1',
    body: {
      mode: 'custom',
      overrides: { ...POLICY, evidence_pass: { mode: 'discard', seconds: null } },
    },
  })
  await expect(effective).toContainText('当前模式：整组自定义')
  await expect(effective).toContainText('显式不保留')
  await expect(defaults).not.toContainText('显式不保留')

  // 基准变化：确认被拒绝而不是盲签成功。
  conflict = true
  await panel.getByRole('button', { name: '查看影响估算', exact: true }).click()
  await panel.getByRole('button', { name: '确认保存工位保留组', exact: true }).click()
  await expect(panel.getByRole('alert')).toContainText('保留策略已被他人修改')
  expect(writes[1]?.revision).toBe('1')
  expect(station.revision).toBe(1)
  expect(estimateCalls).toBe(3)
  incomplete = true
  await panel.getByRole('button', { name: '查看影响估算', exact: true }).click()
  await expect(impact).toContainText('估算不完整，不能确认')
  await expect(impact).toContainText('edge_estimate_refused')
  await expect(impact).toContainText('source-fixture')
  await expect(impact).toContainText('逻辑序列化字节，不代表可回收磁盘空间')
  await expect(
    panel.getByRole('button', { name: '确认保存工位保留组', exact: true }),
  ).toBeDisabled()
  await panel.getByRole('button', { name: '取消', exact: true }).click()
  expect(writes).toHaveLength(2)
  expect(station.revision).toBe(1)
  expect(unexpected).toEqual([])
})
