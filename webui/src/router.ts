import { createRouter, createWebHashHistory } from 'vue-router'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/chat' },
    { path: '/login', name: 'Login', component: () => import('./views/Login.vue') },
    { path: '/chat', name: 'Chat', component: () => import('./views/Chat.vue') },
    { path: '/settings', name: 'Settings', component: () => import('./views/Settings.vue') },
    { path: '/settings/:group', name: 'SettingsGroup', component: () => import('./views/Settings.vue') },
  ],
})

export default router