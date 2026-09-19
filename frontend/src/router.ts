import { createRouter, createWebHistory } from 'vue-router'
import { identity, onUnauthorized, restoreSession } from './api'

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/login', name: 'login', component: () => import('./pages/LoginPage.vue'), meta: { title: '管理员登录' } },
    {
      path: '/',
      component: () => import('./layouts/AppLayout.vue'),
      children: [
        { path: '', redirect: '/overview' },
        { path: 'overview', name: 'overview', component: () => import('./pages/OverviewPage.vue'), meta: { title: '研判总览' } },
        { path: 'markets', name: 'markets', component: () => import('./pages/MarketsPage.vue'), meta: { title: '实时行情' } },
        { path: 'summaries', name: 'summaries', component: () => import('./pages/SummariesPage.vue'), meta: { title: '摘要文章' } },
        { path: 'summaries/:id', name: 'summary-detail', component: () => import('./pages/SummaryDetailPage.vue'), meta: { title: '摘要详情' } },
        { path: 'news', name: 'news', component: () => import('./pages/NewsPage.vue'), meta: { title: '新闻档案' } },
        { path: 'news/:id', name: 'news-detail', component: () => import('./pages/NewsDetailPage.vue'), meta: { title: '新闻详情' } },
        { path: 'sources', name: 'sources', component: () => import('./pages/SourcesPage.vue'), meta: { title: '来源与采集器' } },
        { path: 'runs', name: 'runs', component: () => import('./pages/RunsPage.vue'), meta: { title: '运行记录' } },
        { path: 'proxies', name: 'proxies', component: () => import('./pages/ProxiesPage.vue'), meta: { title: '代理库存' } },
        { path: 'sessions', name: 'sessions', component: () => import('./pages/LoginSessionsPage.vue'), meta: { title: '金十登录会话' } },
        { path: 'alerts', name: 'alerts', component: () => import('./pages/AlertsPage.vue'), meta: { title: '告警与通知' } },
        { path: ':pathMatch(.*)*', name: 'not-found', component: () => import('./pages/NotFoundPage.vue'), meta: { title: '页面未找到' } },
      ],
    },
  ],
  scrollBehavior: () => ({ top: 0 }),
})

router.beforeEach(async (to) => {
  await restoreSession()
  if (to.name !== 'login' && !identity.value)
    return { name: 'login', query: { redirect: to.fullPath } }
  if (to.name === 'login' && identity.value)
    return { name: 'overview' }
})

router.afterEach((to) => {
  document.title = `${String(to.meta.title || '数据工作台')} · MarketMind`
})

onUnauthorized(() => {
  if (router.currentRoute.value.name !== 'login')
    void router.replace({ name: 'login', query: { redirect: router.currentRoute.value.fullPath } })
})
