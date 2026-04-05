import { useState, useRef, useEffect } from "react";
import type { ChatMessage, AppStatus } from "../types";

interface Props {
  messages: ChatMessage[];
  isRunning: boolean;
  hasVideo: boolean;
  pendingAskId: string | null;
  pendingAskQuestions: string[];
  onSend: (text: string) => void;
}

const STATUS_COLOR: Record<string, string> = {
  planning: "var(--blue)",
  executing: "var(--warn)",
  done: "var(--accent)",
  error: "var(--error)",
  asking: "var(--warn)",
  ready: "var(--accent)",
};

const STATUS_LABEL: Record<string, string> = {
  planning: "规划中",
  executing: "执行中",
  done: "完成 ✓",
  error: "失败",
  asking: "等待信息",
};

export default function ChatPanel({
  messages,
  isRunning,
  hasVideo,
  pendingAskId,
  pendingAskQuestions,
  onSend,
}: Props) {
  const [input, setInput] = useState("");
  const endRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, pendingAskId]);

  function handleSend() {
    const text = input.trim();
    if (!text || (isRunning && !pendingAskId)) return;
    setInput("");
    onSend(text);
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Enter" && e.metaKey) {
      e.preventDefault();
      handleSend();
    }
  }

  const inputDisabled = !hasVideo || (isRunning && !pendingAskId);
  const canSend = !inputDisabled && input.trim().length > 0;

  const placeholder = !hasVideo
    ? "先上传一个视频..."
    : pendingAskId
    ? "回答 AI 的问题...（⌘↵ 发送）"
    : isRunning
    ? "正在处理..."
    : "描述你想做什么（⌘↵ 发送）";

  return (
    <div
      style={{
        flex: 1,
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        background: "var(--bg)",
        borderTop: "1px solid var(--border)",
      }}
    >
      {/* Message list */}
      <div
        style={{
          flex: 1,
          overflowY: "auto",
          padding: "14px 14px 6px",
          display: "flex",
          flexDirection: "column",
          gap: 10,
        }}
      >
        {messages.length === 0 && (
          <div
            style={{
              flex: 1,
              display: "flex",
              alignItems: "center",
              justifyContent: "center",
              color: "var(--text3)",
              fontSize: 12,
              fontFamily: "var(--font-mono)",
              letterSpacing: "0.04em",
            }}
          >
            {hasVideo ? "> 输入指令开始处理" : "> 等待视频..."}
          </div>
        )}

        {messages.map((msg) =>
          msg.role === "user" ? (
            <UserBubble key={msg.id} content={msg.content} />
          ) : (
            <StatusRow key={msg.id} content={msg.content} statusType={msg.statusType} />
          )
        )}

        {/* Ask questions card */}
        {pendingAskId && pendingAskQuestions.length > 0 && (
          <div
            style={{
              background: "rgba(77, 159, 255, 0.06)",
              border: "1px solid rgba(77, 159, 255, 0.18)",
              borderRadius: "var(--r)",
              padding: "10px 12px",
              fontSize: 13,
            }}
          >
            <div
              style={{
                fontSize: 10,
                fontFamily: "var(--font-mono)",
                color: "var(--blue)",
                letterSpacing: "0.12em",
                marginBottom: 8,
                textTransform: "uppercase",
              }}
            >
              AI 需要更多信息
            </div>
            {pendingAskQuestions.map((q, i) => (
              <div key={i} style={{ color: "var(--text)", lineHeight: 1.7, fontSize: 13 }}>
                {i + 1}. {q}
              </div>
            ))}
          </div>
        )}

        <div ref={endRef} />
      </div>

      {/* Input bar */}
      <div
        style={{
          padding: "9px 10px",
          borderTop: "1px solid var(--border)",
          background: "var(--surface)",
          display: "flex",
          gap: 7,
          alignItems: "flex-end",
          flexShrink: 0,
        }}
      >
        <textarea
          ref={textareaRef}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={placeholder}
          disabled={inputDisabled}
          rows={2}
          style={{
            flex: 1,
            background: "var(--surface2)",
            border: `1px solid ${canSend ? "var(--border2)" : "var(--border)"}`,
            borderRadius: "var(--r-sm)",
            color: "var(--text)",
            fontSize: 13,
            padding: "7px 10px",
            resize: "none",
            outline: "none",
            lineHeight: 1.6,
            transition: "border-color 0.15s",
          } as React.CSSProperties}
        />
        <button
          onClick={handleSend}
          disabled={!canSend}
          style={{
            background: canSend ? "var(--accent)" : "var(--surface2)",
            color: canSend ? "#000" : "var(--text3)",
            border: "none",
            borderRadius: "var(--r-sm)",
            padding: "0 16px",
            height: 54,
            fontSize: 12,
            fontWeight: 600,
            cursor: canSend ? "pointer" : "not-allowed",
            fontFamily: "var(--font-mono)",
            letterSpacing: "0.06em",
            transition: "background 0.15s, color 0.15s",
            flexShrink: 0,
          } as React.CSSProperties}
        >
          ▶ RUN
        </button>
      </div>
    </div>
  );
}

function UserBubble({ content }: { content: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "flex-end" }}>
      <div
        style={{
          maxWidth: "78%",
          background: "var(--surface2)",
          border: "1px solid var(--border2)",
          borderRadius: "var(--r)",
          padding: "8px 12px",
          fontSize: 13,
          color: "var(--text)",
          lineHeight: 1.6,
          whiteSpace: "pre-wrap",
          wordBreak: "break-word",
        }}
      >
        {content}
      </div>
    </div>
  );
}

function StatusRow({ content, statusType }: { content: string; statusType?: AppStatus }) {
  const color = STATUS_COLOR[statusType ?? ""] ?? "var(--text2)";
  const isActive = statusType === "planning" || statusType === "executing";

  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <div
        style={{
          width: 6,
          height: 6,
          borderRadius: "50%",
          background: color,
          flexShrink: 0,
          animation: isActive ? "pulse-dot 1.2s ease-in-out infinite" : "none",
        }}
      />
      <span
        style={{
          fontSize: 12,
          fontFamily: "var(--font-mono)",
          color,
          letterSpacing: "0.03em",
        }}
      >
        {content}
      </span>
    </div>
  );
}
