<script setup lang="ts">
import type { ConnectorView, StationView } from '@/api/controlPlane'
import { useSessionStore } from '@/session/store'
import BindingValidation from './BindingValidation.vue'
import CapabilityLedger from './CapabilityLedger.vue'
import PointRegister from './PointRegister.vue'
const props = withDefaults(
  defineProps<{ connectors?: ConnectorView[]; stations?: StationView[] }>(),
  { connectors: () => [], stations: () => [] },
)
const emit = defineEmits<{ changed: [] }>()
const session = useSessionStore()
const may = (permission: string): boolean => session.may(permission)
</script>

<template>
  <PointRegister
    :can-view="may('device.point.view')"
    :can-edit="may('device.point.edit')"
    :can-delete="may('device.point.delete')"
    :can-view-stations="may('device.station.view')"
    :can-view-connectors="may('device.connector.view')"
    :stations="props.stations"
    :connectors="props.connectors"
    @changed="emit('changed')"
  />
  <CapabilityLedger
    :can-view="may('device.connector.view')"
    :can-edit="may('device.connector.edit')"
    :connectors="props.connectors"
    @changed="emit('changed')"
  />
  <BindingValidation
    :can-view="may('device.point.view')"
    :can-view-stations="may('device.station.view')"
    :stations="props.stations"
  />
</template>
