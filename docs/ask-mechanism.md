# Ask Mechanism for Agent Loop

## Overview

The **ask mechanism** allows agents in the Lumeri loop to request structured user input and receive validated answers. It provides a rich set of UI control types and ensures type-safe, validated interactions between agent and user.

### Core Concept

1. **Agent emits a question** with control schema (via `elicit` verb)
2. **User interacts with rich controls** (selector, slider, form, etc.)
3. **User submits validated answer**
4. **Agent receives answer** and continues execution

Unlike generic "ask the user" approaches, this mechanism:
- Defines control types explicitly (no free-form guessing about UI)
- Validates answers before returning to agent
- Supports complex nested forms (panels)
- Is extensible via custom panel schemas

---

## Wiring status — IMPLEMENTED (human-in-the-loop)

The `elicit` verb is fully wired into the v3 agent loop. The round-trip is:

1. The model calls `elicit` → `gemia.tools.elicit.dispatch` (async) builds + validates
   the control schema and asks the per-session **`AskBridge`**
   (`gemia/tools/_ask_bridge.py`, injected at `ctx.extra["ask_bridge"]`) to emit an
   `ask_question` SSE event and `await` the answer.
2. Because the dispatcher is awaited at `agent_loop_v3.py` (`await DISPATCHER[name](...)`),
   the turn naturally **pauses** on that `await` — no special loop branching needed.
3. The frontend renders the controls and `POST`s the answer to
   **`/sessions/{id}/ask_response`** `{question_id, answers}` (`v3_routes._ask_response`).
4. The route → `SessionRunner.deliver_ask_answer` → `AgentLoopV3.deliver_ask_answer` →
   `AskBridge.deliver`, which hops the resolution back onto the session's event loop
   via `loop.call_soon_threadsafe` (HTTP and loop run on different threads).
5. The awaited future resolves; the answer is **validated** against the schema and the
   validated values are returned as the tool result, so the model continues with the
   answer in hand.

Resolved design decisions (from the original open questions):
- **Answer routing**: `POST /sessions/{id}/ask_response` (matches the existing route style).
- **Model injection form**: the validated answer is the tool's normal `tool_result` —
  no separate text/JSON injection, so it threads through the existing loop unchanged.
- **No-frontend fallback**: if no answer arrives within the timeout (per-call `timeout`,
  else `AskBridge` default, else `LUMERI_ASK_TIMEOUT_SEC`, else 300s), the dispatcher
  synthesises **per-control defaults** (`fallback_used: true`) so a turn never hangs forever.
- **Option ergonomics**: `select`/`multi_select` `options` accept bare strings *or*
  `{label, value}` dicts (normalised in `ask.py`); malformed options raise
  `E_ASK_INVALID_SCHEMA` rather than silently rejecting every answer.

Frontend rendering of the `ask_question` event (control widgets) is the remaining
client-side piece; the backend contract above is complete and tested.

---

## Architecture

### Three Layers

1. **Control Schema** (`gemia/tools/ask.py`)
   - Pure data structures for control types
   - Validation logic per control
   - Serialization/deserialization for SSE/API

2. **Agent Tool** (`gemia/tools/elicit.py`)
   - Verb that agents call to ask questions
   - Builds control objects from spec
   - Manages question/answer lifecycle

3. **Agent Loop Integration**
   - Loop recognizes `elicit` calls
   - Emits SSE `ask_question` event to frontend
   - Frontend renders controls and collects answer
   - Loop injects answer into next turn context

---

## Control Types

Six built-in control types, plus extensible custom panels:

### 1. Select (Single Choice)

```python
SelectControl(
    options=[
        {"label": "Option A", "value": "a"},
        {"label": "Option B", "value": "b"},
    ],
    default="a"  # optional
)
```

**Returns:** `str` — the selected value

**Validation:** Answer must be one of the option values

### 2. Multi-Select (Multiple Choices)

```python
MultiSelectControl(
    options=[
        {"label": "Red", "value": "red"},
        {"label": "Green", "value": "green"},
        {"label": "Blue", "value": "blue"},
    ],
    min=1,   # at least 1
    max=2,   # at most 2
)
```

**Returns:** `list[str]` — array of selected values

**Validation:** Length must satisfy min/max; all values must be in options

### 3. Text

```python
TextControl(
    placeholder="Enter your name",
    multiline=False,
    pattern=r"^[a-zA-Z]+$",  # optional regex
    min_length=1,
    max_length=100,
)
```

**Returns:** `str` — free-form text

**Validation:** Length and regex pattern (if provided)

### 4. Slider

```python
SliderControl(
    min=0,
    max=100,
    step=10,        # values must align to steps
    default=50,     # optional
)
```

**Returns:** `float` — numeric value

**Validation:** Must be in [min, max] and align to step

### 5. Panel (Grouped Form)

```python
PanelControl(
    description="User information",
    fields={
        "email": TextControl(pattern=r"^[^\s@]+@[^\s@]+\.[^\s@]+$"),
        "age": SliderControl(min=18, max=120),
        "colors": MultiSelectControl(
            options=[...],
            min=1,
        ),
    }
)
```

**Returns:** `dict[str, Any]` — one value per field

**Validation:** Each field validates independently; panel succeeds only if all fields pass

### 6. Custom Panel (Extensible Schema)

```python
CustomPanelControl(
    schema={
        "type": "complex_editing_form",
        "nested": {
            "transform": {"scale": 1.5, "rotate": 45},
        },
    },
    validator=my_custom_validator,  # optional callable
)
```

**Returns:** `Any` — schema-dependent, user-defined

**Validation:** Requires a host-registered `validator(schema, answer)` function.
Model-authored `elicit` calls cannot supply executable validators, so they must
use the built-in controls instead; an unvalidated custom panel fails closed.

---

## Agent-Facing API: `elicit` Verb

### Call Signature

```python
await agent.call_tool("elicit", {
    "title": "What is your preferred video format?",
    "description": "This affects the output...",
    "controls": {
        "format": {
            "type": "select",
            "options": [
                {"label": "MP4", "value": "mp4"},
                {"label": "WebM", "value": "webm"},
            ],
        },
        "quality": {
            "type": "slider",
            "min": 1080,
            "max": 4320,
            "step": 360,
            "default": 1080,
        },
    }
})
```

### Response

```json
{
    "status": "question_emitted",
    "question_id": "ask_a1b2c3d4e5f6",
    "message": "Waiting for user response",
    "question": {
        "question_id": "ask_a1b2c3d4e5f6",
        "title": "What is your preferred video format?",
        "description": "...",
        "controls": {
            "format": { "type": "select", ... },
            "quality": { "type": "slider", ... }
        }
    }
}
```

### Frontend Rendering

The agent loop emits an SSE `ask_question` event with the `question` object. Frontend renders:
- Title + description
- All controls according to their type
- Submit/Cancel buttons

### User Response

Frontend submits answer via API or SSE (implementation-specific):

```json
{
    "question_id": "ask_a1b2c3d4e5f6",
    "answers": {
        "format": "mp4",
        "quality": 1080
    }
}
```

### Answer Injection

The loop validates the answer and injects it into the next model call:

```
User submitted answer to "ask_a1b2c3d4e5f6":
- format: "mp4"
- quality: 1080
```

Agent can now use this validated data.

---

## Data Contract: AskQuestion & AskAnswer

### AskQuestion

```python
@dataclass
class AskQuestion:
    question_id: str                    # unique per turn
    title: str                          # shown to user
    description: str = ""               # optional
    controls: dict[str, Control] = {}   # {key: control_obj}
    metadata: dict[str, Any] = {}       # SSE routing, timestamps, etc.

    def to_dict(self) -> dict[str, Any]: ...
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AskQuestion: ...
```

### AskAnswer

```python
@dataclass
class AskAnswer:
    question_id: str                    # matches AskQuestion.question_id
    answers: dict[str, Any] = {}        # {control_key: validated_value}
    timestamp: Optional[str] = None     # ISO 8601

    def to_dict(self) -> dict[str, Any]: ...
    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> AskAnswer: ...
```

### Validation

```python
validated, error = validate_ask_answer(question, answer)
# (validated_dict, None) on success
# (None, error_message) on failure
```

---

## Error Codes

All ask-related errors follow the stable code pattern:

| Code | Meaning |
|------|---------|
| `E_ASK_INVALID_ANSWER` | Submitted value fails validation |
| `E_ASK_INVALID_TYPE` | Control type mismatch or unknown |
| `E_ASK_INVALID_SCHEMA` | Control schema is malformed |
| `E_ELICIT_NO_CONTROLS` | `elicit` called with no controls |
| `E_ELICIT_INVALID_SPEC` | Control specification cannot be parsed |
| `E_ELICIT_NO_QUESTION_ID` | Response missing `question_id` |
| `E_ELICIT_UNKNOWN_QUESTION` | `question_id` not found or already answered |

---

## Serialization & Roundtrips

All control types and question/answer objects serialize to JSON and deserialize back:

```python
q = AskQuestion(...)
dict_form = q.to_dict()

# Send over SSE/API
json_str = json.dumps(dict_form)

# Receive and deserialize
q2 = AskQuestion.from_dict(json.loads(json_str))
assert q2.question_id == q.question_id
```

Nested structures (panels with nested controls) are preserved across roundtrips.

---

## Usage Example: Multi-Step Video Editor

```python
# Step 1: Ask about format and quality
result1 = await agent.call_tool("elicit", {
    "title": "Export Settings",
    "controls": {
        "settings": {
            "type": "panel",
            "fields": {
                "format": {
                    "type": "select",
                    "options": [
                        {"label": "MP4 (H.264)", "value": "h264"},
                        {"label": "ProRes", "value": "prores"},
                    ],
                },
                "bitrate": {
                    "type": "slider",
                    "min": 5,
                    "max": 50,
                    "step": 5,
                    "default": 20,
                },
            },
        }
    }
})

# Loop validates and injects answer
# Agent continues with next step
```

---

## Frontend Integration Points

### Minimal Frontend

If frontend is unavailable, the agent loop can:
1. Skip ask questions (return a default)
2. Log questions to console/log file
3. Inject predefined answers (testing)

### Rich Frontend

Frontend renders controls as:
- **Select** → dropdown or radio buttons
- **Multi-Select** → checkboxes or multi-select list
- **Text** → text input or textarea
- **Slider** → range input with number display
- **Panel** → grouped form with fieldset styling
- **Custom Panel** → plugin renderer per schema type

---

## Testing

All control types tested with:
- Valid answers ✓
- Boundary values ✓
- Invalid answers (rejects with error) ✓
- Type mismatches ✓
- Nested panels ✓
- Custom validators ✓

See `tests/test_ask_mechanism.py` and `tests/test_elicit_tool.py`.

---

## Future Extensions

1. **Field dependencies** — disable/show fields based on other field values
2. **Conditional controls** — render different controls based on prior selection
3. **Progress indicators** — show user how many steps remain in multi-question flow
4. **Async validators** — fetch options from server (e.g., "choose from 10k clips")
5. **Rich media preview** — display image/video option thumbnails in select
6. **Undo/rewind** — let user go back and change earlier answers in a multi-step flow
