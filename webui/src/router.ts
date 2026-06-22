import { createRouter, createWebHashHistory } from 'vue-router'

const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: '/chat' },
    { path: '/login', name: 'Login', component: () => import('./views/Login.vue') },
    { path: '/chat', name: 'Chat', component: () => import('./views/Chat.vue') },
  ],
})

export default router