import { useState, useRef, useEffect } from "react";
import type { AskQuestion, ChatMessage, AppStatus } from "../types";

interface Props {
  messages: ChatMessage[];
  isRunning: boolean;
  hasVideo: boolean;
  pendingAskId: string | null;
  pendingAskQuestions: AskQuestion[];
  onSend: (text: string) => void;
  onAnswerAsk: (answers: Record<string, string>) => void;
}

const STATUS_COLOR: Record<string, string> = {
  planning: "var(--blue)",
  executing: "var(--accent)",
  done: "var(--accent)",
  error: "var(--error)",
  asking: "var(--warn)",
  ready: "var(--text3)",
};

export default function ChatPanel({
  messages,
  isRunning,
  hasVideo,
  pendingAskId,
  pendingAskQuestions,
  onSend,
  onAnswerAsk,
}: Props) {
  const [input, setInput] = useState("");
  // answers keyed by question id
  const [askAnswers, setAskAnswers] = useState<Record<string, string>>({});
  const endRef = useRef<HTMLDivElement>(null);

  // Reset answers when a new ask arrives
  useEffect(() => {
    if (pendingAskId && pendingAskQuestions.length > 0) {
      const defaults: Record<string, string> = {};
      for (const q of pendingAskQuestions) {
        if (q.input_type === "slider") {
          defaults[q.id] = String(q.default ?? q.min ?? 0);
        } else if (q.input_type === "choices" && q.choices?.length) {
          defaults[q.id] = q.choices[0];
        } else {
          defaults[q.id] = "";
        }
      }
      setAskAnswers(defaults);
    }
  }, [pendingAskId]);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, pendingAskId]);

  function handleSend() {
    const text = input.trim();
    if (!text || isRunning) return;
    setInput("");
    onSend(text);
  }

  function handleSubmitAsk() {
    onAnswerAsk(askAnswers);
  }

  function handleKeyDown(e: React.KeyboardEvent) {
    if (e.key === "Enter" && e.metaKey) {
      e.preventDefault();
      handleSend();
    }
  }

  const inputDisabled = !hasVideo || isRunning;
  const canSend = !inputDisabled && input.trim().length > 0;

  const placeholder = !hasVideo
    ? "先上传一个视频..."
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
        minHeight: 0,
      }}
    >
      {/* Message list */}
      <div
        style={{
          flex: 1,
          overflowY: "auto",
          padding: "16px 16px 8px",
          display: "flex",
          flexDirection: "column",
          gap: 8,
          minHeight: 0,
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
            <AiBubble key={msg.id} content={msg.content} statusType={msg.statusType} />
          )
        )}

        {/* Ask questions card */}
        {pendingAskId && pendingAskQuestions.length > 0 && (
          <div
            style={{
              alignSelf: "flex-start",
              maxWidth: "88%",
              background: "rgba(77,159,255,0.07)",
              border: "1px solid rgba(77,159,255,0.2)",
              borderRadius: "var(--r)",
              padding: "12px 14px",
              fontSize: 13,
            }}
          >
            <div
              style={{
                fontSize: 10,
                fontFamily: "var(--font-mono)",
                color: "var(--blue)",
                letterSpacing: "0.12em",
                marginBottom: 12,
                textTransform: "uppercase",
              }}
            >
              AI 需要更多信息
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
              {pendingAskQuestions.map((q) => (
                <AskQuestionWidget
                  key={q.id}
                  question={q}
                  value={askAnswers[q.id] ?? ""}
                  onChange={(v) => setAskAnswers((prev) => ({ ...prev, [q.id]: v }))}
                />
              ))}
            </div>
            <button
              onClick={handleSubmitAsk}
              style={{
                marginTop: 14,
                background: "var(--blue)",
                color: "#fff",
                border: "none",
                borderRadius: "var(--r-sm)",
                padding: "6px 18px",
                fontSize: 12,
                fontWeight: 700,
                cursor: "pointer",
                fontFamily: "var(--font-mono)",
                letterSpacing: "0.06em",
              }}
            >
              确认
            </button>
          </div>
        )}

        <div ref={endRef} />
      </div>

      {/* Input bar — hidden when waiting for ask answers */}
      <div
        style={{
          padding: "10px 12px",
          borderTop: "1px solid var(--border)",
          background: "var(--surface)",
          display: pendingAskId ? "none" : "flex",
          gap: 8,
          alignItems: "flex-end",
          flexShrink: 0,
        }}
      >
        <textarea
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
            padding: "8px 10px",
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
            padding: "0 18px",
            height: 54,
            fontSize: 12,
            fontWeight: 700,
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
          border: "1px solid var(--border)",
          borderRadius: "12px 12px 2px 12px",
          padding: "9px 13px",
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

function AskQuestionWidget({
  question,
  value,
  onChange,
}: {
  question: AskQuestion;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div>
      <div style={{ color: "var(--text)", fontSize: 13, marginBottom: 6, lineHeight: 1.5 }}>
        {question.text}
      </div>

      {question.input_type === "choices" && question.choices && (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {question.choices.map((c) => (
            <button
              key={c}
              onClick={() => onChange(c)}
              style={{
                padding: "4px 12px",
                borderRadius: 20,
                border: `1px solid ${value === c ? "var(--blue)" : "var(--border2)"}`,
                background: value === c ? "rgba(77,159,255,0.18)" : "transparent",
                color: value === c ? "var(--blue)" : "var(--text2)",
                fontSize: 12,
                cursor: "pointer",
                transition: "all 0.12s",
              }}
            >
              {c}
            </button>
          ))}
        </div>
      )}

      {question.input_type === "slider" && (
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <input
            type="range"
            min={question.min ?? 0}
            max={question.max ?? 100}
            step={question.step ?? 1}
            value={value || String(question.default ?? question.min ?? 0)}
            onChange={(e) => onChange(e.target.value)}
            style={{ flex: 1, accentColor: "var(--blue)" }}
          />
          <span style={{ fontSize: 13, color: "var(--text)", minWidth: 40, textAlign: "right" }}>
            {value || question.default ?? question.min ?? 0}
            {question.unit ? ` ${question.unit}` : ""}
          </span>
        </div>
      )}

      {question.input_type === "text" && (
        <input
          type="text"
          value={value}
          onChange={(e) => onChange(e.target.value)}
          placeholder={question.placeholder ?? ""}
          style={{
            width: "100%",
            background: "var(--surface2)",
            border: "1px solid var(--border2)",
            borderRadius: "var(--r-sm)",
            color: "var(--text)",
            fontSize: 13,
            padding: "6px 10px",
            outline: "none",
            boxSizing: "border-box",
          }}
        />
      )}
    </div>
  );
}

function AiBubble({ content, statusType }: { content: string; statusType?: AppStatus }) {
  const color = STATUS_COLOR[statusType ?? ""] ?? "var(--text2)";
  const isActive = statusType === "planning" || statusType === "executing";

  return (
    <div style={{ display: "flex", justifyContent: "flex-start" }}>
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          maxWidth: "82%",
          background: "var(--surface)",
          border: "1px solid var(--border)",
          borderRadius: "12px 12px 12px 2px",
          padding: "9px 13px",
        }}
      >
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
            lineHeight: 1.5,
          }}
        >
          {content}
        </span>
      </div>
    </div>
  );
}
