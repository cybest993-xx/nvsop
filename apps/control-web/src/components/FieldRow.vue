<script setup lang="ts">
/**
 * A labelled form row.
 *
 * Naive UI's `NFormItem` renders its `<label>` without a `for` and sets no `aria-*`, so a field
 * built from it directly has no programmatic association between its label and its control — the
 * label is text a screen reader reads past, and the control has no accessible name. Q32 requires a
 * label per field, and the browser tests reach fields by name.
 *
 * This component owns that association so a field cannot be added without one: it derives one id
 * for the control and one for the label and hands both to the default slot. A control with an
 * inner input takes `id` through `:input-props` and is named by the label's `for`. A control that
 * renders no id-bearing element — `NSelect` without `filterable` is the case in this application —
 * takes `label-id` as `aria-labelledby` instead: the label's text is still the control's
 * accessible name, which is what `getByLabel` resolves. `useId()` gives ids unique within the
 * application, which is what lets two fields in one form both be labelled 名称.
 *
 * Usage:
 *
 *   <FieldRow label="登录名" :feedback="error" :status="error ? 'error' : undefined">
 *     <template #default="{ id }">
 *       <NInput v-model:value="name" :input-props="{ id, name: 'login_name' }" />
 *     </template>
 *   </FieldRow>
 *
 *   <FieldRow label="连接器类型">
 *     <template #default="{ labelId }">
 *       <NSelect v-model:value="kind" :aria-labelledby="labelId" :options="options" />
 *     </template>
 *   </FieldRow>
 */
import { NFormItem } from 'naive-ui'
import { useId } from 'vue'

defineProps<{
  label: string
  /** The field's rejection text. Naive UI shows feedback only alongside a status. */
  feedback?: string
  status?: 'error' | 'warning' | 'success'
}>()

const controlId = useId()
const labelId = useId()
</script>

<template>
  <NFormItem
    :label="label"
    :label-props="{ for: controlId, id: labelId }"
    :validation-status="status"
    :feedback="feedback"
  >
    <slot :id="controlId" :label-id="labelId" />
  </NFormItem>
</template>
