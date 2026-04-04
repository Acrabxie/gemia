from __future__ import annotations

from typing import Any


class BaseExecutor:
    step_type = "base"

    async def validate(self, step: dict[str, Any], context: dict[str, Any]) -> None:
        return None

    async def submit(self, step: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def finalize(self, execution: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def run(self, step: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        await self.validate(step, context)
        execution = await self.submit(step, context)
        return await self.finalize(execution, context)


class LocalExecutor(BaseExecutor):
    async def submit(self, step: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        return {"status": "submitted", "step": step}
