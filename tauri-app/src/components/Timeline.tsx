import { useState, useEffect } from "react";

interface Props {
  videoRef: React.RefObject<HTMLVideoElement>;
}

function fmt(s: number) {
  if (!isFinite(s)) return "0:00";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, "0")}`;
}

export default function Timeline({ videoRef }: Props) {
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);

  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    const onTime = () => setCurrentTime(v.currentTime);
    const onMeta = () => setDuration(isFinite(v.duration) ? v.duration : 0);
    v.addEventListener("timeupdate", onTime);
    v.addEventListener("loadedmetadata", onMeta);
    return () => {
      v.removeEventListener("timeupdate", onTime);
      v.removeEventListener("loadedmetadata", onMeta);
    };
  }, [videoRef]);

  const pct = duration > 0 ? Math.min((currentTime / duration) * 100, 100) : 0;

  function handleSeek(e: React.ChangeEvent<HTMLInputElement>) {
    const val = parseFloat(e.target.value);
    setCurrentTime(val);
    if (videoRef.current) videoRef.current.currentTime = val;
  }

  return (
    <div
      style={{
        height: 38,
        display: "flex",
        alignItems: "center",
        padding: "0 10px",
        gap: 8,
        borderTop: "1px solid var(--border)",
        borderBottom: "1px solid var(--border)",
        background: "var(--surface)",
        flexShrink: 0,
        margin: "0 10px",
        borderLeft: "1px solid var(--border)",
        borderRight: "1px solid var(--border)",
      }}
    >
      <span
        style={{
          fontSize: 11,
          fontFamily: "var(--font-mono)",
          color: "var(--text2)",
          minWidth: 32,
          letterSpacing: "0.03em",
        }}
      >
        {fmt(currentTime)}
      </span>
      <input
        type="range"
        min={0}
        max={duration || 1}
        value={currentTime}
        step={0.05}
        onChange={handleSeek}
        style={{
          flex: 1,
          background: `linear-gradient(to right, var(--accent) ${pct}%, var(--border2) ${pct}%)`,
        }}
      />
      <span
        style={{
          fontSize: 11,
          fontFamily: "var(--font-mono)",
          color: "var(--text3)",
          minWidth: 32,
          textAlign: "right",
          letterSpacing: "0.03em",
        }}
      >
        {fmt(duration)}
      </span>
    </div>
  );
}
