import { createRouter, createWebHistory } from 'vue-router'

import Overview from '@/views/Overview.vue'
import ProxySwitchView from '@/views/ProxySwitchView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    { path: '/proxy-workbench', name: 'proxy-workbench', component: ProxySwitchView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
