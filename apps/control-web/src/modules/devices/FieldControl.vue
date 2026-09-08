<script setup lang="ts">
import { computed } from 'vue'

export type FieldOption = readonly [string, string]
export type FieldProps = {
  id: string
  fieldKey?: string
  label: string
  ariaLabel?: string
  options?: readonly FieldOption[]
  required?: boolean
  placeholder?: string
  type?: string
  min?: number
}
const props = defineProps<FieldProps>()
const common = computed(() => ({
  id: props.id,
  name: props.id,
  'aria-label': props.ariaLabel,
  required: props.required,
}))
const model = defineModel<string | number>({ required: true })
</script>

<template>
  <label :for="id">
    {{ label }}
    <select v-if="options" v-bind="common" v-model="model">
      <option value="">请选择{{ label }}</option>
      <option v-for="option in options" :key="option[0]" :value="option[0]">{{ option[1] }}</option>
    </select>
    <input
      v-else
      v-bind="common"
      v-model="model"
      :type="type"
      :min="min"
      :placeholder="placeholder"
    />
  </label>
</template>
