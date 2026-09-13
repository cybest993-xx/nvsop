import ElementPlus from 'element-plus'
import { createPinia, setActivePinia } from 'pinia'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

import { ControlPlaneError } from '@/api/controlPlane'
import TemplateManagement from '@/modules/templates/TemplateManagement.vue'
import { useSessionStore } from '@/session/store'

const api = vi.hoisted(() => ({
  readTemplateDrafts: vi.fn(),
  readTemplateImports: vi.fn(),
  downloadTemplateImport: vi.fn(),
  importTemplateDraft: vi.fn(),
  editTemplateDraft: vi.fn(),
  publishTemplateVersion: vi.fn(),
  readTemplateVersions: vi.fn(),
  downloadTemplateVersionArtifact: vi.fn(),
}))

vi.mock('@/api/controlPlane', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/controlPlane')>()),
  ...api,
}))

const DRAFT_STEPS = [
  { number: 1, name: '取料', description: '(1)取料' },
  { number: 2, name: '安装', description: '(2)安装' },
]

const DRAFT = {
  id: 'draft-1',
  template_id: 'template-1',
  source_import_id: 'import-1',
  station_id: 'station-1',
  station_code: 'A-001',
  station_name: '装配一号工位',
  steps: DRAFT_STEPS,
  ordering: 'strict' as const,
  runtime_defaults: {
    idle_timeout_seconds: null,
    step_deadline_seconds: null,
    disposition_policy: null,
  },
  revision: 3,
  created_by: 'operator-1',
  updated_by: 'operator-1',
  created_at: '2026-09-08T01:00:00Z',
  updated_at: '2026-09-08T01:00:00Z',
}

const VERSION = {
  id: 'version-1',
  template_id: 'template-1',
  source_import_id: 'import-1',
  source_draft_id: 'draft-1',
  source_draft_revision: 3,
  steps: DRAFT_STEPS,
  ordering: 'strict' as const,
  start_signal: { kind: 'action' as const, action_number: 1 },
  end_signals: [],
  runtime_defaults: {
    idle_timeout_seconds: 30,
    step_deadline_seconds: 90,
    disposition_policy: 'record',
  },
  artifacts: [
    {
      name: 'actions.json',
      media_type: 'application/json',
      byte_length: 37,
      sha256: 'b'.repeat(64),
    },
    {
      name: 'vlm_prompts.txt',
      media_type: 'text/plain',
      byte_length: 143,
      sha256: 'c'.repeat(64),
    },
    {
      name: 'template.json',
      media_type: 'application/json',
      byte_length: 349,
      sha256: 'd'.repeat(64),
    },
    {
      name: 'manifest.json',
      media_type: 'application/json',
      byte_length: 481,
      sha256: 'e'.repeat(64),
    },
  ],
  sha256: 'f'.repeat(64),
  published_by: 'operator-1',
  published_at: '2026-09-08T01:10:00Z',
}

const IMPORT = {
  id: 'import-1',
  filename: '装配一号.xlsx',
  content_type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
  sha256: 'a'.repeat(64),
  status: 'succeeded' as const,
  errors: [],
  imported_by: 'operator-1',
  imported_at: '2026-09-08T01:00:00Z',
}

function grant(...permissions: string[]) {
  useSessionStore().current = {
    user_id: 'operator-1',
    login_name: 'operator',
    display_name: '配置人员',
    expires_at: '2026-09-08T13:00:00Z',
    permissions,
  }
}

async function chooseFile(wrapper: ReturnType<typeof mount>, file: File): Promise<void> {
  const input = wrapper.find('input[type="file"]')
  Object.defineProperty(input.element, 'files', { configurable: true, value: [file] })
  await input.trigger('change')
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  api.readTemplateDrafts.mockResolvedValue({ items: [DRAFT], page: 1, page_size: 50, total: 1 })
  api.readTemplateImports.mockResolvedValue({ items: [IMPORT], page: 1, page_size: 50, total: 1 })
  api.readTemplateVersions.mockResolvedValue({ items: [VERSION], page: 1, page_size: 50, total: 1 })
  api.downloadTemplateImport.mockResolvedValue(new Blob(['synthetic workbook']))
  api.downloadTemplateVersionArtifact.mockResolvedValue(new Blob(['version artifact']))
  api.publishTemplateVersion.mockResolvedValue(VERSION)
})

describe('SOP 模板工作台', () => {
  it('loads drafts and import history for a viewer without showing edit controls', async () => {
    grant('template.draft.view')

    const wrapper = mount(TemplateManagement, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    expect(api.readTemplateDrafts).toHaveBeenCalledOnce()
    expect(api.readTemplateImports).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('装配一号工位')
    expect(wrapper.text()).toContain('2 个步骤')
    expect(wrapper.text()).toContain('装配一号.xlsx')
    expect(wrapper.find('input[type="file"]').exists()).toBe(false)
    expect(wrapper.findAll('button').map((button) => button.text())).not.toContain('编辑草稿')
  })

  it('downloads the retained original workbook through the control-plane adapter', async () => {
    grant('template.draft.view')
    const createObjectUrl = vi.fn(() => 'blob:template')
    Object.defineProperty(URL, 'createObjectURL', {
      configurable: true,
      value: createObjectUrl,
    })
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined)
    const wrapper = mount(TemplateManagement, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '下载原文件')!
      .trigger('click')
    await flushPromises()

    expect(api.downloadTemplateImport).toHaveBeenCalledWith('import-1')
    expect(createObjectUrl).toHaveBeenCalledOnce()
    click.mockRestore()
  })

  it('imports a selected workbook and leaves secret-like controls out of the workbench', async () => {
    grant('template.draft.view', 'template.draft.edit')
    api.importTemplateDraft.mockResolvedValue({ import_record: IMPORT, draft: DRAFT })
    const wrapper = mount(TemplateManagement, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    const file = new File(['synthetic workbook'], '现场模板.xlsx', {
      type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    })
    await chooseFile(wrapper, file)
    await flushPromises()

    expect(api.importTemplateDraft).toHaveBeenCalledWith(file, '现场模板.xlsx')
    expect(wrapper.text()).not.toContain('密码')
    expect(wrapper.text()).not.toContain('测试连接')
  })

  it('shows a field-located import refusal beside the import result', async () => {
    grant('template.draft.edit')
    api.importTemplateDraft.mockRejectedValue(
      new ControlPlaneError({
        message: 'Excel 工作簿校验失败',
        errorCode: 'TEMPLATE_IMPORT_INVALID',
        status: 422,
        fieldErrors: [{ field: '步骤表[3].步骤描述', message: '必须符合基座动作编码格式' }],
      }),
    )
    const wrapper = mount(TemplateManagement, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    const file = new File(['bad workbook'], '错误模板.xlsx', { type: 'application/octet-stream' })
    await chooseFile(wrapper, file)
    await flushPromises()

    expect(wrapper.find('[role="alert"]').text()).toContain('Excel 工作簿校验失败')
    expect(wrapper.text()).toContain('步骤表[3].步骤描述：必须符合基座动作编码格式')
  })

  it('publishes a draft and shows the immutable version history', async () => {
    grant('template.draft.view', 'template.draft.edit')
    const wrapper = mount(TemplateManagement, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '发布版本')!
      .trigger('click')
    await flushPromises()

    expect(api.publishTemplateVersion).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('确认发布')
    expect(wrapper.text()).toContain('将发布草稿修订 3')

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '确认发布')!
      .trigger('click')
    await flushPromises()

    expect(api.publishTemplateVersion).toHaveBeenCalledWith('draft-1', 3)
    expect(api.readTemplateVersions).toHaveBeenCalledTimes(2)
    expect(wrapper.text()).toContain('version-1')
    expect(wrapper.text()).toContain('f'.repeat(64))
    expect(wrapper.text()).toContain('2026年9月8日 09:10')
    expect(wrapper.text()).toContain('来源导入记录：import-1')
    expect(wrapper.text()).toContain('发布人：operator-1')
    expect(wrapper.text()).toContain('actions.json')

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '查看详情')!
      .trigger('click')
    expect(wrapper.text()).toContain('版本详情')
    expect(wrapper.text()).toContain('模板版本发布后不可修改')
  })

  it('renders a safe label when a boundary signal is missing its payload', async () => {
    grant('template.draft.view')
    api.readTemplateVersions.mockResolvedValueOnce({
      items: [
        {
          ...VERSION,
          start_signal: { kind: 'action' },
          end_signals: [{ kind: 'external' }],
        },
      ],
      page: 1,
      page_size: 50,
      total: 1,
    })

    const wrapper = mount(TemplateManagement, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    expect(wrapper.text()).toContain('开始：动作（未知编号）')
    expect(wrapper.text()).toContain('结束：外部：未知标签')
    expect(wrapper.text()).not.toContain('undefined')
  })

  it('downloads a published artifact through the control-plane adapter', async () => {
    vi.useFakeTimers()
    try {
      grant('template.draft.view')
      const createObjectUrl = vi.fn(() => 'blob:version')
      const revokeObjectUrl = vi.fn()
      Object.defineProperty(URL, 'createObjectURL', {
        configurable: true,
        value: createObjectUrl,
      })
      Object.defineProperty(URL, 'revokeObjectURL', {
        configurable: true,
        value: revokeObjectUrl,
      })
      const click = vi
        .spyOn(HTMLAnchorElement.prototype, 'click')
        .mockImplementation(() => undefined)
      const wrapper = mount(TemplateManagement, { global: { plugins: [ElementPlus] } })
      await flushPromises()

      await wrapper
        .findAll('button')
        .find((button) => button.text() === '下载 actions.json')!
        .trigger('click')
      await flushPromises()

      expect(api.downloadTemplateVersionArtifact).toHaveBeenCalledWith('version-1', 'actions.json')
      expect(createObjectUrl).toHaveBeenCalledOnce()
      expect(revokeObjectUrl).not.toHaveBeenCalled()
      vi.runAllTimers()
      expect(revokeObjectUrl).toHaveBeenCalledWith('blob:version')
      click.mockRestore()
    } finally {
      vi.useRealTimers()
    }
  })

  it('saves explicitly declared boundary fields with a draft', async () => {
    grant('template.draft.view', 'template.draft.edit')
    const wrapper = mount(TemplateManagement, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '编辑草稿')!
      .trigger('click')
    expect(wrapper.text()).toContain('仅使用动作号作为开始信号时，无法确认起始动作是否漏记')
    await wrapper.find('input[name="start-signal-declared"]').setValue(true)
    await wrapper.find('input[name="end-signals-declared"]').setValue(true)
    await wrapper.find('.template-management__edit-form').trigger('submit')
    await flushPromises()

    expect(api.editTemplateDraft).toHaveBeenCalledWith(
      'draft-1',
      {
        steps: DRAFT_STEPS,
        ordering: 'strict',
        runtime_defaults: {
          idle_timeout_seconds: null,
          step_deadline_seconds: null,
          disposition_policy: null,
        },
        start_signal: { kind: 'action', action_number: 1 },
        end_signals: [],
      },
      3,
    )
  })

  it('shows a publication refusal without claiming that a version exists', async () => {
    grant('template.draft.view', 'template.draft.edit')
    api.publishTemplateVersion.mockRejectedValue(
      new ControlPlaneError({
        message: '模板版本发布校验失败',
        errorCode: 'TEMPLATE_VERSION_INVALID',
        status: 422,
        fieldErrors: [{ field: '草稿.start_signal', message: '必须声明开始信号' }],
      }),
    )
    const wrapper = mount(TemplateManagement, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '发布版本')!
      .trigger('click')
    await wrapper
      .findAll('button')
      .find((button) => button.text() === '确认发布')!
      .trigger('click')
    await flushPromises()

    expect(wrapper.find('[role="alert"]').text()).toContain('模板版本发布校验失败')
    expect(wrapper.text()).toContain('草稿.start_signal：必须声明开始信号')
  })

  it('edits a draft with the revision it read', async () => {
    grant('template.draft.view', 'template.draft.edit')
    api.editTemplateDraft.mockResolvedValue({ ...DRAFT, revision: 4 })
    const wrapper = mount(TemplateManagement, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '编辑草稿')!
      .trigger('click')
    await wrapper.find('input[name="step-name-1"]').setValue('安装确认')
    await wrapper.find('.template-management__edit-form').trigger('submit')
    await flushPromises()

    expect(api.editTemplateDraft).toHaveBeenCalledWith(
      'draft-1',
      {
        steps: [
          { number: 1, name: '取料', description: '(1)取料' },
          { number: 2, name: '安装确认', description: '(2)安装确认' },
        ],
        ordering: 'strict',
        runtime_defaults: {
          idle_timeout_seconds: null,
          step_deadline_seconds: null,
          disposition_policy: null,
        },
      },
      3,
    )
  })

  it('announces an expired revision instead of hiding the concurrent edit', async () => {
    grant('template.draft.view', 'template.draft.edit')
    api.editTemplateDraft.mockRejectedValue(
      new ControlPlaneError({
        message: '模板草稿已被他人修改，请刷新后重试',
        errorCode: 'STALE_REVISION',
        status: 409,
      }),
    )
    const wrapper = mount(TemplateManagement, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    await wrapper
      .findAll('button')
      .find((button) => button.text() === '编辑草稿')!
      .trigger('click')
    await wrapper.find('.template-management__edit-form').trigger('submit')
    await flushPromises()

    expect(wrapper.find('[role="alert"]').text()).toContain('列表已刷新；请重新打开并核对后重试')
    expect(api.readTemplateDrafts).toHaveBeenCalledTimes(2)
  })

  it('does not list resources for an edit-only caller and offers known-id editing', async () => {
    grant('template.draft.edit')

    const wrapper = mount(TemplateManagement, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    expect(api.readTemplateDrafts).not.toHaveBeenCalled()
    expect(api.readTemplateImports).not.toHaveBeenCalled()
    expect(wrapper.text()).toContain('当前没有模板查看权限')
    expect(wrapper.text()).toContain('按标识编辑')
  })

  it('keeps an unknown ordering value raw and disables unsafe editing', async () => {
    grant('template.draft.view', 'template.draft.edit')
    api.readTemplateDrafts.mockResolvedValue({
      items: [{ ...DRAFT, ordering: 'future_ordering' as never }],
      page: 1,
      page_size: 50,
      total: 1,
    })

    const wrapper = mount(TemplateManagement, { global: { plugins: [ElementPlus] } })
    await flushPromises()

    expect(wrapper.text()).toContain('未知顺序声明（future_ordering）')
    expect(wrapper.findAll('button').map((button) => button.text())).not.toContain('编辑草稿')
    expect(wrapper.text()).toContain('未知值，不能编辑')
  })
})
