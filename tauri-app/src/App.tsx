import { useState, useRef, useEffect, useCallback } from "react";
import { invoke } from "@tauri-apps/api/core";
import VideoPreview from "./components/VideoPreview";
import Timeline from "./components/Timeline";
import ChatPanel from "./components/ChatPanel";
import SkillsPanel from "./components/SkillsPanel";
import type { AppStatus, ChatMessage, Skill } from "./types";

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
let _mid = 0;
const newId = () => `m${++_mid}_${Date.now()}`;

type ApiResult = { ok: boolean; status: number; data: Record<string, unknown> };

export default function App() {
  const [status, setStatus] = useState<AppStatus>("starting");
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [skills, setSkills] = useState<Skill[]>([]);
  const [videoSrc, setVideoSrc] = useState<string | null>(null);
  const [serverVideoPath, setServerVideoPath] = useState<string | null>(null);
  const [pendingAskId, setPendingAskId] = useState<string | null>(null);
  const [pendingAskQuestions, setPendingAskQuestions] = useState<string[]>([]);
  const [isRunning, setIsRunning] = useState(false);
  const videoRef = useRef<HTMLVideoElement>(null!);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // ── Helpers ───────────────────────────────────────────────────────────

  const api = useCallback(async (method: string, path: string, body?: unknown): Promise<ApiResult> => {
    const raw = await invoke<{ status: number; body: string }>("api_call", {
      method,
      path,
      body: body != null ? JSON.stringify(body) : null,
    });
    return { ok: raw.status < 400, status: raw.status, data: JSON.parse(raw.body) };
  }, []);

  const addMsg = useCallback((msg: Omit<ChatMessage, "id" | "timestamp">) => {
    const full: ChatMessage = { ...msg, id: newId(), timestamp: Date.now() };
    setMessages((prev) => [...prev, full]);
    return full.id;
  }, []);

  const updateLastStatus = useCallback((content: string, statusType: AppStatus) => {
    setMessages((prev) => {
      const idx = [...prev].reverse().findIndex((m) => m.role === "status");
      if (idx === -1) return prev;
      const at = prev.length - 1 - idx;
      const next = [...prev];
      next[at] = { ...next[at], content, statusType };
      return next;
    });
  }, []);

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearInterval(pollRef.current);
      pollRef.current = null;
    }
  }, []);

  async function b64ToVideoSrc(b64: string): Promise<string> {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return URL.createObjectURL(new Blob([bytes], { type: "video/mp4" }));
  }

  async function loadVideoPreview(relPath: string) {
    try {
      const b64 = await invoke<string>("fetch_video_b64", { serverRelPath: relPath });
      const src = await b64ToVideoSrc(b64);
      setVideoSrc(src);
      if (videoRef.current) {
        videoRef.current.src = src;
        videoRef.current.load();
      }
    } catch {
      // preview failed, non-fatal
    }
  }

  // ── Boot ──────────────────────────────────────────────────────────────

  useEffect(() => {
    (async () => {
      for (let i = 0; i < 40; i++) {
        try {
          const raw = await invoke<{ status: number; body: string }>("api_call", {
            method: "GET",
            path: "/skills",
            body: null,
          });
          if (raw.status < 500) {
            setStatus("ready");
            const data = JSON.parse(raw.body);
            if (data.skills) {
              setSkills((data.skills as string[]).map((name) => ({ name })));
            }
            return;
          }
        } catch {
          // still starting
        }
        await sleep(500);
      }
      setStatus("error");
    })();
  }, []);

  // ── Video upload ──────────────────────────────────────────────────────

  const handleVideoSelect = useCallback(
    async (filePath: string) => {
      try {
        const result = await invoke<{ status: number; body: string }>("upload_video", {
          srcPath: filePath,
        });
        const data = JSON.parse(result.body);
        if (result.status >= 400) throw new Error(data.error ?? "Upload failed");
        setServerVideoPath(data.path as string);
        await loadVideoPreview("inputs/" + (data.name as string));
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        addMsg({ role: "status", content: `上传失败: ${msg}`, statusType: "error" });
      }
    },
    [addMsg]
  );

  // ── Polling ───────────────────────────────────────────────────────────

  const startPolling = useCallback(
    (taskId: string) => {
      pollRef.current = setInterval(async () => {
        try {
          const r = await api("GET", `/task/${taskId}`);
          const taskStatus = r.data.status as string;
          const outputs = r.data.outputs as string[] | undefined;

          if (taskStatus === "succeeded") {
            stopPolling();
            setStatus("done");
            setIsRunning(false);
            updateLastStatus("完成 ✓", "done");
            if (outputs?.length) {
              const filename = outputs[0].split("/").pop() ?? "";
              await loadVideoPreview("outputs/" + filename);
              if (videoRef.current) videoRef.current.play().catch(() => {});
            }
          } else if (taskStatus === "failed") {
            stopPolling();
            setStatus("error");
            setIsRunning(false);
            updateLastStatus("执行失败", "error");
          }
        } catch {
          // ignore transient errors
        }
      }, 2000);
    },
    [api, stopPolling, updateLastStatus]
  );

  // ── Run prompt ────────────────────────────────────────────────────────

  const handleSend = useCallback(
    async (text: string) => {
      if (!serverVideoPath) return;

      if (pendingAskId) {
        // answering a question
        const askId = pendingAskId;
        setPendingAskId(null);
        setPendingAskQuestions([]);
        setIsRunning(true);
        setStatus("planning");
        addMsg({ role: "user", content: text });
        addMsg({ role: "status", content: "正在规划...", statusType: "planning" });
        try {
          const r = await api("POST", `/answer-ask/${askId}`, { answers: { answer: text } });
          if (r.data.ask) {
            setStatus("asking");
            setIsRunning(false);
            setPendingAskId(r.data.ask_id as string);
            setPendingAskQuestions((r.data.questions as string[]) ?? []);
            updateLastStatus("需要更多信息", "asking");
            return;
          }
          if (!r.ok || !r.data.task_id) throw new Error((r.data.error as string) ?? "Server error");
          setStatus("executing");
          updateLastStatus("正在执行...", "executing");
          startPolling(r.data.task_id as string);
        } catch (e) {
          const msg = e instanceof Error ? e.message : String(e);
          setStatus("error");
          setIsRunning(false);
          updateLastStatus(`错误: ${msg}`, "error");
        }
        return;
      }

      // new prompt
      stopPolling();
      setIsRunning(true);
      setStatus("planning");
      addMsg({ role: "user", content: text });
      addMsg({ role: "status", content: "正在规划...", statusType: "planning" });
      try {
        const r = await api("POST", "/run-prompt", { prompt: text, video: serverVideoPath });
        if (r.data.ask) {
          setStatus("asking");
          setIsRunning(false);
          setPendingAskId(r.data.ask_id as string);
          setPendingAskQuestions((r.data.questions as string[]) ?? []);
          updateLastStatus("需要更多信息", "asking");
          return;
        }
        if (!r.ok || !r.data.task_id) throw new Error((r.data.error as string) ?? "Server error");
        setStatus("executing");
        updateLastStatus("正在执行...", "executing");
        startPolling(r.data.task_id as string);
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        setStatus("error");
        setIsRunning(false);
        updateLastStatus(`错误: ${msg}`, "error");
      }
    },
    [serverVideoPath, pendingAskId, addMsg, api, stopPolling, updateLastStatus, startPolling]
  );

  // ── Run skill ─────────────────────────────────────────────────────────

  const handleRunSkill = useCallback(
    async (skillName: string) => {
      if (!serverVideoPath || isRunning) return;
      setIsRunning(true);
      setStatus("executing");
      addMsg({ role: "user", content: `▶ ${skillName}` });
      addMsg({ role: "status", content: "正在执行...", statusType: "executing" });
      try {
        const r = await api("POST", "/run-skill", {
          skill_id: skillName,
          inputs: { video: serverVideoPath },
        });
        if (!r.ok || !r.data.task_id) throw new Error((r.data.error as string) ?? "Skill error");
        startPolling(r.data.task_id as string);
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        setStatus("error");
        setIsRunning(false);
        updateLastStatus(`错误: ${msg}`, "error");
      }
    },
    [serverVideoPath, isRunning, addMsg, api, startPolling, updateLastStatus]
  );

  // ── Render ────────────────────────────────────────────────────────────

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100vh", overflow: "hidden" }}>
      <AppHeader status={status} />
      <div style={{ display: "flex", flex: 1, overflow: "hidden" }}>
        {/* Main column */}
        <div style={{ flex: 1, display: "flex", flexDirection: "column", overflow: "hidden" }}>
          <VideoPreview videoRef={videoRef} videoSrc={videoSrc} onFileSelect={handleVideoSelect} />
          {videoSrc && <Timeline videoRef={videoRef} />}
          <ChatPanel
            messages={messages}
            isRunning={isRunning}
            hasVideo={!!serverVideoPath}
            pendingAskId={pendingAskId}
            pendingAskQuestions={pendingAskQuestions}
            onSend={handleSend}
          />
        </div>
        {/* Skills sidebar */}
        <SkillsPanel
          skills={skills}
          hasVideo={!!serverVideoPath}
          isRunning={isRunning}
          onRunSkill={handleRunSkill}
        />
      </div>
    </div>
  );
}

// ── Header ────────────────────────────────────────────────────────────────

const STATUS_INFO: Record<AppStatus, { label: string; color: string }> = {
  starting: { label: "启动中", color: "var(--text3)" },
  ready:    { label: "就绪",   color: "var(--accent)" },
  planning: { label: "规划中", color: "var(--blue)" },
  executing:{ label: "执行中", color: "var(--warn)" },
  done:     { label: "完成",   color: "var(--accent)" },
  error:    { label: "错误",   color: "var(--error)" },
  asking:   { label: "等待",   color: "var(--warn)" },
};

function AppHeader({ status }: { status: AppStatus }) {
  const info = STATUS_INFO[status];
  const isPulsing = status === "planning" || status === "executing";

  return (
    <header
      style={{
        height: 42,
        background: "var(--surface)",
        borderBottom: "1px solid var(--border)",
        display: "flex",
        alignItems: "center",
        padding: "0 14px",
        gap: 12,
        WebkitAppRegion: "drag",
        flexShrink: 0,
      } as React.CSSProperties}
    >
      <span
        style={{
          fontFamily: "var(--font-mono)",
          fontSize: 13,
          fontWeight: 500,
          color: "var(--accent)",
          letterSpacing: "0.2em",
          WebkitAppRegion: "no-drag",
        } as React.CSSProperties}
      >
        GEMIA
      </span>

      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          WebkitAppRegion: "no-drag",
        } as React.CSSProperties}
      >
        <div
          style={{
            width: 6,
            height: 6,
            borderRadius: "50%",
            background: info.color,
            flexShrink: 0,
            animation: isPulsing ? "pulse-dot 1s ease-in-out infinite" : "none",
          }}
        />
        <span
          style={{
            fontSize: 11,
            fontFamily: "var(--font-mono)",
            color: info.color,
            letterSpacing: "0.04em",
          }}
        >
          {info.label}
        </span>
      </div>
    </header>
  );
}
