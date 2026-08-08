"""Tests for AskBridge: the human-in-the-loop emit/await/deliver/timeout plumbing."""
import asyncio
import threading
import time

from gemia.tools._ask_bridge import AskBridge


def _question(qid="ask_test"):
    return {
        "question_id": qid,
        "title": "t",
        "controls": {"choice": {"type": "text", "min_length": 1}},
    }


def test_deliver_resolves_awaiting_future():
    emitted = []
    bridge = AskBridge(lambda ev: emitted.append(ev), default_timeout=5.0)

    async def main():
        # Deliver from another thread once the question is pending.
        def deliver_later():
            for _ in range(200):
                if bridge.pending_ids():
                    bridge.deliver("ask_test", {"choice": "yes"})
                    return
                time.sleep(0.005)
        threading.Thread(target=deliver_later, daemon=True).start()
        return await bridge.emit_and_wait(_question("ask_test"))

    answer = asyncio.run(main())
    assert answer == {"choice": "yes"}
    assert emitted and emitted[0]["kind"] == "ask_question"
    assert bridge.pending_ids() == []  # cleaned up


def test_timeout_returns_none_sentinel():
    bridge = AskBridge(lambda ev: None, default_timeout=0.15)

    async def main():
        t0 = time.monotonic()
        ans = await bridge.emit_and_wait(_question("ask_to"))
        return ans, time.monotonic() - t0

    ans, elapsed = asyncio.run(main())
    assert ans is None
    assert 0.1 <= elapsed < 1.0
    assert bridge.pending_ids() == []


def test_explicit_timeout_overrides_default():
    bridge = AskBridge(lambda ev: None, default_timeout=99.0)

    async def main():
        return await bridge.emit_and_wait(_question("ask_o"), timeout=0.1)

    assert asyncio.run(main()) is None


def test_explicit_timeout_is_clamped_to_host_maximum():
    bridge = AskBridge(
        lambda ev: None, default_timeout=99.0, max_timeout=0.1
    )

    async def main():
        t0 = time.monotonic()
        answer = await bridge.emit_and_wait(_question("ask_clamp"), timeout=60.0)
        return answer, time.monotonic() - t0

    answer, elapsed = asyncio.run(main())
    assert answer is None
    assert elapsed < 1.0


def test_required_decision_ignores_timeout_until_answer_arrives():
    emitted = []
    bridge = AskBridge(
        lambda event: emitted.append(event),
        default_timeout=0.01,
        max_timeout=0.01,
    )

    async def main():
        task = asyncio.create_task(
            bridge.emit_and_wait(
                _question("ask_required"), timeout=0.01, required=True
            )
        )
        await asyncio.sleep(0.04)
        assert bridge.pending_ids() == ["ask_required"]
        assert bridge.deliver("ask_required", {"choice": "yes"}) is True
        return await task

    assert asyncio.run(main()) == {"choice": "yes"}
    assert emitted and emitted[0]["kind"] == "ask_question"
    assert bridge.pending_ids() == []


def test_deliver_unknown_question_returns_false():
    bridge = AskBridge(lambda ev: None, default_timeout=0.1)

    async def main():
        # No question pending yet → deliver is a no-op returning False.
        before = bridge.deliver("nope", {"a": 1})
        # Now register one and deliver to a *different* id.
        async def waiter():
            return await bridge.emit_and_wait(_question("real"))
        task = asyncio.ensure_future(waiter())
        await asyncio.sleep(0.01)
        wrong = bridge.deliver("other", {"a": 1})
        right = bridge.deliver("real", {"choice": "yes"})
        answer = await task
        return before, wrong, right, answer

    before, wrong, right, answer = asyncio.run(main())
    assert before is False and wrong is False and right is True
    assert answer == {"choice": "yes"}


def test_invalid_required_answer_keeps_wait_pending():
    bridge = AskBridge(lambda _event: None, default_timeout=0.01)

    async def main():
        task = asyncio.create_task(
            bridge.emit_and_wait(_question("ask_strict"), required=True)
        )
        await asyncio.sleep(0.01)
        assert bridge.deliver("ask_strict", {}) is False
        assert bridge.pending_ids() == ["ask_strict"]
        assert not task.done()
        assert bridge.deliver("ask_strict", {"choice": "yes"}) is True
        return await task

    assert asyncio.run(main()) == {"choice": "yes"}


def test_env_default_timeout(monkeypatch):
    monkeypatch.setenv("LUMERI_ASK_TIMEOUT_SEC", "0.12")
    bridge = AskBridge(lambda ev: None)  # no explicit default → reads env

    async def main():
        t0 = time.monotonic()
        ans = await bridge.emit_and_wait(_question("ask_env"))
        return ans, time.monotonic() - t0

    ans, elapsed = asyncio.run(main())
    assert ans is None and elapsed < 1.0
