<script setup lang="ts">
import { ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { errorMessage, login, sessionError } from '../api'

const route = useRoute()
const router = useRouter()
const username = ref('')
const password = ref('')
const loading = ref(false)
const error = ref('')

async function submit() {
  if (loading.value)
    return
  loading.value = true
  error.value = ''
  try {
    await login(username.value.trim(), password.value)
    password.value = ''
    const redirect = route.query.redirect
    const target = typeof redirect === 'string' && redirect.startsWith('/') && !redirect.startsWith('//') && !redirect.startsWith('/login')
      ? redirect
      : '/sources'
    await router.replace(target)
  }
  catch (cause) {
    error.value = errorMessage(cause)
  }
  finally {
    loading.value = false
  }
}
</script>

<template>
  <main class="login-page">
    <section class="login-story" aria-label="MarketMind 简介">
      <a class="brand" href="/login"><span class="brand-mark">M<span>m</span></span><span>MarketMind</span></a>
      <div class="story-copy">
        <p class="eyebrow">
          FINANCIAL INTELLIGENCE / DATA DESK
        </p>
        <h1>让每一条资讯，<br>有迹可循。</h1>
        <p>连接来源，留存原文，追踪修订。<br>从可信的数据基础开始。</p>
        <div class="signal-diagram" aria-hidden="true">
          <span>来源</span><i /><span>新闻</span><i /><span>修订</span>
        </div>
      </div>
      <p class="story-footer">
        MARKETMIND <span>个人金融资讯工作台</span>
      </p>
    </section>
    <section class="login-panel">
      <div class="login-form-wrap">
        <p class="eyebrow">
          ADMIN ACCESS
        </p>
        <h2>登录数据工作台</h2>
        <p class="muted login-intro">
          使用已配置的管理员账号继续。
        </p>
        <ElAlert v-if="error || sessionError" class="mb-5" type="error" :closable="false" :title="error || sessionError" show-icon role="alert" />
        <form class="login-form" :aria-busy="loading" @submit.prevent="submit">
          <div class="form-field">
            <label for="username">管理员账号</label>
            <ElInput id="username" v-model="username" autocomplete="username" size="large" required :disabled="loading" />
          </div>
          <div class="form-field">
            <label for="password">密码</label>
            <ElInput id="password" v-model="password" type="password" autocomplete="current-password" size="large" show-password required :disabled="loading" />
          </div>
          <ElButton type="primary" size="large" native-type="submit" class="login-submit" :loading="loading">
            {{ loading ? '正在验证身份' : '登录工作台' }}
          </ElButton>
        </form>
        <p class="login-note">
          仅限管理员访问。会话通过 HttpOnly Cookie 保护，不在浏览器本地存储认证令牌。
        </p>
      </div>
      <p class="login-bottom">
        数据基础工程 <span> / </span> P1
      </p>
    </section>
  </main>
</template>
