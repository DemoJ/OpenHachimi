<template>
  <div class="schedules-page">
    <header class="header">
      <button class="btn" @click="goBack">← 返回</button>
      <div class="brand">定时任务</div>
      <button class="btn btn-primary" @click="openCreate">+ 新建任务</button>
    </header>

    <div class="schedules-body">
      <div v-if="loading" class="status-line"><span class="activity-spinner" /> 加载中…</div>
      <div v-else-if="errorText" class="status-line error">{{ errorText }} <button class="btn" @click="load">重试</button></div>
      <div v-else-if="tasks.length === 0" class="status-line">
        暂无定时任务。点击右上角"新建任务"，或在对话里让 Agent 创建（"每天早上 9 点提醒我…"）。
      </div>

      <div v-for="t in tasks" :key="t.id" class="task-card" :class="{ paused: t.status === 'paused' }">
        <div class="task-head">
          <span class="task-name">{{ t.name }}</span>
          <span class="task-status" :class="`task-status--${t.status}`">{{ statusLabel(t.status) }}</span>
          <span v-if="t.running" class="task-status task-status--running">运行中</span>
        </div>
        <div class="task-prompt" :title="t.prompt">{{ t.prompt }}</div>
        <div class="task-meta">
          <span>{{ scheduleLabel(t) }}</span>
          <span v-if="t.next_run_at">下次：{{ formatTime(t.next_run_at) }}</span>
          <span v-if="t.last_status">上次：{{ t.last_status }}{{ t.last_error ? `（${t.last_error}）` : '' }}</span>
        </div>
        <div class="task-actions">
          <button v-if="t.status === 'enabled'" class="btn" @click="onPause(t)">暂停</button>
          <button v-else-if="t.status === 'paused'" class="btn" @click="onResume(t)">恢复</button>
          <button class="btn btn-danger" @click="onRemove(t)">删除</button>
        </div>
      </div>
    </div>

    <!-- 新建任务弹窗 -->
    <div v-if="showCreate" class="modal-overlay" @click.self="showCreate = false">
      <div class="modal">
        <h3>新建定时任务</h3>
        <label class="field">
          <span>任务名称</span>
          <input v-model="form.name" placeholder="如：早间新闻摘要" />
        </label>
        <label class="field">
          <span>提示词（到期后交给 Agent 执行）</span>
          <textarea v-model="form.prompt" rows="3" placeholder="如：总结今天的科技新闻，生成一份简报" />
        </label>
        <div class="field-row">
          <label class="field">
            <span>类型</span>
            <select v-model="form.schedule_type">
              <option value="cron">cron 表达式（如 0 9 * * * = 每天 9 点）</option>
              <option value="interval">固定间隔（如 30m、2h、86400）</option>
              <option value="once">一次性（ISO 时间，如 2026-08-23T09:00:00）</option>
            </select>
          </label>
          <label class="field">
            <span>表达式</span>
            <input v-model="form.schedule_expr" :placeholder="exprPlaceholder" />
          </label>
        </div>
        <label class="field">
          <span>时区（默认本机时区，如 Asia/Shanghai）</span>
          <input v-model="form.timezone" placeholder="留空 = 本机时区" />
        </label>
        <p v-if="createError" class="status-line error">{{ createError }}</p>
        <div class="modal-actions">
          <button class="btn" @click="showCreate = false">取消</button>
          <button class="btn btn-primary" :disabled="creating" @click="onCreate">{{ creating ? '创建中…' : '创建' }}</button>
        </div>
      </div>
    </div>

    <!-- 删除确认 -->
    <ConfirmDialog
      v-if="pendingDelete"
      title="删除定时任务"
      message="确定删除该定时任务？任务将停止执行并被标记为已删除。"
      confirm-text="删除"
      :loading="deleting"
      @confirm="confirmDelete"
      @cancel="pendingDelete = null"
    />
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref, computed } from 'vue'
import { useRouter } from 'vue-router'
import ConfirmDialog from '../components/ConfirmDialog.vue'
import {
  listSchedules, createSchedule, pauseSchedule, resumeSchedule, removeSchedule,
  type ScheduleTask,
} from '../api'
import { getToken } from '../api'

const router = useRouter()
const tasks = ref<ScheduleTask[]>([])
const loading = ref(false)
const errorText = ref('')

const showCreate = ref(false)
const creating = ref(false)
const createError = ref('')
const form = ref({
  name: '',
  prompt: '',
  schedule_type: 'cron' as 'cron' | 'interval' | 'once',
  schedule_expr: '',
  timezone: '',
})

const pendingDelete = ref<ScheduleTask | null>(null)
const deleting = ref(false)

const exprPlaceholder = computed(() => {
  if (form.value.schedule_type === 'cron') return '0 9 * * *'
  if (form.value.schedule_type === 'interval') return '30m / 2h / 86400'
  return '2026-08-23T09:00:00'
})

function goBack() {
  if (!getToken()) {
    router.replace('/login')
    return
  }
  router.push('/chat')
}

async function load() {
  loading.value = true
  errorText.value = ''
  try {
    const res = await listSchedules()
    tasks.value = (res as unknown as ScheduleTask[])
  } catch (e) {
    errorText.value = `加载失败：${e instanceof Error ? e.message : String(e)}`
  } finally {
    loading.value = false
  }
}

onMounted(load)

function statusLabel(status: string): string {
  if (status === 'enabled') return '启用'
  if (status === 'paused') return '已暂停'
  if (status === 'deleted') return '已删除'
  return status
}

function scheduleLabel(t: ScheduleTask): string {
  const kind = t.schedule_type === 'cron' ? 'cron' : t.schedule_type === 'interval' ? '间隔' : '一次性'
  return `${kind}：${t.schedule_expr}`
}

function formatTime(iso: string | null): string {
  if (!iso) return '-'
  try {
    const d = new Date(iso)
    return d.toLocaleString()
  } catch {
    return iso
  }
}

function openCreate() {
  form.value = { name: '', prompt: '', schedule_type: 'cron', schedule_expr: '', timezone: '' }
  createError.value = ''
  showCreate.value = true
}

async function onCreate() {
  if (!form.value.name.trim() || !form.value.prompt.trim() || !form.value.schedule_expr.trim()) {
    createError.value = '请填写任务名称、提示词与表达式。'
    return
  }
  creating.value = true
  createError.value = ''
  try {
    await createSchedule({
      name: form.value.name.trim(),
      prompt: form.value.prompt.trim(),
      schedule_type: form.value.schedule_type,
      schedule_expr: form.value.schedule_expr.trim(),
      timezone: form.value.timezone.trim() || null,
      delivery_mode: 'inbox',
    })
    showCreate.value = false
    await load()
  } catch (e) {
    createError.value = e instanceof Error ? e.message : String(e)
  } finally {
    creating.value = false
  }
}

async function onPause(t: ScheduleTask) {
  try {
    await pauseSchedule(t.id)
    await load()
  } catch (e) {
    errorText.value = `暂停失败：${e instanceof Error ? e.message : String(e)}`
  }
}

async function onResume(t: ScheduleTask) {
  try {
    await resumeSchedule(t.id)
    await load()
  } catch (e) {
    errorText.value = `恢复失败：${e instanceof Error ? e.message : String(e)}`
  }
}

async function confirmDelete() {
  if (!pendingDelete.value) return
  deleting.value = true
  try {
    await removeSchedule(pendingDelete.value.id)
    pendingDelete.value = null
    await load()
  } catch (e) {
    errorText.value = `删除失败：${e instanceof Error ? e.message : String(e)}`
    pendingDelete.value = null
  } finally {
    deleting.value = false
  }
}

function onRemove(t: ScheduleTask) {
  pendingDelete.value = t
}
</script>

<style scoped>
.schedules-page {
  display: flex;
  flex-direction: column;
  height: 100%;
}
.header {
  height: var(--header-height, 52px);
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 0 var(--sp-lg, 16px);
  border-bottom: 1px solid var(--hairline, #212327);
}
.brand {
  flex: 1;
  font-size: 14px;
  letter-spacing: 0.4px;
}
.schedules-body {
  flex: 1;
  overflow-y: auto;
  padding: var(--sp-lg, 16px);
  display: flex;
  flex-direction: column;
  gap: 10px;
  max-width: 760px;
  width: 100%;
  margin: 0 auto;
}
.status-line {
  color: var(--body-mid, #7d8187);
  font-size: 13px;
  padding: 12px;
  display: flex;
  align-items: center;
  gap: 8px;
}
.status-line.error { color: #ff8589; }
.task-card {
  border: 1px solid var(--hairline, #212327);
  border-radius: 10px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.task-card.paused { opacity: 0.65; }
.task-head {
  display: flex;
  align-items: center;
  gap: 10px;
}
.task-name { font-size: 14px; font-weight: 600; color: var(--ink, #ededed); }
.task-status {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 999px;
  border: 1px solid var(--hairline, #212327);
  color: var(--body-mid, #7d8187);
}
.task-status--enabled { color: #46a758; border-color: rgba(70, 167, 88, 0.4); }
.task-status--paused { color: #f5a524; border-color: rgba(245, 165, 36, 0.4); }
.task-status--running { color: #5e9bff; border-color: rgba(94, 155, 255, 0.4); }
.task-prompt {
  font-size: 13px;
  color: var(--body-mid, #aaa);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.task-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 12px;
  color: var(--body-mid, #7d8187);
  font-family: 'Geist Mono', ui-monospace, monospace;
}
.task-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}
.btn-danger { color: #ff6b6e; border-color: rgba(229, 72, 77, 0.4); }
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 50;
}
.modal {
  background: var(--canvas-soft, #1a1c20);
  border: 1px solid var(--hairline, #212327);
  border-radius: 12px;
  padding: 20px;
  width: min(92vw, 520px);
  display: flex;
  flex-direction: column;
  gap: 12px;
  max-height: 86vh;
  overflow-y: auto;
}
.modal h3 { margin: 0; font-size: 15px; }
.field {
  display: flex;
  flex-direction: column;
  gap: 4px;
  flex: 1;
  font-size: 12px;
  color: var(--body-mid, #7d8187);
}
.field input, .field textarea, .field select {
  background: var(--canvas, #0a0a0a);
  border: 1px solid var(--hairline, #212327);
  border-radius: 8px;
  color: var(--ink, #ededed);
  padding: 8px 10px;
  font-size: 13px;
  font-family: inherit;
}
.field-row { display: flex; gap: 10px; }
.modal-actions { display: flex; justify-content: flex-end; gap: 8px; }
</style>
