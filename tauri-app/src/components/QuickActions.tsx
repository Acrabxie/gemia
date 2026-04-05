import { invoke } from "@tauri-apps/api/core";

interface Props {
  serverVideoPath: string | null;
  isRunning: boolean;
  onTaskStart: (taskId: string) => void;
  onError: (msg: string) => void;
}

const ACTIONS = [
  { id: "rotate_ccw", label: "逆转90°", icon: RotateCCWIcon },
  { id: "rotate_cw",  label: "顺转90°", icon: RotateCWIcon  },
  { id: "rotate_180", label: "旋转180°", icon: Rotate180Icon },
  { id: "flip_h",     label: "水平镜像", icon: FlipHIcon     },
  { id: "flip_v",     label: "垂直镜像", icon: FlipVIcon     },
] as const;

export default function QuickActions({ serverVideoPath, isRunning, onTaskStart, onError }: Props) {
  const disabled = !serverVideoPath || isRunning;

  async function handleAction(action: string) {
    if (disabled) return;
    try {
      const raw = await invoke<{ status: number; body: string }>("api_call", {
        method: "POST",
        path: "/quick-action",
        body: JSON.stringify({ action, video: serverVideoPath }),
      });
      const data = JSON.parse(raw.body);
      if (raw.status >= 400) throw new Error(data.error ?? "Quick action failed");
      onTaskStart(data.task_id as string);
    } catch (e) {
      onError(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 4,
        padding: "0 10px",
        height: 36,
        borderBottom: "1px solid var(--border)",
        background: "var(--surface)",
        flexShrink: 0,
        opacity: disabled ? 0.35 : 1,
        transition: "opacity 0.15s",
      }}
    >
      {ACTIONS.map(({ id, label, icon: Icon }) => (
        <button
          key={id}
          title={label}
          onClick={() => handleAction(id)}
          disabled={disabled}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 4,
            background: "transparent",
            border: "1px solid transparent",
            borderRadius: "var(--r-sm)",
            color: "var(--text2)",
            padding: "3px 7px",
            cursor: disabled ? "default" : "pointer",
            fontSize: 10,
            fontFamily: "var(--font-mono)",
            letterSpacing: "0.02em",
            transition: "all 0.12s",
            whiteSpace: "nowrap",
          }}
          onMouseEnter={e => {
            if (!disabled) {
              (e.currentTarget as HTMLButtonElement).style.borderColor = "var(--border2)";
              (e.currentTarget as HTMLButtonElement).style.color = "var(--text1)";
            }
          }}
          onMouseLeave={e => {
            (e.currentTarget as HTMLButtonElement).style.borderColor = "transparent";
            (e.currentTarget as HTMLButtonElement).style.color = "var(--text2)";
          }}
        >
          <Icon size={13} />
          <span>{label}</span>
        </button>
      ))}
    </div>
  );
}

// ── Icons ────────────────────────────────────────────────────────────────

function RotateCCWIcon({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="1 4 1 10 7 10" />
      <path d="M3.51 15a9 9 0 1 0 .49-4.5" />
    </svg>
  );
}

function RotateCWIcon({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <polyline points="23 4 23 10 17 10" />
      <path d="M20.49 15a9 9 0 1 1-.49-4.5" />
    </svg>
  );
}

function Rotate180Icon({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 2a10 10 0 0 1 0 20" />
      <path d="M12 2a10 10 0 0 0 0 20" strokeDasharray="3 3" />
      <polyline points="16 16 12 20 8 16" />
    </svg>
  );
}

function FlipHIcon({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M12 3v18" strokeDasharray="2 2" />
      <path d="M3 7l9 3-9 3V7z" fill="currentColor" strokeWidth="0" opacity="0.5" />
      <path d="M21 7l-9 3 9 3V7z" />
    </svg>
  );
}

function FlipVIcon({ size = 14 }: { size?: number }) {
  return (
    <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round">
      <path d="M3 12h18" strokeDasharray="2 2" />
      <path d="M7 3l3 9-3 9H7V3z" fill="currentColor" strokeWidth="0" opacity="0.5" />
      <path d="M17 3l-3 9 3 9h0V3z" />
    </svg>
  );
}
