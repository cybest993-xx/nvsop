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
}))

vi.mock('@/api/controlPlane', async (importOriginal) => ({
  ...(await importOriginal<typeof import('@/api/controlPlane')>()),
  ...api,
}))

const DRAFT = {
  id: 'draft-1',
  template_id: 'template-1',
  source_import_id: 'import-1',
  station_id: 'station-1',
  station_code: 'A-001',
  station_name: '装配一号工位',
  steps: [
    { number: 1, name: '取料', description: '(1)取料' },
    { number: 2, name: '安装', description: '(2)安装' },
  ],
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
  api.downloadTemplateImport.mockResolvedValue(new Blob(['synthetic workbook']))
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

    expect(wrapper.find('[role="alert"]').text()).toContain('请刷新后重试')
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
