"use client"

import Link from "next/link"
import { useRouter } from "next/navigation"
import { use, useEffect, useMemo, useRef, useState } from "react"
import {
  ArrowDownToLine,
  CheckCircle2,
  Circle,
  Download,
  FileText,
  Image as ImageIcon,
  Loader2,
  Play,
  RotateCw,
  Send,
  Tags,
  Trash2,
  XCircle,
} from "lucide-react"

import {
  ExecutionMode,
  LocalizedMetadata,
  StageStatus,
  Task,
  cancelTask,
  continueTask,
  deleteTask,
  finalVideoDownloadUrl,
  finalVideoUrl,
  generateLocalizedMetadata,
  getLocalizedMetadata,
  getTask,
  getTaskLog,
  redoStage,
  rerunTask,
  resumeTask,
  thumbnailUrl,
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
import {
  Dialog,
  DialogClose,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog"
import { Progress } from "@/components/ui/progress"

function stageIcon(status: StageStatus) {
  if (status === "succeeded") return <CheckCircle2 className="size-5 text-[#00aeec]" />
  if (status === "failed") return <XCircle className="size-5 text-[#ff0033]" />
  if (status === "running") return <Loader2 className="size-5 animate-spin text-[#fb7299]" />
  return <Circle className="size-5 text-muted-foreground" />
}

function formatTime(value: string | null) {
  if (!value) return ""
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

function durationOf(start: string | null, end: string | null) {
  if (!start) return ""
  const startMs = new Date(start).getTime()
  const endMs = end ? new Date(end).getTime() : Date.now()
  if (!Number.isFinite(startMs) || !Number.isFinite(endMs)) return ""
  const seconds = Math.max(0, Math.round((endMs - startMs) / 1000))
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  const rem = seconds % 60
  return `${minutes}m${rem.toString().padStart(2, "0")}s`
}

function formatVideoDuration(value: number | null) {
  if (!value || value <= 0) return ""
  const seconds = Math.round(value)
  const hours = Math.floor(seconds / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  const rem = seconds % 60
  if (hours > 0) {
    return `${hours}:${minutes.toString().padStart(2, "0")}:${rem.toString().padStart(2, "0")}`
  }
  return `${minutes}:${rem.toString().padStart(2, "0")}`
}

function normalizeProgress(value: number | null | undefined) {
  if (typeof value !== "number") return null
  return Math.max(0, Math.min(100, Math.round(value)))
}

export default function TaskDetailPage({ params }: { params: Promise<{ id: string }> }) {
  const { id } = use(params)
  const router = useRouter()
  const { stageLabel, statusLabel, t } = useI18n()
  const logRef = useRef<HTMLDivElement | null>(null)
  const [task, setTask] = useState<Task | null>(null)
  const [log, setLog] = useState("")
  const [followLog, setFollowLog] = useState(true)
  const [error, setError] = useState("")
  const [deleteOpen, setDeleteOpen] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [deleteError, setDeleteError] = useState("")
  const [rerunOpen, setRerunOpen] = useState(false)
  const [rerunning, setRerunning] = useState(false)
  const [rerunError, setRerunError] = useState("")
  const [resuming, setResuming] = useState(false)
  const [resumeError, setResumeError] = useState("")
  const [continuing, setContinuing] = useState(false)
  const [continueError, setContinueError] = useState("")
  const [cancelOpen, setCancelOpen] = useState(false)
  const [cancelling, setCancelling] = useState(false)
  const [cancelError, setCancelError] = useState("")
  const [redoingStage, setRedoingStage] = useState<string | null>(null)
  const [redoConfirmStage, setRedoConfirmStage] = useState<string | null>(null)
  const [redoError, setRedoError] = useState("")
  const [localizedMetadata, setLocalizedMetadata] = useState<LocalizedMetadata | null>(null)
  const [localizingMetadata, setLocalizingMetadata] = useState(false)
  const [localizedMetadataError, setLocalizedMetadataError] = useState("")

  const handleDelete = async () => {
    setDeleting(true)
    setDeleteError("")
    try {
      await deleteTask(id)
      router.replace("/")
    } catch (err) {
      setDeleteError(err instanceof Error ? err.message : t.task.deleteError)
      setDeleting(false)
    }
  }

  const handleRerun = async () => {
    setRerunning(true)
    setRerunError("")
    try {
      const next = await rerunTask(id)
      setRerunOpen(false)
      setTask(next)
      setLog("")
    } catch (err) {
      setRerunError(err instanceof Error ? err.message : t.task.rerunError)
    } finally {
      setRerunning(false)
    }
  }

  const handleResume = async () => {
    setResuming(true)
    setResumeError("")
    try {
      const next = await resumeTask(id)
      setTask(next)
    } catch (err) {
      setResumeError(err instanceof Error ? err.message : t.task.resumeError)
    } finally {
      setResuming(false)
    }
  }

  const handleContinue = async (executionMode?: ExecutionMode) => {
    setContinuing(true)
    setContinueError("")
    try {
      const next = await continueTask(id, executionMode)
      setTask(next)
    } catch (err) {
      setContinueError(err instanceof Error ? err.message : t.task.continueError)
    } finally {
      setContinuing(false)
    }
  }

  const handleCancelTask = async () => {
    setCancelling(true)
    setCancelError("")
    try {
      const next = await cancelTask(id)
      setTask(next)
      setCancelOpen(false)
    } catch (err) {
      setCancelError(err instanceof Error ? err.message : t.task.cancelError)
    } finally {
      setCancelling(false)
    }
  }

  const handleRedoStage = async (stageName: string) => {
    setRedoingStage(stageName)
    setRedoError("")
    try {
      const next = await redoStage(id, stageName)
      setTask(next)
      return true
    } catch (err) {
      setRedoError(err instanceof Error ? err.message : t.task.redoStageError)
      return false
    } finally {
      setRedoingStage(null)
    }
  }

  const handleConfirmRedoStage = async () => {
    if (!redoConfirmStage) return
    const succeeded = await handleRedoStage(redoConfirmStage)
    if (succeeded) setRedoConfirmStage(null)
  }

  const handleLocalizeMetadata = async () => {
    setLocalizingMetadata(true)
    setLocalizedMetadataError("")
    try {
      const next = await generateLocalizedMetadata(id)
      setLocalizedMetadata(next)
    } catch (err) {
      setLocalizedMetadataError(err instanceof Error ? err.message : t.task.localizedMetadataError)
    } finally {
      setLocalizingMetadata(false)
    }
  }

  const scrollLogToBottom = (behavior: ScrollBehavior = "smooth") => {
    const element = logRef.current
    if (!element) return
    element.scrollTo({ top: element.scrollHeight, behavior })
  }

  const isRunning = task?.status === "running"
  const isQueued = task?.status === "queued"
  const isFailed = task?.status === "failed"
  const isPaused = task?.status === "paused"
  const isCancelled = task?.status === "cancelled"
  const canContinue = isPaused || isCancelled
  const isCancellable = Boolean(task && ["queued", "running", "paused"].includes(task.status))
  const isManual = task?.execution_mode === "manual"
  const canRedoStage = isManual && !isRunning && !isQueued
  const redoConfirmStageInfo = task?.stages.find((stage) => stage.name === redoConfirmStage)

  useEffect(() => {
    let cancelled = false
    const load = async () => {
      try {
        const next = await getTask(id)
        if (cancelled) return
        setTask(next)
        const logText = await getTaskLog(id)
        if (cancelled) return
        setLog(logText)
        getLocalizedMetadata(id)
          .then((metadata) => {
            if (!cancelled) setLocalizedMetadata(metadata)
          })
          .catch(() => {
            if (!cancelled) setLocalizedMetadata(null)
          })
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : t.task.loadError)
      }
    }
    load()
    const interval = window.setInterval(load, 2000)
    return () => {
      cancelled = true
      window.clearInterval(interval)
    }
  }, [id, t.task.loadError])

  useEffect(() => {
    if (!followLog) return
    window.requestAnimationFrame(() => scrollLogToBottom("auto"))
  }, [followLog, log])

  const progress = useMemo(() => {
    if (!task?.stages?.length) return 0
    const completed = task.stages.filter((stage) => stage.status === "succeeded").length
    return Math.round((completed / task.stages.length) * 100)
  }, [task])

  if (error && !task) {
    return (
      <main className="min-h-screen bg-[linear-gradient(135deg,#fff5f5_0%,#f2fbff_48%,#fff4fa_100%)] text-foreground">
        <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 px-4 py-6 sm:px-6 lg:px-8">
          <AppHeader backHref="/" />
          <Card>
            <CardContent className="px-6 py-10 text-sm text-red-600">{error}</CardContent>
          </Card>
        </div>
      </main>
    )
  }

  return (
    <main className="min-h-screen bg-[linear-gradient(135deg,#fff5f5_0%,#f2fbff_48%,#fff4fa_100%)] text-foreground">
      <div className="mx-auto flex w-full max-w-4xl flex-col gap-6 px-4 py-6 sm:px-6 lg:px-8">
        <AppHeader backHref="/" />

        <Card>
          <CardHeader className="gap-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <CardTitle>{t.task.overview}</CardTitle>
              <Badge className={statusBadgeClass(task?.status)}>{statusLabel(task?.status)}</Badge>
            </div>
            <Progress value={progress} />
          </CardHeader>
          <CardContent>
            {task ? (
              <div className="grid gap-4 md:grid-cols-[220px_1fr]">
                {task.thumbnail_url ? (
                  // eslint-disable-next-line @next/next/no-img-element
                  <img
                    src={task.thumbnail_url}
                    alt={task.title || task.id}
                    className="aspect-video w-full rounded-md border border-border object-cover"
                  />
                ) : (
                  <div className="hidden aspect-video items-center justify-center rounded-md border border-dashed text-xs text-muted-foreground md:flex">
                    {t.task.cover}
                  </div>
                )}
                <dl className="grid grid-cols-1 gap-x-6 gap-y-2 text-sm sm:grid-cols-[120px_1fr]">
                  {task.title ? (
                    <>
                      <dt className="text-muted-foreground">{t.task.title}</dt>
                      <dd className="break-words font-medium">{task.title}</dd>
                    </>
                  ) : null}
                  {task.source_author ? (
                    <>
                      <dt className="text-muted-foreground">{t.task.author}</dt>
                      <dd className="break-words">{task.source_author}</dd>
                    </>
                  ) : null}
                  {task.source_published_at ? (
                    <>
                      <dt className="text-muted-foreground">{t.task.publishedAt}</dt>
                      <dd>{task.source_published_at}</dd>
                    </>
                  ) : null}
                  <dt className="text-muted-foreground">URL</dt>
                  <dd className="break-all">
                    <a href={task.url} target="_blank" rel="noreferrer" className="text-[#00aeec] hover:underline">
                      {task.url}
                    </a>
                  </dd>
                  <dt className="text-muted-foreground">{t.task.taskId}</dt>
                  <dd className="font-mono text-xs">{task.id}</dd>
                  <dt className="text-muted-foreground">{t.task.created}</dt>
                  <dd>{formatTime(task.created_at)}</dd>
                  {task.duration_seconds ? (
                    <>
                      <dt className="text-muted-foreground">{t.task.duration}</dt>
                      <dd>{formatVideoDuration(task.duration_seconds)}</dd>
                    </>
                  ) : null}
                  <dt className="text-muted-foreground">{t.task.started}</dt>
                  <dd>{formatTime(task.started_at)}</dd>
                  <dt className="text-muted-foreground">{t.task.completed}</dt>
                  <dd>{formatTime(task.completed_at) || "—"}</dd>
                  <dt className="text-muted-foreground">{t.task.executionMode}</dt>
                  <dd>
                    {task.execution_mode === "manual" ? t.task.executionManual : t.task.executionAuto}
                  </dd>
                  {task.session_path ? (
                    <>
                      <dt className="text-muted-foreground">{t.task.session}</dt>
                      <dd className="break-all text-xs text-muted-foreground">{task.session_path}</dd>
                    </>
                  ) : null}
                  {task.source_description ? (
                    <>
                      <dt className="text-muted-foreground">{t.task.sourceDescription}</dt>
                      <dd className="line-clamp-8 whitespace-pre-wrap text-muted-foreground">
                        {task.source_description}
                      </dd>
                    </>
                  ) : null}
                </dl>
              </div>
            ) : (
              <div className="py-6 text-center text-sm text-muted-foreground">{t.task.loading}</div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="gap-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <CardTitle>{t.task.localizedMetadata}</CardTitle>
                <p className="mt-1 text-sm text-muted-foreground">{t.task.localizedMetadataHelp}</p>
              </div>
              <Button onClick={handleLocalizeMetadata} disabled={localizingMetadata || !task?.session_path}>
                {localizingMetadata ? <Loader2 className="size-4 animate-spin" /> : <Tags className="size-4" />}
                {localizingMetadata ? t.task.localizingMetadata : t.task.localizeMetadata}
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            {localizedMetadataError ? (
              <div className="mb-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                {localizedMetadataError}
              </div>
            ) : null}
            {localizedMetadata ? (
              <div className="grid gap-4 md:grid-cols-[220px_1fr]">
                <div className="space-y-2">
                  <div className="flex items-center gap-2 text-sm font-medium">
                    <ImageIcon className="size-4" />
                    {t.task.cover}
                  </div>
                  {localizedMetadata.thumbnail_api_url || localizedMetadata.thumbnail_url ? (
                    // eslint-disable-next-line @next/next/no-img-element
                    <img
                      src={
                        localizedMetadata.thumbnail_api_url
                          ? thumbnailUrl(localizedMetadata.thumbnail_api_url)
                          : localizedMetadata.thumbnail_url
                      }
                      alt={localizedMetadata.translated_title || localizedMetadata.title}
                      className="aspect-video w-full rounded-md border border-border object-cover"
                    />
                  ) : (
                    <div className="flex aspect-video items-center justify-center rounded-md border border-dashed text-xs text-muted-foreground">
                      {t.task.noMetadata}
                    </div>
                  )}
                </div>
                <div className="grid gap-4 text-sm">
                  <div className="grid gap-2">
                    <Badge variant="secondary" className="w-fit">{t.task.translatedMetadata}</Badge>
                    <p className="font-medium">{localizedMetadata.translated_title || localizedMetadata.title}</p>
                    {localizedMetadata.translated_description ? (
                      <p className="whitespace-pre-wrap text-muted-foreground">{localizedMetadata.translated_description}</p>
                    ) : null}
                    {localizedMetadata.translated_tags.length ? (
                      <div className="flex flex-wrap gap-2">
                        {localizedMetadata.translated_tags.map((tag) => (
                          <Badge key={tag} variant="outline">{tag}</Badge>
                        ))}
                      </div>
                    ) : null}
                  </div>
                  <div className="grid gap-2 border-t pt-3">
                    <Badge variant="secondary" className="w-fit">{t.task.sourceMetadata}</Badge>
                    <p className="font-medium">{localizedMetadata.title}</p>
                    {localizedMetadata.description ? (
                      <p className="line-clamp-6 whitespace-pre-wrap text-muted-foreground">{localizedMetadata.description}</p>
                    ) : null}
                    {localizedMetadata.tags.length ? (
                      <div className="flex flex-wrap gap-2">
                        {localizedMetadata.tags.map((tag) => (
                          <Badge key={tag} variant="outline">{tag}</Badge>
                        ))}
                      </div>
                    ) : null}
                  </div>
                </div>
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">{t.task.noMetadata}</p>
            )}
          </CardContent>
        </Card>

        {task?.status === "succeeded" && task.final_video_path ? (
          <Card>
            <CardHeader>
              <CardTitle>{t.task.finalVideo}</CardTitle>
            </CardHeader>
            <CardContent className="space-y-3">
              <video
                key={task.id}
                src={finalVideoUrl(task.id)}
                controls
                preload="metadata"
                className="w-full rounded-md border border-emerald-200 bg-black"
              />
              <p className="break-all text-xs text-muted-foreground">{task.final_video_path}</p>
              <div className="flex flex-wrap gap-2">
                <Button nativeButton={false} render={<a href={finalVideoDownloadUrl(task.id)} />}>
                  <Download className="size-4" />
                  {t.task.download}
                </Button>
                <Button variant="outline" nativeButton={false} render={<Link href="/bilibili" />}>
                  <Send className="size-4" />
                  {t.task.bilibiliPublish}
                </Button>
              </div>
            </CardContent>
          </Card>
        ) : null}

        <Card>
          <CardHeader>
            <CardTitle>{t.task.stages}</CardTitle>
          </CardHeader>
          <CardContent>
            {isManual && canRedoStage ? (
              <p className="mb-3 text-sm text-muted-foreground">{t.task.redoStageHelp}</p>
            ) : null}
            {redoError ? (
              <div className="mb-3 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                {redoError}
              </div>
            ) : null}
            {task ? (
              <ol className="grid gap-3">
                {task.stages.map((stage, index) => {
                  const stageProgress = normalizeProgress(stage.progress)
                  const showRedo =
                    canRedoStage && (stage.status === "succeeded" || stage.status === "failed")
                  return (
                    <li
                      key={stage.name}
                      className="flex items-start gap-3 rounded-lg border border-border bg-background px-4 py-3"
                    >
                      <div className="mt-0.5">{stageIcon(stage.status)}</div>
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-center gap-2">
                          <span className="text-xs text-muted-foreground">#{index + 1}</span>
                          <p className="font-medium">{stageLabel(stage.name, stage.label)}</p>
                          <Badge className={statusBadgeClass(stage.status)}>{statusLabel(stage.status)}</Badge>
                          {stage.started_at ? (
                            <span className="text-xs text-muted-foreground">
                              {durationOf(stage.started_at, stage.completed_at)}
                            </span>
                          ) : null}
                          {showRedo ? (
                            <Button
                              variant="outline"
                              size="sm"
                              className="ml-auto h-7 px-2 text-xs"
                              disabled={redoingStage !== null}
                              onClick={() => setRedoConfirmStage(stage.name)}
                            >
                              {redoingStage === stage.name ? (
                                <Loader2 className="size-3 animate-spin" />
                              ) : (
                                <RotateCw className="size-3" />
                              )}
                              {redoingStage === stage.name ? t.task.redoingStage : t.task.redoStage}
                            </Button>
                          ) : null}
                        </div>
                        <p className="mt-1 text-sm text-muted-foreground">
                          {stage.error_message || stage.last_message || t.common.waiting}
                        </p>
                        {stage.status === "running" && stageProgress !== null ? (
                          <div className="mt-2 flex items-center gap-3">
                            <Progress value={stageProgress} className="min-w-0 flex-1" />
                            <span className="w-10 text-right text-xs tabular-nums text-muted-foreground">
                              {stageProgress}%
                            </span>
                          </div>
                        ) : null}
                      </div>
                    </li>
                  )
                })}
              </ol>
            ) : null}

            {task?.error_message ? (
              <div className="mt-4 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                {task.error_message}
              </div>
            ) : null}
            <Dialog
              open={redoConfirmStage !== null}
              onOpenChange={(open) => {
                if (!open && redoingStage === null) setRedoConfirmStage(null)
              }}
            >
              <DialogContent>
                <DialogHeader>
                  <DialogTitle>{t.task.redoStageTitle}</DialogTitle>
                  <DialogDescription>
                    {t.task.redoStageDescription} {redoConfirmStageInfo ? stageLabel(redoConfirmStageInfo.name, redoConfirmStageInfo.label) : ""}
                    {t.common.sentenceEnd}
                  </DialogDescription>
                </DialogHeader>
                <DialogFooter>
                  <DialogClose render={<Button variant="outline" disabled={redoingStage !== null} />}>
                    {t.common.cancel}
                  </DialogClose>
                  <Button onClick={handleConfirmRedoStage} disabled={redoingStage !== null}>
                    {redoingStage ? <Loader2 className="size-4 animate-spin" /> : <RotateCw className="size-4" />}
                    {redoingStage ? t.task.redoingStage : t.task.confirmRedoStage}
                  </Button>
                </DialogFooter>
              </DialogContent>
            </Dialog>
            {canContinue ? (
              <div className="mt-4 space-y-3 rounded-lg border border-sky-200 bg-sky-50 px-3 py-3">
                <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                  <p className="text-sm text-sky-900">{t.task.continueHelp}</p>
                  <Button onClick={() => handleContinue()} disabled={continuing}>
                    {continuing ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}
                    {continuing ? t.task.continuing : t.task.continueTask}
                  </Button>
                </div>
                <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
                  <p className="text-sm text-sky-900">{t.task.continueAutoHelp}</p>
                  <Button variant="outline" onClick={() => handleContinue("auto")} disabled={continuing}>
                    {continuing ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}
                    {continuing ? t.task.continuing : t.task.continueAutoTask}
                  </Button>
                </div>
              </div>
            ) : null}
            {continueError ? (
              <div className="mt-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                {continueError}
              </div>
            ) : null}
            {isFailed ? (
              <div className="mt-4 flex flex-col gap-2 rounded-lg border border-amber-200 bg-amber-50 px-3 py-3 sm:flex-row sm:items-center sm:justify-between">
                <p className="text-sm text-amber-800">
                  {t.task.resumeHelp}
                </p>
                <Button onClick={handleResume} disabled={resuming}>
                  {resuming ? <Loader2 className="size-4 animate-spin" /> : <Play className="size-4" />}
                  {resuming ? t.task.resuming : t.task.resumeTask}
                </Button>
              </div>
            ) : null}
            {resumeError ? (
              <div className="mt-2 rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                {resumeError}
              </div>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-3">
            <CardTitle>{t.task.runLog}</CardTitle>
            <div className="flex flex-wrap items-center gap-2">
              <label className="flex items-center gap-2 text-xs text-muted-foreground">
                <input
                  type="checkbox"
                  className="size-4 rounded border-input accent-[#00aeec]"
                  checked={followLog}
                  onChange={(event) => setFollowLog(event.target.checked)}
                />
                <span>{t.task.followLog}</span>
              </label>
              <Button type="button" variant="outline" size="sm" onClick={() => scrollLogToBottom()}>
                <ArrowDownToLine className="size-4" />
                {t.task.scrollLogBottom}
              </Button>
              <FileText className="size-4 text-muted-foreground" />
            </div>
          </CardHeader>
          <CardContent>
            <div
              ref={logRef}
              className="h-80 overflow-y-auto rounded-lg border bg-zinc-950 p-3 text-xs text-zinc-100 [scrollbar-gutter:stable]"
            >
              {log ? (
                <pre className="whitespace-pre-wrap break-words font-mono">{log}</pre>
              ) : (
                <p className="text-zinc-400">{t.task.emptyLog}</p>
              )}
            </div>
          </CardContent>
        </Card>

        <Card className="border-red-200">
          <CardHeader>
            <CardTitle className="text-red-700">{t.task.dangerZone}</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-col gap-4">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <p className="text-sm text-muted-foreground">
                {t.task.rerunHelp}
              </p>
              <Dialog open={rerunOpen} onOpenChange={setRerunOpen}>
                <DialogTrigger
                  render={
                    <Button variant="outline" disabled={!task || isRunning}>
                      <RotateCw className="size-4" />
                      {t.task.rerunTask}
                    </Button>
                  }
                />
                <DialogContent>
                  <DialogHeader>
                    <DialogTitle>{t.task.rerunTitle}</DialogTitle>
                    <DialogDescription>
                      {t.task.rerunDescription}
                    </DialogDescription>
                  </DialogHeader>
                  {rerunError ? (
                    <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                      {rerunError}
                    </div>
                  ) : null}
                  <DialogFooter>
                    <DialogClose render={<Button variant="outline" disabled={rerunning} />}>
                      {t.common.cancel}
                    </DialogClose>
                    <Button onClick={handleRerun} disabled={rerunning}>
                      {rerunning ? <Loader2 className="size-4 animate-spin" /> : <RotateCw className="size-4" />}
                      {rerunning ? t.task.rerunning : t.task.confirmRerun}
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            </div>
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <p className="text-sm text-muted-foreground">
                {t.task.cancelHelp}
              </p>
              <Dialog open={cancelOpen} onOpenChange={setCancelOpen}>
                <DialogTrigger
                  render={
                    <Button variant="outline" disabled={!isCancellable}>
                      <XCircle className="size-4" />
                      {t.task.cancelTask}
                    </Button>
                  }
                />
                <DialogContent>
                  <DialogHeader>
                    <DialogTitle>{t.task.cancelTitle}</DialogTitle>
                    <DialogDescription>
                      {t.task.cancelDescription}
                    </DialogDescription>
                  </DialogHeader>
                  {cancelError ? (
                    <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                      {cancelError}
                    </div>
                  ) : null}
                  <DialogFooter>
                    <DialogClose render={<Button variant="outline" disabled={cancelling} />}>
                      {t.common.cancel}
                    </DialogClose>
                    <Button variant="destructive" onClick={handleCancelTask} disabled={cancelling}>
                      {cancelling ? <Loader2 className="size-4 animate-spin" /> : <XCircle className="size-4" />}
                      {cancelling ? t.task.cancelling : t.task.confirmCancel}
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            </div>
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <p className="text-sm text-muted-foreground">
                {t.task.deleteHelp} <code className="font-mono text-xs">workfolder/</code>
                {t.common.sentenceEnd}
              </p>
              <Dialog open={deleteOpen} onOpenChange={setDeleteOpen}>
                <DialogTrigger
                  render={
                    <Button variant="destructive" disabled={!task || isRunning}>
                      <Trash2 className="size-4" />
                      {t.task.deleteTask}
                    </Button>
                  }
                />
                <DialogContent>
                  <DialogHeader>
                    <DialogTitle>{t.task.deleteTitle}</DialogTitle>
                    <DialogDescription>
                      {t.task.deleteDescription}
                    </DialogDescription>
                  </DialogHeader>
                  {deleteError ? (
                    <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
                      {deleteError}
                    </div>
                  ) : null}
                  <DialogFooter>
                    <DialogClose render={<Button variant="outline" disabled={deleting} />}>
                      {t.common.cancel}
                    </DialogClose>
                    <Button variant="destructive" onClick={handleDelete} disabled={deleting}>
                      {deleting ? <Loader2 className="size-4 animate-spin" /> : <Trash2 className="size-4" />}
                      {deleting ? t.task.deleting : t.task.confirmDelete}
                    </Button>
                  </DialogFooter>
                </DialogContent>
              </Dialog>
            </div>
            {isRunning ? (
              <p className="text-xs text-amber-600">{t.task.runningLocked}</p>
            ) : null}
          </CardContent>
        </Card>
      </div>
    </main>
  )
}
