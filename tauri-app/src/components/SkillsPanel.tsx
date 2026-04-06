import type { Skill } from "../types";

interface Props {
  skills: Skill[];
  hasVideo: boolean;
  isRunning: boolean;
  onRunSkill: (name: string) => void;
}

export default function SkillsPanel({ skills, hasVideo, isRunning, onRunSkill }: Props) {
  const canRun = hasVideo && !isRunning;

  return (
    <div
      style={{
        width: 200,
        borderLeft: "1px solid var(--border)",
        background: "var(--surface)",
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        flexShrink: 0,
      }}
    >
      {/* Header */}
      <div
        style={{
          padding: "11px 12px 10px",
          fontSize: 10,
          fontFamily: "var(--font-mono)",
          color: "var(--text3)",
          letterSpacing: "0.15em",
          textTransform: "uppercase",
          borderBottom: "1px solid var(--border)",
          flexShrink: 0,
        }}
      >
        SKILLS
      </div>

      {/* List */}
      <div style={{ flex: 1, overflowY: "auto", padding: "6px 0" }}>
        {skills.length === 0 ? (
          <div
            style={{
              padding: "20px 12px",
              fontSize: 12,
              color: "var(--text3)",
              fontFamily: "var(--font-mono)",
              textAlign: "center",
              lineHeight: 1.7,
            }}
          >
            无 Skills
          </div>
        ) : (
          skills.map((skill) => (
            <SkillRow
              key={skill.name}
              name={skill.name}
              canRun={canRun}
              onRun={() => onRunSkill(skill.name)}
            />
          ))
        )}
      </div>

      {/* Footer hint */}
      {!hasVideo && (
        <div
          style={{
            padding: "8px 12px",
            fontSize: 10,
            color: "var(--text3)",
            borderTop: "1px solid var(--border)",
            fontFamily: "var(--font-mono)",
            textAlign: "center",
          }}
        >
          上传视频后可用
        </div>
      )}
    </div>
  );
}

function SkillRow({
  name,
  canRun,
  onRun,
}: {
  name: string;
  canRun: boolean;
  onRun: () => void;
}) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        justifyContent: "space-between",
        padding: "7px 10px",
        gap: 6,
        borderRadius: 0,
        transition: "background 0.1s",
      }}
      onMouseEnter={(e) => {
        (e.currentTarget as HTMLDivElement).style.background = "var(--surface2)";
      }}
      onMouseLeave={(e) => {
        (e.currentTarget as HTMLDivElement).style.background = "transparent";
      }}
    >
      <span
        title={name}
        style={{
          fontSize: 12,
          color: "var(--text2)",
          flex: 1,
          overflow: "hidden",
          textOverflow: "ellipsis",
          whiteSpace: "nowrap",
        }}
      >
        {name}
      </span>
      <button
        onClick={onRun}
        disabled={!canRun}
        title={canRun ? `运行 ${name}` : "需要上传视频"}
        style={{
          background: "none",
          border: `1px solid ${canRun ? "var(--accent-border)" : "var(--border)"}`,
          borderRadius: "var(--r-sm)",
          color: canRun ? "var(--accent)" : "var(--text3)",
          fontSize: 10,
          padding: "3px 7px",
          cursor: canRun ? "pointer" : "not-allowed",
          fontFamily: "var(--font-mono)",
          flexShrink: 0,
          transition: "all 0.15s",
          lineHeight: 1,
        }}
      >
        ▶
      </button>
    </div>
  );
}
