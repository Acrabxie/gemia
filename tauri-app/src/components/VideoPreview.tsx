import { useRef, useState } from "react";
import { open as openDialog } from "@tauri-apps/plugin-dialog";

interface Props {
  videoRef: React.RefObject<HTMLVideoElement>;
  videoSrc: string | null;
  onFileSelect: (path: string) => void;
}

export default function VideoPreview({ videoRef, videoSrc, onFileSelect }: Props) {
  const [dragOver, setDragOver] = useState(false);
  const [uploading, setUploading] = useState(false);

  async function pickFile() {
    const path = await openDialog({
      multiple: false,
      filters: [{ name: "Video", extensions: ["mp4", "mov", "avi", "mkv"] }],
    });
    if (typeof path === "string") {
      setUploading(true);
      await onFileSelect(path);
      setUploading(false);
    }
  }

  function handleDragOver(e: React.DragEvent) {
    e.preventDefault();
    setDragOver(true);
  }
  function handleDragLeave() { setDragOver(false); }
  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file) {
      const filePath = (file as unknown as { path?: string }).path;
      if (filePath) onFileSelect(filePath);
    }
  }

  if (!videoSrc) {
    return (
      <div
        onClick={pickFile}
        onDragOver={handleDragOver}
        onDragLeave={handleDragLeave}
        onDrop={handleDrop}
        style={{
          width: "100%",
          height: "100%",
          border: `1px solid ${dragOver ? "var(--accent)" : "var(--border)"}`,
          borderRadius: 0,
          background: dragOver ? "var(--accent-dim)" : "var(--surface2)",
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          justifyContent: "center",
          gap: 10,
          cursor: "pointer",
          color: dragOver ? "var(--accent)" : "var(--text2)",
          transition: "all 0.15s",
          flexShrink: 0,
          boxSizing: "border-box",
        }}
      >
        {uploading ? (
          <span style={{ fontSize: 12, fontFamily: "var(--font-mono)", color: "var(--text2)" }}>
            上传中...
          </span>
        ) : (
          <>
            <VideoIcon color={dragOver ? "var(--accent)" : "var(--text3)"} />
            <div style={{ textAlign: "center", lineHeight: 1.7 }}>
              <div style={{ fontSize: 13, color: "var(--text2)" }}>拖入视频 · 点击选择</div>
              <div style={{ fontSize: 11, color: "var(--text3)" }}>MP4 · MOV · AVI · MKV</div>
            </div>
          </>
        )}
      </div>
    );
  }

  return (
    <div
      style={{
        position: "relative",
        width: "100%",
        height: "100%",
        background: "#000",
        flexShrink: 0,
        overflow: "hidden",
      }}
    >
      <video
        ref={videoRef}
        src={videoSrc}
        controls
        style={{ width: "100%", height: "100%", objectFit: "contain", display: "block" }}
      />
      <button
        onClick={pickFile}
        style={{
          position: "absolute",
          top: 8,
          right: 8,
          background: "rgba(255,255,255,0.85)",
          border: "1px solid var(--border2)",
          borderRadius: "var(--r-sm)",
          color: "var(--text2)",
          fontSize: 11,
          padding: "3px 8px",
          cursor: "pointer",
          fontFamily: "var(--font-mono)",
          backdropFilter: "blur(4px)",
        }}
      >
        更换
      </button>
    </div>
  );
}

function VideoIcon({ color }: { color: string }) {
  return (
    <svg width="32" height="32" viewBox="0 0 24 24" fill="none" stroke={color} strokeWidth="1.2">
      <rect x="2" y="4" width="15" height="16" rx="2" />
      <path d="M17 9l5-3v12l-5-3V9z" />
    </svg>
  );
}
