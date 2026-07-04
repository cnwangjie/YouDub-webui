"use client"

import Link from "next/link"
import QRCode from "qrcode"
import { useEffect, useMemo, useRef, useState } from "react"
import { ExternalLink, Loader2, QrCode, RotateCw, Send } from "lucide-react"

import {
  BilibiliAccountArchive,
  BilibiliCredentialsStatus,
  BilibiliPartition,
  BilibiliPublishRecord,
  clearBilibiliCredentials,
  createBilibiliQrCode,
  getBilibiliCredentialsStatus,
  listBilibiliAccountArchives,
  listBilibiliPartitions,
  listBilibiliPublishRecords,
  pollBilibiliQrCode,
  startBilibiliPublish,
  thumbnailUrl,
} from "@/lib/api"
import { useI18n } from "@/lib/i18n"
import { AppHeader } from "@/components/app-header"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Input } from "@/components/ui/input"
import { Progress } from "@/components/ui/progress"
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select"
import { Textarea } from "@/components/ui/textarea"

type Filter = "all" | "draft" | "published"
type PublishEdit = {
  title: string
  description: string
  source: string
  tags: string
  tid: string
}

function formatTime(value: string | null) {
  if (!value) return ""
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return date.toLocaleString()
}

function normalizeProgress(value: number) {
  return Math.max(0, Math.min(100, Math.round(value || 0)))
}

export default function BilibiliPage() {
  const { t } = useI18n()
  const [credentials, setCredentials] = useState<BilibiliCredentialsStatus | null>(null)
  const [records, setRecords] = useState<BilibiliPublishRecord[]>([])
  const [accountArchives, setAccountArchives] = useState<BilibiliAccountArchive[]>([])
  const [accountArchivesTotal, setAccountArchivesTotal] = useState(0)
  const [partitions, setPartitions] = useState<BilibiliPartition[]>([])
  const [publishEdits, setPublishEdits] = useState<Record<string, PublishEdit>>({})
  const [filter, setFilter] = useState<Filter>("all")
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState("")
  const [qr, setQr] = useState<{ authCode: string; url: string } | null>(null)
  const [qrMessage, setQrMessage] = useState("")
  const [creatingQr, setCreatingQr] = useState(false)
  const [publishingTask, setPublishingTask] = useState<string | null>(null)
  const qrCanvasRef = useRef<HTMLCanvasElement | null>(null)

  const load = async () => {
    const [nextCredentials, nextRecords] = await Promise.all([
      getBilibiliCredentialsStatus(),
      listBilibiliPublishRecords(),
    ])
    setCredentials(nextCredentials)
    setRecords(nextRecords.records)
    setPublishEdits((current) => {
      const next = { ...current }
      for (const record of nextRecords.records) {
        if (next[record.task_id]) continue
        next[record.task_id] = {
          title: record.title || "",
          description: record.description || "",
          source: record.source || "",
          tags: (record.tags || []).join(", "),
          tid: record.tid ? String(record.tid) : "",
        }
      }
      return next
    })
    if (nextCredentials.configured) {
      const [nextArchives, nextPartitions] = await Promise.all([
        listBilibiliAccountArchives({ page: 1, page_size: 20 }),
        listBilibiliPartitions(),
      ])
      setAccountArchives(nextArchives.archives)
      setAccountArchivesTotal(nextArchives.page.total)
      setPartitions(nextPartitions.partitions)
    } else {
      setAccountArchives([])
      setAccountArchivesTotal(0)
      setPartitions([])
    }
  }

  useEffect(() => {
    let cancelled = false
    const run = async () => {
      try {
        await load()
        if (!cancelled) setError("")
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : t.bilibili.loadError)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }
    run()
    const interval = window.setInterval(run, 3000)
    return () => {
      cancelled = true
      window.clearInterval(interval)
    }
  }, [t.bilibili.loadError])

  useEffect(() => {
    if (!qr?.authCode) return
    let cancelled = false
    const interval = window.setInterval(async () => {
      try {
        const result = await pollBilibiliQrCode(qr.authCode)
        if (cancelled) return
        if (result.status === "pending") {
          setQrMessage(result.message || t.bilibili.waitingScan)
          return
        }
        if (result.status === "succeeded") {
          setQrMessage(t.bilibili.loginSaved)
          setQr(null)
          setCredentials(await getBilibiliCredentialsStatus())
          window.clearInterval(interval)
          return
        }
        setError(result.message || t.bilibili.loginError)
        window.clearInterval(interval)
      } catch (err) {
        if (!cancelled) setError(err instanceof Error ? err.message : t.bilibili.loginError)
      }
    }, 2000)
    return () => {
      cancelled = true
      window.clearInterval(interval)
    }
  }, [qr, t.bilibili.loginError, t.bilibili.loginSaved, t.bilibili.waitingScan])

  useEffect(() => {
    if (!qr?.url || !qrCanvasRef.current) return
    QRCode.toCanvas(qrCanvasRef.current, qr.url, {
      errorCorrectionLevel: "M",
      margin: 2,
      scale: 6,
      width: 180,
    }).catch((err: unknown) => {
      setError(err instanceof Error ? err.message : t.bilibili.qrError)
    })
  }, [qr?.url, t.bilibili.qrError])

  const filteredRecords = useMemo(() => {
    if (filter === "all") return records
    return records.filter((record) => record.type === filter)
  }, [filter, records])

  const createQr = async () => {
    setCreatingQr(true)
    setError("")
    setQrMessage("")
    try {
      const next = await createBilibiliQrCode()
      setQr({ authCode: next.data.auth_code, url: next.data.url })
    } catch (err) {
      setError(err instanceof Error ? err.message : t.bilibili.qrError)
    } finally {
      setCreatingQr(false)
    }
  }

  const clearLogin = async () => {
    setError("")
    setQr(null)
    setQrMessage("")
    try {
      await clearBilibiliCredentials()
      setCredentials({ configured: false })
    } catch (err) {
      setError(err instanceof Error ? err.message : t.bilibili.loginError)
    }
  }

  const updatePublishEdit = (taskId: string, field: keyof PublishEdit, value: string) => {
    setPublishEdits((current) => {
      const existing = current[taskId] || {
        title: "",
        description: "",
        source: "",
        tags: "",
        tid: "",
      }
      return {
        ...current,
        [taskId]: {
          ...existing,
          [field]: value,
        },
      }
    })
  }

  const publish = async (taskId: string) => {
    setPublishingTask(taskId)
    setError("")
    try {
      const edit = publishEdits[taskId]
      await startBilibiliPublish(taskId, {
        title: edit?.title || "",
        description: edit?.description || "",
        source: edit?.source || "",
        tags: (edit?.tags || "")
          .replaceAll("，", ",")
          .split(/[,\n]/)
          .map((tag) => tag.trim())
          .filter(Boolean),
        tid: edit?.tid ? Number(edit.tid) : null,
      })
      await load()
    } catch (err) {
      setError(err instanceof Error ? err.message : t.bilibili.publishError)
    } finally {
      setPublishingTask(null)
    }
  }

  const statusLabel = (record: BilibiliPublishRecord) => {
    if (record.publish_status === "running") return t.bilibili.running
    if (record.publish_status === "succeeded") return t.bilibili.succeeded
    if (record.publish_status === "failed") return t.bilibili.failed
    return t.bilibili.draft
  }

  return (
    <main className="min-h-screen bg-[linear-gradient(135deg,#fff5f5_0%,#f2fbff_48%,#fff4fa_100%)] text-foreground">
      <div className="mx-auto flex w-full max-w-6xl flex-col gap-6 px-4 py-6 sm:px-6 lg:px-8">
        <AppHeader backHref="/" />

        <section>
          <h1 className="text-3xl font-bold tracking-tight">{t.bilibili.title}</h1>
          <p className="mt-2 text-sm text-muted-foreground">{t.bilibili.description}</p>
        </section>

        {error ? (
          <div className="rounded-lg border border-red-200 bg-red-50 px-3 py-2 text-sm text-red-700">
            {error}
          </div>
        ) : null}

        <Card>
          <CardHeader className="gap-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <CardTitle>{t.bilibili.loginTitle}</CardTitle>
                <p className="mt-1 text-sm text-muted-foreground">
                  {credentials?.configured ? t.bilibili.loginConfigured : t.bilibili.loginMissing}
                </p>
              </div>
              <div className="flex flex-wrap gap-2">
                <Button variant="outline" onClick={createQr} disabled={creatingQr}>
                  {creatingQr ? <Loader2 className="size-4 animate-spin" /> : <QrCode className="size-4" />}
                  {creatingQr ? t.bilibili.creatingQr : credentials?.configured ? t.bilibili.relogin : t.bilibili.scanLogin}
                </Button>
                {credentials?.configured ? (
                  <Button variant="outline" onClick={clearLogin}>
                    {t.bilibili.clearLogin}
                  </Button>
                ) : null}
              </div>
            </div>
          </CardHeader>
          {qr ? (
            <CardContent>
              <div className="flex flex-col gap-3 sm:flex-row sm:items-center">
                <canvas ref={qrCanvasRef} aria-label="Bilibili QR login" className="size-[180px] rounded-md border bg-white p-2" />
                <div className="min-w-0 text-sm">
                  <p className="font-medium">{t.bilibili.qrHelp}</p>
                  <p className="mt-1 text-muted-foreground">{qrMessage || t.bilibili.waitingScan}</p>
                  <a href={qr.url} target="_blank" rel="noreferrer" className="mt-2 block break-all text-[#00aeec] hover:underline">
                    {qr.url}
                  </a>
                </div>
              </div>
            </CardContent>
          ) : null}
        </Card>

        <Card>
          <CardHeader className="gap-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <CardTitle>{t.bilibili.publishRecords}</CardTitle>
              <div className="flex flex-wrap gap-2">
                {(["all", "draft", "published"] as const).map((item) => (
                  <Button
                    key={item}
                    variant={filter === item ? "default" : "outline"}
                    onClick={() => setFilter(item)}
                  >
                    {item === "all" ? t.bilibili.all : item === "draft" ? t.bilibili.drafts : t.bilibili.published}
                  </Button>
                ))}
                <Button variant="outline" onClick={load}>
                  <RotateCw className="size-4" />
                </Button>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            {loading ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="size-4 animate-spin" />
                {t.common.loading}
              </div>
            ) : filteredRecords.length ? (
              <div className="grid gap-3">
                {filteredRecords.map((record) => (
                  <article key={record.task_id} className="grid gap-3 rounded-lg border bg-background p-3 sm:grid-cols-[160px_1fr]">
                    <div className="aspect-video overflow-hidden rounded-md border bg-muted">
                      {record.thumbnail_api_url ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img src={thumbnailUrl(record.thumbnail_api_url)} alt="" className="h-full w-full object-cover" />
                      ) : (
                        <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
                          Bilibili
                        </div>
                      )}
                    </div>
                    <div className="min-w-0 space-y-3">
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div className="min-w-0">
                          <h2 className="truncate font-medium">{record.title}</h2>
                          <p className="mt-1 text-xs text-muted-foreground">
                            {formatTime(record.updated_at || record.completed_at || record.created_at)}
                          </p>
                        </div>
                        <Badge variant={record.publish_status === "failed" ? "destructive" : "secondary"}>
                          {statusLabel(record)}
                        </Badge>
                      </div>
                      {record.publish_status === "running" ? (
                        <div className="flex items-center gap-3">
                          <Progress value={normalizeProgress(record.progress)} className="min-w-0 flex-1" />
                          <span className="w-10 text-right text-xs tabular-nums text-muted-foreground">
                            {normalizeProgress(record.progress)}%
                          </span>
                        </div>
                      ) : null}
                      {record.message || record.error ? (
                        <p className={record.error ? "text-sm text-red-600" : "text-sm text-muted-foreground"}>
                          {record.error || record.message}
                        </p>
                      ) : null}
                      {record.tags.length ? (
                        <div className="flex flex-wrap gap-2">
                          {record.tags.slice(0, 8).map((tag) => (
                            <Badge key={tag} variant="outline">{tag}</Badge>
                          ))}
                        </div>
                      ) : null}
                      {record.type !== "published" ? (
                        <div className="grid gap-3 rounded-lg border bg-muted/30 p-3">
                          <div className="grid gap-1">
                            <label className="text-xs font-medium text-muted-foreground" htmlFor={`bili-title-${record.task_id}`}>
                              {t.bilibili.metadataTitle}
                            </label>
                            <Input
                              id={`bili-title-${record.task_id}`}
                              maxLength={80}
                              value={publishEdits[record.task_id]?.title || ""}
                              onChange={(event) => updatePublishEdit(record.task_id, "title", event.target.value)}
                            />
                          </div>
                          <div className="grid gap-1">
                            <label className="text-xs font-medium text-muted-foreground" htmlFor={`bili-desc-${record.task_id}`}>
                              {t.bilibili.metadataDescription}
                            </label>
                            <Textarea
                              id={`bili-desc-${record.task_id}`}
                              maxLength={2000}
                              rows={5}
                              value={publishEdits[record.task_id]?.description || ""}
                              onChange={(event) => updatePublishEdit(record.task_id, "description", event.target.value)}
                            />
                          </div>
                          <div className="grid gap-3 md:grid-cols-2">
                            <div className="grid gap-1">
                              <label className="text-xs font-medium text-muted-foreground" htmlFor={`bili-tid-${record.task_id}`}>
                                {t.bilibili.metadataPartition}
                              </label>
                              <Select
                                value={publishEdits[record.task_id]?.tid || ""}
                                onValueChange={(value) => updatePublishEdit(record.task_id, "tid", value ?? "")}
                              >
                                <SelectTrigger id={`bili-tid-${record.task_id}`}>
                                  <SelectValue placeholder={t.bilibili.selectPartition} />
                                </SelectTrigger>
                                <SelectContent>
                                  {partitions.map((partition) => (
                                    <SelectItem key={partition.id} value={String(partition.id)}>
                                      {partition.label}
                                    </SelectItem>
                                  ))}
                                </SelectContent>
                              </Select>
                            </div>
                            <div className="grid gap-1">
                              <label className="text-xs font-medium text-muted-foreground" htmlFor={`bili-source-${record.task_id}`}>
                                {t.bilibili.metadataSource}
                              </label>
                              <Input
                                id={`bili-source-${record.task_id}`}
                                value={publishEdits[record.task_id]?.source || ""}
                                onChange={(event) => updatePublishEdit(record.task_id, "source", event.target.value)}
                              />
                            </div>
                            <div className="grid gap-1 md:col-span-2">
                              <label className="text-xs font-medium text-muted-foreground" htmlFor={`bili-tags-${record.task_id}`}>
                                {t.bilibili.metadataTags}
                              </label>
                              <Input
                                id={`bili-tags-${record.task_id}`}
                                value={publishEdits[record.task_id]?.tags || ""}
                                onChange={(event) => updatePublishEdit(record.task_id, "tags", event.target.value)}
                              />
                            </div>
                          </div>
                        </div>
                      ) : null}
                      <div className="flex flex-wrap gap-2">
                        <Button variant="outline" nativeButton={false} render={<Link href={`/tasks/${record.task_id}`} />}>
                          {t.bilibili.openTask}
                        </Button>
                        {record.draft_url ? (
                          <Button variant="outline" nativeButton={false} render={<a href={record.draft_url} target="_blank" rel="noreferrer" />}>
                            {t.bilibili.openDraft}
                          </Button>
                        ) : null}
                        {record.url ? (
                          <Button variant="outline" nativeButton={false} render={<a href={record.url} target="_blank" rel="noreferrer" />}>
                            <ExternalLink className="size-4" />
                            {t.bilibili.openBilibili}
                          </Button>
                        ) : null}
                        {record.type !== "published" ? (
                          <Button
                            onClick={() => publish(record.task_id)}
                            disabled={
                              publishingTask === record.task_id ||
                              !credentials?.configured ||
                              !record.final_video_available ||
                              record.publish_status === "running"
                            }
                          >
                            {publishingTask === record.task_id || record.publish_status === "running" ? (
                              <Loader2 className="size-4 animate-spin" />
                            ) : (
                              <Send className="size-4" />
                            )}
                            {!record.final_video_available ? t.bilibili.finalVideoMissing : t.bilibili.publish}
                          </Button>
                        ) : null}
                      </div>
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">{t.bilibili.empty}</p>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="gap-3">
            <div className="flex flex-wrap items-center justify-between gap-3">
              <div>
                <CardTitle>{t.bilibili.accountArchives}</CardTitle>
                <p className="mt-1 text-sm text-muted-foreground">
                  {accountArchivesTotal ? `${t.bilibili.accountArchivesTotal}: ${accountArchivesTotal}` : t.bilibili.accountArchivesHelp}
                </p>
              </div>
              <Button variant="outline" onClick={load}>
                <RotateCw className="size-4" />
              </Button>
            </div>
          </CardHeader>
          <CardContent>
            {loading ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="size-4 animate-spin" />
                {t.common.loading}
              </div>
            ) : accountArchives.length ? (
              <div className="grid gap-3">
                {accountArchives.map((archive) => (
                  <article key={archive.bvid || archive.aid || archive.title} className="grid gap-3 rounded-lg border bg-background p-3 sm:grid-cols-[160px_1fr]">
                    <div className="aspect-video overflow-hidden rounded-md border bg-muted">
                      {archive.cover ? (
                        // eslint-disable-next-line @next/next/no-img-element
                        <img src={archive.cover} alt="" className="h-full w-full object-cover" />
                      ) : (
                        <div className="flex h-full items-center justify-center text-xs text-muted-foreground">
                          Bilibili
                        </div>
                      )}
                    </div>
                    <div className="min-w-0 space-y-3">
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div className="min-w-0">
                          <h2 className="truncate font-medium">{archive.title}</h2>
                          <p className="mt-1 text-xs text-muted-foreground">
                            {formatTime(archive.published_at || archive.created_at)}
                            {archive.bvid ? ` · ${archive.bvid}` : ""}
                          </p>
                        </div>
                        <Badge variant="secondary">{archive.state_desc || t.bilibili.succeeded}</Badge>
                      </div>
                      <div className="flex flex-wrap gap-3 text-xs text-muted-foreground">
                        <span>{t.bilibili.views}: {archive.stats.view}</span>
                        <span>{t.bilibili.likes}: {archive.stats.like}</span>
                        <span>{t.bilibili.replies}: {archive.stats.reply}</span>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        {archive.url ? (
                          <Button variant="outline" nativeButton={false} render={<a href={archive.url} target="_blank" rel="noreferrer" />}>
                            <ExternalLink className="size-4" />
                            {t.bilibili.openBilibili}
                          </Button>
                        ) : null}
                        {archive.edit_url ? (
                          <Button variant="outline" nativeButton={false} render={<a href={archive.edit_url} target="_blank" rel="noreferrer" />}>
                            {t.bilibili.openManager}
                          </Button>
                        ) : null}
                      </div>
                    </div>
                  </article>
                ))}
              </div>
            ) : (
              <p className="text-sm text-muted-foreground">
                {credentials?.configured ? t.bilibili.noAccountArchives : t.bilibili.loginMissing}
              </p>
            )}
          </CardContent>
        </Card>

      </div>
    </main>
  )
}
