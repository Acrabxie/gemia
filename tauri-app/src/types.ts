export type AppStatus =
  | "starting"
  | "ready"
  | "planning"
  | "executing"
  | "done"
  | "error"
  | "asking";

export interface ChatMessage {
  id: string;
  role: "user" | "status";
  content: string;
  statusType?: AppStatus;
  timestamp: number;
}

export interface Skill {
  name: string;
}
