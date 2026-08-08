from __future__ import annotations

from pathlib import Path

from gemia import session_history


def _patch_roots(monkeypatch, tmp_path: Path) -> Path:
    root = tmp_path / "sessions"
    monkeypatch.setattr(session_history, "SESSION_ROOT", root)
    monkeypatch.setattr(session_history, "CURRENT_SESSION_PATH", root / "current.json")
    monkeypatch.setattr(session_history, "SNAPSHOT_ROOT", root / "history")
    return root


def test_save_and_load_current_session(monkeypatch, tmp_path: Path) -> None:
    root = _patch_roots(monkeypatch, tmp_path)
    source_video = tmp_path / "a.mp4"
    source_video.write_bytes(b"fake mp4 placeholder")
    payload = {
        "messages": [
            {"id": "m1", "role": "user", "content": "剪一条预告", "timestamp": 123},
            {"id": "bad", "role": "assistant", "content": "ignored", "timestamp": 124},
        ],
        "project_state": {"clips": [{"id": "clip_1", "name": "a.mp4"}]},
        "timeline_markers": [
            {"time": 1.23456, "label": "开场复核", "color": "#ffcf3b"},
            {"time": -1, "label": "invalid"},
        ],
        "server_video_path": str(source_video),
        "creative_runtime_task_id": "task_crt_restore",
    }

    saved = session_history.save_current_session(payload)
    loaded = session_history.load_current_session()

    assert saved["title"] == "剪一条预告"
    assert saved["project"]["schema"] == "gemia.project"
    assert saved["project"]["timeline"]["clips"][0]["name"] == "a.mp4"
    assert loaded["messages"] == [{"id": "m1", "role": "user", "content": "剪一条预告", "statusType": None, "timestamp": 123}]
    assert loaded["project"]["version"] == 1
    assert loaded["timeline_markers"] == [
        {"time": 1.235, "label": "开场复核", "color": "#ffcf3b"}
    ]
    assert loaded["server_video_path"] == str(source_video)
    assert loaded["creative_runtime_task_id"] == "task_crt_restore"
    assert (root / "current.json").stat().st_mode & 0o777 == 0o600
    assert list((root / "history").glob("*.json"))


def test_list_session_snapshots_is_metadata_only(monkeypatch, tmp_path: Path) -> None:
    _patch_roots(monkeypatch, tmp_path)
    session_history.save_current_session(
        {
            "messages": [{"id": "m1", "role": "user", "content": "hello", "timestamp": 1}],
            "project_state": {"clips": [{"id": "clip_1"}, {"id": "clip_2"}]},
        }
    )

    items = session_history.list_session_snapshots()

    assert len(items) == 1
    assert items[0]["message_count"] == 1
    assert items[0]["clip_count"] == 2
    assert "messages" not in items[0]


def test_session_history_preserves_safe_skill_space_selection(monkeypatch, tmp_path: Path) -> None:
    _patch_roots(monkeypatch, tmp_path)
    digest = "a" * 64

    loaded = session_history.save_current_session(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "做一支产品宣传片",
                    "skillSpace": [
                        {
                            "kind": "workflow",
                            "id": "product-promo",
                            "version": "1.0.0",
                            "title": "产品宣传片制作",
                            "publisher": "Lumeri",
                            "visibility": "public",
                            "access": "public",
                            "content_sha256": digest,
                        },
                        {"kind": "skill", "id": "../unsafe", "version": "1.0.0"},
                    ],
                }
            ]
        }
    )

    assert loaded["messages"][0]["skillSpace"] == [
        {
            "kind": "workflow",
            "id": "product-promo",
            "version": "1.0.0",
            "title": "产品宣传片制作",
            "publisher": "Lumeri",
            "visibility": "public",
            "access": "public",
            "content_sha256": digest,
        }
    ]


def test_session_history_preserves_only_safe_explicit_workspace_context(monkeypatch, tmp_path: Path) -> None:
    _patch_roots(monkeypatch, tmp_path)

    loaded = session_history.save_current_session(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "调一下这个图层",
                    "workspaceContext": [
                        {
                            "kind": "canvas_layer",
                            "id": "shape_f522ea15d181",
                            "label": "主标题背景",
                            "layer_type": "shape",
                            "frame": 42,
                            "surface": "preview",
                        },
                        {"kind": "canvas_layer", "id": "../unsafe", "label": "bad"},
                        {"kind": "unknown", "id": "thing", "label": "bad"},
                    ],
                }
            ]
        }
    )

    assert loaded["messages"][0]["workspaceContext"] == [
        {
            "kind": "canvas_layer",
            "id": "shape_f522ea15d181",
            "label": "主标题背景",
            "layer_type": "shape",
            "surface": "preview",
            "frame": 42.0,
        }
    ]


def test_linked_v3_session_autosave_updates_one_history_item(
    monkeypatch, tmp_path: Path
) -> None:
    root = _patch_roots(monkeypatch, tmp_path)
    identity = {
        "v3_session_id": "v3-linked",
        "project_id": "project-linked",
        "run_id": "run-linked",
        "project_revision": 7,
        "production_state": "ready_for_review",
    }
    session_history.save_current_session(
        {
            **identity,
            "messages": [{"role": "user", "content": "first"}],
        }
    )
    original = next((root / "history").glob("*.json"))
    stable = original.with_name("20000101T000000Z-linked.json")
    original.rename(stable)

    session_history.save_current_session(
        {
            **identity,
            "messages": [{"role": "user", "content": "updated"}],
        }
    )

    snapshots = list((root / "history").glob("*.json"))
    assert snapshots == [stable]
    assert session_history.load_session_snapshot(stable.stem)["messages"][0]["content"] == "updated"

    duplicate = stable.with_name("20000102T000000Z-linked-copy.json")
    duplicate.write_bytes(stable.read_bytes())
    assert len(session_history.list_session_snapshots()) == 1


def test_load_session_snapshot_can_activate_previous_session(monkeypatch, tmp_path: Path) -> None:
    _patch_roots(monkeypatch, tmp_path)
    first = session_history.save_current_session(
        {
            "messages": [{"id": "m1", "role": "user", "content": "第一个会话", "timestamp": 1}],
            "project_state": {"clips": [{"id": "clip_1"}]},
        }
    )
    first_id = session_history.list_session_snapshots()[0]["id"]
    assert first["title"] == "第一个会话"

    session_history.save_current_session(
        {
            "messages": [{"id": "m2", "role": "user", "content": "第二个会话", "timestamp": 2}],
            "project_state": {"clips": []},
        }
    )

    opened = session_history.load_session_snapshot(first_id, activate=True)
    current = session_history.load_current_session()

    assert opened["title"] == "第一个会话"
    assert opened["session_id"] == first_id
    assert current["title"] == "第一个会话"
    assert current["messages"][0]["content"] == "第一个会话"


def test_session_history_persists_creative_runtime_task_id(monkeypatch, tmp_path: Path) -> None:
    _patch_roots(monkeypatch, tmp_path)

    saved = session_history.save_current_session(
        {
            "messages": [{"id": "m1", "role": "user", "content": "restore crt", "timestamp": 1}],
            "creative_runtime_task_id": "task_abc123",
        }
    )
    snapshot_id = session_history.list_session_snapshots()[0]["id"]
    opened = session_history.load_session_snapshot(snapshot_id, activate=True)
    current = session_history.load_current_session()

    assert saved["creative_runtime_task_id"] == "task_abc123"
    assert opened["creative_runtime_task_id"] == "task_abc123"
    assert current["creative_runtime_task_id"] == "task_abc123"


def test_session_history_settles_transient_messages_for_finished_runtime_task(monkeypatch, tmp_path: Path) -> None:
    _patch_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(session_history, "PROJECT_ROOT", tmp_path)
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "task_done.json").write_text('{"status": "succeeded"}', encoding="utf-8")

    loaded = session_history.save_current_session(
        {
            "messages": [
                {"id": "m1", "role": "user", "content": "做一个视频", "timestamp": 1},
                {"id": "m2", "role": "status", "content": "正在使用 Veo 生成...", "statusType": "executing", "timestamp": 2},
                {"id": "m3", "role": "status", "content": "完成。", "statusType": "done", "timestamp": 3},
            ],
            "creative_runtime_task_id": "task_done",
        }
    )

    assert loaded["messages"][1]["statusType"] == "done"
    assert loaded["messages"][2]["statusType"] == "done"


def test_session_history_repairs_trimmed_clip_from_runtime_task(monkeypatch, tmp_path: Path) -> None:
    _patch_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(session_history, "PROJECT_ROOT", tmp_path)
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "task_restore_trim.json").write_text(
        """
        {
          "task_id": "task_restore_trim",
          "project_state": {
            "clips": [
              {
                "id": "clip_1",
                "name": "a.mp4",
                "duration": 32.448333,
                "inPoint": 5.3,
                "outPoint": 32.448333,
                "trimmed": true
              }
            ]
          }
        }
        """,
        encoding="utf-8",
    )

    session_history.save_current_session(
        {
            "creative_runtime_task_id": "task_restore_trim",
            "project_state": {
                "clips": [
                    {
                        "id": "clip_1",
                        "name": "a.mp4",
                        "duration": 2.933333,
                        "inPoint": 5.3,
                        "outPoint": 2.933333,
                        "trimmed": True,
                    }
                ]
            },
        }
    )

    loaded = session_history.load_current_session()
    clip = loaded["project_state"]["clips"][0]

    assert clip["duration"] == 32.448333
    assert clip["inPoint"] == 5.3
    assert clip["outPoint"] == 32.448333
    assert loaded["project"]["timeline"]["clips"][0]["source_in"] == 5.3
    assert loaded["project"]["timeline"]["clips"][0]["source_out"] == 32.448333


def test_load_corrupt_session_returns_empty(monkeypatch, tmp_path: Path) -> None:
    root = _patch_roots(monkeypatch, tmp_path)
    root.mkdir(parents=True)
    (root / "current.json").write_text("{not json", encoding="utf-8")

    loaded = session_history.load_current_session()

    assert loaded["messages"] == []
    assert loaded["project_state"] is None


def test_session_history_does_not_restore_dev_brief_as_video(monkeypatch, tmp_path: Path) -> None:
    _patch_roots(monkeypatch, tmp_path)
    brief_path = tmp_path / "outputs" / "bad.lumeri-dev-brief.md"
    brief_path.parent.mkdir()
    brief_path.write_text("# brief\n", encoding="utf-8")

    saved = session_history.save_current_session(
        {
            "messages": [
                {"id": "m1", "role": "user", "content": "hello", "timestamp": 1},
                {
                    "id": "m2",
                    "role": "status",
                    "content": f"Cannot open video: {brief_path}",
                    "statusType": "error",
                    "timestamp": 2,
                },
                {
                    "id": "m3",
                    "role": "status",
                    "content": f"这一小段效果已经渲染成可看的小样：{brief_path.name}",
                    "statusType": "done",
                    "timestamp": 3,
                },
            ],
            "project_state": {
                "clips": [
                    {
                        "id": "clip_1",
                        "name": "bad brief",
                        "serverPath": str(brief_path),
                        "previewSrc": f"http://127.0.0.1:7788/file/outputs/{brief_path.name}",
                        "thumbnailStrip": [str(brief_path)],
                    }
                ]
            },
            "server_video_path": str(brief_path),
            "video_src": f"http://127.0.0.1:7788/file/outputs/{brief_path.name}",
        }
    )
    loaded = session_history.load_current_session()

    assert saved["server_video_path"] is None
    assert saved["video_src"] is None
    assert loaded["server_video_path"] is None
    assert loaded["video_src"] is None
    assert loaded["messages"][1]["content"] == "旧错误已修复：开发 brief 是文档产物，Lumeri 现在不会再把它作为视频预览打开。"
    assert loaded["messages"][2]["content"] == "旧记录已更正：这一轮生成的是开发 brief 文档，不是可播放小样。"
    clip = loaded["project_state"]["clips"][0]
    assert clip["serverPath"] == ""
    assert clip["previewSrc"] == ""
    assert clip["thumbnailStrip"] == []


def test_session_history_drops_missing_local_media_refs(monkeypatch, tmp_path: Path) -> None:
    _patch_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(session_history, "PROJECT_ROOT", tmp_path)
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    missing_video = outputs / "missing.mp4"
    existing_video = outputs / "existing.mp4"
    existing_video.write_bytes(b"fake mp4 placeholder")

    saved = session_history.save_current_session(
        {
            "messages": [{"id": "m1", "role": "user", "content": "hello", "timestamp": 1}],
            "project_state": {
                "clips": [
                    {
                        "id": "clip_1",
                        "serverPath": str(missing_video),
                        "previewSrc": "http://127.0.0.1:7788/file/outputs/missing.mp4",
                        "thumbnailStrip": [
                            "http://127.0.0.1:7788/file/outputs/missing.mp4",
                            str(existing_video),
                        ],
                    }
                ]
            },
            "server_video_path": str(missing_video),
            "video_src": "http://127.0.0.1:7788/file/outputs/missing.mp4",
        }
    )
    loaded = session_history.load_current_session()

    assert saved["server_video_path"] is None
    assert saved["video_src"] is None
    assert loaded["server_video_path"] is None
    assert loaded["video_src"] is None
    clip = loaded["project_state"]["clips"][0]
    assert clip["serverPath"] == ""
    assert clip["previewSrc"] == ""
    assert clip["thumbnailStrip"] == [str(existing_video)]


def test_session_history_keeps_temp_runtime_media_refs(monkeypatch, tmp_path: Path) -> None:
    _patch_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(session_history, "PROJECT_ROOT", tmp_path)
    preview = tmp_path / "temp" / "veo" / "preview.mp4"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"fake mp4 placeholder")

    saved = session_history.save_current_session(
        {
            "messages": [{"id": "m1", "role": "user", "content": "hello", "timestamp": 1}],
            "project_state": {
                "clips": [
                    {
                        "id": "clip_1",
                        "serverPath": str(preview),
                        "previewSrc": "http://127.0.0.1:7788/file/temp/veo/preview.mp4",
                        "thumbnailStrip": [str(preview)],
                    }
                ]
            },
            "server_video_path": str(preview),
            "video_src": "http://127.0.0.1:7788/file/temp/veo/preview.mp4",
        }
    )
    loaded = session_history.load_current_session()

    assert saved["server_video_path"] == str(preview)
    assert saved["video_src"] == "http://127.0.0.1:7788/file/temp/veo/preview.mp4"
    clip = loaded["project_state"]["clips"][0]
    assert clip["serverPath"] == str(preview)
    assert clip["previewSrc"] == "http://127.0.0.1:7788/file/temp/veo/preview.mp4"
    assert clip["thumbnailStrip"] == [str(preview)]


def test_session_history_hydrates_blank_project_from_finished_task_output(monkeypatch, tmp_path: Path) -> None:
    _patch_roots(monkeypatch, tmp_path)
    monkeypatch.setattr(session_history, "PROJECT_ROOT", tmp_path)
    preview = tmp_path / "temp" / "veo" / "preview.mp4"
    preview.parent.mkdir(parents=True)
    preview.write_bytes(b"fake mp4 placeholder")
    tasks = tmp_path / "tasks"
    tasks.mkdir()
    (tasks / "task_output.json").write_text(
        """
        {
          "status": "succeeded",
          "outputs": ["%s"],
          "render_passes": [
            {
              "output_path": "%s",
              "preview_path": "%s",
              "layers": [
                {"timing": {"start": 0, "duration": 5.0}}
              ]
            }
          ]
        }
        """
        % (preview, preview, preview),
        encoding="utf-8",
    )

    loaded = session_history.save_current_session(
        {
            "messages": [{"id": "m1", "role": "user", "content": "prompt-only", "timestamp": 1}],
            "project_state": {"clips": [], "selectedClipId": None, "playhead": 0},
            "creative_runtime_task_id": "task_output",
        }
    )

    clip = loaded["project_state"]["clips"][0]
    assert clip["serverPath"] == str(preview)
    assert clip["previewSrc"] == "/file/temp/veo/preview.mp4"
    assert clip["duration"] == 5.0
    assert loaded["project_state"]["selectedClipId"] == clip["id"]
    assert loaded["server_video_path"] == str(preview)
    assert loaded["video_src"] == "/file/temp/veo/preview.mp4"
