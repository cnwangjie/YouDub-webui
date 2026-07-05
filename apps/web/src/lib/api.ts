function configuredApiBase(): string {
  const configured = process.env.NEXT_PUBLIC_API_BASE_URL?.trim()
  if (configured) return configured.replace(/\/$/, "")

  if (typeof window === "undefined") return "http://127.0.0.1:8000"
  return ""
}

export const API_BASE = configuredApiBase()

export type StageStatus = "pending" | "running" | "succeeded" | "failed"
export type TaskStatus = "queued" | "running" | "paused" | "succeeded" | "failed" | "cancelled"
export type ExecutionMode = "auto" | "manual"

export type TaskStage = {
  task_id: string
  name: string
  label: string
  status: StageStatus
  progress: number | null
  started_at: string | null
  completed_at: string | null
  last_message: string | null
  error_message: string | null
}

export type Task = {
  id: string
  url: string
  title: string | null
  source_author: string | null
  source_description: string | null
  source_published_at: string | null
  thumbnail_url: string | null
  status: TaskStatus
  current_stage: string | null
  session_path: string | null
  final_video_path: string | null
  duration_seconds: number | null
  error_message: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
  execution_mode: ExecutionMode
  stages: TaskStage[]
}

export type CookieInfo = {
  exists: boolean
  size: number
  updated_at: number | null
  content: string
}

export type OpenAISettings = {
  base_url: string
  api_key: string
  has_api_key: boolean
  model: string
  translate_concurrency: string
  translate_use_batch: boolean
}

export type OpenAIModels = {
  models: string[]
}

export type OpenAITestResult = {
  ok: boolean
  model: string
  message: string
}

export type YtdlpSettings = {
  proxy_port: string
}

export type SyncedSettings = {
  openai: OpenAISettings
  ytdlp: YtdlpSettings
}

export type LocalDirection = "en-zh" | "zh-en"

export type LocalizedMetadata = {
  title: string
  description: string
  tags: string[]
  thumbnail_url: string
  source_thumbnail_file: string
  thumbnail_file: string
  translated_thumbnail_file: string
  thumbnail_translation?: {
    model: string
    base_url: string
    error: string
  }
  thumbnail_api_url: string
  translated_title: string
  translated_description: string
  translated_tags: string[]
  model: string
  base_url: string
}

export type BilibiliCredentialsStatus = {
  configured: boolean
  platform?: string
  has_bili_jct?: boolean
  has_sessdata?: boolean
}

export type BilibiliQrCode = {
  code: number
  data: {
    auth_code: string
    url: string
  }
}

export type BilibiliQrPoll = {
  status: "pending" | "succeeded" | "failed"
  message?: string
}

export type BilibiliPublishStatus = {
  status: "idle" | "running" | "succeeded" | "failed"
  progress: number
  message: string
  error: string
  result: unknown
}

export type TaskPublishStatus = "unpublished" | "draft" | "running" | "succeeded" | "failed"

export type BilibiliPublishRecord = {
  task_id: string
  task_status: TaskStatus
  type: "draft" | "published"
  publish_status: "draft" | "running" | "succeeded" | "failed"
  progress: number
  message: string
  error: string
  title: string
  description: string
  tags: string[]
  source: string
  tid: number
  thumbnail_api_url: string
  final_video_available: boolean
  created_at: string
  completed_at: string | null
  updated_at: string | null
  draft_url: string
  aid: number | null
  bvid: string
  url: string
}

export type BilibiliPublishRecordsResponse = {
  records: BilibiliPublishRecord[]
}

export type BilibiliPublishMetadata = {
  title: string
  description: string
  source: string
  tags: string[]
  tid: number | null
}

export type BilibiliPartition = {
  id: number
  name: string
  parent_name: string
  label: string
  description: string
}

export type BilibiliPartitionsResponse = {
  partitions: BilibiliPartition[]
}

export type BilibiliAccountArchive = {
  aid: number | null
  bvid: string
  title: string
  description: string
  cover: string
  tag: string
  duration: number
  state: number | null
  state_desc: string
  created_at: string | null
  published_at: string | null
  url: string
  edit_url: string
  stats: {
    view: number
    danmaku: number
    reply: number
    favorite: number
    coin: number
    share: number
    like: number
  }
}

export type BilibiliAccountArchivesResponse = {
  archives: BilibiliAccountArchive[]
  page: {
    page: number
    page_size: number
    total: number
  }
}

async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      "Content-Type": "application/json",
      ...(options?.headers || {}),
    },
    cache: "no-store",
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail || `Request failed: ${response.status}`)
  }
  if (response.status === 204) {
    return undefined as T
  }
  return response.json()
}

export type TaskSummary = {
  id: string
  url: string
  title: string | null
  source_author: string | null
  source_published_at: string | null
  thumbnail_url: string | null
  status: TaskStatus
  current_stage: string | null
  session_path: string | null
  final_video_path: string | null
  duration_seconds: number | null
  error_message: string | null
  created_at: string
  started_at: string | null
  completed_at: string | null
  execution_mode?: ExecutionMode
  stages: TaskStage[]
  bilibili_publish_status: TaskPublishStatus
}

export type TaskListStatus = "all" | "incomplete" | TaskStatus
export type TaskListExecutionMode = "all" | ExecutionMode
export type TaskListSort =
  | "created_desc"
  | "created_asc"
  | "started_desc"
  | "started_asc"
  | "completed_desc"
  | "completed_asc"
  | "status_asc"
  | "status_desc"
  | "title_asc"
  | "title_desc"

export type TaskListParams = {
  page?: number
  page_size?: number
  q?: string
  status?: TaskListStatus
  execution_mode?: TaskListExecutionMode
  sort?: TaskListSort
  hide_completed?: boolean
}

export type TaskListResponse = {
  tasks: TaskSummary[]
  total: number
  page: number
  page_size: number
}

export type RequeueAllTasksResponse = {
  queued: number
  task_ids: string[]
}

export type WorkerStatus = {
  running: boolean
  thread_alive: boolean
  queue_size: number
  current_task_id: string | null
}

export function getCurrentTask() {
  return request<Task | null>("/api/tasks/current")
}

export async function getTaskLog(taskId: string): Promise<string> {
  const response = await fetch(`${API_BASE}/api/tasks/${taskId}/log`, { cache: "no-store" })
  if (!response.ok) {
    throw new Error(`Failed to load log: ${response.status}`)
  }
  return response.text()
}

export function listTasks(params: TaskListParams | number = {}) {
  const normalized = typeof params === "number" ? { page_size: params } : params
  const search = new URLSearchParams()

  Object.entries(normalized).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return
    search.set(key, String(value))
  })

  const query = search.toString()
  return request<TaskListResponse>(`/api/tasks${query ? `?${query}` : ""}`)
}

export function requeueAllTasks() {
  return request<RequeueAllTasksResponse>("/api/tasks/requeue-all", { method: "POST" })
}

export function getWorkerStatus() {
  return request<WorkerStatus>("/api/worker")
}

export function startWorker() {
  return request<WorkerStatus>("/api/worker/start", { method: "POST" })
}

export function stopWorker() {
  return request<WorkerStatus>("/api/worker/stop", { method: "POST" })
}

export function getTask(taskId: string) {
  return request<Task>(`/api/tasks/${taskId}`)
}

export function deleteTask(taskId: string) {
  return request<void>(`/api/tasks/${taskId}`, { method: "DELETE" })
}

export function rerunTask(taskId: string) {
  return request<Task>(`/api/tasks/${taskId}/rerun`, { method: "POST" })
}

export function resumeTask(taskId: string) {
  return request<Task>(`/api/tasks/${taskId}/resume`, { method: "POST" })
}

export function continueTask(taskId: string, executionMode?: ExecutionMode) {
  return request<Task>(`/api/tasks/${taskId}/continue`, {
    method: "POST",
    body: JSON.stringify(executionMode ? { execution_mode: executionMode } : {}),
  })
}

export function cancelTask(taskId: string) {
  return request<Task>(`/api/tasks/${taskId}/cancel`, { method: "POST" })
}

export function redoStage(taskId: string, stageName: string) {
  return request<Task>(`/api/tasks/${taskId}/stages/${stageName}/redo`, { method: "POST" })
}

export function getLocalizedMetadata(taskId: string) {
  return request<LocalizedMetadata>(`/api/tasks/${taskId}/metadata/localized`)
}

export function generateLocalizedMetadata(taskId: string) {
  return request<LocalizedMetadata>(`/api/tasks/${taskId}/metadata/localized`, { method: "POST" })
}

export function thumbnailUrl(path: string) {
  return `${API_BASE}${path}`
}

export function getBilibiliCredentialsStatus() {
  return request<BilibiliCredentialsStatus>("/api/bilibili/credentials")
}

export function clearBilibiliCredentials() {
  return request<void>("/api/bilibili/credentials", { method: "DELETE" })
}

export function createBilibiliQrCode() {
  return request<BilibiliQrCode>("/api/bilibili/qrcode", { method: "POST" })
}

export function pollBilibiliQrCode(authCode: string) {
  return request<BilibiliQrPoll>("/api/bilibili/qrcode/poll", {
    method: "POST",
    body: JSON.stringify({ auth_code: authCode }),
  })
}

export function getBilibiliPublishStatus(taskId: string) {
  return request<BilibiliPublishStatus>(`/api/tasks/${taskId}/bilibili/publish`)
}

export function listBilibiliPublishRecords() {
  return request<BilibiliPublishRecordsResponse>("/api/bilibili/publish/records")
}

export function listBilibiliPartitions() {
  return request<BilibiliPartitionsResponse>("/api/bilibili/partitions")
}

export function listBilibiliAccountArchives(params: { page?: number; page_size?: number; status?: string } = {}) {
  const search = new URLSearchParams()
  Object.entries(params).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") return
    search.set(key, String(value))
  })
  const query = search.toString()
  return request<BilibiliAccountArchivesResponse>(`/api/bilibili/account/archives${query ? `?${query}` : ""}`)
}

export function startBilibiliPublish(taskId: string, metadata?: BilibiliPublishMetadata) {
  return request<BilibiliPublishStatus>(`/api/tasks/${taskId}/bilibili/publish`, {
    method: "POST",
    body: JSON.stringify(metadata || {}),
  })
}

export function createTask(url: string, executionMode: ExecutionMode = "auto", autoStart = true) {
  return request<Task>("/api/tasks", {
    method: "POST",
    body: JSON.stringify({ url, execution_mode: executionMode, auto_start: autoStart }),
  })
}

export async function uploadLocalTask(
  file: File,
  direction: LocalDirection,
  subtitleFile: File | null = null,
  executionMode: ExecutionMode = "auto",
  autoStart = true,
) {
  const form = new FormData()
  form.append("direction", direction)
  form.append("file", file)
  if (subtitleFile) {
    form.append("subtitle_file", subtitleFile)
  }
  form.append("execution_mode", executionMode)
  form.append("auto_start", String(autoStart))

  const response = await fetch(`${API_BASE}/api/tasks/upload`, {
    method: "POST",
    body: form,
    cache: "no-store",
  })
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    throw new Error(body.detail || `Request failed: ${response.status}`)
  }
  return response.json() as Promise<Task>
}

export function getCookieInfo() {
  return request<CookieInfo>("/api/cookies/youtube")
}

export function saveCookie(content: string) {
  return request<CookieInfo>("/api/cookies/youtube", {
    method: "POST",
    body: JSON.stringify({ content }),
  })
}

export function getOpenAISettings() {
  return request<OpenAISettings>("/api/settings/openai")
}

export function saveOpenAISettings(settings: {
  base_url: string
  api_key: string
  clear_api_key?: boolean
  model: string
  translate_concurrency: string
  translate_use_batch: boolean
}) {
  return request<OpenAISettings>("/api/settings/openai", {
    method: "POST",
    body: JSON.stringify(settings),
  })
}

export function getOpenAIModels(settings: {
  base_url: string
  api_key: string
}) {
  return request<OpenAIModels>("/api/settings/openai/models", {
    method: "POST",
    body: JSON.stringify(settings),
  })
}

export function testOpenAIConnection(settings: {
  base_url: string
  api_key: string
  model: string
}) {
  return request<OpenAITestResult>("/api/settings/openai/test", {
    method: "POST",
    body: JSON.stringify(settings),
  })
}

export function getYtdlpSettings() {
  return request<YtdlpSettings>("/api/settings/ytdlp")
}

export function saveYtdlpSettings(settings: YtdlpSettings) {
  return request<YtdlpSettings>("/api/settings/ytdlp", {
    method: "POST",
    body: JSON.stringify(settings),
  })
}

export function syncSettingsFromEnv() {
  return request<SyncedSettings>("/api/settings/sync-env", { method: "POST" })
}

export function finalVideoUrl(taskId: string) {
  return `${API_BASE}/api/tasks/${taskId}/artifact/final-video`
}

export function finalVideoDownloadUrl(taskId: string) {
  return `${API_BASE}/api/tasks/${taskId}/artifact/final-video?download=1`
}
