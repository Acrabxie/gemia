import { useState, useEffect, useRef, useCallback } from "react";

interface Props {
  videoRef: React.RefObject<HTMLVideoElement>;
  hasVideo: boolean;
}

function fmt(s: number) {
  if (!isFinite(s)) return "0:00";
  const m = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, "0")}`;
}

// Generate pseudo-waveform bars from a seed (deterministic per video)
function buildWaveform(count: number, seed: number): number[] {
  const bars: number[] = [];
  let v = seed;
  for (let i = 0; i < count; i++) {
    v = (v * 1664525 + 1013904223) & 0xffffffff;
    const base = 0.3 + ((v >>> 0) / 0xffffffff) * 0.7;
    bars.push(base);
  }
  return bars;
}

export default function Timeline({ videoRef, hasVideo }: Props) {
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const trackRef = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);
  const videoSeed = useRef(Math.floor(Math.random() * 0xffff));
  const waveform = useRef<number[]>([]);

  useEffect(() => {
    const v = videoRef.current;
    if (!v) return;
    const onTime = () => setCurrentTime(v.currentTime);
    const onMeta = () => {
      const d = isFinite(v.duration) ? v.duration : 0;
      setDuration(d);
      videoSeed.current = Math.floor(d * 1000) % 0xffff;
      waveform.current = buildWaveform(120, videoSeed.current);
    };
    v.addEventListener("timeupdate", onTime);
    v.addEventListener("loadedmetadata", onMeta);
    return () => {
      v.removeEventListener("timeupdate", onTime);
      v.removeEventListener("loadedmetadata", onMeta);
    };
  }, [videoRef]);

  const seekToX = useCallback((clientX: number) => {
    const el = trackRef.current;
    if (!el || !duration) return;
    const rect = el.getBoundingClientRect();
    const ratio = Math.max(0, Math.min(1, (clientX - rect.left) / rect.width));
    const t = ratio * duration;
    setCurrentTime(t);
    if (videoRef.current) videoRef.current.currentTime = t;
  }, [duration, videoRef]);

  function handleTrackMouseDown(e: React.MouseEvent) {
    if (!hasVideo || !duration) return;
    dragging.current = true;
    seekToX(e.clientX);
    const onMove = (ev: MouseEvent) => { if (dragging.current) seekToX(ev.clientX); };
    const onUp = () => { dragging.current = false; window.removeEventListener("mousemove", onMove); window.removeEventListener("mouseup", onUp); };
    window.addEventListener("mousemove", onMove);
    window.addEventListener("mouseup", onUp);
  }

  const pct = duration > 0 ? Math.min((currentTime / duration) * 100, 100) : 0;

  // Time ruler ticks
  const ticks = buildRulerTicks(duration);

  const bars = waveform.current.length > 0 ? waveform.current : buildWaveform(120, 0);

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        background: "var(--surface)",
        borderTop: "1px solid var(--border)",
        borderBottom: "1px solid var(--border)",
        flexShrink: 0,
        opacity: hasVideo ? 1 : 0.3,
        pointerEvents: hasVideo ? "auto" : "none",
        userSelect: "none",
      }}
    >
      {/* Time display row */}
      <div style={{ display: "flex", alignItems: "center", padding: "4px 12px 0", gap: 8 }}>
        <span style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--accent)", minWidth: 34 }}>
          {fmt(currentTime)}
        </span>
        <span style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text3)" }}>
          /
        </span>
        <span style={{ fontSize: 10, fontFamily: "var(--font-mono)", color: "var(--text3)" }}>
          {fmt(duration)}
        </span>
      </div>

      {/* Ruler + track area */}
      <div
        ref={trackRef}
        onMouseDown={handleTrackMouseDown}
        style={{ position: "relative", margin: "4px 12px 6px", cursor: "pointer" }}
      >
        {/* Ruler ticks */}
        <div style={{ position: "relative", height: 14, marginBottom: 2 }}>
          {ticks.map(({ pct: tp, label }) => (
            <div key={tp} style={{ position: "absolute", left: `${tp}%`, top: 0, transform: "translateX(-50%)" }}>
              <div style={{ width: 1, height: 4, background: "var(--border2)", margin: "0 auto" }} />
              <div style={{ fontSize: 9, fontFamily: "var(--font-mono)", color: "var(--text3)", marginTop: 1, whiteSpace: "nowrap" }}>
                {label}
              </div>
            </div>
          ))}
        </div>

        {/* Clip strip with waveform */}
        <div
          style={{
            position: "relative",
            height: 32,
            borderRadius: 3,
            overflow: "hidden",
            background: "linear-gradient(180deg, #1a2a1a 0%, #0d1a0d 100%)",
            border: "1px solid #2a3a2a",
          }}
        >
          {/* Waveform bars */}
          <div style={{ position: "absolute", inset: 0, display: "flex", alignItems: "center", gap: 0 }}>
            {bars.map((h, i) => (
              <div
                key={i}
                style={{
                  flex: 1,
                  height: `${h * 70}%`,
                  background: i / bars.length < pct / 100
                    ? "rgba(0,255,100,0.55)"
                    : "rgba(0,255,100,0.18)",
                  borderRadius: 1,
                  transition: "background 0.05s",
                }}
              />
            ))}
          </div>

          {/* Progress fill overlay */}
          <div
            style={{
              position: "absolute",
              left: 0,
              top: 0,
              bottom: 0,
              width: `${pct}%`,
              background: "rgba(0,200,80,0.06)",
              pointerEvents: "none",
            }}
          />

          {/* Playhead */}
          <div
            style={{
              position: "absolute",
              left: `${pct}%`,
              top: -2,
              bottom: -2,
              width: 2,
              background: "var(--accent)",
              borderRadius: 1,
              pointerEvents: "none",
              boxShadow: "0 0 6px var(--accent)",
              transform: "translateX(-50%)",
            }}
          />

          {/* Playhead handle */}
          <div
            style={{
              position: "absolute",
              left: `${pct}%`,
              top: -4,
              width: 8,
              height: 8,
              background: "var(--accent)",
              borderRadius: "50%",
              transform: "translateX(-50%)",
              pointerEvents: "none",
              boxShadow: "0 0 4px var(--accent)",
            }}
          />
        </div>
      </div>
    </div>
  );
}

function buildRulerTicks(duration: number): { pct: number; label: string }[] {
  if (!duration || !isFinite(duration) || duration <= 0) return [];
  // Choose step: aim for ~6-8 ticks
  const steps = [1, 2, 5, 10, 15, 30, 60, 120, 300];
  const step = steps.find(s => duration / s <= 8) ?? 300;
  const ticks: { pct: number; label: string }[] = [];
  for (let t = 0; t <= duration; t += step) {
    ticks.push({ pct: (t / duration) * 100, label: fmt(t) });
  }
  return ticks;
}
