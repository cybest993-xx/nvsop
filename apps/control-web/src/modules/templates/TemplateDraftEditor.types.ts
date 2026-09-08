import type { TemplateDraftView } from '@/api/controlPlane'

export interface DraftEditorTarget {
  draft: TemplateDraftView | null
  id: string
  revision: string
}
