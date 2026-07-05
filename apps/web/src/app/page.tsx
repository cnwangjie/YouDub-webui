"use client"

import Link from "next/link"
import { useRouter } from "next/navigation"
import { ChangeEvent, FormEvent, useEffect, useRef, useState } from "react"
import { ChevronLeft, ChevronRight, Pause, Play, Plus, Search, Upload } from "lucide-react"

import {
  ExecutionMode,
  LocalDirection,
  TaskPublishStatus,
  TaskListExecutionMode,
  TaskListResponse,
  TaskListSort,
  TaskListStatus,
  TaskStage,
  TaskSummary,
  WorkerStatus,
  createTask,
  getWorkerStatus,
  listTasks,
  requeueAllTasks,
  startWorker,
  stopWorker,
  uploadLocalTask,
} from "@/lib/api"
import { useI18n } from "@/lib/i18n"
import { statusBadgeClass } from "@/lib/status"
import { AppHeader } from "@/components/app-header"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Card,
  CardContent,
  CardHeader,
  CardTitle,
} from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
} from "@/components/ui/select"

const PAGE_SIZE_OPTIONS = [10, 20, 50, 100]

function isActive(status: string) {
  return status === "queued" || status === "running"
}

function isAwaitingAction(status: string) {
  return status === "paused"
}

function formatTime(value: string | null) {
  if (!value) return ""
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

function shortUrl(url: string) {
  return url.replace(/^https?:\/\/(www\.)?/, "")
}

function activeCount(tasks: TaskSummary[]) {
  return tasks.filter((t) => isActive(t.status)).length
}

function stageDotClass(status: string) {
  if (status === "succeeded") return "border-[#00aeec] bg-[#00aeec]"
  if (status === "running") return "border-[#fb7299] bg-[#fb7299]"
  if (status === "failed") return "border-[#ff0033] bg-[#ff0033]"
  return "border-zinc-300 bg-white"
}

function publishBadgeClass(status: TaskPublishStatus) {
  if (status === "succeeded") return "bg-[#00aeec]/10 text-[#008ac0] border-transparent"
  if (status === "running") return "bg-[#fb7299]/15 text-[#c2185b] border-transparent"
  if (status === "failed") return "bg-[#ff0033]/10 text-[#ff0033] border-transparent"
  if (status === "draft") return "bg-amber-100 text-amber-800 border-transparent"
  return "bg-zinc-100 text-zinc-600 border-transparent"
}

function publishStatusLabel(status: TaskPublishStatus, t: ReturnType<typeof useI18n>["t"]) {
  if (status === "succeeded") return t.home.publishSucceeded
  if (status === "running") return t.home.publishRunning
  if (status === "failed") return t.home.publishFailed
  if (status === "draft") return t.home.publishDraft
  return t.home.publishUnpublished
}

function TaskStageDots({
  stages,
  stageLabel,
  statusLabel,
  currentStage,
}: {
  stages: TaskStage[]
  stageLabel: (stage: string) => string
  statusLabel: (status?: string) => string
  currentStage: string | null
}) {
  if (!stages.length) return null
  return (
    <div className="flex min-w-0 items-center gap-2">
      <div className="flex shrink-0 items-center gap-1.5 px-0.5 py-0.5">
        {stages.map((stage) => (
          <span
            key={stage.name}
            title={`${stageLabel(stage.name)} · ${statusLabel(stage.status)}`}
            className={`size-2.5 shrink-0 rounded-full border ${stageDotClass(stage.status)}`}
          />
        ))}
      </div>
      {currentStage && currentStage !== "done" ? (
        <span className="min-w-0 truncate text-xs text-muted-foreground">
          {stageLabel(currentStage)}
        </span>
      ) : null}
    </div>
  )
}

function TaskProgressOrPublish({
  item,
  stageLabel,
  statusLabel,
  t,
}: {
  item: TaskSummary
  stageLabel: (stage: string) => string
  statusLabel: (status?: string) => string
  t: ReturnType<typeof useI18n>["t"]
}) {
  if (item.status === "succeeded") {
    return (
      <div className="mt-1.5 flex h-4 items-center">
        <Badge className={publishBadgeClass(item.bilibili_publish_status)}>
          {publishStatusLabel(item.bilibili_publish_status, t)}
        </Badge>
      </div>
    )
  }
  return (
    <div className="h-4">
      <TaskStageDots
        stages={item.stages || []}
        stageLabel={stageLabel}
        statusLabel={statusLabel}
        currentStage={item.current_stage}
      />
    </div>
  )
}

function selectedLabel<T extends string>(options: { value: T; label: string }[], value: T) {
  return options.find((option) => option.value === value)?.label || value
}

function pageRangeText(language: string, start: number, end: number, total: number) {
  if (language === "zh") return `显示 ${start}-${end} / 共 ${total} 个任务`
  return `Showing ${start}-${end} of ${total} tasks`
}

function pageIndexText(language: string, page: number, totalPages: number) {
  if (language === "zh") return `第 ${page} / ${totalPages} 页`
  return `Page ${page} / ${totalPages}`
}

export default function Home() {
  const router = useRouter()
  const { activeTasksText, language, stageLabel, statusLabel, t } = useI18n()
  const fileInputRef = useRef<HTMLInputElement>(null)
  const subtitleInputRef = useRef<HTMLInputElement>(null)
  const [youtubeUrl, setYoutubeUrl] = useState("")
  const [bilibiliUrl, setBilibiliUrl] = useState("")
  const [localFile, setLocalFile] = useState<File | null>(null)
  const [localSubtitleFile, setLocalSubtitleFile] = useState<File | null>(null)
  const [localDirection, setLocalDirection] = useState<LocalDirection>("en-zh")
  const [executionMode, setExecutionMode] = useState<ExecutionMode>("auto")
  const [tasks, setTasks] = useState<TaskSummary[]>([])
  const [taskTotal, setTaskTotal] = useState(0)
  const [taskPage, setTaskPage] = useState(1)
  const [taskPageSize, setTaskPageSize] = useState(20)
  const [taskQuery, setTaskQuery] = useState("")
  const [taskStatus, setTaskStatus] = useState<TaskListStatus>("incomplete")
  const [taskExecutionMode, setTaskExecutionMode] = useState<TaskListExecutionMode>("all")
  const [taskSort, setTaskSort] = useState<TaskListSort>("created_asc")
  const [error, setError] = useState("")
  const [submittingAction, setSubmittingAction] = useState<"open" | "next" | null>(null)
  const [requeueingAll, setRequeueingAll] = useState(false)
  const [workerStatus, setWorkerStatus] = useState<WorkerStatus | null>(null)
  const [togglingWorker, setTogglingWorker] = useState(false)

  const localDirectionOptions: { value: LocalDirection; label: string }[] = [
    { value: "en-zh", label: t.home.localEnZh },
    { value: "zh-en", label: t.home.localZhEn },
  ]

  const executionModeOptions: { value: ExecutionMode; label: string }[] = [
    { value: "auto", label: t.home.executionAuto },
    { value: "manual", label: t.home.executionManual },
  ]

  const statusOptions: { value: TaskListStatus; label: string }[] = [
    { value: "incomplete", label: t.home.incompleteStatuses },
    { value: "all", label: t.home.allStatuses },
    { value: "queued", label: statusLabel("queued") },
    { value: "running", label: statusLabel("running") },
    { value: "paused", label: statusLabel("paused") },
    { value: "succeeded", label: statusLabel("succeeded") },
    { value: "failed", label: statusLabel("failed") },
    { value: "cancelled", label: statusLabel("cancelled") },
  ]

  const modeOptions: { value: TaskListExecutionMode; label: string }[] = [
    { value: "all", label: t.home.allModes },
    { value: "auto", label: t.home.modeAuto },
    { value: "manual", label: t.home.modeManual },
  ]

  const sortOptions: { value: TaskListSort; label: string }[] = [
    { value: "created_desc", label: t.home.sortCreatedDesc },
    { value: "created_asc", label: t.home.sortCreatedAsc },
    { value: "started_desc", label: t.home.sortStartedDesc },
    { value: "started_asc", label: t.home.sortStartedAsc },
    { value: "completed_desc", label: t.home.sortCompletedDesc },
    { value: "completed_asc", label: t.home.sortCompletedAsc },
    { value: "status_asc", label: t.home.sortStatusAsc },
    { value: "status_desc", label: t.home.sortStatusDesc },
    { value: "title_asc", label: t.home.sortTitleAsc },
    { value: "title_desc", label: t.home.sortTitleDesc },
  ]

  function applyTaskList(result: TaskListResponse) {
    const lastPage = Math.max(1, Math.ceil(result.total / result.page_size))
    setTaskTotal(result.total)
    if (result.total > 0 && result.tasks.length === 0 && result.page > lastPage) {
      setTasks([])
      setTaskPage(lastPage)
      return
    }
    setTasks(result.tasks)
  }

  async function refreshWorkerStatus() {
    setWorkerStatus(await getWorkerStatus())
  }

  async function refreshTasks() {
    const result = await listTasks({
      page: taskPage,
      page_size: taskPageSize,
      q: taskQuery,
        status: taskStatus,
        execution_mode: taskExecutionMode,
        sort: taskSort,
      })
    applyTaskList(result)
    refreshWorkerStatus().catch(() => undefined)
  }

  useEffect(() => {
    let cancelled = false

    const loadTasks = async () => {
      try {
        const [result, nextWorkerStatus] = await Promise.all([
          listTasks({
            page: taskPage,
            page_size: taskPageSize,
            q: taskQuery,
            status: taskStatus,
            execution_mode: taskExecutionMode,
            sort: taskSort,
          }),
          getWorkerStatus(),
        ])
        if (!cancelled) applyTaskList(result)
        if (!cancelled) setWorkerStatus(nextWorkerStatus)
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : t.home.loadError)
      }
    }

    loadTasks()
    const interval = window.setInterval(loadTasks, 2000)
    return () => {
      cancelled = true
      window.clearInterval(interval)
    }
  }, [taskExecutionMode, taskPage, taskPageSize, taskQuery, taskSort, taskStatus, t.home.loadError])

  function resetTaskPage() {
    setTaskPage(1)
  }

  function selectLocalFile(event: ChangeEvent<HTMLInputElement>) {
    setError("")
    setLocalFile(event.target.files?.[0] || null)
  }

  function selectLocalSubtitleFile(event: ChangeEvent<HTMLInputElement>) {
    setError("")
    setLocalSubtitleFile(event.target.files?.[0] || null)
  }

  async function submitTask(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    setError("")
    const submittedUrl = youtubeUrl.trim() || bilibiliUrl.trim()
    if (!submittedUrl && !localFile) return
    const submitter = (event.nativeEvent as SubmitEvent).submitter as HTMLButtonElement | null
    const action = submitter?.value === "next" ? "next" : "open"
    setSubmittingAction(action)
    try {
      const created = localFile
        ? await uploadLocalTask(localFile, localDirection, localSubtitleFile, executionMode, true)
        : await createTask(submittedUrl, executionMode, true)
      setYoutubeUrl("")
      setBilibiliUrl("")
      setLocalFile(null)
      setLocalSubtitleFile(null)
      if (fileInputRef.current) {
        fileInputRef.current.value = ""
      }
      if (subtitleInputRef.current) {
        subtitleInputRef.current.value = ""
      }
      refreshTasks().catch(() => undefined)
      if (action === "open") {
        router.push(`/tasks/${created.id}`)
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : t.home.createError)
    } finally {
      setSubmittingAction(null)
    }
  }

  async function queueAllTasks() {
    setError("")
    setRequeueingAll(true)
    try {
      const result = await requeueAllTasks()
      await refreshTasks()
      setError(result.queued ? "" : t.home.queueAllEmpty)
    } catch (err) {
      setError(err instanceof Error ? err.message : t.home.queueAllError)
    } finally {
      setRequeueingAll(false)
    }
  }

  async function toggleWorker() {
    setError("")
    setTogglingWorker(true)
    try {
      const next = workerStatus?.running ? await stopWorker() : await startWorker()
      setWorkerStatus(next)
    } catch (err) {
      setError(err instanceof Error ? err.message : t.home.workerToggleError)
    } finally {
      setTogglingWorker(false)
    }
  }

  const queued = activeCount(tasks)
  const hasUrl = Boolean(youtubeUrl.trim() || bilibiliUrl.trim())
  const hasLocalFile = Boolean(localFile)
  const canSubmit = Boolean((hasUrl || hasLocalFile) && !submittingAction)
  const totalPages = Math.max(1, Math.ceil(taskTotal / taskPageSize))
  const displayPage = Math.min(taskPage, totalPages)
  const pageStart = taskTotal === 0 ? 0 : (displayPage - 1) * taskPageSize + 1
  const pageEnd = Math.min(taskTotal, displayPage * taskPageSize)
  const hasTaskFilters = Boolean(taskQuery.trim()) || taskStatus !== "incomplete" || taskExecutionMode !== "all"

  return (
    <main className="min-h-screen bg-[linear-gradient(135deg,#fff5f5_0%,#f2fbff_48%,#fff4fa_100%)] text-foreground">
      <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 px-4 py-6 sm:px-6 lg:px-8">
        <AppHeader />

        <Card>
          <CardHeader>
            <CardTitle>{t.home.createTitle}</CardTitle>
          </CardHeader>
          <CardContent>
            <form onSubmit={submitTask} className="space-y-4">
              <div className="space-y-2">
                <Label htmlFor="youtube-url">{t.home.youtubeLabel}</Label>
                <Input
                  id="youtube-url"
                  value={youtubeUrl}
                  onChange={(event) => setYoutubeUrl(event.target.value)}
                  placeholder="https://www.youtube.com/watch?v=..."
                  disabled={Boolean(bilibiliUrl.trim()) || hasLocalFile}
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="bilibili-url">{t.home.bilibiliLabel}</Label>
                <Input
                  id="bilibili-url"
                  value={bilibiliUrl}
                  onChange={(event) => setBilibiliUrl(event.target.value)}
                  placeholder="https://www.bilibili.com/video/BV..."
                  disabled={Boolean(youtubeUrl.trim()) || hasLocalFile}
                />
              </div>
              <div className="grid gap-3 sm:grid-cols-[1fr_180px]">
                <div className="space-y-2">
                  <Label htmlFor="local-video">{t.home.localVideoLabel}</Label>
                  <Input
                    ref={fileInputRef}
                    id="local-video"
                    type="file"
                    accept="video/*,.mp4,.mov,.m4v,.mkv,.webm,.avi,.flv,.wmv"
                    onChange={selectLocalFile}
                    disabled={hasUrl}
                  />
                </div>
                <div className="space-y-2">
                  <Label htmlFor="local-direction">{t.home.localDirectionLabel}</Label>
                  <Select
                    value={localDirection}
                    onValueChange={(value) => setLocalDirection(value as LocalDirection)}
                    disabled={hasUrl}
                  >
                    <SelectTrigger id="local-direction" className="h-10">
                      <span className="min-w-0 truncate text-left">
                        {selectedLabel(localDirectionOptions, localDirection)}
                      </span>
                    </SelectTrigger>
                    <SelectContent>
                      {localDirectionOptions.map((option) => (
                        <SelectItem key={option.value} value={option.value}>
                          {option.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>
              <div className="space-y-2">
                <Label htmlFor="local-subtitle">{t.home.localSubtitleLabel}</Label>
                <Input
                  ref={subtitleInputRef}
                  id="local-subtitle"
                  type="file"
                  accept=".srt"
                  onChange={selectLocalSubtitleFile}
                  disabled={hasUrl || !hasLocalFile}
                />
                <p className="text-xs text-muted-foreground">
                  {t.home.localSubtitleHelp}
                </p>
              </div>
              <div className="space-y-2">
                <Label htmlFor="execution-mode">{t.home.executionModeLabel}</Label>
                <Select
                  value={executionMode}
                  onValueChange={(value) => setExecutionMode(value as ExecutionMode)}
                >
                  <SelectTrigger id="execution-mode" className="h-10">
                    <span className="min-w-0 truncate text-left">
                      {selectedLabel(executionModeOptions, executionMode)}
                    </span>
                  </SelectTrigger>
                  <SelectContent>
                    {executionModeOptions.map((option) => (
                      <SelectItem key={option.value} value={option.value}>
                        {option.label}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              </div>
              <div className="flex items-center justify-between gap-3">
                {queued > 0 ? (
                  <p className="text-xs text-muted-foreground">
                    {activeTasksText(queued)}
                  </p>
                ) : (
                  <span />
                )}
                <div className="flex flex-wrap justify-end gap-2">
                  <Button type="submit" name="intent" value="next" variant="outline" disabled={!canSubmit}>
                    <Plus className="size-4" />
                    {submittingAction === "next" ? t.home.submitting : t.home.createAndNext}
                  </Button>
                  <Button type="submit" name="intent" value="open" disabled={!canSubmit}>
                    {hasLocalFile ? <Upload className="size-4" /> : <Play className="size-4" />}
                    {submittingAction === "open" ? t.home.submitting : t.home.createTask}
                  </Button>
                </div>
              </div>
            </form>

            {error ? (
              <div className="mt-4 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                {error}
              </div>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="gap-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <CardTitle>{t.home.taskHistory} ({taskTotal})</CardTitle>
              <div className="flex flex-wrap items-center gap-2">
                <Badge variant={workerStatus?.running ? "secondary" : "outline"}>
                  {workerStatus?.running ? t.home.workerRunning : t.home.workerStopped}
                </Badge>
                <Button variant="outline" onClick={toggleWorker} disabled={togglingWorker}>
                  {workerStatus?.running ? <Pause className="size-4" /> : <Play className="size-4" />}
                  {workerStatus?.running ? t.home.stopWorker : t.home.startWorker}
                </Button>
                <Button variant="outline" onClick={queueAllTasks} disabled={requeueingAll}>
                  <Play className="size-4" />
                  {requeueingAll ? t.home.queueingAll : t.home.queueAll}
                </Button>
              </div>
            </div>
          </CardHeader>
          <CardContent className="px-0">
            <div className="border-b border-border/60 px-4 pb-4">
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-[minmax(0,1fr)_140px_140px_180px_120px]">
                <div className="relative sm:col-span-2 lg:col-span-1">
                  <Label htmlFor="task-search" className="sr-only">
                    {t.home.taskSearchPlaceholder}
                  </Label>
                  <Search className="pointer-events-none absolute left-2.5 top-2.5 size-4 text-muted-foreground" />
                  <Input
                    id="task-search"
                    className="h-9 pl-8"
                    value={taskQuery}
                    onChange={(event) => {
                      setTaskQuery(event.target.value)
                      resetTaskPage()
                    }}
                    placeholder={t.home.taskSearchPlaceholder}
                  />
                </div>

                <div>
                  <Label htmlFor="task-status-filter" className="sr-only">
                    {t.home.taskStatusFilter}
                  </Label>
                  <Select
                    value={taskStatus}
                    onValueChange={(value) => {
                      setTaskStatus(value as TaskListStatus)
                      resetTaskPage()
                    }}
                  >
                    <SelectTrigger id="task-status-filter" className="h-9">
                      <span className="min-w-0 truncate text-left">
                        {selectedLabel(statusOptions, taskStatus)}
                      </span>
                    </SelectTrigger>
                    <SelectContent>
                      {statusOptions.map((option) => (
                        <SelectItem key={option.value} value={option.value}>
                          {option.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div>
                  <Label htmlFor="task-mode-filter" className="sr-only">
                    {t.home.taskModeFilter}
                  </Label>
                  <Select
                    value={taskExecutionMode}
                    onValueChange={(value) => {
                      setTaskExecutionMode(value as TaskListExecutionMode)
                      resetTaskPage()
                    }}
                  >
                    <SelectTrigger id="task-mode-filter" className="h-9">
                      <span className="min-w-0 truncate text-left">
                        {selectedLabel(modeOptions, taskExecutionMode)}
                      </span>
                    </SelectTrigger>
                    <SelectContent>
                      {modeOptions.map((option) => (
                        <SelectItem key={option.value} value={option.value}>
                          {option.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div>
                  <Label htmlFor="task-sort" className="sr-only">
                    {t.home.taskSort}
                  </Label>
                  <Select
                    value={taskSort}
                    onValueChange={(value) => {
                      setTaskSort(value as TaskListSort)
                      resetTaskPage()
                    }}
                  >
                    <SelectTrigger id="task-sort" className="h-9">
                      <span className="min-w-0 truncate text-left">
                        {selectedLabel(sortOptions, taskSort)}
                      </span>
                    </SelectTrigger>
                    <SelectContent>
                      {sortOptions.map((option) => (
                        <SelectItem key={option.value} value={option.value}>
                          {option.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>

                <div>
                  <Label htmlFor="task-page-size" className="sr-only">
                    {t.home.taskPageSize}
                  </Label>
                  <Select
                    value={String(taskPageSize)}
                    onValueChange={(value) => {
                      setTaskPageSize(Number(value))
                      resetTaskPage()
                    }}
                  >
                    <SelectTrigger id="task-page-size" className="h-9">
                      <span className="min-w-0 truncate text-left">
                        {taskPageSize}
                      </span>
                    </SelectTrigger>
                    <SelectContent>
                      {PAGE_SIZE_OPTIONS.map((option) => (
                        <SelectItem key={option} value={String(option)}>
                          {option}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
              </div>
            </div>

            {tasks.length === 0 ? (
              <div className="px-6 py-12 text-center text-sm text-muted-foreground">
                {hasTaskFilters ? t.home.noMatchingTasks : t.home.empty}
              </div>
            ) : (
              <ul className="flex flex-col">
                {tasks.map((item) => (
                  <li key={item.id} className="border-b border-border/60 last:border-b-0">
                    <Link
                      href={`/tasks/${item.id}`}
                      className="flex w-full items-center gap-3 px-6 py-2.5 text-sm transition-colors hover:bg-muted/60"
                    >
                      <div className="min-w-0 flex-1">
                        <p className="truncate text-left font-medium text-zinc-900">
                          {item.title || shortUrl(item.url)}
                        </p>
                        <div className="mt-1 flex flex-wrap items-center gap-x-2 gap-y-1 text-xs text-muted-foreground">
                          <Badge className={statusBadgeClass(item.status)}>{statusLabel(item.status)}</Badge>
                          {item.source_author ? <span>{item.source_author}</span> : null}
                          {item.source_published_at ? <span>· {item.source_published_at}</span> : null}
                          <span>{formatTime(item.created_at)}</span>
                          {isActive(item.status) && item.current_stage ? (
                            <span>· {stageLabel(item.current_stage)}</span>
                          ) : null}
                          {isAwaitingAction(item.status) ? (
                            <span>· {t.status.paused}</span>
                          ) : null}
                        </div>
                        <TaskProgressOrPublish
                          item={item}
                          stageLabel={stageLabel}
                          statusLabel={statusLabel}
                          t={t}
                        />
                      </div>
                      <ChevronRight className="size-4 shrink-0 text-muted-foreground" />
                    </Link>
                  </li>
                ))}
              </ul>
            )}

            {taskTotal > 0 ? (
              <div className="flex flex-col gap-3 border-t border-border/60 px-4 py-3 text-xs text-muted-foreground sm:flex-row sm:items-center sm:justify-between">
                <span>{pageRangeText(language, pageStart, pageEnd, taskTotal)}</span>
                <div className="flex items-center justify-between gap-3 sm:justify-end">
                  <span>{pageIndexText(language, displayPage, totalPages)}</span>
                  <div className="flex items-center gap-2">
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => setTaskPage((page) => Math.max(1, page - 1))}
                      disabled={displayPage <= 1}
                    >
                      <ChevronLeft className="size-4" />
                      {t.home.previousPage}
                    </Button>
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      onClick={() => setTaskPage((page) => Math.min(totalPages, page + 1))}
                      disabled={displayPage >= totalPages}
                    >
                      {t.home.nextPage}
                      <ChevronRight className="size-4" />
                    </Button>
                  </div>
                </div>
              </div>
            ) : null}
          </CardContent>
        </Card>
      </div>
    </main>
  )
}
