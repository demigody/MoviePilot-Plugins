import { createApp } from 'vue'
import { createVuetify } from 'vuetify'
import * as components from 'vuetify/components'
import * as directives from 'vuetify/directives'
import 'vuetify/styles'
import Page from './Page.vue'

const vuetify = createVuetify({ components, directives })
createApp(Page).use(vuetify).mount('#app')
