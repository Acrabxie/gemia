"""Compiled, validated capability surface for the production agent.

Historically Lumeri's schemas, dispatchers, router packs, plan policy and cost
table could drift independently.  ``ToolCapabilityRegistry`` compiles those
inputs into one immutable runtime catalog.  AgentLoop uses this compiled
catalog for both model schemas and execution lookup, while help and audit
surfaces render from the same records.

The source declarations remain intentionally small and reviewable in their
domain modules; no capability becomes callable until compilation validates all
of them together.
"""
from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Awaitable, Callable, Iterable, Mapping


Dispatcher = Callable[[dict[str, Any], Any], Awaitable[dict[str, Any]]]


class CapabilityRegistryError(RuntimeError):
    """Raised when the runtime capability surface contains drift."""


@dataclass(frozen=True)
class ToolCapability:
    name: str
    schema: Mapping[str, Any]
    dispatcher: Dispatcher
    workflows: tuple[str, ...]
    plan_mode: str
    estimated_usd: float
    estimated_eta_sec: float
    paid_media: bool
    uses_default_cost: bool
    effect: str
    execution: str
    surface: str
    exposed_via: tuple[str, ...]
    requires_idempotency_key: bool
    requires_project_revision: bool

    @property
    def description(self) -> str:
        function = self.schema.get("function")
        return str(function.get("description") or "") if isinstance(function, Mapping) else ""

    def manifest_record(self) -> dict[str, Any]:
        """Return the generated first-party contract for this capability."""

        function = self.schema.get("function")
        parameters = (
            dict(function.get("parameters") or {})
            if isinstance(function, Mapping)
            else {}
        )
        return {
            "name": self.name,
            "description": self.description,
            "parameters": parameters,
            "effect": self.effect,
            "execution": self.execution,
            "surface": self.surface,
            "exposed_via": list(self.exposed_via),
            "plan_mode": self.plan_mode,
            "workflows": list(self.workflows),
            "paid": self.paid_media,
            "estimated_usd": self.estimated_usd,
            "estimated_eta_sec": self.estimated_eta_sec,
            "requires_idempotency_key": self.requires_idempotency_key,
            "requires_project_revision": self.requires_project_revision,
        }


class ToolCapabilityRegistry:
    """Immutable runtime authority for schemas, dispatch and audit metadata."""

    def __init__(self, capabilities: Iterable[ToolCapability]) -> None:
        ordered = tuple(capabilities)
        by_name = {item.name: item for item in ordered}
        if len(by_name) != len(ordered):
            duplicates = sorted(
                {item.name for item in ordered if sum(x.name == item.name for x in ordered) > 1}
            )
            raise CapabilityRegistryError(f"duplicate capabilities: {duplicates}")
        self._ordered = ordered
        self._by_name = MappingProxyType(by_name)

    @classmethod
    def compile(
        cls,
        *,
        schemas: Iterable[Mapping[str, Any]],
        dispatchers: Mapping[str, Dispatcher],
        workflow_packs: Mapping[str, Iterable[str]],
        plan_allowed: Iterable[str],
        plan_blocked: Iterable[str],
        tool_costs: Mapping[str, Mapping[str, float]],
        default_cost_tools: Iterable[str] = (),
        paid_media_tools: Iterable[str] = (),
        control_tools: Iterable[str] = (),
        effect_by_name: Mapping[str, str] | None = None,
        execution_by_name: Mapping[str, str] | None = None,
        surface_by_name: Mapping[str, str] | None = None,
        exposed_via_by_name: Mapping[str, Iterable[str]] | None = None,
    ) -> "ToolCapabilityRegistry":
        schema_list = tuple(schemas)
        names: list[str] = []
        schema_by_name: dict[str, Mapping[str, Any]] = {}
        for schema in schema_list:
            function = schema.get("function") if isinstance(schema, Mapping) else None
            name = str(function.get("name") or "") if isinstance(function, Mapping) else ""
            if not name:
                raise CapabilityRegistryError("tool schema is missing function.name")
            if name in schema_by_name:
                raise CapabilityRegistryError(f"duplicate tool schema: {name}")
            names.append(name)
            schema_by_name[name] = schema

        real_names = set(names)
        dispatch_names = set(dispatchers)
        packed_names = {
            str(name)
            for members in workflow_packs.values()
            for name in members
        }
        allowed = {str(name) for name in plan_allowed}
        blocked = {str(name) for name in plan_blocked}
        defaults = {str(name) for name in default_cost_tools}
        paid = {str(name) for name in paid_media_tools}
        controls = {str(name) for name in control_tools}
        effect_by_name = dict(effect_by_name or {})
        execution_by_name = dict(execution_by_name or {})
        surface_by_name = dict(surface_by_name or {})
        exposed_via_by_name = {
            str(name): tuple(str(item) for item in values)
            for name, values in (exposed_via_by_name or {}).items()
        }

        problems: list[str] = []
        missing_dispatch = sorted(real_names - dispatch_names)
        unknown_dispatch = sorted(dispatch_names - real_names)
        missing_pack = sorted(real_names - packed_names - controls)
        unknown_pack = sorted(packed_names - real_names)
        both_plan = sorted(real_names & allowed & blocked)
        missing_plan = sorted(real_names - allowed - blocked)
        missing_cost = sorted(real_names - set(tool_costs) - defaults)
        stale_defaults = sorted(defaults - real_names)
        stale_paid = sorted(paid - real_names)
        stale_controls = sorted(controls - real_names)
        missing_effect = sorted(real_names - set(effect_by_name))
        unknown_effect = sorted(set(effect_by_name) - real_names)
        missing_execution = sorted(real_names - set(execution_by_name))
        unknown_execution = sorted(set(execution_by_name) - real_names)
        missing_surface = sorted(real_names - set(surface_by_name))
        unknown_surface = sorted(set(surface_by_name) - real_names)
        missing_exposure = sorted(real_names - set(exposed_via_by_name))
        unknown_exposure = sorted(set(exposed_via_by_name) - real_names)
        if missing_dispatch:
            problems.append(f"missing dispatchers={missing_dispatch}")
        if unknown_dispatch:
            problems.append(f"unknown dispatchers={unknown_dispatch}")
        if missing_pack:
            problems.append(f"unrouted capabilities={missing_pack}")
        if unknown_pack:
            problems.append(f"unknown packed names={unknown_pack}")
        if both_plan:
            problems.append(f"dual plan policy={both_plan}")
        if missing_plan:
            problems.append(f"missing plan policy={missing_plan}")
        if missing_cost:
            problems.append(f"missing cost policy={missing_cost}")
        if stale_defaults:
            problems.append(f"stale default costs={stale_defaults}")
        if stale_paid:
            problems.append(f"stale paid-media names={stale_paid}")
        if stale_controls:
            problems.append(f"stale control names={stale_controls}")
        if missing_effect:
            problems.append(f"missing effect policy={missing_effect}")
        if unknown_effect:
            problems.append(f"unknown effect policy={unknown_effect}")
        if missing_execution:
            problems.append(f"missing execution policy={missing_execution}")
        if unknown_execution:
            problems.append(f"unknown execution policy={unknown_execution}")
        if missing_surface:
            problems.append(f"missing surface policy={missing_surface}")
        if unknown_surface:
            problems.append(f"unknown surface policy={unknown_surface}")
        if missing_exposure:
            problems.append(f"missing exposure policy={missing_exposure}")
        if unknown_exposure:
            problems.append(f"unknown exposure policy={unknown_exposure}")
        for name, effect in effect_by_name.items():
            if effect not in {"read", "write"}:
                problems.append(f"invalid effect policy={name}:{effect}")
        for name, execution in execution_by_name.items():
            if execution not in {"sync", "job"}:
                problems.append(f"invalid execution policy={name}:{execution}")
        for name, surface in surface_by_name.items():
            if surface not in {"product", "host", "internal"}:
                problems.append(f"invalid surface policy={name}:{surface}")
        valid_exposures = {"agent", "http", "mcp"}
        for name, exposures in exposed_via_by_name.items():
            unknown = sorted(set(exposures) - valid_exposures)
            if unknown:
                problems.append(f"invalid exposure policy={name}:{unknown}")
        for name in sorted(real_names & dispatch_names):
            dispatcher = dispatchers[name]
            if getattr(dispatcher, "__name__", "").startswith("stub_"):
                problems.append(f"stub dispatcher={name}")
        if problems:
            raise CapabilityRegistryError("; ".join(problems))

        workflows_by_name: dict[str, list[str]] = {name: [] for name in names}
        for workflow, members in workflow_packs.items():
            for name in members:
                if str(name) in workflows_by_name:
                    workflows_by_name[str(name)].append(str(workflow))

        capabilities: list[ToolCapability] = []
        for name in names:
            cost = tool_costs.get(name)
            uses_default = cost is None
            estimated_usd = float(cost.get("usd", 0.0)) if cost is not None else 0.0
            estimated_eta = float(cost.get("eta_sec", 5.0)) if cost is not None else 5.0
            capabilities.append(
                ToolCapability(
                    name=name,
                    schema=schema_by_name[name],
                    dispatcher=dispatchers[name],
                    workflows=tuple(workflows_by_name[name]),
                    plan_mode="allowed" if name in allowed else "blocked",
                    estimated_usd=estimated_usd,
                    estimated_eta_sec=estimated_eta,
                    paid_media=name in paid,
                    uses_default_cost=uses_default,
                    effect=effect_by_name[name],
                    execution=execution_by_name[name],
                    surface=surface_by_name[name],
                    exposed_via=exposed_via_by_name[name],
                    requires_idempotency_key=(
                        effect_by_name[name] == "write" or name in paid
                    ),
                    requires_project_revision=effect_by_name[name] == "write",
                )
            )
        return cls(capabilities)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self._ordered)

    def get(self, name: str) -> ToolCapability:
        try:
            return self._by_name[str(name)]
        except KeyError:
            raise KeyError(f"capability is not installed: {name!r}") from None

    def schemas(self, names: Iterable[str] | None = None) -> list[dict[str, Any]]:
        allowed = set(self.names if names is None else (str(name) for name in names))
        unknown = sorted(allowed - set(self._by_name))
        if unknown:
            raise CapabilityRegistryError(f"schema request contains unknown tools: {unknown}")
        return [dict(item.schema) for item in self._ordered if item.name in allowed]

    def dispatcher(self, name: str) -> Dispatcher:
        return self.get(name).dispatcher

    def validate_arguments(self, name: str, arguments: Mapping[str, Any]) -> None:
        """Validate the deterministic JSON-schema subset used by tool schemas."""

        from gemia.errors import RECOVERY_FIX_ARGS, ToolError

        capability = self.get(name)
        function = capability.schema.get("function")
        parameters = (
            function.get("parameters")
            if isinstance(function, Mapping)
            else None
        )
        if not isinstance(arguments, Mapping):
            raise ToolError(
                "capability arguments must be an object",
                code="E_BAD_ARG",
                recovery=RECOVERY_FIX_ARGS,
            )
        if not isinstance(parameters, Mapping):
            return
        properties = parameters.get("properties")
        properties = properties if isinstance(properties, Mapping) else {}
        required = parameters.get("required")
        required = required if isinstance(required, (list, tuple)) else ()
        missing = [str(key) for key in required if key not in arguments]
        if missing:
            raise ToolError(
                f"missing required capability arguments: {', '.join(missing)}",
                code="E_BAD_ARG",
                recovery=RECOVERY_FIX_ARGS,
                valid_options=list(properties),
            )
        if parameters.get("additionalProperties") is False:
            unknown = sorted(str(key) for key in arguments if key not in properties)
            if unknown:
                raise ToolError(
                    f"unknown capability arguments: {', '.join(unknown)}",
                    code="E_BAD_ARG",
                    recovery=RECOVERY_FIX_ARGS,
                    valid_options=list(properties),
                )
        expected_types = {
            "string": str,
            "number": (int, float),
            "integer": int,
            "boolean": bool,
            "object": Mapping,
            "array": (list, tuple),
        }
        for key, value in arguments.items():
            spec = properties.get(key)
            if not isinstance(spec, Mapping) or value is None:
                continue
            declared = spec.get("type")
            declared_types = (
                tuple(str(item) for item in declared)
                if isinstance(declared, (list, tuple))
                else (str(declared),)
            )
            candidates = tuple(
                expected_types[item]
                for item in declared_types
                if item in expected_types
            )
            invalid = bool(candidates) and not any(
                isinstance(value, candidate) for candidate in candidates
            )
            if (
                any(item in {"number", "integer"} for item in declared_types)
                and isinstance(value, bool)
                and "boolean" not in declared_types
            ):
                invalid = True
            if invalid:
                raise ToolError(
                    f"capability argument {key!r} must be one of {list(declared_types)}",
                    code="E_BAD_ARG",
                    recovery=RECOVERY_FIX_ARGS,
                )
            enum = spec.get("enum")
            if isinstance(enum, (list, tuple)) and value not in enum:
                raise ToolError(
                    f"capability argument {key!r} must be one of {list(enum)}",
                    code="E_BAD_ARG",
                    recovery=RECOVERY_FIX_ARGS,
                    valid_options=list(enum),
                )

    def help_rows(self, names: Iterable[str] | None = None) -> list[dict[str, Any]]:
        selected = set(self.names if names is None else (str(name) for name in names))
        rows: list[dict[str, Any]] = []
        for item in self._ordered:
            if item.name not in selected:
                continue
            rows.append(
                {
                    "name": item.name,
                    "description": item.description,
                    "workflows": list(item.workflows),
                    "plan_mode": item.plan_mode,
                    "estimated_usd": item.estimated_usd,
                    "estimated_eta_sec": item.estimated_eta_sec,
                    "paid_media": item.paid_media,
                    "effect": item.effect,
                    "execution": item.execution,
                    "surface": item.surface,
                    "exposed_via": list(item.exposed_via),
                }
            )
        return rows

    def manifest(
        self,
        *,
        surface: str | None = None,
        exposed_via: str | None = None,
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for item in self._ordered:
            if surface is not None and item.surface != surface:
                continue
            if exposed_via is not None and exposed_via not in item.exposed_via:
                continue
            records.append(item.manifest_record())
        return records


def build_default_registry() -> ToolCapabilityRegistry:
    """Compile the installed runtime surface without exposing private tables."""

    from gemia.budget_guard import _PAID_MEDIA_TOOLS, _TOOL_COSTS
    from gemia.plan_mode import PLAN_ALLOWED_TOOLS, PLAN_BLOCKED_TOOLS
    from gemia.tool_router import CONTROL_TOOLS, TOOL_PACKS
    from gemia.tools import DISPATCHER
    from gemia.tools._schema import TOOL_SCHEMAS
    from gemia.mcp.toolset import MCP_TOOLSET

    # Kept here rather than in tests: these are legitimate zero-cost utility
    # defaults and therefore part of the runtime contract.
    default_cost_tools = {
        "recall_skills",
        "remember",
        "log_note",
        "project_export_otio",
        "project_import_otio",
        "read_file",
        "write_file",
        "copy_in",
        "list_dir",
        "move_file",
        "organize_files",
        "elicit",
    }
    names = {
        str(schema["function"]["name"])
        for schema in TOOL_SCHEMAS
    }
    # The generic first-party product call surface deliberately excludes host
    # escape hatches and private agent plumbing. Project-scoped file work stays
    # available through domain services with project:// roots; arbitrary host
    # paths never become product capabilities.
    host_capabilities = {
        "web_open", "fetch",
        "run_shell", "build", "check_job", "wait_for_job", "kill_job",
        "read_file", "write_file", "copy_in", "list_dir", "file_delete",
        "move_file", "organize_files",
        "project_import_otio",
    }
    internal_capabilities = {
        "save_skill", "recall_skills", "remember", "log_note",
        "elicit", "spawn_subtasks",
    }
    job_capabilities = {
        "generate_image", "generate_video", "generate_audio", "narrate",
        "edit_image", "edit_video", "composite", "color_grade",
        "adjust_media", "paint_mask_effect", "add_overlay",
        "arrange_timeline", "mix_audio", "edit_audio", "smart_reframe",
        "assemble_shotlist", "assemble_quanta", "prepare_roughcut",
        "export", "lumen_render", "lumen_render_range", "render_preview",
        "project_export", "verify_delivery",
        "run_shell", "build", "wait_for_job", "spawn_subtasks",
    }
    surface_by_name = {
        name: (
            "host" if name in host_capabilities
            else "internal" if name in internal_capabilities
            else "product"
        )
        for name in names
    }
    effect_by_name = {
        name: ("read" if name in PLAN_ALLOWED_TOOLS else "write")
        for name in names
    }
    execution_by_name = {
        name: ("job" if name in job_capabilities else "sync")
        for name in names
    }
    exposed_via_by_name = {
        name: tuple(
            exposure
            for exposure in ("agent", "http", "mcp")
            if (
                exposure == "agent"
                or (exposure == "http" and surface_by_name[name] == "product")
                or (exposure == "mcp" and name in MCP_TOOLSET)
            )
        )
        for name in names
    }
    return ToolCapabilityRegistry.compile(
        schemas=TOOL_SCHEMAS,
        dispatchers=DISPATCHER,
        workflow_packs=TOOL_PACKS,
        plan_allowed=PLAN_ALLOWED_TOOLS,
        plan_blocked=PLAN_BLOCKED_TOOLS,
        tool_costs=_TOOL_COSTS,
        default_cost_tools=default_cost_tools,
        paid_media_tools=_PAID_MEDIA_TOOLS,
        control_tools=CONTROL_TOOLS,
        effect_by_name=effect_by_name,
        execution_by_name=execution_by_name,
        surface_by_name=surface_by_name,
        exposed_via_by_name=exposed_via_by_name,
    )


__all__ = [
    "CapabilityRegistryError",
    "ToolCapability",
    "ToolCapabilityRegistry",
    "build_default_registry",
]
