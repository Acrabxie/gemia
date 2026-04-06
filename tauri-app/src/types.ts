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

export interface AskQuestion {
  id: string;
  text: string;
  input_type: "choices" | "slider" | "text";
  // choices
  choices?: string[];
  // slider
  min?: number;
  max?: number;
  default?: number;
  step?: number;
  unit?: string;
  // text
  placeholder?: string;
}
