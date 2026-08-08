"""Prompt and live-context assembly for AgentLoopV3."""

from __future__ import annotations

import json
from typing import Any

from gemia import memory as _memory
from gemia import project_context as _project_context
from gemia.agent_loop_v3_protocol import _looks_like_model_identity_question
from gemia.env_probe import format_environment_summary
from gemia.plan_mode import PLAN_MODE_PROMPT

_FRESH_LUMENFRAME_OPS = """Fresh/empty LumenFrame bootstrap:
- Inspect the current layer tree with `get_lumenframe`.
- Apply one or more LayerPatch operations atomically with `lumen_patch` using `{"ops":[...]}`.
- Root children composite bottom -> top; give created layers stable ids.

Appearance LayerPatch examples (combine only the operations the edit needs):
- Gradient: `{"op":"add_gradient","id":"bg","mode":"linear","stops":[[0,"#000000"],[1,"#3344ff"]],"angle":90}`
- Shape: `{"op":"add_shape","id":"shape1","kind":"rect","fill":"#ff0044","rect":[0.1,0.1,0.9,0.9],"radius":12}`
- Opacity: `{"op":"set_opacity","layer_id":"shape1","opacity":0.7}` (range 0..1)
- Blend: `{"op":"set_blend_mode","layer_id":"shape1","blend_mode":"screen"}`
- Effect/blur: `{"op":"add_effect","layer_id":"shape1","effect":{"type":"gaussian_blur","params":{"radius":8}}}`
- Mask: `{"op":"set_mask","layer_id":"shape1","mask":{"kind":"shape","shape":{"type":"rectangle","x0":0.1,"y0":0.1,"x1":0.9,"y1":0.9},"feather":0.02}}`
- Clip to the immediate layer below: `{"op":"clip_to_below","layer_id":"shape1","enabled":true}`
- Adjustment layer (affects layers below): `{"op":"add_adjustment_layer","name":"Soften","start":0,"duration":5,"effects":[{"type":"gaussian_blur","params":{"radius":2}}]}`

After the first layer exists, the complete operations catalog is injected automatically."""


class AgentLoopPromptMixin:
    # Cap for the injected layer-tree summary: a deep comp tree must not
    # balloon every prompt; past this the model should get_lumenframe for detail.
    _LUMENFRAME_PROMPT_CAP = 3500

    def _get_lumenframe_prompt_text(self) -> str:
        """Get lumenframe document summary for prompt injection.

        Reads the session's REAL document via ``layer.peek_lumendoc`` (in v3
        that is ``<project_dir>/lumenframe.json`` — ``ctx.project`` is always
        set, so the old ``_DOC_CACHE`` lookup never saw saved edits and the
        slot was permanently empty in real sessions). Read-only: never creates
        the file. Size-capped so a deep tree cannot balloon every prompt.
        """
        try:
            from gemia.tools import layer as _layer

            doc = _layer.peek_lumendoc(self._tool_ctx)
            if doc is None:
                return "(no lumenframe document in session yet)"
            root = doc.get("root", {})
            selection = doc.get("selection", [])
            canvas = doc.get("canvas", {})

            lines = []
            lines.append(
                f"Canvas: {canvas.get('width')}×{canvas.get('height')} @ {canvas.get('fps')} fps"
            )
            if root:
                lines.append("Layer tree:")
                # Use the compact tree summary function from layer.py
                tree_summary = _layer._compact_tree_summary(root)
                lines.append(tree_summary)
            if selection:
                # Full ids — the model targets layers by this exact string.
                lines.append(f"Selection: {', '.join(str(id) for id in selection)}")
            text = "\n".join(lines) if lines else "(empty document)"
            if len(text) > self._LUMENFRAME_PROMPT_CAP:
                text = (
                    text[: self._LUMENFRAME_PROMPT_CAP]
                    + "\n… (layer tree truncated — use get_lumenframe for the full document)"
                )
            return text
        except (ImportError, AttributeError, KeyError):
            return "(lumenframe not available)"

    def _get_lumenframe_ops_catalog(self) -> str:
        """Get lumenframe operations catalog for prompt injection.

        **Conditional injection (on-demand):**
        - If the session has a non-empty lumenframe doc (root.children with ≥1 layer),
          inject the complete operation vocabulary from lumenframe.describe_ops().
        - If the doc is empty or absent, return a compact appearance bootstrap
          instead of the much larger full catalog. Once the user starts a
          lumenframe edit, the full vocabulary auto-injects.

        Like ``_get_lumenframe_prompt_text`` this reads the persisted document
        (``peek_lumendoc``), not the legacy in-memory cache.
        """
        try:
            from gemia.tools import layer as _layer
            from lumenframe import describe_ops

            doc = _layer.peek_lumendoc(self._tool_ctx)
            if doc is not None:
                root = doc.get("root", {})
                children = root.get("children", [])
                # Non-empty doc: inject full ops catalog
                if children:
                    return describe_ops()

            # Empty or missing doc: compact discovery + appearance bootstrap.
            return _FRESH_LUMENFRAME_OPS
        except (ImportError, Exception):
            return "(lumenframe operations not available)"

    def _memory_for_prompt(self) -> str:
        """Global memory plus the current Project's shared memory and log.

        Delegates to ``gemia.memory.format_memory_for_prompt`` (which reads
        MEMORY.md, capped at a few KB) and never raises: any failure degrades
        to a short placeholder so prompt assembly cannot break on a missing or
        unreadable memory store. Deliberately excludes model/provider defaults
        — see ``_runtime_engine_text`` for the one place that fact belongs.
        """
        try:
            global_memory = _memory.format_memory_for_prompt()
            store = self._tool_ctx.extra.get("production_store")
            project_id = str(self._tool_ctx.extra.get("project_id") or "")
            if not _project_context.is_project_workspace(store, project_id):
                return "Global memory (applies across all Projects):\n" + global_memory
            project_memory = _project_context.format_for_prompt(store, project_id)
            return (
                "Global memory (applies across all Projects):\n"
                + global_memory
                + "\n\n"
                + project_memory
            )
        except Exception:  # noqa: BLE001 — memory must never break prompt build
            return "(durable memory unavailable this session)"

    def _project_workspace_for_prompt(self) -> str:
        """Describe the named Project as one long-lived multi-session workspace.

        The production store keeps absolute host paths, but the model only needs
        stable workspace semantics and the virtual roots exposed by file tools.
        Avoiding raw paths here also keeps local machine details out of prompts
        and transcripts.
        """
        try:
            store = self._tool_ctx.extra.get("production_store")
            project_id = str(self._tool_ctx.extra.get("project_id") or "").strip()
            if store is None or not project_id:
                return (
                    "This conversation is not inside a named Project workspace. "
                    "Do not invent Project identity or shared Project history."
                )
            if not _project_context.is_project_workspace(store, project_id):
                return (
                    "This is an independent Chat with an internal session workspace, "
                    "not a user-created Project. Do not claim shared Project context."
                )
            record = store.load_project(project_id)
            name = " ".join(str(record.get("name") or "").split())[:160]
            source_text = (
                "`project://source/` is the bound source folder."
                if str(record.get("source_root") or "").strip()
                else "No source folder is bound."
            )
            session_count = len(record.get("session_ids") or [])
            return (
                f"Current Project: {name}\n"
                "Treat this Project as one long-lived workspace, not as a folder "
                "label and not as a fresh workspace per chat.\n"
                f"{source_text} `project://edit/` is Lumeri's private editing root.\n"
                f"Known sessions in this workspace: {session_count}.\n"
                "Every session in this Project continues the same assets, edit and "
                "timeline state, Project memory, and Project logs. Reuse that shared "
                "state before creating replacements. Conversation wording remains "
                "session-specific; durable Project facts, decisions, constraints, "
                "and progress belong in Project memory or Project logs. Never carry "
                "this Project's private context into another Project."
            )
        except Exception:  # noqa: BLE001 — prompt assembly must stay available
            return (
                "Project workspace identity is unavailable for this session. "
                "Do not guess its name, roots, or shared history."
            )

    def _runtime_engine_text(self) -> str:
        """The ACTUAL provider/model this turn is running on, read live off
        ``self.client`` — for the ``{{runtime_engine}}`` slot.

        This is the one place the model is told what it is: a ground-truth
        fact resolved by the host from live config each turn (see
        ``GeminiClientV3.__init__``), not a static or stale value baked into
        the prompt or memory. The point isn't to hide this — it's that a
        model has no reliable way to know it from the inside, so if asked, it
        should read the real answer here instead of guessing from its own
        training-time self-belief (which is routinely wrong once routed
        through this host) or reciting an unrelated cached value.
        """
        try:
            provider = getattr(self.client, "provider", "") or "(unknown)"
            model = getattr(self.client, "model", "") or "(unknown)"
            return f"provider = `{provider}`, model = `{model}`"
        except Exception:  # noqa: BLE001 — must never break prompt build
            return "(runtime engine info unavailable this session)"

    def _search_engine_text(self) -> str:
        """Quiet live context for using first-party search when relevant."""

        try:
            from gemia.tools.web_search import search_provider_status

            status = search_provider_status()
            built_in = status["built_in"]
            return (
                f"effective={status['effective_provider']}; "
                f"ready={str(bool(status['ready'])).lower()}; "
                f"built_in_{built_in['role']}={built_in['provider']}"
            )
        except Exception:  # noqa: BLE001 — must never break prompt build
            return "status unavailable"

    def _auto_log_turn(
        self,
        *,
        tools_succeeded: int,
        tools_failed: int,
        assets_produced: int,
    ) -> None:
        """Append ONE concise line to today's daily log at turn end.

        Records the user's ask (truncated) + what was done (tool / asset
        counts). Best-effort and non-fatal by contract: secret-looking content
        is dropped inside ``append_daily_entry`` and the whole thing is wrapped
        in try/except so a logging failure can never break the turn. Skips
        cleanly when there is no pinned intent (nothing to log)."""
        try:
            ask = (self._pinned_intent or "").strip()
            if not ask:
                return
            ask = " ".join(ask.split())
            if len(ask) > 140:
                ask = ask[:139].rstrip() + "…"

            done_bits: list[str] = []
            if tools_succeeded:
                done_bits.append(f"{tools_succeeded} tool call(s)")
            if assets_produced:
                done_bits.append(f"{assets_produced} asset(s)")
            if tools_failed:
                done_bits.append(f"{tools_failed} failure(s)")
            done = ", ".join(done_bits) if done_bits else "no tool calls"

            note = f"v3 turn — ask: {ask} | done: {done}"
            _memory.append_daily_entry(note)
            store = self._tool_ctx.extra.get("production_store")
            project_id = str(self._tool_ctx.extra.get("project_id") or "")
            if _project_context.is_project_workspace(store, project_id):
                _project_context.append_log(store, project_id, note)
        except Exception:  # noqa: BLE001 — logging must never break the turn
            pass

    def render_messages(self) -> list[dict[str, Any]]:
        """Build the messages list for the next model call.

        System prompt = ``system_v3.md`` with the placeholders filled in:
        ``{{environment}}`` from a live probe of the running interpreter and
        installed dependencies (gemia.env_probe.format_environment_summary),
        ``{{memory}}`` from the durable Gemia memory store
        (gemia.memory.format_memory_for_prompt — MEMORY.md only),
        ``{{project_workspace}}`` from the current named Project record,
        ``{{runtime_engine}}`` from the live client's resolved provider/model
        (self._runtime_engine_text — the ground truth if the model is asked
        what it's running on; recomputed fresh every call, never cached),
        ``{{asset_registry}}`` from the live AssetRegistry compact text,
        ``{{pending_jobs}}`` from the live JobRegistry compact text,
        ``{{lumenframe_ops}}`` from conditional lumenframe operation guidance,
        ``{{lumenframe}}`` from the session lumenframe document state (if any),
        ``{{timeline}}`` from the session project's compact timeline summary,
        and ``{{pinned_intent}}`` from the user's first message in this
        session. After the system message comes the rolling
        user/assistant/tool window in chronological order.
        """
        lumenframe_ops = self._get_lumenframe_ops_catalog()
        lumenframe_text = self._get_lumenframe_prompt_text()
        if self._turn_ledger is not None:
            ledger = self._turn_ledger
            turn_ledger_text = (
                "Tool activity (observational): "
                f"calls={ledger.sequence}; "
                f"final_assets={ledger.final_asset_ids}; "
                f"pending_jobs={ledger.pending_jobs}; "
                f"unresolved_tool_failures={list(ledger.unresolved_failures)}; "
                f"compact_history={ledger.compact_history[-12:]}"
            )
        else:
            turn_ledger_text = "(no tool activity in this turn)"
        if self._turn_ledger is None and self._compacted_history:
            turn_ledger_text += f"; compact_history={self._compacted_history[-12:]}"
        system_filled = (
            self._system_template.replace(
                "{{plan_mode}}", PLAN_MODE_PROMPT if self.plan_mode else ""
            )
            .replace("{{turn_ledger}}", turn_ledger_text)
            .replace("{{environment}}", format_environment_summary())
            .replace("{{project_workspace}}", self._project_workspace_for_prompt())
            .replace("{{memory}}", self._memory_for_prompt())
            .replace("{{runtime_engine}}", self._runtime_engine_text())
            .replace("{{search_engine}}", self._search_engine_text())
            .replace("{{asset_registry}}", self.registry.compact_text())
            .replace("{{pending_jobs}}", self._tool_ctx.jobs.compact_text_for_prompt())
            .replace("{{production_context}}", self._production_context_for_prompt())
            .replace("{{lumenframe_ops}}", lumenframe_ops)
            .replace("{{lumenframe}}", lumenframe_text)
            .replace(
                "{{timeline}}",
                self.project.compact_text() + "\n" + self.project.segment_compact_text(),
            )
            .replace("{{pinned_intent}}", self._pinned_intent or "(not yet provided)")
        )
        continuation_text = self._provider_continuation_text
        self._provider_continuation_text = {}
        history = [
            {
                **message,
                "content": continuation_text[id(message)],
            }
            if id(message) in continuation_text
            else message
            for message in self._messages
        ]
        msgs = [{"role": "system", "content": system_filled}, *history]

        # Recency grounding: the live state already lives in the system prompt, but
        # it sits in a low-attention slot while the pinned first request is re-shown
        # every turn — so the model over-anchors on the original framing and
        # under-reads the current reality. Surface a short state digest in the most
        # RECENT message (the slot the model attends to most) so each next step is
        # grounded in what is actually there now. We append into the last message's
        # content rather than adding a message, to preserve the alternating-role
        # contract the client requires (consecutive tool results fold into one user
        # message; a second user message would break it).
        digest = self._env_recency_digest()
        if digest and len(msgs) > 1:
            tail = msgs[-1]
            if tail.get("role") in ("user", "tool") and isinstance(tail.get("content"), str):
                tail = dict(tail)
                tail["content"] = f"{tail['content']}\n\n{digest}" if tail["content"] else digest
                msgs[-1] = tail
        return msgs

    def _production_context_for_prompt(self) -> str:
        status = self._production_status()
        if not status:
            return "(no durable ProductionRun in this session)"
        return json.dumps(status, ensure_ascii=False, sort_keys=True, default=str)[:12000]

    def _env_recency_digest(self) -> str:
        """A short snapshot of the live state for the recency slot.

        Deliberately brief: the full Timeline / Layer Document / asset registry are
        in the system prompt. This is the *pointer* that pulls attention back to the
        present and tells the model to act on current reality, not on the pinned
        original request or its memory of earlier turns.
        """

        def _first(text: str) -> str:
            for line in (text or "").splitlines():
                line = line.strip()
                if line:
                    return line
            return ""

        snaps: list[str] = []
        if self.plan_mode:
            # Reinforcement only — the authoritative instructions live in the
            # system prompt's {{plan_mode}} slot. This line rides the recency
            # digest so the model is reminded right where it attends most.
            snaps.append(
                "Plan mode: ON — inspect and plan only; mutating tools are "
                "blocked until the user approves the plan."
            )
        tl = _first(self.project.compact_text())
        if _looks_like_model_identity_question(getattr(self, "_last_user_message", "")):
            snaps.append(
                "Conversation turn: answer the user's question about you directly. "
                "Do not create media, inspect assets, or continue an older task unless the user asks for that now."
            )
        if tl:
            snaps.append(f"Timeline: {tl}")
        lf = self._get_lumenframe_prompt_text() or ""
        if lf and not lf.startswith("("):
            snaps.append(
                "Layers: " + " | ".join(s.strip() for s in lf.splitlines() if s.strip())[:240]
            )
        assets = [r.asset_id for r in self.registry.list_records()][-4:]
        if assets:
            snaps.append("Latest assets: " + ", ".join(assets))
        production = self._production_status()
        if production.get("production_state"):
            gaps = production.get("evidence_gaps") or []
            snaps.append(
                "Production: "
                + str(production.get("production_state"))
                + (
                    " | missing evidence: " + ", ".join(str(gap) for gap in gaps)
                    if gaps
                    else " | current gate satisfied"
                )
            )
            scope = (production.get("creative_ir") or {}).get("active_revision_scope")
            if scope:
                snaps.append("Revision scope: " + json.dumps(scope, ensure_ascii=False)[:500])
        if not snaps:
            return ""
        return (
            "[Current state — ground your NEXT step in this present reality, not the "
            "original request or your memory of earlier turns. Re-read the full "
            "Timeline / Layer Document / asset registry above before a consequential "
            "step; after a change, confirm the result here and correct course if it "
            "diverged. Narrate and reply in the USER's language (match their latest "
            "message) from the first line of the turn — no stock English openers, and "
            "vary your phrasing: never open every narration line with the same formula "
            "(e.g. 「我将…」/'I will …').]\n" + "\n".join(snaps)
        )


__all__ = ["AgentLoopPromptMixin"]
