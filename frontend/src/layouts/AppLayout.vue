<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { errorMessage, identity, logout } from '../api'

const route = useRoute()
const router = useRouter()
const loggingOut = ref(false)
const error = ref('')
const links = [
  { to: '/overview', number: '01', label: '研判总览', note: 'Market intelligence' },
  { to: '/markets', number: '02', label: '实时行情', note: 'Live market tape' },
  { to: '/summaries', number: '03', label: '摘要文章', note: 'Research briefs' },
  { to: '/news', number: '04', label: '新闻档案', note: 'News archive' },
  { to: '/sources', number: '05', label: '来源与采集器', note: 'Sources' },
  { to: '/runs', number: '06', label: '运行记录', note: 'Run history' },
  { to: '/proxies', number: '07', label: '代理库存', note: 'Egress inventory' },
  { to: '/sessions', number: '08', label: '金十登录会话', note: 'Protected access' },
  { to: '/alerts', number: '09', label: '告警与通知', note: 'Incident desk' },
]

async function signOut() {
  loggingOut.value = true
  error.value = ''
  try {
    await logout()
    await router.replace('/login')
  }
  catch (cause) {
    error.value = errorMessage(cause)
  }
  finally {
    loggingOut.value = false
  }
}
</script>

<template>
  <div class="workspace">
    <a class="skip-link" href="#main-content">跳转到主要内容</a>
    <aside class="sidebar">
      <RouterLink to="/overview" class="brand">
        <span class="brand-mark">M<span>m</span></span><span>MarketMind<small>金融资讯 · 智能研判台</small></span>
      </RouterLink>
      <p class="nav-caption">
        WORKSPACE
      </p>
      <nav aria-label="主导航" class="navigation">
        <RouterLink v-for="link in links" :key="link.to" :to="link.to" :class="{ active: route.path.startsWith(link.to) }" :aria-current="route.path.startsWith(link.to) ? 'page' : undefined">
          <span class="nav-number">{{ link.number }}</span>
          <span>{{ link.label }}<small>{{ link.note }}</small></span>
          <span class="nav-indicator" aria-hidden="true" />
        </RouterLink>
      </nav>
      <div class="sidebar-note">
        <span class="phase-label">PHASE 03 · 分析与呈现</span>
        <p>从原始资讯到聚合研判。<br>每一个结论均可追溯来源。</p>
      </div>
      <div class="sidebar-bottom">
        保留来源 · 保留事实
      </div>
    </aside>
    <div class="workspace-body">
      <header class="topbar">
        <div class="breadcrumb">
          工作台 <span>/</span> <strong>{{ route.meta.title }}</strong>
        </div>
        <div class="account">
          <span class="account-avatar" aria-hidden="true">{{ identity?.username.slice(0, 1).toUpperCase() }}</span>
          <span class="account-name">{{ identity?.username }}</span>
          <ElButton text :loading="loggingOut" @click="signOut">
            退出登录
          </ElButton>
        </div>
      </header>
      <main id="main-content" class="page-content" tabindex="-1">
        <ElAlert v-if="error" class="mb-5" type="error" :closable="false" :title="error" show-icon role="alert" />
        <RouterView />
      </main>
      <footer class="workspace-footer">
        <span>MarketMind / 数据来源可追溯</span><span>时间统一显示为北京时间 UTC+8</span>
      </footer>
    </div>
  </div>
</template>
