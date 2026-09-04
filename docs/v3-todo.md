# v3 polish backlog (post v3-alive, pre-production)

The two P2 findings from the 2026-05-27 Codex review are resolved and no
longer pending:

- **F2** — Silent-video speed handling was fixed in
  `gemia/tools/edit_video.py::_speed`, which checks ffprobe metadata via
  `has_audio` before choosing the audio or video-only filter graph.
- **F3** — Image overlay positioning was fixed in
  `gemia/tools/add_overlay.py::_OVERLAY_POSITIONS`, which uses the overlay
  filter's `W`/`H` and `w`/`h` variables.

---

Selected next direction: A (Tauri SSE integration for real creative tasks).
See chat for prerequisite decisions and Opus spec.
