/**
 * DevPanel — Developer input overlay for live Claude Code editing.
 *
 * TO REMOVE: delete this file + the 3 lines marked [DEV] in App.tsx
 *
 * Toggle: Ctrl+Shift+D
 * Submit: Ctrl+Enter or button
 */

import { useState, useEffect, useRef, useCallback } from "react";
import { invoke } from "@tauri-apps/api/core";

interface DevPanelProps {
  visible: boolean;
  onClose: () => void;
}

type RunState = "idle" | "running" | "done" | "error";

export default function DevPanel({ visible, onClose }: DevPanelProps) {
  const [prompt, setPrompt] = useState("");
  const [output, setOutput] = useState("");
  const [runState, setRunState] = useState<RunState>("idle");
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const outputRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (visible) textareaRef.current?.focus();
  }, [visible]);

  useEffect(() => {
    if (outputRef.current) {
      outputRef.current.scrollTop = outputRef.current.scrollHeight;
    }
  }, [output]);

  const handleSubmit = useCallback(async () => {
    const p = prompt.trim();
    if (!p || runState === "running") return;
    setRunState("running");
    setOutput("⏳ Running claude...\n");
    try {
      const raw = await invoke<{ status: number; body: string }>("api_call", {
        method: "POST",
        path: "/dev/claude",
        body: JSON.stringify({ prompt: p }),
      });
      const data = JSON.parse(raw.body);
      if (raw.status >= 400) {
        setOutput(`❌ ${data.error ?? "Server error"}`);
        setRunState("error");
        return;
      }
      const out = [
        data.stdout && `${data.stdout}`,
        data.stderr && `\n--- stderr ---\n${data.stderr}`,
      ]
        .filter(Boolean)
        .join("");
      setOutput(out || "(no output)");
      setRunState(data.ok ? "done" : "error");
    } catch (e) {
      setOutput(`❌ ${e instanceof Error ? e.message : String(e)}`);
      setRunState("error");
    }
  }, [prompt, runState]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
        e.preventDefault();
        handleSubmit();
      }
    },
    [handleSubmit]
  );

  if (!visible) return null;

  const stateColor = {
    idle: "#666",
    running: "#f5a623",
    done: "#22dd77",
    error: "#ff5555",
  }[runState];

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        zIndex: 9999,
        background: "rgba(0,0,0,0.55)",
        display: "flex",
        alignItems: "flex-end",
        justifyContent: "center",
        padding: "0 0 32px",
      }}
      onClick={(e) => {
        if (e.target === e.currentTarget) onClose();
      }}
    >
      <div
        style={{
          width: "min(860px, 96vw)",
          background: "#111318",
          border: "1px solid #2a2d36",
          borderRadius: 12,
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
          boxShadow: "0 24px 80px rgba(0,0,0,0.7)",
        }}
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "10px 14px",
            borderBottom: "1px solid #1e2028",
            background: "#0d0f14",
          }}
        >
          <span
            style={{
              fontSize: 11,
              fontFamily: "monospace",
              color: "#7b8494",
              letterSpacing: "0.1em",
              flex: 1,
            }}
          >
            DEV / CLAUDE CODE
          </span>
          <div
            style={{
              width: 8,
              height: 8,
              borderRadius: "50%",
              background: stateColor,
              transition: "background 0.2s",
            }}
          />
          <span
            style={{
              fontSize: 10,
              color: stateColor,
              fontFamily: "monospace",
              width: 50,
              transition: "color 0.2s",
            }}
          >
            {runState.toUpperCase()}
          </span>
          <button
            onClick={onClose}
            style={{
              background: "none",
              border: "none",
              color: "#555",
              fontSize: 16,
              cursor: "pointer",
              padding: "0 4px",
              lineHeight: 1,
            }}
          >
            ✕
          </button>
        </div>

        {/* Input */}
        <textarea
          ref={textareaRef}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="在这里输入 Claude Code 指令，例如：在 ChatPanel.tsx 里加一个清空历史的按钮..."
          rows={5}
          style={{
            background: "#0d0f14",
            border: "none",
            borderBottom: "1px solid #1e2028",
            color: "#c8cdd8",
            fontFamily: "monospace",
            fontSize: 13,
            padding: "12px 16px",
            resize: "vertical",
            outline: "none",
            lineHeight: 1.6,
          }}
        />

        {/* Controls */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            padding: "8px 12px",
            background: "#0a0c10",
            borderBottom: "1px solid #1e2028",
          }}
        >
          <button
            onClick={handleSubmit}
            disabled={runState === "running" || !prompt.trim()}
            style={{
              background: runState === "running" ? "#1e2028" : "#1d3a5e",
              border: "1px solid " + (runState === "running" ? "#2a2d36" : "#2d5fa0"),
              color: runState === "running" ? "#555" : "#7ec8ff",
              borderRadius: 6,
              padding: "4px 14px",
              fontSize: 12,
              fontFamily: "monospace",
              cursor: runState === "running" ? "not-allowed" : "pointer",
              letterSpacing: "0.05em",
            }}
          >
            {runState === "running" ? "Running..." : "Run  ⌘↵"}
          </button>
          <button
            onClick={() => {
              setPrompt("");
              setOutput("");
              setRunState("idle");
            }}
            style={{
              background: "none",
              border: "1px solid #2a2d36",
              color: "#555",
              borderRadius: 6,
              padding: "4px 10px",
              fontSize: 12,
              fontFamily: "monospace",
              cursor: "pointer",
            }}
          >
            Clear
          </button>
          <span
            style={{
              marginLeft: "auto",
              fontSize: 11,
              color: "#3a3f4e",
              fontFamily: "monospace",
            }}
          >
            Ctrl+Shift+D to toggle
          </span>
        </div>

        {/* Output */}
        {output && (
          <div
            ref={outputRef}
            style={{
              maxHeight: 320,
              overflowY: "auto",
              padding: "12px 16px",
              background: "#07090d",
              fontFamily: "monospace",
              fontSize: 12,
              color: runState === "error" ? "#ff8888" : "#8fbfff",
              whiteSpace: "pre-wrap",
              wordBreak: "break-word",
              lineHeight: 1.6,
            }}
          >
            {output}
          </div>
        )}
      </div>
    </div>
  );
}
